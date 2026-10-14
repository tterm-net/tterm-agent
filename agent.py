#!/usr/bin/env python3
"""
tterm-agent — connects this machine to tTerm: the Telegram bot, or tTerm Web.

Why an agent at all. A laptop cannot be reached from the outside: it has no
stable address, it sits behind NAT and it goes to sleep. So the direction is
reversed — the agent opens the connection itself and keeps it alive. No port
is ever opened on your machine.

What it does. Starts a local shell in a pseudo-terminal and pipes its bytes
both ways. For tTerm Web it also runs, when asked, short sh scripts outside
any terminal: one puts tTerm's shell scripts into ~/.tterm as a terminal
opens, others answer Tab and list directories and git branches for the page.
No logic of its own on top of that, no access to files other than through
the shell and those scripts. Anything the agent can do, so can anyone
sitting at this keyboard. tTerm Web's terminals outlive a dropped
connection: the laptop sleeps, the network blinks, and they go on where
they were when the link is back.

What it does not do:
  * never opens an inbound port;
  * never runs as root — it uses your own user's permissions;
  * never stores your passwords or keys;
  * never sends anything except what your terminals print and the answers
    to those scripts.

Stop it: launchctl unload (macOS) / systemctl --user stop (Linux),
or just Ctrl+C if you started it by hand.

Source: https://github.com/tterm-net/tterm-agent
License: AGPL-3.0-only (see LICENSE)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
import concurrent.futures
import contextlib
import fcntl
import json
import math
import os
import platform
import pwd
import pty
import select
import signal
import struct
import sys
import termios
import threading
import time
import warnings

__version__ = "0.12.0"

DEFAULT_HUB = "wss://install.tterm.net/agent"

#: How long to wait before reconnecting. Grows up to half a minute so we
#: neither hammer a service that is down nor sleep through a network blip.
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0

#: A connection that held this long was a working one. When it drops, the
#: pause starts over from BACKOFF_START instead of staying at what failures
#: before it (no network yet after a wake, say) had grown it to.
HEALTHY_FOR = 60.0

READ_CHUNK = 65536

#: How often we send a sign of life, and how long we tolerate silence back.
HEARTBEAT = 20.0
SILENCE_LIMIT = 50.0

#: How often to say in the log that the link is still up. Without this a quiet
#: log is ambiguous: it reads the same whether the agent sat there healthy for
#: an hour or hung in a dead socket. One line every ten minutes turns a gap in
#: the log into evidence instead of a guess.
ALIVE_EVERY = 600.0

#: Shells one connection may hold, one per terminal window. Not a policy, only
#: a stop for a runaway that keeps opening windows.
MAX_SHELLS = 16

#: Each shell's output is read by a thread, as before: it behaves the same on
#: every macOS and Linux, while watching a pseudo-terminal from asyncio has
#: not always worked on macOS. The default pool is sized by CPU count, and
#: with a reader per window it would run out – the next window's output
#: would sit unread. So readers get their own pool, one thread per shell the
#: cap allows and a few spare.
READERS = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_SHELLS + 4, thread_name_prefix="tterm-read")

#: Keys for tTerm Web's terminals are written by a thread too. A write to a
#: pseudo-terminal waits while the program in it reads nothing, and on macOS
#: its buffer holds about a kilobyte: a long paste written from the event
#: loop would stop every terminal and the link with it.
WRITERS = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_SHELLS + 4, thread_name_prefix="tterm-write")

#: How long a reader or a writer waits in select before it looks whether its
#: shell is being stopped. A read blocked for good could never be woken: a
#: shell that ignores the hangup would hold its thread forever, and the
#: agent could not even exit.
POLL = 0.5

#: How often a running shell is looked at to see whether it has ended.
WAIT_STEP = 0.2

#: A shell that ended may still have output on its way, a lot of it when
#: sending is slow. It is read on while it keeps coming, and the end is
#: reported at the first pause this long. Something the shell left in the
#: background can hold its terminal open and keep printing, though: then the
#: end comes at DRAIN_MAX anyway, as sshd does not wait for it either.
DRAIN_GAP = 0.5
DRAIN_MAX = 10.0

#: A shell that has not ended this long after its hangup is killed.
HANGUP_GRACE = 3.0

#: tTerm Web's terminals outlive a dropped connection (from 0.12.0): the
#: laptop sleeps, the network blinks, and tTerm Web gets each terminal back
#: where it was. What a shell printed is kept this far back, so that what
#: was on its way when the connection went can be sent again.
RING_MAX = 1024 * 1024
#: Printed and not sent yet, at most. Past it the shell's output waits, as
#: it does in a terminal nobody reads.
UNSENT_MAX = 512 * 1024
#: Output in one frame, at most.
FRAME_OUT = 64 * 1024
#: How long terminals wait for the link, counted while the computer is
#: awake: a laptop asleep all night loses nothing.
KEEP_FOR = 3600.0
#: After a reconnect, how long tTerm Web has to take back the terminals
#: offered to it. One older than 0.0.7 does not know how: they then end.
CLAIM_WAIT = 30.0
#: A terminal whose shell ended is kept this long after its end was sent, and
#: offered after a reconnect: the link may have gone with the end on its way.
#: Longer than it takes to notice a dead link (SILENCE_LIMIT).
ENDED_KEEP = 120.0

#: How far the monotonic clock must drift from the wall clock before we call
#: it a sleep. On macOS the monotonic clock stops while asleep, so the gap
#: between the two is the most reliable hint that the lid was closed.
SLEEP_JUMP = 15.0

#: What this agent can do for tTerm Web, said in its hello: terminals of the
#: page's size, raw bytes both ways, and the short scripts it asks for (1);
#: terminals kept through a dropped connection and taken back after it (2).
#: The bot does not look for it and keeps talking the way it always did.
WEB_PROTOCOL = 2

#: tTerm Web's binary frames: a byte of kind, four of channel, then the data.
OUTPUT, INPUT = 1, 2

#: tTerm Web's scripts: how many at once, how long by default and at most,
#: how much of what they print goes back.
SIDE_AT_ONCE = 4
SIDE_TIMEOUT = 10.0
SIDE_TIMEOUT_MAX = 60.0
SIDE_OUT_MAX = 2 * 1024 * 1024
SIDE_ERR_MAX = 4096

#: A plain PATH for tTerm Web's terminals and scripts, as sshd gives one. The
#: login shell builds the person's own from there, as in any terminal. On a
#: Mac with Apple silicon Homebrew lives in /opt/homebrew, which the person's
#: profile adds; tTerm's scripts run with sh and read no profile, so it comes
#: first here as it does in their terminal.
PLAIN_PATH = ":".join(
    [d for d in ("/opt/homebrew/bin", "/opt/homebrew/sbin")
     if sys.platform == "darwin" and os.path.isdir(d)]
    + ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])


class Revoked(Exception):
    """The hub says this machine was removed. Retrying is pointless."""


def machine_name() -> str:
    """The machine's own name.

    platform.node() returns "MacBook-Pro-Denis.local" on macOS — the .local
    suffix only gets in the way, everyone has it.
    """
    return (platform.node() or "computer").removesuffix(".local")


def set_process_name(name: str = "tterm-agent") -> None:
    """Rename the process so it is recognisable in system listings.

    macOS shows the process name under "Allow in the Background", and without
    this it reads as a nameless "python" from an unidentified developer.

    A wrapper script does not solve this: exec replaces it with python and the
    name is lost immediately. setproctitle does the real renaming; when it is
    missing we simply carry on — this is cosmetics, not a requirement.
    """
    try:
        import setproctitle
    except ImportError:
        return
    with contextlib.suppress(Exception):
        setproctitle.setproctitle(name)


def log(msg: str) -> None:
    # The window it ran in may be gone: nowhere to say it then.
    with contextlib.suppress(OSError, ValueError):
        print(f"[tterm-agent] {msg}", flush=True)


def login_shell() -> str:
    """The person's login shell, the one sshd would start: from SHELL when
    the agent was started from a terminal, from the account database when
    autostart gave it none."""
    shell = os.environ.get("SHELL", "")
    if not shell:
        with contextlib.suppress(Exception):
            shell = pwd.getpwuid(os.getuid()).pw_shell
    return shell if shell and os.path.isfile(shell) else "/bin/sh"


def web_env(terminal: bool) -> dict[str, str]:
    """A clean environment for tTerm Web's terminals and scripts, made the
    way sshd makes one: who, where, a plain PATH and the locale.

    Not the agent's own. Started by hand from another terminal, the agent
    carries that terminal's settings (Warp's, a virtualenv's), and a shell
    that inherits them behaves like that terminal instead of like a fresh
    login. Without a locale the shell would read every non-ASCII letter as
    separate bytes, so a UTF-8 one is set when none is given.
    """
    try:
        entry = pwd.getpwuid(os.getuid())
        user, home = entry.pw_name, entry.pw_dir
    except KeyError:
        user, home = os.environ.get("USER", ""), os.path.expanduser("~")
    env = {
        "HOME": os.environ.get("HOME") or home,
        "USER": user,
        "LOGNAME": user,
        "SHELL": login_shell(),
        "PATH": PLAIN_PATH,
    }
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SSH_AUTH_SOCK"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    if not any(env.get(n) for n in ("LANG", "LC_ALL", "LC_CTYPE")):
        env["LANG"] = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    if terminal:
        env["TERM"] = "xterm-256color"
    return env


def number(value, default: int, top: int) -> int:
    """A size from the hub as a whole number from 1 to `top`, or the default
    when it is not a number at all."""
    try:
        whole = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, min(whole, top))


def winsize(cols, rows) -> bytes:
    return struct.pack("HHHH", number(rows, 24, 500), number(cols, 80, 1000),
                       0, 0)


def script_timeout(value) -> float:
    """How long one of tTerm Web's scripts may run, in seconds."""
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return SIDE_TIMEOUT
    if not math.isfinite(seconds) or seconds <= 0:
        return SIDE_TIMEOUT
    return min(seconds, SIDE_TIMEOUT_MAX)


def fork() -> int:
    """os.fork, without Python's warning about forking while threads run.

    The readers and writers are threads, so the warning would come with the
    first terminal. It is about a child that goes on running Python and
    takes a lock some other thread held; this child only sets up its
    terminal and becomes the shell, as the child of pty.fork does.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return os.fork()


def take_terminal(fd: int) -> None:
    """In the child: a session of its own, with this pseudo-terminal as its
    controlling terminal and as its input and output."""
    if hasattr(os, "login_tty"):
        os.login_tty(fd)
        return
    os.setsid()
    request = getattr(termios, "TIOCSCTTY", None)
    if request is not None:
        fcntl.ioctl(fd, request, 0)
    for target in (0, 1, 2):
        os.dup2(fd, target)
    if fd > 2:
        os.close(fd)
    if request is None:
        os.close(os.open(os.ttyname(0), os.O_RDWR))


def default_signals() -> None:
    """In the child: the signals Python ignores, back to normal.

    Python ignores SIGPIPE and SIGXFSZ for itself, and a program started
    with exec keeps what was ignored. A shell started without this would
    not end `yes | head` by SIGPIPE: yes would complain about a broken pipe
    instead, and so would much else.
    """
    for name in ("SIGPIPE", "SIGXFSZ", "SIGXFZ"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, signal.SIG_DFL)


class Shell:
    """A local shell running in a pseudo-terminal."""

    def __init__(self) -> None:
        self.pid: int | None = None
        self.fd: int | None = None
        #: The bot gets text, tTerm Web the bytes as they are.
        self.web = False
        #: A UTF-8 letter can be split between two reads. Decoding each read
        #: on its own turned a Cyrillic letter or a line of htop's frame on
        #: such a seam into "�"; this keeps the first half until the rest
        #: of it arrives.
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        #: tTerm Web's keys, written in order by a thread of their own.
        self.inbox: asyncio.Queue[bytes] | None = None
        #: Set once the shell is over: its reader and writer let go.
        self.ending = threading.Event()
        #: Done once the shell has ended and its exit status is collected:
        #: the code, or None when a signal ended it.
        self.ended: asyncio.Future | None = None
        #: When that was, by the monotonic clock: from then on its reader
        #: stops at the first pause in the output.
        self.ended_at: float | None = None
        #: Threads using the descriptor right now. It is closed only after
        #: the last of them is done: closed earlier, its number could be
        #: given to another file and a late write would land there.
        self._lock = threading.Lock()
        self._users = 0
        self._closing = False

    #: Shells the marker is written for. Anything else falls back to bash,
    #: which is present on macOS and on nearly every Linux.
    KNOWN = ("bash", "zsh")

    @staticmethod
    def pick() -> str:
        """The shell to run: the person's own, when we can speak to it.

        Started from autostart there is no SHELL in the environment, so ask
        the account database — otherwise everyone on macOS, where zsh has been
        the default since 2019, would get bash and none of their PATH.
        """
        shell = os.environ.get("SHELL", "")
        if not shell:
            with contextlib.suppress(Exception):
                shell = pwd.getpwuid(os.getuid()).pw_shell
        return shell if any(k in os.path.basename(shell or "")
                            for k in Shell.KNOWN) else "/bin/bash"

    def start(self) -> None:
        shell = self.pick()
        pid, fd = pty.fork()
        if pid == 0:
            try:
                default_signals()
                os.environ["TERM"] = "xterm-256color"
                # The agent's own virtualenv must not leak into the user's
                # shell, or a stray (venv) shows up in the prompt and pip
                # installs into the wrong place.
                os.environ.pop("VIRTUAL_ENV", None)
                # Started from autostart, the working directory is the
                # filesystem root. Opening a terminal, a person expects to
                # land at home.
                home = os.path.expanduser("~")
                if os.path.isdir(home):
                    os.chdir(home)
                # Not a login shell, deliberately. A login shell reads the
                # profile files, which on macOS spawn path_helper — and while
                # that runs the shell is not yet reading its input, so the
                # bootstrap sent right after start goes into a buffer the
                # line editor then discards. The marker never arrives and
                # the machine looks broken.
                #
                # What a login shell was wanted for — the system PATH — the
                # bootstrap takes directly instead.
                if "zsh" in os.path.basename(shell):
                    # zsh has no --noediting; the bootstrap turns off its
                    # line editor instead.
                    os.execvp(shell, [shell, "-i"])
                else:
                    os.execvp(shell, [shell, "--noediting", "-i"])
            finally:
                os._exit(127)
        # The shells of other windows, started later, must not inherit this
        # one's terminal: they would hold it open after it ended.
        os.set_inheritable(fd, False)
        self.pid, self.fd = pid, fd
        self.ended = asyncio.get_running_loop().create_future()

    def start_web(self, command: str, cols, rows, echo: bool) -> None:
        """A terminal for tTerm Web: the command it asks for, run the way
        sshd runs one, by the login shell with -c, in a pseudo-terminal of
        the page's size.

        tTerm Web asks for echo off at the start when it is about to type
        its one-time key; its shell scripts turn echo back on once they have
        read it. Size and echo are set here, before the child exists: set in
        the child, they raced the key, which could arrive and be echoed
        before the child got to it.
        """
        shell = login_shell()
        env = web_env(terminal=True)
        home = env["HOME"]
        argv = [shell, "-c", command]
        size = winsize(cols, rows)
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
            if not echo:
                attrs = termios.tcgetattr(slave)
                attrs[3] &= ~termios.ECHO
                termios.tcsetattr(slave, termios.TCSANOW, attrs)
            pid = fork()
        except BaseException:
            os.close(master)
            os.close(slave)
            raise
        if pid == 0:
            try:
                os.close(master)
                take_terminal(slave)
                default_signals()
                if os.path.isdir(home):
                    os.chdir(home)
                os.execve(shell, argv, env)
            finally:
                os._exit(127)
        os.close(slave)
        # Writes must never block: a terminal whose program reads nothing
        # would hold its writer for good. The writer waits in select instead.
        os.set_blocking(master, False)
        self.pid, self.fd = pid, master
        self.web = True
        self.inbox = asyncio.Queue()
        self.ended = asyncio.get_running_loop().create_future()

    @property
    def alive(self) -> bool:
        return self.fd is not None

    @property
    def running(self) -> bool:
        """Not yet ended and collected. Only while this holds may the shell
        be signalled: once collected, its number may belong to another
        process. Collecting happens on the event loop, as all signalling
        does, so the two never cross."""
        return self.ended is not None and not self.ended.done()

    def collect(self) -> bool:
        """Whether the shell has ended; collects its exit status if so,
        without waiting, so that it never lingers as a zombie."""
        if not self.running:
            return True
        assert self.pid is not None and self.ended is not None
        try:
            done, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.ended_at = time.monotonic()
            self.ended.set_result(None)
            return True
        if not done:
            return False
        code = os.waitstatus_to_exitcode(status)
        self.ended_at = time.monotonic()
        self.ended.set_result(code if code >= 0 else None)
        return True

    async def wait(self, timeout: float) -> bool:
        """Whether the shell ends within `timeout` seconds."""
        if self.ended is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(self.ended), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    @property
    def code(self) -> int | None:
        if self.ended is not None and self.ended.done():
            return self.ended.result()
        return None

    def stop(self) -> None:
        """Ends the shell: a hangup, as when a terminal window is closed,
        and whoever reads or writes for it lets go.

        The child goes first and the descriptor later: it is closed by
        release, once no thread uses it any more.
        """
        self.ending.set()
        if self.pid and self.running:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGHUP)

    def kill(self) -> None:
        """For a shell that ignored its hangup."""
        if self.pid and self.running:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGKILL)

    def use(self) -> int | None:
        """The descriptor, for one read, write or resize; None once it is
        being closed. Each use ends with done_using."""
        with self._lock:
            if self._closing or self.fd is None:
                return None
            self._users += 1
            return self.fd

    def done_using(self) -> None:
        with self._lock:
            self._users -= 1
            last = self._closing and self._users == 0
        if last:
            self._close()

    def release(self) -> None:
        """Closes the descriptor, now or as soon as the last thread using
        it is done."""
        with self._lock:
            self._closing = True
            last = self._users == 0
        if last:
            self._close()

    def _close(self) -> None:
        with self._lock:
            if self.fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self.fd)
                self.fd = None

    def write(self, data: str) -> None:
        fd = self.use()
        if fd is None:
            return
        try:
            os.write(fd, data.encode())
        except OSError:
            pass        # the shell is ending; its pump says so
        finally:
            self.done_using()

    def resize(self, cols, rows) -> None:
        """The page's size changed. The program in front hears SIGWINCH."""
        fd = self.use()
        if fd is None:
            return
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize(cols, rows))
        except OSError:
            pass
        finally:
            self.done_using()


def read_some(shell: Shell) -> bytes:
    """In a reader thread: what the shell printed, as soon as there is some;
    b"" once its terminal has closed or the shell is over: stopped, or ended
    and its output paused (DRAIN_GAP)."""
    fd = shell.use()
    if fd is None:
        return b""
    try:
        while not shell.ending.is_set():
            ended_at = shell.ended_at
            wait = POLL
            if ended_at is not None:
                since = time.monotonic() - ended_at
                if since >= DRAIN_MAX:
                    return b""
                wait = min(DRAIN_GAP, DRAIN_MAX - since)
            ready, _, _ = select.select([fd], [], [], wait)
            if not ready:
                if ended_at is not None and \
                        time.monotonic() - ended_at >= DRAIN_GAP:
                    return b""
                continue
            try:
                return os.read(fd, READ_CHUNK)
            except BlockingIOError:
                continue
            except OSError:
                return b""      # Linux says EIO once the shell side is closed
        return b""
    finally:
        shell.done_using()


def write_all(shell: Shell, data: bytes) -> None:
    """In a writer thread: writes everything, however many writes it takes,
    unless the shell is over first."""
    fd = shell.use()
    if fd is None:
        return
    try:
        view = memoryview(data)
        while view and not shell.ending.is_set():
            try:
                done = os.write(fd, view)
            except BlockingIOError:
                select.select([], [fd], [], POLL)
                continue
            except OSError:
                return
            view = view[done:]
    finally:
        shell.done_using()


class Health:
    """Tracks whether the link is actually alive.

    A socket error cannot be relied upon: when the laptop falls asleep the
    server sees the connection drop while the client is left half-open —
    nothing to read, nothing to fail on, and the agent can hang like that for
    hours. So we check for ourselves: send a sign of life, expect any answer,
    and separately watch for the machine having slept.
    """

    def __init__(self) -> None:
        now = time.monotonic()
        self.last_in = now
        self.mono = now
        self.wall = time.time()

    def heard(self) -> None:
        self.last_in = time.monotonic()

    def slept(self) -> float:
        """How far the wall clock ran ahead of the monotonic one."""
        mono, wall = time.monotonic(), time.time()
        drift = (wall - self.wall) - (mono - self.mono)
        self.mono, self.wall = mono, wall
        return drift

    def silent_for(self) -> float:
        return time.monotonic() - self.last_in


async def run_script(script: str, timeout: float) -> tuple[int | None, bytes, bytes]:
    """Runs one of tTerm Web's scripts with sh, outside any terminal: the
    exit code (None when it did not finish in time), what it printed, and
    the end of what it complained about."""
    env = web_env(terminal=False)
    home = env["HOME"]
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-s", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=home if os.path.isdir(home) else "/", env=env,
            start_new_session=True)
    except OSError as exc:
        return None, b"", str(exc).encode()
    try:
        out, err = await asyncio.wait_for(proc.communicate(script.encode()),
                                          timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.wait()
        return None, b"", b"the script did not finish in time"
    except asyncio.CancelledError:
        # The link went while it ran: nobody will read the answer.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        raise
    return proc.returncode, out[:SIDE_OUT_MAX], err[-SIDE_ERR_MAX:]


async def bury(shell: Shell) -> int | None:
    """The exit code of a shell whose terminal is over. One that has not
    ended by then gets a hangup, and is killed if it ignores that."""
    if await shell.wait(HANGUP_GRACE):
        return shell.code
    shell.stop()
    if await shell.wait(HANGUP_GRACE):
        return shell.code
    shell.kill()
    await shell.wait(HANGUP_GRACE)
    return shell.code


async def finish(shell: Shell) -> None:
    """After the hangup: a shell that ignores it is killed."""
    if not await shell.wait(HANGUP_GRACE):
        shell.kill()


async def watch(shell: Shell) -> None:
    """Collects the shell's exit status as soon as it ends, so that it
    never lingers as a zombie, and so that its reader knows it ended."""
    while not shell.collect():
        await asyncio.sleep(WAIT_STEP)


async def feed(shell: Shell) -> None:
    """Writes tTerm Web's keys into one terminal, in the order they came."""
    loop = asyncio.get_running_loop()
    inbox = shell.inbox
    assert inbox is not None
    while True:
        data = await inbox.get()
        while not inbox.empty():
            data += inbox.get_nowait()
        if shell.ending.is_set():
            return
        if data:
            await loop.run_in_executor(WRITERS, write_all, shell, data)


def offset(value) -> int:
    """A byte offset from the hub: a whole number from 0, or 0."""
    try:
        whole = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, whole)


class WebShell:
    """One of tTerm Web's terminals for as long as its shell runs, over
    whichever connection there is: none at all while the link is down.

    Everything it prints is counted from its first byte, and the last of it
    is kept. Coming back after a dropped connection, tTerm Web says how much
    it has, and the rest is sent again: what was on its way when the link
    went is not lost, and nothing arrives twice.
    """

    def __init__(self, ch: int, shell: Shell, registry: "WebShells") -> None:
        self.ch = ch
        self.shell = shell
        self.registry = registry
        self.head = bytes([OUTPUT]) + ch.to_bytes(4, "big")
        #: The last of what it printed, from offset `ring_start` on.
        self.ring = bytearray()
        self.ring_start = 0
        #: Bytes printed in all, and how many of them went to a connection.
        self.produced = 0
        self.sent = 0
        #: Bytes tTerm Web asked for that are no longer kept, to say first.
        self.gap = 0
        #: The connection it goes over, None while there is none.
        self.link: Shells | None = None
        self.detached_at = time.monotonic()
        #: Offered back after a reconnect: if not taken by then, it ends.
        self.claim_by: float | None = None
        #: Its output is over; the exit code to report, and when it was.
        self.over = False
        self.code: int | None = None
        self.reported: float | None = None
        self.closed = False
        self.wake = asyncio.Event()
        self.room = asyncio.Event()
        self.room.set()

    def start(self) -> None:
        for coro in (self._pump(), self._send(), watch(self.shell)):
            self.registry.spawn(coro)
        if self.shell.inbox is not None:
            self.registry.spawn(feed(self.shell))

    def attach(self, link: "Shells", start: int) -> None:
        """Goes over `link` from now on, from byte `start` of its output:
        how much tTerm Web has. Anything it is missing is sent again."""
        start = min(start, self.produced)
        if start < self.ring_start:
            self.gap = self.ring_start - start
            start = self.ring_start
        self.sent = start
        self.link = link
        self.claim_by = None
        # Its end, if it ended, is said again after what is sent again.
        self.reported = None
        self.wake.set()

    def detach(self) -> None:
        self.link = None
        self.claim_by = None
        self.detached_at = time.monotonic()

    def close(self) -> None:
        """tTerm Web is done with it: the shell gets its hangup."""
        if self.closed:
            return
        self.closed = True
        self.shell.stop()
        if self.shell.inbox is not None:
            self.shell.inbox.put_nowait(b"")
        self.room.set()
        self.wake.set()
        self.registry.forget(self)
        self.registry.spawn(finish(self.shell))

    def _keep(self, data: bytes) -> None:
        self.ring += data
        self.produced += len(data)
        # Only what was sent goes. After a reconnect nearly all of the ring
        # may wait to be sent again, and a read under way lands on top of
        # it: the ring then holds that read more until it is sent.
        extra = min(len(self.ring) - RING_MAX, self.sent - self.ring_start)
        if extra > 0:
            del self.ring[:extra]
            self.ring_start += extra

    async def _pump(self) -> None:
        """Reads what the shell prints into the ring. With too much of it
        unsent, a slow or missing connection, it stops reading, and the
        programs in the terminal wait, as in a terminal nobody reads."""
        loop = asyncio.get_running_loop()
        shell = self.shell
        try:
            while True:
                if self.produced - self.sent > UNSENT_MAX \
                        and not shell.ending.is_set():
                    self.room.clear()
                    paused = time.monotonic()
                    await self.room.wait()
                    # Waiting here is not draining: what the shell printed
                    # before it ended is read whole, however long the link
                    # took to come back.
                    ended = shell.ended_at
                    if ended is not None:
                        shell.ended_at = ended + (time.monotonic()
                                                  - max(paused, ended))
                    continue
                data = await loop.run_in_executor(READERS, read_some, shell)
                if not data:
                    break
                self._keep(data)
                self.wake.set()
            shell.ending.set()
            if shell.inbox is not None:
                shell.inbox.put_nowait(b"")
            if not self.closed:
                self.code = await bury(shell)
                self.over = True
                self.wake.set()
        finally:
            shell.release()

    async def _send(self) -> None:
        """Sends what is not sent yet, in order, and the end after it."""
        while not self.closed:
            await self.wake.wait()
            self.wake.clear()
            while self.link is not None and not self.closed:
                link = self.link
                if self.sent < self.ring_start:
                    # Never so; but were it, what is gone is said as such.
                    self.gap += self.ring_start - self.sent
                    self.sent = self.ring_start
                if self.gap:
                    lost, self.gap = self.gap, 0
                    await link.send(self.ch, {"t": "gap", "lost": lost})
                elif self.sent < self.produced:
                    at = self.sent - self.ring_start
                    chunk = bytes(self.ring[at:at + FRAME_OUT])
                    if not chunk:
                        break
                    self.sent += len(chunk)
                    if self.produced - self.sent <= UNSENT_MAX:
                        self.room.set()
                    await link.send_bytes(self.head + chunk)
                elif self.over:
                    # Gone on its own – someone typed `exit` – rather than
                    # closed by the hub. Said after all it printed; kept a
                    # while more, in case the link went with it (ENDED_KEEP).
                    if self.reported is None:
                        self.reported = time.monotonic()
                        await link.send(self.ch, {"t": "exit", "code": self.code})
                    break
                else:
                    break


class WebShells:
    """tTerm Web's terminals on this computer. They belong to the agent, not
    to a connection: the laptop sleeps, the network blinks, and tTerm Web
    finds them where they were when the agent is back. Kept while the link
    is down for KEEP_FOR at most, counted while the computer is awake."""

    def __init__(self) -> None:
        self._shells: dict[int, WebShell] = {}
        self._tasks: set[asyncio.Task] = set()

    def __len__(self) -> int:
        return len(self._shells)

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def channels(self) -> list[int]:
        return sorted(self._shells)

    def forget(self, web: WebShell) -> None:
        if self._shells.get(web.ch) is web:
            del self._shells[web.ch]

    async def open(self, ch: int, msg: dict, link: "Shells") -> None:
        old = self._shells.get(ch)
        if old is not None:
            if old.link is not None and not old.over:
                return          # the same window twice: the first stays
            # Kept from before, or over, and the hub no longer knows it: the
            # number is the hub's to give.
            old.close()
        if sum(1 for web in self._shells.values() if not web.over) >= MAX_SHELLS:
            await link.send(ch, {"t": "exit", "code": None, "error":
                                 "too many terminals open on this machine"})
            return
        shell = Shell()
        try:
            shell.start_web(str(msg.get("cmd") or ""), msg.get("cols"),
                            msg.get("rows"), bool(msg.get("echo", True)))
        except (OSError, ValueError, TypeError) as exc:
            await link.send(ch, {"t": "exit", "code": None,
                                 "error": f"could not start the shell: {exc}"})
            return
        web = WebShell(ch, shell, self)
        self._shells[ch] = web
        web.attach(link, 0)
        web.start()

    async def resume(self, ch: int, start: int, link: "Shells") -> None:
        """tTerm Web takes a kept terminal back, having `start` bytes of it."""
        web = self._shells.get(ch)
        if web is None:
            await link.send(ch, {"t": "exit", "code": None, "gone": True})
            return
        web.attach(link, start)
        log(f"tTerm Web took terminal {ch} back")

    def input(self, ch: int, data: bytes) -> bool:
        web = self._shells.get(ch)
        if web is None:
            return False
        if web.link is not None and web.shell.inbox is not None:
            web.shell.inbox.put_nowait(data)
        return True

    def resize(self, ch: int, msg: dict) -> bool:
        web = self._shells.get(ch)
        if web is None:
            return False
        web.shell.resize(msg.get("cols"), msg.get("rows"))
        return True

    def close(self, ch: int | None) -> bool:
        web = self._shells.get(ch) if isinstance(ch, int) else None
        if web is None:
            return False
        web.close()
        return True

    def detach(self, link: "Shells") -> None:
        """The connection went: its terminals wait for the next one."""
        kept = [web for web in self._shells.values() if web.link is link]
        for web in kept:
            web.detach()
        if kept:
            log(f"keeping {len(kept)} terminal(s) for tTerm Web until the "
                "link is back")

    def claim(self, wait: float) -> None:
        """Offered back in the hello of a connection that is now up: tTerm
        Web takes them, or ends them; one that does neither is older than
        this, and they end by themselves."""
        deadline = time.monotonic() + wait
        for web in self._shells.values():
            if web.link is None:
                web.claim_by = deadline

    def sweep(self) -> None:
        now = time.monotonic()
        for web in list(self._shells.values()):
            if web.link is not None:
                if web.reported is not None and now - web.reported > ENDED_KEEP:
                    web.close()         # its end reached tTerm Web
                continue
            if web.claim_by is not None and now > web.claim_by:
                log(f"terminal {web.ch} was not taken back: ending it")
                web.close()
            elif now - web.detached_at > KEEP_FOR:
                log(f"terminal {web.ch} waited {KEEP_FOR / 60:.0f} minutes "
                    "for the link: ending it")
                web.close()

    async def keep(self) -> None:
        while True:
            await asyncio.sleep(5)
            self.sweep()

    def close_all(self) -> None:
        for web in list(self._shells.values()):
            web.close()


class Shells:
    """The shells of one connection: one per terminal window in the bot.

    The hub names each window with a channel number, and every message about
    it carries that number both ways. Before this there was one shell per
    machine, so the windows in the bot were only labels: `cd` in one moved all
    of them, a command in one went into whatever was running in another, and
    closing one killed the rest.

    An older hub sends no channel at all. Everything then goes to a single
    shell and the replies go back without a channel, exactly as before.

    tTerm Web opens its terminals explicitly, with the command to run and
    the size of the page, and gets the bytes as they are, in binary frames.
    Those outlive the connection: see WebShells.
    """

    def __init__(self, ws, web: WebShells | None = None) -> None:
        self.ws = ws
        self.web = web if web is not None else WebShells()
        self._shells: dict[int | None, Shell] = {}
        #: Each shell's tasks: its pump and its watch. They end by
        #: themselves once the shell is over.
        self._tasks: set[asyncio.Task] = set()
        #: tTerm Web's scripts, cancelled when the connection goes.
        self._scripts: set[asyncio.Task] = set()
        self._side = asyncio.Semaphore(SIDE_AT_ONCE)
        #: Set when the single shell an older hub uses exits on its own. Such
        #: a hub learns that only by the connection dropping, so it is dropped.
        self.legacy_gone = asyncio.Event()

    def _task(self, coro, group: set[asyncio.Task] | None = None) -> None:
        tasks = self._tasks if group is None else group
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def send(self, ch: int | None, msg: dict) -> None:
        if ch is not None:
            msg["ch"] = ch
        with contextlib.suppress(Exception):    # a dying link is serve's news
            await self.ws.send(json.dumps(msg))

    async def send_bytes(self, data: bytes) -> None:
        with contextlib.suppress(Exception):
            await self.ws.send(data)

    async def write(self, ch: int | None, data: str) -> None:
        shell = self._shells.get(ch)
        if shell is None:
            # Started on first use. A shell sits idle in memory otherwise, and
            # the hub may never open a second window at all.
            if len(self._shells) >= MAX_SHELLS:
                await self.send(ch, {"t": "out", "data":
                    "tterm-agent: too many terminals open on this machine\r\n"})
                await self.send(ch, {"t": "exit"})
                return
            shell = Shell()
            shell.start()
            self._shells[ch] = shell
            self._task(self._pump(ch, shell))
            self._task(watch(shell))
        shell.write(str(data))

    async def open(self, ch: int, msg: dict) -> None:
        """tTerm Web opens a terminal."""
        if 0 < ch < 2 ** 32:
            await self.web.open(ch, msg, self)

    async def resume(self, ch: int, msg: dict) -> None:
        """tTerm Web takes back a terminal kept from a connection before."""
        if 0 < ch < 2 ** 32:
            await self.web.resume(ch, offset(msg.get("from")), self)

    def input(self, ch: int, data: bytes) -> None:
        """Keys from tTerm Web for one of its terminals."""
        self.web.input(ch, data)

    def resize(self, ch: int, msg: dict) -> None:
        self.web.resize(ch, msg)

    def side(self, msg: dict) -> None:
        """One of tTerm Web's scripts; the answer carries the id it came with."""
        self._task(self._side_run(msg), self._scripts)

    async def _side_run(self, msg: dict) -> None:
        timeout = script_timeout(msg.get("timeout"))
        async with self._side:
            code, out, err = await run_script(str(msg.get("script") or ""),
                                              timeout)
        await self.send(None, {
            "t": "ran", "id": msg.get("id"), "code": code,
            "out": base64.b64encode(out).decode("ascii"),
            "err": base64.b64encode(err).decode("ascii")})

    def close(self, ch: int | None) -> bool:
        """The hub is done with this window: end its shell and only that."""
        if self.web.close(ch):
            return True
        shell = self._shells.pop(ch, None)
        if shell is None:
            return False        # already gone, for instance after `exit`
        shell.stop()
        self._task(finish(shell))
        return True

    def close_all(self) -> None:
        """The connection is over. The bot's shells get their hangup and
        are collected when they end, killed if they do not; the scripts
        stop. tTerm Web's terminals wait for the next connection."""
        for ch in list(self._shells):
            self.close(ch)
        for task in list(self._scripts):
            task.cancel()
        self.web.detach(self)

    async def _pump(self, ch: int | None, shell: Shell) -> None:
        """Forwards one shell's output to the bot, tagged with its window."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                data = await loop.run_in_executor(READERS, read_some, shell)
                if not data:
                    break
                await self.send(ch, {"t": "out",
                                     "data": shell.decoder.decode(data)})
            rest = shell.decoder.decode(b"", final=True)
            if rest:
                await self.send(ch, {"t": "out", "data": rest})
            # The terminal is over.
            shell.ending.set()

            if self._shells.get(ch) is shell:
                # Gone on its own – someone typed `exit` – rather than closed
                # by the hub. Say so, or the hub keeps typing into a fresh
                # shell that never got its prompt marker and waits for an
                # answer forever.
                self._shells.pop(ch, None)
                await bury(shell)
                await self.send(ch, {"t": "exit"})
                if ch is None:
                    self.legacy_gone.set()
        finally:
            shell.release()


async def serve(ws, shells: Shells, health: Health) -> None:
    """Takes commands from the hub and writes them into the right shell."""
    async for raw in ws:
        health.heard()
        if isinstance(raw, bytes):
            # tTerm Web: keys for one of its terminals.
            if len(raw) > 5 and raw[0] == INPUT:
                shells.input(int.from_bytes(raw[1:5], "big"), raw[5:])
            continue
        try:
            msg = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if not isinstance(msg, dict):
            continue
        kind = msg.get("t")
        ch = msg.get("ch")
        if ch is not None and not isinstance(ch, int):
            continue
        if kind == "in":
            await shells.write(ch, msg.get("data", ""))
        elif kind == "close":
            # The hub closes a window it has not seen used for a while, or one
            # the person closed. That is about the window, not about the
            # link: the connection and every other window stay as they are.
            if shells.close(ch):
                where = "the session" if ch is None else f"terminal {ch}"
                log(f"hub closed {where}, keeping the link")
        elif kind == "open" and ch is not None:
            await shells.open(ch, msg)
        elif kind == "resume" and ch is not None:
            await shells.resume(ch, msg)
        elif kind == "size" and ch is not None:
            shells.resize(ch, msg)
        elif kind == "run":
            shells.side(msg)
        elif kind == "ping":
            await ws.send(json.dumps({"t": "pong"}))
        elif kind == "error":
            # tTerm Web ends the connection and says why. A computer it no
            # longer knows is not coming back; anything else is tried again
            # after the usual pause, growing, rather than every second.
            error = str(msg.get("error") or "the hub ended the connection")
            if msg.get("revoked") is True:
                raise Revoked(error)
            raise RuntimeError(error)


async def watchdog(ws, health: Health) -> None:
    """Drops a dead connection so that reconnection can kick in."""
    last_note = time.monotonic()
    while True:
        await asyncio.sleep(HEARTBEAT)

        if time.monotonic() - last_note >= ALIVE_EVERY:
            last_note = time.monotonic()
            log(f"link alive, hub answered {health.silent_for():.0f}s ago")

        drift = health.slept()
        if drift > SLEEP_JUMP:
            log(f"machine slept for about {drift:.0f}s — reconnecting")
            return

        try:
            await ws.send(json.dumps({"t": "hb"}))
        except Exception as exc:
            log(f"could not send sign of life: {type(exc).__name__}")
            return

        if health.silent_for() > SILENCE_LIMIT:
            log(f"hub silent for {health.silent_for():.0f}s — reconnecting")
            return


async def connect_once(hub: str, token: str, name: str,
                       web: WebShells | None = None) -> None:
    """One connection, from the hello until it drops. tTerm Web's terminals
    are `web`'s, kept for the next one; without it they end with this."""
    if web is None:
        web = WebShells()
        try:
            await connect_once(hub, token, name, web)
        finally:
            web.close_all()
        return

    import websockets

    async with websockets.connect(hub, ping_interval=20, ping_timeout=20,
                                  max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "t": "hello",
            "token": token,
            "name": name,
            "user": os.environ.get("USER") or os.environ.get("LOGNAME", ""),
            "os": f"{platform.system()} {platform.release()}",
            "agent": __version__,
            # The bot writes its prompt marker differently for each shell, so
            # it has to know which one is on this end.
            "shell": os.path.basename(Shell.pick()),
            # This agent keeps a shell per terminal window. A hub that does
            # not look for this keeps talking the old way and still works.
            "channels": 1,
            # And it can serve tTerm Web; the bot does not look for this.
            "web": WEB_PROTOCOL,
            # tTerm Web's terminals still running from a connection before.
            "kept": web.channels(),
        }))
        reply = json.loads(await ws.recv())
        if not isinstance(reply, dict):
            raise RuntimeError("the hub's answer was not understood")
        if reply.get("t") != "welcome":
            error = reply.get("error", "the hub refused the connection")
            if reply.get("revoked"):
                raise Revoked(error)
            raise RuntimeError(error)
        log(f"connected as «{reply.get('name', name)}»")

        shells = Shells(ws, web)
        web.claim(CLAIM_WAIT)
        health = Health()
        srv = asyncio.create_task(serve(ws, shells, health))
        dog = asyncio.create_task(watchdog(ws, health))
        gone = asyncio.create_task(shells.legacy_gone.wait())
        try:
            # Wait for the first task to finish, not for all of them. When the
            # link drops (the laptop was closed) serve fails, and waiting for
            # the rest would mean waiting forever — the agent would never
            # reconnect. That is exactly what used to break after sleep.
            done, _ = await asyncio.wait(
                [srv, dog, gone], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        except asyncio.CancelledError:
            # Stopped on purpose (Ctrl+C): tTerm Web's terminals end with the
            # agent, and it hears so now rather than waiting for them to come
            # back. The bot does not look for this.
            web.close_all()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.send(json.dumps({"t": "bye"})), 1)
            raise
        finally:
            # Ending each shell unblocks its reader thread — without this the
            # threads would live until the process exits.
            shells.close_all()
            for task in (srv, dog, gone):
                task.cancel()
            with contextlib.suppress(Exception):
                await ws.close()


def installed() -> bool:
    """Whether this is the agent the bot's installer put in ~/.tterm, which
    comes with its uninstaller, rather than one started by hand."""
    with contextlib.suppress(Exception):
        return os.path.samefile(__file__,
                                os.path.expanduser("~/.tterm/agent.py"))
    return False


def stop_on_signals() -> None:
    """A stop by the system (launchd, systemd) or the window the agent runs
    in being closed goes the way Ctrl+C does: tTerm Web hears that its
    terminals end. A hangup the agent was started to ignore (nohup) stays
    ignored."""
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task is None:
        return
    for sig in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) is signal.SIG_IGN:
            continue
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, task.cancel)


async def main_loop(hub: str, token: str, name: str) -> None:
    backoff = BACKOFF_START
    stop_on_signals()
    # tTerm Web's terminals live as long as the agent does, through any
    # number of connections, and end with it.
    web = WebShells()
    keeper = asyncio.create_task(web.keep())
    try:
        while True:
            # By the wall clock, which runs on while the machine sleeps: a
            # connection that was up before a sleep was a working one too.
            started = time.time()
            try:
                await connect_once(hub, token, name, web)
            except asyncio.CancelledError:
                raise
            except Revoked as exc:
                log(f"access revoked: {exc}. Stopping.")
                if installed():
                    log("Remove the agent completely: ~/.tterm/uninstall.sh")
                return
            except Exception as exc:
                if time.time() - started >= HEALTHY_FOR:
                    # It had been working, so this is an ordinary drop rather
                    # than a hub that keeps failing: try again soon.
                    backoff = BACKOFF_START
                log(f"no link ({type(exc).__name__}: {exc}), retrying in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            # A clean disconnect: reconnect straight away, no pause needed.
            backoff = BACKOFF_START
            log("connection closed, reconnecting")
            await asyncio.sleep(BACKOFF_START)
    finally:
        keeper.cancel()
        web.close_all()


def main() -> None:
    parser = argparse.ArgumentParser(description="tTerm agent")
    parser.add_argument("--hub", default=os.environ.get("TTERM_HUB", DEFAULT_HUB))
    parser.add_argument("--token", default=os.environ.get("TTERM_TOKEN", ""))
    parser.add_argument("--name",
                        default=os.environ.get("TTERM_NAME") or machine_name())
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    if not args.token:
        sys.exit("No token given. Get the command from the bot (/addhost) or "
                 "from tTerm Web (Add a machine, then Computer).")
    if os.geteuid() == 0:
        # The agent must never hold more privileges than the person at the
        # keyboard. There is deliberately no way around this check.
        sys.exit("Do not run the agent as root — run it as your own user.")

    set_process_name()
    log(f"version {__version__}, hub {args.hub}")
    try:
        asyncio.run(main_loop(args.hub, args.token, args.name))
    except (KeyboardInterrupt, asyncio.CancelledError):
        log("stopped")


if __name__ == "__main__":
    main()
