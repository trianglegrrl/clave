#!/usr/bin/env python3
"""drumrec: MIDI practice recorder with a click track (Python 3.9+, mido + python-rtmidi).

  drumrec ports
  drumrec record --bpm 106 --bars 32 --label groove
  drumrec list [--json]
  drumrec analyze FILE [--skip-count-in]

File layout (type 1, ppq 960, fixed tempo, 4/4):
  track 0 "drumrec tempo"        set_tempo, time_signature, markers ("count-in", "take")
  track 1 "drumrec click"        click notes (channel 10, 76 downbeat / 77 other beats) at ACTUAL send times
  track 2 "drumrec performance"  every channel/sysex message from the input, at receive times
Tick 0 is the actual send time of the FIRST click (the first count-in click when there is a count-in).
The count-in is whole bars, so the bar/beat grid from tick 0 lines up with the take either way.
"""
import argparse, datetime, json, math, pathlib, statistics as st, sys, threading, time

HOME = pathlib.Path.home() / "drumrec"
TAKES = HOME / "takes"
PPQ = 960
CLICK_CH = 9  # channel 10, zero-based
DOWN, BEAT = 76, 77
CLICK_LEN = 0.02  # s between click note-on and note-off
CLICK_TRACK, PERF_TRACK = "drumrec click", "drumrec performance"
SPIN = 0.002  # sleep until this many seconds early, then spin
NOTE_NAMES = {36: "kick", 38: "snare", 6: "snare head", 12: "closed hat", 21: "hat foot", DOWN: "click down", BEAT: "click"}


def need_rtmidi():
    try:
        import rtmidi  # noqa
        return rtmidi
    except ImportError:
        sys.exit("python-rtmidi is not installed (use ~/drumrec/.venv)")


def sleep_chunk(rem):
    """How long to sleep with `rem` seconds to go (0 = spin). macOS timer coalescing lets sleep(x)
    overshoot by up to ~50% of x, capped near 10 ms (measured on the drum Mac), so sleep in shrinking
    chunks that cannot pass the target and spin only the last ~2 ms."""
    if rem > 0.02:
        return rem - 0.011
    if rem > SPIN:
        return (rem - 0.0005) / 1.6
    return 0.0


def wait_until(target):
    """High-resolution wait on time.perf_counter: sleep in safe chunks to ~2 ms early, then spin."""
    while True:
        rem = target - time.perf_counter()
        if rem <= 0:
            return
        s = sleep_chunk(rem)
        if s:
            time.sleep(s)


def find_port(names, sub):
    sub = sub.lower()
    hits = [i for i, n in enumerate(names) if sub in n.lower()]
    return hits[0] if hits else None


def jitter_stats(diffs_ms):
    if not diffs_ms:
        return None
    return {"n": len(diffs_ms), "mean_ms": round(st.fmean(diffs_ms), 3),
            "sd_ms": round(st.pstdev(diffs_ms), 3), "max_ms": round(max(diffs_ms), 3),
            "min_ms": round(min(diffs_ms), 3)}


# ---------------------------------------------------------------- ports
def cmd_ports(_args):
    rtmidi = need_rtmidi()
    mi, mo = rtmidi.MidiIn(), rtmidi.MidiOut()
    print("inputs:")
    for i, n in enumerate(mi.get_ports()):
        print(f"  [{i}] {n}")
    print("outputs:")
    for i, n in enumerate(mo.get_ports()):
        print(f"  [{i}] {n}")
    mi.delete(); mo.delete()


# ---------------------------------------------------------------- record
def cmd_record(args):
    rtmidi = need_rtmidi()
    import mido
    sys.setswitchinterval(0.0002)  # the click thread spins; keep the input callback from waiting on the GIL

    if (args.bars is None) == (args.seconds is None):
        sys.exit("give exactly one of --bars or --seconds")
    beat = 60.0 / args.bpm
    tempo_us = int(round(60_000_000 / args.bpm))
    count_beats = args.count_in * 4
    take_beats = args.bars * 4 if args.bars is not None else int(math.ceil(args.seconds / beat))
    total_beats = count_beats + take_beats

    # input
    midi_in = rtmidi.MidiIn(queue_size_limit=8192)
    in_names = midi_in.get_ports()
    sub = args.in_port or "edrumin"
    idx = find_port(in_names, sub)
    if idx is None:
        sys.exit(f"no MIDI input matching {sub!r}; inputs: {in_names}")
    in_name = in_names[idx]
    midi_in.ignore_types(sysex=False, timing=False, active_sense=False)
    received = []  # (perf_counter, bytes)

    def on_msg(event, _data=None):
        received.append((time.perf_counter(), event[0]))

    midi_in.set_callback(on_msg)
    midi_in.open_port(idx, "drumrec in")

    # click output
    midi_out = rtmidi.MidiOut()
    if args.click_out:
        out_names = midi_out.get_ports()
        oidx = find_port(out_names, args.click_out)
        if oidx is None:
            sys.exit(f"no MIDI output matching {args.click_out!r}; outputs: {out_names}")
        click_name = out_names[oidx]
        midi_out.open_port(oidx, "drumrec click")
    else:
        click_name = args.click_virtual
        midi_out.open_virtual_port(click_name)
    click_desc = click_name + ("" if args.click_out else " (virtual)")

    # schedule: note-on at each beat, note-off CLICK_LEN later
    start_wall = datetime.datetime.now().astimezone()
    t0 = time.perf_counter() + args.lead_in
    events = []
    for b in range(total_beats):
        note = DOWN if b % 4 == 0 else BEAT
        vel = 110 if b % 4 == 0 else 90
        events.append((t0 + b * beat, 0, b, [0x90 | CLICK_CH, note, vel]))
        events.append((t0 + b * beat + CLICK_LEN, 1, b, [0x80 | CLICK_CH, note, 0]))
    events.sort()
    end_t = t0 + total_beats * beat
    print(f"recording {in_name!r}, click -> {click_desc}, {args.bpm} bpm, "
          f"{args.count_in} bar(s) count-in + {take_beats} beats ({take_beats * beat:.1f} s)", flush=True)

    clicks = []  # (beat index, note, scheduled, actual)
    for when, kind, b, msg in events:
        wait_until(when)
        actual = time.perf_counter()
        midi_out.send_message(msg)
        if kind == 0:
            clicks.append((b, msg[1], when, actual))
            if b == count_beats and count_beats:
                print("  take starts", flush=True)
    wait_until(end_t + args.tail)
    midi_in.cancel_callback()
    midi_in.close_port(); midi_out.close_port()
    snapshot = list(received)

    # ---- write MIDI
    first = clicks[0][3]

    def tick(t):
        return int(round((t - first) * 1_000_000 / tempo_us * PPQ))

    mid = mido.MidiFile(type=1, ticks_per_beat=PPQ)
    t_tempo, t_click, t_perf = mido.MidiTrack(), mido.MidiTrack(), mido.MidiTrack()
    mid.tracks += [t_tempo, t_click, t_perf]
    t_tempo.append(mido.MetaMessage("track_name", name="drumrec tempo", time=0))
    t_tempo.append(mido.MetaMessage("set_tempo", tempo=tempo_us, time=0))
    t_tempo.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    markers = ([(0, "count-in")] if count_beats else []) + [(count_beats * PPQ, "take")]
    last = 0
    for tk, text in markers:
        t_tempo.append(mido.MetaMessage("marker", text=text, time=tk - last)); last = tk

    def emit(track, name, items):  # items: (tick, mido.Message)
        track.append(mido.MetaMessage("track_name", name=name, time=0))
        prev = 0
        for tk, m in sorted(items, key=lambda x: x[0]):
            track.append(m.copy(time=tk - prev)); prev = tk

    click_items = []  # note-offs sit their nominal gap after the actual note-on
    for b, note, sched, actual in clicks:
        on = tick(actual)
        click_items.append((on, mido.Message("note_on", channel=CLICK_CH, note=note, velocity=110 if note == DOWN else 90)))
        click_items.append((on + max(1, tick(actual + CLICK_LEN) - on), mido.Message("note_off", channel=CLICK_CH, note=note, velocity=0)))
    emit(t_click, CLICK_TRACK, click_items)

    counts, perf_items, skipped_rt, before_start = {}, [], 0, 0
    for t, data in snapshot:
        try:
            m = mido.Message.from_bytes(data)
        except (ValueError, TypeError):
            counts["unparsed"] = counts.get("unparsed", 0) + 1
            continue
        counts[m.type] = counts.get(m.type, 0) + 1
        if data[0] >= 0xF1 and data[0] != 0xF7:  # system common/realtime: not valid in a MIDI file
            skipped_rt += 1
            continue
        tk = tick(t)
        if tk < 0:
            before_start += 1
            continue
        perf_items.append((tk, m))
    emit(t_perf, PERF_TRACK, perf_items)

    TAKES.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in args.label).strip("-") or "take"
    stem = f"{start_wall:%Y-%m-%d-%H%M}-{safe}"
    path = TAKES / f"{stem}.mid"
    mid.save(path)

    diffs = [(a - s) * 1000 for _, _, s, a in clicks]
    hits = sum(1 for t, d in snapshot if (d[0] & 0xF0) == 0x90 and len(d) > 2 and d[2] > 0)
    side = {
        "file": path.name, "label": args.label, "bpm": args.bpm, "tempo_us": tempo_us,
        "bars": args.bars, "seconds": args.seconds, "take_beats": take_beats,
        "count_in_bars": args.count_in, "time_signature": "4/4", "ppq": PPQ, "midi_type": 1,
        "tick0": "actual send time of the first click (first count-in click when count-in > 0)",
        "duration_s": round(total_beats * beat, 3),
        "input_port": in_name, "click_port": click_desc,
        "click_notes": {"channel": 10, "downbeat": DOWN, "beat": BEAT},
        "start_wall": start_wall.isoformat(timespec="seconds"),
        "click_send_jitter": jitter_stats(diffs),
        "click_times_ms": [round((a - first) * 1000, 3) for _, _, _, a in clicks],        "message_counts": counts, "hits": hits,
        "dropped": {"system_realtime": skipped_rt, "before_first_click": before_start},
    }
    path.with_suffix(".json").write_text(json.dumps(side, indent=1))
    j = side["click_send_jitter"]
    print(f"saved {path}")
    print(f"  {len(snapshot)} messages ({hits} hits) {counts}")
    print(f"  click send jitter: mean {j['mean_ms']:.3f} ms, SD {j['sd_ms']:.3f}, max {j['max_ms']:.3f} over {j['n']} clicks")


# ---------------------------------------------------------------- list
def take_rows():
    rows = []
    for p in sorted(TAKES.glob("*.mid")):
        side = p.with_suffix(".json")
        d = json.loads(side.read_text()) if side.exists() else {}
        rows.append({"file": p.name, "date": d.get("start_wall", p.name[:10]), "label": d.get("label", p.stem[16:]),
                     "bpm": d.get("bpm"), "duration_s": d.get("duration_s"), "hits": d.get("hits")})
    return rows


def cmd_list(args):
    rows = take_rows()
    if args.json:
        print(json.dumps(rows, indent=1)); return
    if not rows:
        print(f"no takes in {TAKES}"); return
    for r in rows:
        dur = f'{r["duration_s"]:.0f}s' if r["duration_s"] else "?"
        print(f'{r["date"][:16]:16}  {r["label"]:20}  {r["bpm"] or "?":>5} bpm  {dur:>5}  {r["hits"] if r["hits"] is not None else "?":>4} hits')


# ---------------------------------------------------------------- analyze
def read_take(path):
    """Return (tempo_us, ppq, click_ms[], hits{note: [(ms, vel)]}, take_start_ms). Works for type 0 and 1."""
    import mido
    mid = mido.MidiFile(path)
    ppq, tempo = mid.ticks_per_beat, None
    clicks, hits, take_tick = [], {}, 0
    for track in mid.tracks:
        is_click = track.name == CLICK_TRACK
        t = 0
        for m in track:
            t += m.time
            if m.type == "set_tempo" and tempo is None:
                tempo = m.tempo
            elif m.type == "marker" and m.text == "take":
                take_tick = t
            elif m.type == "note_on" and m.velocity > 0:
                if is_click or (m.channel == CLICK_CH and m.note in (DOWN, BEAT)):
                    clicks.append(t)
                else:
                    hits.setdefault(m.note, []).append((t, m.velocity))
    tempo = tempo or 500000
    ms = tempo / 1000.0 / ppq
    return tempo, ppq, [c * ms for c in sorted(clicks)], {n: [(t * ms, v) for t, v in sorted(s)] for n, s in hits.items()}, take_tick * ms


def cmd_analyze(args):
    import bisect
    tempo, ppq, clicks, hits, take_ms = read_take(args.file)
    if len(clicks) < 2:
        sys.exit("need at least two click notes in the file")
    beat_ms = clicks[1] - clicks[0]
    # actual clicks plus the 8th halfway between consecutive clicks; extend two beats past each end
    # (at the nominal beat) so a hit on the beat after the last click is not scored against its "&"
    nominal = tempo / 1000.0
    ext = [clicks[0] - 2 * nominal, clicks[0] - nominal] + clicks + [clicks[-1] + nominal, clicks[-1] + 2 * nominal]
    grid = []
    for a, b in zip(ext, ext[1:]):
        grid += [a, (a + b) / 2]
    grid.append(ext[-1])
    print(f"{pathlib.Path(args.file).name}: {60000 / (tempo / 1000):.1f} bpm, {len(clicks)} clicks"
          + (", skipping count-in" if args.skip_count_in else ""))
    for note in sorted(hits):
        offs = []
        for t, _v in hits[note]:
            if args.skip_count_in and t < take_ms - beat_ms / 4:
                continue
            i = bisect.bisect_left(grid, t)
            near = min((g for g in grid[max(0, i - 1):i + 1]), key=lambda g: abs(t - g))
            offs.append(t - near)
        if not offs:
            continue
        name = NOTE_NAMES.get(note, "")
        print(f"  note {note:3} {name:11} n={len(offs):4}  mean={st.fmean(offs):+7.2f} ms  SD={st.pstdev(offs):6.2f} ms"
              f"  median={st.median(offs):+7.2f}")


def main():
    ap = argparse.ArgumentParser(prog="drumrec", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("ports", help="list MIDI inputs and outputs").set_defaults(fn=cmd_ports)
    r = sp.add_parser("record", help="record a take with a click")
    r.add_argument("--bpm", type=float, required=True)
    r.add_argument("--bars", type=int)
    r.add_argument("--seconds", type=float)
    r.add_argument("--count-in", type=int, default=1, metavar="BARS")
    r.add_argument("--in", dest="in_port", metavar="PORT", help="input port substring (default: eDRUMin)")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--click-out", metavar="PORT", help="send the click to an existing output (substring)")
    g.add_argument("--click-virtual", metavar="NAME", default="drumrec click", help='virtual click port (default "drumrec click")')
    r.add_argument("--label", required=True)
    r.add_argument("--lead-in", type=float, default=1.0, help="seconds between opening ports and the first click (default 1)")
    r.add_argument("--tail", type=float, default=0.3, help="seconds to keep recording after the last beat ends (default 0.3)")
    r.set_defaults(fn=cmd_record)
    l = sp.add_parser("list", help="list takes")
    l.add_argument("--json", action="store_true")
    l.set_defaults(fn=cmd_list)
    a = sp.add_parser("analyze", help="per-note offsets against the recorded click (nearest 8th)")
    a.add_argument("file")
    a.add_argument("--skip-count-in", action="store_true")
    a.set_defaults(fn=cmd_analyze)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
