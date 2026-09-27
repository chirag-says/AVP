r"""Echo-guard matrix: Cases A-E across echo and patient levels, and a margin sweep.

    .venv\Scripts\python.exe server\voice_eval\test_echo_guard_matrix.py

Levels are what reaches the SERVER (after the browser's processing):
  echo    -60 = browser AEC working well ... -20 = AEC failed, speaker loud
  patient -26 = normal, close; -40/-46 = quiet; "-26 via AEC" = a normal
          patient while the bot talks, after Chrome's double-talk attenuation
          (~24 dB measured by chrome_aec_probe.py) = -50
Local only (real Silero VAD, bot.py VAD settings), no network.
Limitation: echo here is a scaled copy of the bot's TTS; real residual echo
after AEC is distorted and non-stationary.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import voice.echo_guard as guard  # noqa: E402
from voice_eval.test_echo_guard import simulate  # noqa: E402

ECHO = [-60, -50, -40, -30, -26, -20]
PATIENT = {"normal -26": -26, "quiet -40": -40, "very quiet -46": -46, "normal via AEC -50": -50}


async def run_matrix(margin: float):
    guard.BARGE_IN_MARGIN_DB = margin
    false_turns, rows = 0, []
    for echo in ECHO:  # Case A / D: nobody speaks
        ev = await simulate(echo, None)
        false_turns += bool(ev["turn_starts"])
        rows.append(("A/D echo only", echo, "-", "FALSE TURN" if ev["turn_starts"] else "ok", ""))
    detected = total = 0
    for pname, plevel in PATIENT.items():  # Case B / C: patient barges in 1.5 s into bot speech
        for echo in ECHO:
            ev = await simulate(echo, plevel, patient_at=1.5)
            stock = await simulate(echo, plevel, patient_at=1.5, guard=False)
            starts, onset, bot_end = ev["turn_starts"], ev["patient_start"], ev["bot_audio_end"]
            early = [s for s in starts if s < onset]
            in_bot = [s for s in starts if onset <= s < bot_end]
            total += 1
            detected += bool(in_bot) and not early
            verdict = "EARLY" if early else (f"+{in_bot[0] - onset:.2f}s" if in_bot else "missed during TTS")
            # Did Silero hear the patient at all? (stock VAD start after onset)
            heard = [v for v in stock["vad_starts"] if onset <= v < bot_end] or [
                v for v in stock["vad_starts"] if v < onset]  # already "speaking" from echo
            rows.append(("B/C barge-in", echo, pname, verdict, "VAD heard" if heard else "VAD never fired"))
    for echo in (-50, -30):  # Case E: brief overlap, patient starts 0.7 s before the bot ends
        ev = await simulate(echo, -26, patient_at=3.8)
        starts, onset = ev["turn_starts"], ev["patient_start"]
        v = [s for s in starts if s >= onset]
        rows.append(("E brief overlap", echo, "normal -26", f"+{v[0] - onset:.2f}s" if v else "no VAD turn", ""))
    return false_turns, detected, total, rows


async def main():
    summary = {}
    for margin in (3.0,) if '--current' in sys.argv else (3.0, 0.0, -3.0):
        false_turns, detected, total, rows = await run_matrix(margin)
        summary[margin] = (false_turns, detected, total)
        print(f"\n=== BARGE_IN_MARGIN_DB = {margin:+.0f} dB ===")
        print(f"{'case':16s} {'echo dBFS':>9s}  {'patient':20s} result")
        for case, echo, patient, verdict, note in rows:
            print(f"{case:16s} {echo:>9d}  {patient:20s} {verdict:20s} {note}")
    print("\nmargin  false turns (echo only, of 6)  patient barge-ins detected during TTS")
    for m, (f, d, t) in summary.items():
        print(f"{m:+5.0f}   {f:^30d}  {d}/{t}")


if __name__ == "__main__":
    asyncio.run(main())
