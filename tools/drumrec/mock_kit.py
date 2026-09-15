#!/usr/bin/env python3
"""mock_kit: a fake e-kit that plays along to the drumrec click, for end-to-end timing tests.

Listens on the "drumrec click" source and plays on a virtual output "drumrec mock kit":
kick 36 on beats 1 and 3, snare head 6 on 2 and 4, closed hat 12 on every 8th.

Each click heard anchors the NEXT beat: beat k+1 is expected at (click k receive time + one beat),
so notes with negative noise can still be sent early instead of being clamped to "now". The 8th
between is half a beat later. Every note goes out at anchor + --offset-ms + N(0, --jitter-ms).
Every intended and actual send time is logged to --log (time.perf_counter seconds; on the drum
Mac's Python 3.9 that clock starts near zero per process, so it is not comparable with drumrec's).

  python mock_kit.py --bpm 106 --offset-ms 8 --jitter-ms 10 --seed 1 --log mock.json
"""
import argparse, heapq, json, pathlib, random, statistics as st, sys, threading, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from drumrec import sleep_chunk, DOWN, BEAT  # noqa: E402  (same chunked sleep as the recorder)

KICK, SNARE, HAT = 36, 6, 12
NOTE_LEN = 0.03


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bpm", type=float, required=True)
    ap.add_argument("--offset-ms", type=float, default=0.0)
    ap.add_argument("--jitter-ms", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--click", default="drumrec click", help="click source substring")
    ap.add_argument("--out", default="drumrec mock kit", help="virtual output name")
    ap.add_argument("--channel", type=int, default=1, help="MIDI channel for the kit notes (1-16)")
    ap.add_argument("--velocity", type=int, default=90)
    ap.add_argument("--log", default="mock_kit_log.json")
    ap.add_argument("--timeout", type=float, default=120, help="exit if no click for this long after the last one (or before the first)")
    args = ap.parse_args()

    import rtmidi
    sys.setswitchinterval(0.0002)
    rng = random.Random(args.seed)
    beat = 60.0 / args.bpm
    ch = args.channel - 1

    out = rtmidi.MidiOut()
    out.open_virtual_port(args.out)
    print(f"virtual output {args.out!r} open; waiting for {args.click!r}", flush=True)

    heap, cv = [], threading.Condition()
    log, clicks, state = [], [], {"pos": None, "last": time.perf_counter()}
    seq = [0]

    def push(when, msg, meta):
        seq[0] += 1
        heapq.heappush(heap, (when, seq[0], msg, meta))

    def on_click(event, _data=None):
        t = time.perf_counter()
        data = event[0]
        if len(data) < 3 or (data[0] & 0xF0) != 0x90 or data[2] == 0 or data[1] not in (DOWN, BEAT):
            return
        pos = 0 if data[1] == DOWN else ((state["pos"] + 1) % 4 if state["pos"] is not None else None)
        state["pos"], state["last"] = pos, t
        clicks.append(t)
        if pos is None:
            return  # joined mid-bar; wait for a downbeat
        nxt = (pos + 1) % 4  # the beat we are about to play
        anchor = t + beat
        notes = [(0.0, HAT)] + [(0.0, KICK if nxt in (0, 2) else SNARE)] + [(beat / 2, HAT)]
        with cv:
            for rel, note in notes:
                noise = rng.gauss(0.0, args.jitter_ms) if args.jitter_ms > 0 else 0.0
                intended = anchor + rel + (args.offset_ms + noise) / 1000.0
                meta = {"note": note, "beat": nxt + 1, "eighth": "&" if rel else "", "click_recv": t,
                        "anchor": anchor + rel, "noise_ms": round(noise, 3), "intended": intended}
                push(intended, [0x90 | ch, note, args.velocity], meta)
                push(intended + NOTE_LEN, [0x80 | ch, note, 0], None)
            cv.notify()

    def sender():
        while True:
            with cv:
                while not heap:
                    cv.wait(0.05)
                    if stop.is_set():
                        return
                when = heap[0][0]
            s = sleep_chunk(when - time.perf_counter())
            if s:
                with cv:
                    cv.wait(s)  # wakes early if an earlier note was pushed
                continue
            while time.perf_counter() < when:
                pass
            with cv:
                when, _, msg, meta = heapq.heappop(heap)
            actual = time.perf_counter()
            out.send_message(msg)
            if meta:
                meta["actual"] = actual
                log.append(meta)

    stop = threading.Event()
    th = threading.Thread(target=sender, daemon=True)
    th.start()

    mi = rtmidi.MidiIn()
    mi.set_callback(on_click)
    connected = None
    try:
        while True:
            if connected is None:
                names = mi.get_ports()
                hit = [i for i, n in enumerate(names) if args.click.lower() in n.lower()]
                if hit:
                    connected = names[hit[0]]
                    mi.open_port(hit[0], "mock kit listen")
                    print(f"listening to {connected!r}", flush=True)
                    state["last"] = time.perf_counter()
            elif clicks and time.perf_counter() - state["last"] > max(2.0, 4 * beat) and not heap:
                break  # the click stopped and everything queued has been sent
            if time.perf_counter() - state["last"] > args.timeout:
                break
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    stop.set()
    mi.close_port(); out.close_port()

    lat = [(m["actual"] - m["intended"]) * 1000 for m in log]
    summary = None
    if lat:
        summary = {"n": len(lat), "mean_ms": round(st.fmean(lat), 3), "sd_ms": round(st.pstdev(lat), 3),
                   "max_ms": round(max(lat), 3), "min_ms": round(min(lat), 3)}
    json.dump({"bpm": args.bpm, "offset_ms": args.offset_ms, "jitter_ms": args.jitter_ms, "seed": args.seed,
               "clock": "time.perf_counter seconds", "click_port": connected, "clicks_heard": len(clicks),
               "send_jitter_actual_minus_intended": summary, "notes": log}, open(args.log, "w"), indent=1)
    print(f"sent {len(log)} notes for {len(clicks)} clicks; actual-intended {summary}; log {args.log}")


if __name__ == "__main__":
    main()
