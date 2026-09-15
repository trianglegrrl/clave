#!/usr/bin/env python3
"""Rebuild dashboards/timing-tracker/timing-tracker.html from every takes/*.mid.

Each take is a benchmark MIDI recording named YYYY-MM-DD-label.mid (Superior Drummer 3
via eDRUMin, or a drumrec take (github.com/trianglegrrl/drumrec): type 1, click notes on channel 10 in the "drumrec click"
track, which are skipped). Claude republishes the output with the Artifact tool using the URL in CLAUDE.md.
"""
import json, re, statistics as st, sys, pathlib
import mido

HERE = pathlib.Path(__file__).resolve().parent
HATS = {11, 12, 13, 14, 15, 16, 17, 63}
FOOT, KICK, SNARE = 21, 36, 6
DOUBLE_MS, DOUBLE_VEL = 80.0, 0.60
KICK_GRID = "8th"  # kick plays 8ths; a 16th grid would hide off-16th misses (baseline SD 26.84 on 8ths)
LIMBS = [("hats", "Hats"), ("foot", "Hat foot"), ("back", "Snare backbeats"),
         ("ghost", "Snare ghosts"), ("kick", "Kick")]


def load(path):
    mid = mido.MidiFile(path)
    ppq = mid.ticks_per_beat
    tempo = None
    hits = {}  # note -> [(tick, vel)]
    for track in mid.tracks:  # type 0 or 1: tracks are merged by absolute tick
        t = 0
        click = track.name == "drumrec click"  # drumrec takes: click notes are not hits
        for msg in track:
            t += msg.time
            if msg.type == "set_tempo" and tempo is None:
                tempo = msg.tempo
            elif msg.type == "note_on" and msg.velocity > 0:
                if click or (msg.channel == 9 and msg.note in (76, 77)):
                    continue
                hits.setdefault(msg.note, []).append((t, msg.velocity))
    tempo = tempo or 500000
    return ppq, tempo, {n: sorted(v) for n, v in hits.items()}


def dedupe(seq, ms_per_tick):
    """Drop soft double triggers: within 80 ms after the last kept hit AND under 60% of its velocity."""
    kept, dropped = [], 0
    for tick, vel in seq:
        if kept and (tick - kept[-1][0]) * ms_per_tick < DOUBLE_MS and vel < DOUBLE_VEL * kept[-1][1]:
            dropped += 1
            continue
        kept.append((tick, vel))
    return kept, dropped


def offset(tick, grid, ms_per_tick):
    return (tick - round(tick / grid) * grid) * ms_per_tick


def metrics(offs, vels):
    if not offs:
        return None
    med = st.median(offs)
    mad = st.median(abs(o - med) for o in offs)
    n = len(offs)
    return {
        "n": n,
        "sd": round(st.pstdev(offs), 2),  # population SD, matches the numbers given to Alaina
        "rsd": round(1.4826 * mad, 2),
        "mean": round(st.fmean(offs), 2),
        "median": round(med, 2),
        "in10": round(sum(abs(o) <= 10 for o in offs) / n, 4),
        "in20": round(sum(abs(o) <= 20 for o in offs) / n, 4),
        "vel": st.median(vels),
    }


def analyse(path):
    ppq, tempo, notes = load(path)
    mpt = tempo / 1000.0 / ppq
    eighth, sixteenth = ppq / 2, ppq / 4
    groups = {"hats": [], "foot": [], "kick": [], "snare": []}
    for note, seq in notes.items():
        if note in HATS:
            groups["hats"] += seq
        elif note == FOOT:
            groups["foot"] += seq
        elif note == KICK:
            groups["kick"] += seq
        elif note == SNARE:
            groups["snare"] += seq
    limbs, dropped = {k: {"o": [], "v": []} for k, _ in LIMBS}, {}
    for g, seq in groups.items():
        # hats: all openness variants count as one limb, so dedupe across them together
        kept, dropped[g] = dedupe(sorted(seq), mpt)
        for tick, vel in kept:
            if g == "snare":
                pos = round(tick / sixteenth) % 16
                key = "back" if pos in (4, 12) else "ghost"
                o = offset(tick, sixteenth, mpt)
            else:
                key = g
                grid = sixteenth if (g == "kick" and KICK_GRID == "16th") else eighth
                o = offset(tick, grid, mpt)
            limbs[key]["o"].append(o)  # keep unrounded: rounding to 0.1 moves 19.96 ms hits outside 20
            limbs[key]["v"].append(vel)
    name = pathlib.Path(path).stem
    m = re.match(r"(\d{4}-\d{2}-\d{2})-?(.*)", name)
    date, label = (m.group(1), m.group(2)) if m else (name, "")
    return {
        "date": date, "label": label.replace("-", " "), "file": pathlib.Path(path).name,
        "bpm": round(60_000_000 / tempo, 1), "dropped": dropped,
        "limbs": {k: dict(metrics(d["o"], d["v"]) or {"n": 0}, hits=[round(o, 1) for o in d["o"]], vels=d["v"])
                  for k, d in limbs.items()},
    }


def main():
    takes = [analyse(p) for p in sorted((HERE / "takes").glob("*.mid"))]
    if not takes:
        sys.exit("no takes in takes/")
    data = {"limbs": [{"key": k, "name": n} for k, n in LIMBS], "takes": takes}
    (HERE / "timing-data.json").write_text(json.dumps(data))
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = (HERE / "template.html").read_text().replace("__DATA__", blob)
    (HERE / "timing-tracker.html").write_text(html)
    for t in takes:
        print(t["date"], t["label"], f'{t["bpm"]} bpm', "dropped", t["dropped"])
        for k, name in LIMBS:
            L = t["limbs"][k]
            if L["n"]:
                print(f'  {name:16} n={L["n"]:3} sd={L["sd"]:5.1f} rsd={L["rsd"]:5.1f} mean={L["mean"]:+5.1f} '
                      f'in10={L["in10"]:.0%} in20={L["in20"]:.0%} vel={L["vel"]}')
    print("built timing-tracker.html:", len(takes), "take(s)")


if __name__ == "__main__":
    main()
