#!/usr/bin/env bash
# Rebuilds dashboards/practice-tides/practice-tides.html from live MyMusicStaff data.
# Claude then republishes it with the Artifact tool using the URL in CLAUDE.md.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SKILL="$HOME/.claude/skills/mymusicstaff/scripts"
for who in maya me; do "$SKILL/mms-practice-list.sh" --student "$who" --all --json > "$HERE/list-$who.json"; done
python3 - "$HERE" <<'PY'
import json, sys, pathlib, datetime as dt
H = pathlib.Path(sys.argv[1])
data = {"maya": json.load(open(H/"list-maya.json"))["sessions"], "alaina": json.load(open(H/"list-me.json"))["sessions"]}
data = {k: [{"date": r["date"], "duration": r["duration"], "notes": r["notes"]} for r in v] for k, v in data.items()}
html = (H/"template.html").read_text().replace("__DATA__", json.dumps(data, ensure_ascii=False)).replace("__TODAY__", dt.date.today().isoformat())
(H/"practice-tides.html").write_text(html)
print("built practice-tides.html:", {k: len(v) for k, v in data.items()})
PY
