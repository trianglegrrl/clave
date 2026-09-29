# clave

This is my always-on assistant: one Claude Code session that lives on my Mac,
answers texts from my phone, and does the boring bits for me (logging practice time for me
and my kid, rebuilding a dashboard every night, eventually poking Home Assistant). It's named
for the clave--the rhythm everything else in the band locks to--because that's the job.

## How it works

- **One hub session**, started with `bin/clave-start` (tmux + caffeinate + Remote Control). I
  message it from the Claude app. Everything runs locally, so it can use logins and files that
  never leave the machine.
- **Skills are separate.** clave owns no domain logic. It has a routing table in `CLAUDE.md`
  that says "requests like X go to skill Y", and the skills themselves are ordinary Claude Code
  skills installed globally (mine come from the `clave` marketplace). Registering a skill is one
  row in the table.
- **Events are files.** Anything on the Mac can run `bin/clave-send "log 15 minutes for Maya"`
  and a file lands in `events/pending/`. A `/loop` in the hub drains that folder every few minutes
  (`LOOP.md` is the recipe), routes each event, and files it under `done/` or `failed/`. Cron
  jobs, Shortcuts, and Home Assistant all just call `clave-send`.

## Running it

```
~/clave/bin/clave-start
# then, once, inside the session:
/loop 1h read LOOP.md and process the event inbox
```

The Mac has to stay awake (that's the `caffeinate`). Locked is fine.

On an always-on Linux box, install it as a user service instead so it survives reboots and restarts itself
if Claude or the Telegram channel dies (lingering must be on: `loginctl enable-linger $USER`):

```
mkdir -p ~/.config/systemd/user && ln -sf ~/clave/systemd/clave.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now clave
tmux attach -t clave   # watch it; Ctrl-b d to detach
```

The service enters the loop by itself after each (re)start.

## Memory and research

The hub keeps what it learns about me in `memory/`, one fact per file with a `MEMORY.md` index:
`bin/clave-remember` writes, `bin/clave-recall` searches, and the rules in `CLAUDE.md` say when to do
each (read before answering about me, write when I tell it something). It looks things up with the
`pplx` skill (Perplexity through the `llm` CLI) or web search rather than trusting its own recall,
and it re-checks anything I'm about to act on.

## Caveats

This is a personal setup, not a product. The event loop is a polling loop on purpose--it's
simple and I can see every event as a file--and I haven't stress-tested any of it beyond my
own handful messages a day. 
