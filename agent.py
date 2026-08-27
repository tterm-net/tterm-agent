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
import contextlib
import json
import os
import platform
import pty
import signal
import sys
import time

__version__ = "0.7.0"

DEFAULT_HUB = "wss://install.tterm.net/agent"

#: How long to wait before reconnecting. Grows up to half a minute so we
#: neither hammer a service that is down nor sleep through a network blip.
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
READ_CHUNK = 65536

#: How often we send a sign of life, and how long we tolerate silence back.
HEARTBEAT = 20.0
SILENCE_LIMIT = 50.0

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

    def start(self) -> None:
        shell = os.environ.get("SHELL", "/bin/bash")
        # The prompt marker and bootstrap assume bash. If the user's login
        # shell is zsh or fish we still start bash: it is present on macOS and
        # on nearly every Linux, and we do not yet ship a bootstrap per shell.
        if "bash" not in os.path.basename(shell):
            shell = "/bin/bash"
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
            os.execvp(shell, [shell, "--noediting", "-i"])
        self.pid, self.fd = pid, fd

    @property
    def alive(self) -> bool:
        return self.fd is not None

    def write(self, data: str) -> None:
        if self.fd is not None:
            os.write(self.fd, data.encode())

    def stop(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None
        if self.pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGHUP)
            self.pid = None


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


async def pump_shell(shell: Shell, ws) -> None:
    """Reads the shell's output and forwards it to the hub."""
    loop = asyncio.get_running_loop()
    while shell.alive:
        assert shell.fd is not None
        try:
            data = await loop.run_in_executor(None, os.read, shell.fd, READ_CHUNK)
        except OSError:
            break
        if not data:
            break
        await ws.send(json.dumps({
            "t": "out",
            "data": data.decode("utf-8", "replace"),
        }))


async def serve(ws, shell: Shell, health: Health) -> None:
    """Takes commands from the hub and writes them into the shell."""
    async for raw in ws:
        health.heard()
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        kind = msg.get("t")
        if kind == "in":
            shell.write(msg.get("data", ""))
        elif kind == "close":
            log("hub closed the session")
            return
        elif kind == "ping":
            await ws.send(json.dumps({"t": "pong"}))


async def watchdog(ws, health: Health) -> None:
    """Drops a dead connection so that reconnection can kick in."""
    while True:
        await asyncio.sleep(HEARTBEAT)

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
        }))
        reply = json.loads(await ws.recv())
        if reply.get("t") != "welcome":
            error = reply.get("error", "the hub refused the connection")
            if reply.get("revoked"):
                raise Revoked(error)
            raise RuntimeError(error)
        log(f"connected as \u00ab{reply.get('name', name)}\u00bb")

        shell = Shell()
        shell.start()
        health = Health()
        pump = asyncio.create_task(pump_shell(shell, ws))
        srv = asyncio.create_task(serve(ws, shell, health))
        dog = asyncio.create_task(watchdog(ws, health))
        try:
            # Wait for the first task to finish, not for all of them.
            # Otherwise, when the link drops (the laptop was closed), serve
            # fails while pump_shell stays forever inside a blocking os.read
            # in its thread — and the agent never reconnects. That is exactly
            # what used to break after sleep.
            done, _ = await asyncio.wait(
                [pump, srv, dog], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        finally:
            # Closing the fd unblocks the stuck os.read in its thread —
            # without this the thread would live until the process exits.
            shell.stop()
            for task in (pump, srv, dog):
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
