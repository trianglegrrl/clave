# clave event loop

Run inside the hub session, once, after starting: `/loop 5m read LOOP.md and process the event inbox`

On each tick:
1. List `events/pending/*.md` sorted by name (names start with a timestamp). Nothing there: stop, say nothing.
2. For each file: read the front matter (`type`, `source`) and the body. Route it with the table in `CLAUDE.md`. A typed event routes directly; a plain `message` routes on its text.
3. Do the work. Anything that takes more than a minute goes to a subagent so the loop stays quick.
4. Append `result: <one line>` and `finished: <ISO time>` to the front matter, then move the file to `events/done/` (or `events/failed/` with `error: <why>`). Never delete an event.
5. If a result should reach Alaina, send it as the reply to this tick; keep it to a sentence or two.
6. Scheduled jobs are ordinary events: a launchd or cron entry calls `bin/clave-send --type schedule.dashboard "rebuild the practice dashboard"`.

Do not process the same event twice: a file in `pending/` is unclaimed, a file elsewhere is finished.
