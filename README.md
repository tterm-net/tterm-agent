# tterm-agent

Connects your computer to **[tTerm](https://tterm.net)** — Terminal in Telegram
with multi-user access.

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

Does: starts a local `bash` in a pseudo-terminal and pipes its bytes both ways.

Does **not**:

- open any inbound port;
- run as root — it only ever uses your own user's permissions;
- store passwords or keys;
- send anything except the output of commands you asked for;
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

## Requirements

Python 3.11+. On macOS: `brew install python@3.12`.

## Links

- [tterm.net](https://tterm.net) — the site, with release notes
- [@tTermNetBot](https://t.me/tTermNetBot) — the bot itself
- [@tTermBlog](https://t.me/tTermBlog) — updates and news
- [tTerm](https://github.com/tterm-net/tTerm) — the bot's source code

## License

MIT
