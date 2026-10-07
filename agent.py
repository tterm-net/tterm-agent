#!/usr/bin/env python3
"""
tterm-agent — connects this machine to the tTerm bot.

Why an agent at all. A laptop cannot be reached from the outside: it has no
stable address, it sits behind NAT and it goes to sleep. So the direction is
reversed — the agent opens the connection itself and keeps it alive. No port
is ever opened on your machine.

What it does. Starts a local shell in a pseudo-terminal and pipes its bytes
both ways. Nothing else: no command parsing, no logic of its own, no access
to files other than through that shell. Anything the agent can do, so can
anyone sitting at this keyboard.

What it does not do:
  * never opens an inbound port;
  * never runs as root — it uses your own user's permissions;
  * never stores your passwords or keys;
  * never sends anything except the output of commands you asked for.

Stop it: launchctl unload (macOS) / systemctl --user stop (Linux),
or just Ctrl+C if you started it by hand.

Source: https://github.com/tterm-net/tterm-agent
License: MIT
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import json
import os
import platform
import pwd
import pty
import signal
import sys
import time

__version__ = "0.10.0"

DEFAULT_HUB = "wss://install.tterm.net/agent"

#: How long to wait before reconnecting. Grows up to half a minute so we
#: neither hammer a service that is down nor sleep through a network blip.
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
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

#: Each shell's output is read by a thread blocked in os.read, as before: it
#: behaves the same on every macOS and Linux, while watching a pseudo-terminal
#: from asyncio has not always worked on macOS. The default pool is sized by
#: CPU count, and with a reader per window it would run out – the next
#: window's output would sit unread. So readers get their own pool, one
#: thread per shell the cap allows and a few spare.
READERS = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_SHELLS + 4, thread_name_prefix="tterm-read")

#: How far the monotonic clock must drift from the wall clock before we call
#: it a sleep. On macOS the monotonic clock stops while asleep, so the gap
#: between the two is the most reliable hint that the lid was closed.
SLEEP_JUMP = 15.0


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
    print(f"[tterm-agent] {msg}", flush=True)


class Shell:
    """A local shell running in a pseudo-terminal."""

    def __init__(self) -> None:
        self.pid: int | None = None
        self.fd: int | None = None

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
            os.environ["TERM"] = "xterm-256color"
            # The agent's own virtualenv must not leak into the user's shell,
            # or a stray (venv) shows up in the prompt and pip installs into
            # the wrong place.
            os.environ.pop("VIRTUAL_ENV", None)
            # Started from autostart, the working directory is the filesystem
            # root. Opening a terminal, a person expects to land at home.
            home = os.path.expanduser("~")
            if os.path.isdir(home):
                os.chdir(home)
            # Not a login shell, deliberately. A login shell reads the profile
            # files, which on macOS spawn path_helper — and while that runs the
            # shell is not yet reading its input, so the bootstrap sent right
            # after start goes into a buffer the line editor then discards.
            # The marker never arrives and the machine looks broken.
            #
            # What a login shell was wanted for — the system PATH — the
            # bootstrap takes directly instead.
            if "zsh" in os.path.basename(shell):
                # zsh has no --noediting; the bootstrap turns off its line
                # editor instead.
                os.execvp(shell, [shell, "-i"])
            else:
                os.execvp(shell, [shell, "--noediting", "-i"])
        self.pid, self.fd = pid, fd

    @property
    def alive(self) -> bool:
        return self.fd is not None

    def write(self, data: str) -> None:
        if self.fd is not None:
            os.write(self.fd, data.encode())

    def stop(self) -> None:
        """Ends the shell, waking whoever is reading it.

        The child goes first and the descriptor second. Closing the descriptor
        does not wake a read already blocked on it — the reading thread would
        hang on a dead shell. Ending the child makes that read return
        end-of-stream, which is the only thing that frees it.
        """
        if self.pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGHUP)
            self.pid = None
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None


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


class Shells:
    """The shells of one connection: one per terminal window in the bot.

    The hub names each window with a channel number, and every message about
    it carries that number both ways. Before this there was one shell per
    machine, so the windows in the bot were only labels: `cd` in one moved all
    of them, a command in one went into whatever was running in another, and
    closing one killed the rest.

    An older hub sends no channel at all. Everything then goes to a single
    shell and the replies go back without a channel, exactly as before.
    """

    def __init__(self, ws) -> None:
        self.ws = ws
        self._shells: dict[int | None, Shell] = {}
        self._pumps: set[asyncio.Task] = set()
        #: Set when the single shell an older hub uses exits on its own. Such
        #: a hub learns that only by the connection dropping, so it is dropped.
        self.legacy_gone = asyncio.Event()

    async def send(self, ch: int | None, msg: dict) -> None:
        if ch is not None:
            msg["ch"] = ch
        with contextlib.suppress(Exception):    # a dying link is serve's news
            await self.ws.send(json.dumps(msg))

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
            task = asyncio.create_task(self._pump(ch, shell))
            self._pumps.add(task)
            task.add_done_callback(self._pumps.discard)
        shell.write(data)

    def close(self, ch: int | None) -> bool:
        """The hub is done with this window: end its shell and only that."""
        shell = self._shells.pop(ch, None)
        if shell is None:
            return False        # already gone, for instance after `exit`
        shell.stop()
        return True

    def close_all(self) -> None:
        for ch in list(self._shells):
            self.close(ch)
        for task in list(self._pumps):
            task.cancel()

    async def _pump(self, ch: int | None, shell: Shell) -> None:
        """Forwards one shell's output, tagged with its window."""
        loop = asyncio.get_running_loop()
        while shell.fd is not None:
            try:
                data = await loop.run_in_executor(READERS, os.read, shell.fd,
                                                  READ_CHUNK)
            except (OSError, TypeError):
                data = b""
            if not data:
                break
            await self.send(ch, {"t": "out",
                                 "data": data.decode("utf-8", "replace")})

        if self._shells.get(ch) is shell:
            # Gone on its own – someone typed `exit` – rather than closed by
            # the hub. Say so, or the hub keeps typing into a fresh shell that
            # never got its prompt marker and waits for an answer forever.
            self._shells.pop(ch, None)
            shell.stop()
            await self.send(ch, {"t": "exit"})
            if ch is None:
                self.legacy_gone.set()


async def serve(ws, shells: Shells, health: Health) -> None:
    """Takes commands from the hub and writes them into the right shell."""
    async for raw in ws:
        health.heard()
        try:
            msg = json.loads(raw)
        except ValueError:
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
        elif kind == "ping":
            await ws.send(json.dumps({"t": "pong"}))


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


async def connect_once(hub: str, token: str, name: str) -> None:
    import websockets

    async with websockets.connect(hub, ping_interval=20, ping_timeout=20) as ws:
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
        }))
        reply = json.loads(await ws.recv())
        if reply.get("t") != "welcome":
            error = reply.get("error", "the hub refused the connection")
            if reply.get("revoked"):
                raise Revoked(error)
            raise RuntimeError(error)
        log(f"connected as \u00ab{reply.get('name', name)}\u00bb")

        shells = Shells(ws)
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
        finally:
            # Ending each shell unblocks its reader thread — without this the
            # threads would live until the process exits.
            shells.close_all()
            for task in (srv, dog, gone):
                task.cancel()
            with contextlib.suppress(Exception):
                await ws.close()


async def main_loop(hub: str, token: str, name: str) -> None:
    backoff = BACKOFF_START
    while True:
        try:
            await connect_once(hub, token, name)
        except asyncio.CancelledError:
            raise
        except Revoked as exc:
            log(f"access revoked: {exc}. Stopping.")
            log("Remove the agent completely: ~/.tterm/uninstall.sh")
            return
        except Exception as exc:
            log(f"no link ({type(exc).__name__}: {exc}), retrying in {backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
        # A clean disconnect — reconnect straight away, no pause needed.
        backoff = BACKOFF_START
        log("connection closed, reconnecting")
        await asyncio.sleep(BACKOFF_START)


def main() -> None:
    parser = argparse.ArgumentParser(description="tTerm agent")
    parser.add_argument("--hub", default=os.environ.get("TTERM_HUB", DEFAULT_HUB))
    parser.add_argument("--token", default=os.environ.get("TTERM_TOKEN", ""))
    parser.add_argument("--name",
                        default=os.environ.get("TTERM_NAME") or machine_name())
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    if not args.token:
        sys.exit("No token given. Get the connection command from the bot: /addhost")
    if os.geteuid() == 0:
        # The agent must never hold more privileges than the person at the
        # keyboard. There is deliberately no way around this check.
        sys.exit("Do not run the agent as root — run it as your own user.")

    set_process_name()
    log(f"version {__version__}, hub {args.hub}")
    try:
        asyncio.run(main_loop(args.hub, args.token, args.name))
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
