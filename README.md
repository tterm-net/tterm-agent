# tterm-agent

Connects your computer to **[tTerm](https://tterm.net)** — Terminal in Telegram
with multi-user access, and, from version 0.11.0, to
[tTerm Web](https://github.com/tterm-net/tterm-web), tTerm in the browser,
which is in development.

[tterm.net](https://tterm.net) · [open the bot](https://t.me/tTermNetBot) ·
[updates](https://t.me/tTermBlog) · [bot source](https://github.com/tterm-net/tTerm)

## Why it exists

A server with a public address we connect to directly over SSH. A laptop is
different: it has no stable address, it sits behind NAT and it goes to sleep.
So the direction is reversed — the agent opens the connection **itself** and
keeps it alive.

Not a single port is opened on your machine.

## Install

The bot gives you the command with your token when you run `/addhost`.
It looks like this:

```bash
curl -fsSL https://install.tterm.net/a/YOUR_TOKEN | sh
```

The script never asks for root and puts everything under `~/.tterm`.

Windows is not supported yet. If you have WSL2, connect it as a Linux
computer — that works today.

## What the agent does, and what it does not

Does: starts your own shell, bash or zsh, in a pseudo-terminal and pipes its
bytes both ways. Each terminal window you open in the bot gets a shell of its
own, so a `cd` in one leaves the others where they were.

For tTerm Web it also runs, when asked, short sh scripts outside any
terminal: one puts tTerm's shell scripts into `~/.tterm` as a terminal opens,
others answer Tab and list directories and git branches for the page.

To update, run the install command from the bot again (`/addhost` → Computer).
The computer stays in your list as it is.

Does **not**:

- open any inbound port;
- run as root — it only ever uses your own user's permissions;
- store passwords or keys;
- send anything except what your terminals print and the answers to those
  scripts;
- carry any logic of its own on top of the shell — anything the agent can do,
  so can anyone sitting at this keyboard.

All parsing of the output happens on the service side, not here. That is why
the agent stays small and rarely changes.

## What to understand about security

Whoever controls the bot controls this machine with your user's permissions.
The token in `~/.tterm/env` is this machine's credential — treat it like
a password.

Traffic goes through Telegram's servers and they can see its contents:
conversations with bots are not end-to-end encrypted. Do not print private
keys or passwords into the chat.

## Remove

```bash
~/.tterm/uninstall.sh
```

Removes autostart, the environment and the agent itself. Nothing is left
behind on the system.

## Run by hand

```bash
~/.tterm/venv/bin/python ~/.tterm/agent.py \
    --hub wss://install.tterm.net/agent --token YOUR_TOKEN
```

Autostart log: `~/.tterm/agent.log`

## With tTerm Web

The same agent serves both; which one it talks to is set by `--hub`, and a
computer is connected to one of them at a time. tTerm Web gives the command
under Add a machine, then Computer. For now it is started by hand, from a
checkout of this repository:

```bash
TTERM_TOKEN=YOUR_TOKEN python3 agent.py --hub wss://YOUR-TTERM-WEB/agent
```

The computer is online while it runs; Ctrl+C disconnects it. The token
connects it within ten minutes and from then on is the computer's key. It
goes in the environment rather than on the command line, where any user of
the computer could read it in the list of processes.

How it talks to tTerm Web, all on one WebSocket: the hello says `"web": 1`.
tTerm Web asks it to `open` a terminal (the command, run by your login shell
with `-c`, the page's size, echo off for the first line), to `size` it and
`close` it, and to `run` a script with `sh -s`. Keys come in and output goes
out as binary frames, a byte of kind and four of channel, then the bytes as
they are. The Telegram bot never sends these and gets the agent it always
had.

## Requirements

Python 3.11+. On macOS: `brew install python@3.12`.

## Links

- [tterm.net](https://tterm.net) — the site, with release notes
- [@tTermNetBot](https://t.me/tTermNetBot) — the bot itself
- [@tTermBlog](https://t.me/tTermBlog) — updates and news
- [tTerm](https://github.com/tterm-net/tTerm) — the bot's source code

## License

Copyright (C) 2026 Denis Gerasimenko

The tTerm agent is free software: you can redistribute it and/or modify it
under the terms of the GNU Affero General Public License, version 3, as
published by the Free Software Foundation. See [LICENSE](LICENSE).

Using the tTerm agent with your own machines places no obligations on you.
The license matters when you distribute a modified version or run one as a
service for others: then you must offer its source to the people who use it.

Versions published before 10 October 2026 were released under the MIT license
and remain available under it.
