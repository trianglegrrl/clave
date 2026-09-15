# drumrec

MIDI practice recorder with a click track, for Alaina's e-kit (eDRUMin 8 on the drum Mac, `drumz.local`).
It sends a click, records every message from the kit, and writes a MIDI file whose tempo grid lines up
with the click. `mock_kit.py` is a fake drummer for testing it without playing.

## Install (drum Mac)

Code lives in the clave repo (`tools/drumrec/`) and is copied to `~/drumrec/` on the Mac:

    rsync -a --exclude __pycache__ tools/drumrec/ fragglerock@drumz.local:drumrec/
    ssh fragglerock@drumz.local 'python3 -m venv ~/drumrec/.venv && ~/drumrec/.venv/bin/pip install --only-binary :all: mido python-rtmidi'

No Homebrew or Xcode needed (python-rtmidi has a prebuilt macOS wheel for Python 3.9). The venv is about 10 MB.
Run everything through `~/drumrec/drumrec`, which uses the venv.

## Commands

    drumrec ports                                   # MIDI inputs and outputs
    drumrec record --bpm 106 --bars 32 --label groove
    drumrec record --bpm 90 --seconds 60 --count-in 2 --label slow-cascara
    drumrec list [--json]                           # date, label, bpm, duration, hit count
    drumrec analyze FILE [--skip-count-in]          # per-note mean/SD against the recorded click

`record` options: `--in PORT` (substring, default the eDRUMin port), `--click-out PORT` (an existing output)
or `--click-virtual NAME` (default: a virtual port "drumrec click"), `--count-in BARS` (default 1),
`--lead-in` seconds before the first click (default 1), `--tail` seconds recorded after the last beat (default 0.3).

Takes land in `~/drumrec/takes/YYYY-MM-DD-HHMM-label.mid` with a `.json` sidecar (bpm, length, count-in,
ports, start time, click send jitter, the actual click times, message counts). On the hub,
`bin/clave-pull-takes` copies new takes into `recordings/drumz/`.

## File format

- Type 1, ppq 960, one fixed tempo from `--bpm`, 4/4.
- Track 0 `drumrec tempo`: tempo, time signature, markers `count-in` and `take`.
- Track 1 `drumrec click`: the click, channel 10, note 76 on downbeats, 77 on other beats, at the times the notes were actually sent.
- Track 2 `drumrec performance`: everything received from the kit (notes, CC, aftertouch, sysex) at the
  times it arrived. System realtime messages (clock, active sensing) are counted in the sidecar but not written (not valid in a MIDI file).
- **Tick 0 is the actual send time of the first click**, which is the first count-in click when there is a count-in.
  The count-in is whole bars, so the bar grid is the same either way; the `take` marker shows where the take starts.
- `dashboards/timing-tracker/build.py` reads these files: it merges tracks and skips the click track.

## How the click works

All beat times are computed up front from `time.perf_counter()` (start + n × 60/bpm). A single thread waits
for each one and sends the note-on (and a note-off 20 ms later) straight through python-rtmidi. The wait
sleeps in shrinking chunks and spins only the last ~2 ms. On this Mac, macOS timer coalescing lets
`sleep(x)` overshoot by up to half of x (capped near 10 ms), so a plain sleep plus a 2 ms spin sent clicks about 6 ms late.
The sidecar logs actual-minus-scheduled send time. Incoming messages are timestamped with `perf_counter`
as the first thing in the rtmidi callback.

`mock_kit.py --bpm 106 --offset-ms 8 --jitter-ms 10 --seed 1` listens to "drumrec click", plays kick/snare/8th hats on a
virtual output "drumrec mock kit", and logs every intended and actual send time. Test with
`drumrec record ... --in "drumrec mock kit"`.

## Routing the click into Superior Drummer 3 (manual, for Alaina)

drumrec does not touch SD3. To hear the click through SD3 and your headphones:

1. Start a recording once so the virtual port "drumrec click" exists (or leave one running).
2. In SD3's settings, open MIDI input and enable the "drumrec click" port alongside the eDRUMin.
3. Map notes 76 and 77 (channel 10) to a click, cowbell or rim sound you like, with 76 louder or different for the downbeat.

Or send the click to another output with `--click-out PORT`.

## Latency caveats

- The recorded click time is when drumrec handed the note to CoreMIDI. It does **not** include SD3's
  processing or its audio buffer latency, i.e. when you actually *hear* the click. The same delay applies to
  hearing your own drums, but a steady offset between you and the click can partly be buffer latency.
  This tool does not measure it. Smaller SD3 buffer = smaller offset.
- Kit hits are timestamped when the MIDI reaches Python, so eDRUMin trigger-detection latency and USB latency are included.
- The measured floor on the drum Mac (mock kit, zero offset and jitter) is in the section below. It is a
  software-only floor: two virtual ports, no USB, no audio.

## Measured on the drum Mac

End-to-end mock test, 2026-09-15, 106 bpm, 40 s (75 clicks, 225 notes), SD3 running in the background.
Recovered = `drumrec analyze` / `build.py` against the recorded click. "Realised" = offset + the actual seeded noise draws (seed 1).

| run | limb | realised mean / SD | recovered mean / SD |
|---|---|---|---|
| offset 8, jitter 10 | kick (n=37) | +9.58 / 8.24 ms | +9.85 / 8.12 ms |
| | snare (n=38) | +9.89 / 10.21 ms | +10.21 / 10.25 ms |
| | hats (n=150) | +8.02 / 9.93 ms | +8.36 / 9.91 ms |
| offset 0, jitter 0 | all limbs | 0 / 0 | +0.59 to +0.61 / 0.10 to 0.21 ms |

- The software floor (virtual ports, two processes) is about +0.6 ms mean with an SD of about 0.2 ms. 0.59 ms is one tick at 960 ppq and 106 bpm, so the true figure is below file resolution.
- Click send jitter (actual minus scheduled): mean 0.002 to 0.004 ms, max 0.09 ms. Mock send lateness: mean 0.02 to 0.08 ms, max 1.2 ms.
- With plain `sleep` + 2 ms spin, before the chunked sleep, clicks averaged 6.3 ms late (max 8.2).
- The small-sample means (n=37) sit up to 2.2 ms from the nominal 8 ms. That is the draw, not the recorder: recovered is within 0.35 ms of what was actually injected.
