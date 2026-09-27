"""Endpointing test: does a pause end the patient's turn too early, or the end too late?

Replays synthetic answers with real pauses through the same models the intake
bot uses (Silero VAD + Smart Turn v3, both local) and applies the same timing
rules as Pipecat's TurnAnalyzerUserTurnStopStrategy:

  VAD stop (after stop_secs of quiet) -> Smart Turn scores the turn
    complete   -> turn ends max(0, STT p99 1.17 s - stop_secs) later
    incomplete -> keep listening; Smart Turn's own 3 s silence fallback ends it

    .venv\\Scripts\\python.exe server\\voice_eval\\test_endpointing.py            # uses cached TTS
    .venv\\Scripts\\python.exe server\\voice_eval\\test_endpointing.py --stop-secs 0.5,0.8

Synthesizes the answer fragments once via Sarvam TTS (a few paise). Caveat:
TTS prosody at a mid-sentence cut ("...diabetes for") is not how a person
trails off, so Smart Turn verdicts here are indicative, not ground truth.
"""
import argparse
import asyncio
import json
import os
import pathlib
import sys

import numpy as np
import soundfile as sf

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402
from loguru import logger  # noqa: E402

load_dotenv(SERVER_DIR / ".env", override=True)
logger.remove()
logger.add(sys.stderr, level="WARNING")

import pipecat.audio.turn.smart_turn.base_smart_turn as smart_turn_base  # noqa: E402
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3  # noqa: E402
from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState  # noqa: E402
from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.audio.vad.vad_analyzer import VADParams, VADState  # noqa: E402

SR, FRAME = 16000, 320
STT_P99 = 1.17  # pipecat.services.stt_latency.SARVAM_TTFS_P99
CACHE = pathlib.Path(__file__).resolve().parent / ".cache" / "endpointing"

# (id, [fragments], [pauses between fragments in s]); a turn is "premature"
# if it ends before the patient's last word.
CASES = [
    ("short_yes", ["Yes."], []),
    ("short_no_allergies", ["No, I don't have any allergies."], []),
    ("age", ["Forty two."], []),
    ("long_answer", ["I have had a headache and mild fever since Monday, and the headache gets worse in the evening."], []),
    ("diabetes_pause_1s", ["Yes, I have diabetes for", "about five years."], [1.0]),
    ("diabetes_pause_2s", ["Yes, I have diabetes for", "about five years."], [2.0]),
    ("phone_pause_1s", ["My number is nine eight seven six five,", "four three two one zero."], [1.0]),
    ("phone_pause_2s", ["My number is nine eight seven six five,", "four three two one zero."], [2.0]),
    ("dob_pause_1s", ["My date of birth is", "fourteenth March, nineteen eighty two."], [1.0]),
    ("meds_list_pauses", ["I take metformin,", "amlodipine,", "and atorvastatin."], [0.9, 0.9]),
    ("spelling", ["R.", "A.", "M.", "E.", "S.", "H."], [0.5, 0.5, 0.5, 0.5, 0.5]),
    ("thinking_pause_1_5s", ["I am taking some tablet for thyroid,", "I think it is thyronorm."], [1.5]),
    # Intake numbers/dates (validation pass, Part 12): does a natural pause end the turn early?
    ("n_phone_group_0_7s", ["My number is nine eight seven six five,", "four three two one zero."], [0.7]),
    ("n_phone_digits_slow", ["Nine eight seven,", "six five four,", "three two one zero."], [0.6, 0.6]),
    ("n_date_pause", ["My date of birth is twelve June,", "two thousand four."], [0.8]),
    ("n_dosage_pause", ["Five hundred milligrams,", "twice a day."], [0.8]),
    ("n_duration_pause", ["I have had diabetes", "for five years."], [1.0]),
    ("n_age_short", ["Twenty two."], []),
]


def _synthesize():
    from sarvamai import SarvamAI

    from voice_eval.stt_ab import _tts

    CACHE.mkdir(parents=True, exist_ok=True)
    client = None
    for cid, fragments, _ in CASES:
        for i, text in enumerate(fragments):
            path = CACHE / f"{cid}_{i}.wav"
            if path.exists():
                continue
            client = client or SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])
            sf.write(path, _tts(client, text, "rahul", "en-IN"), SR)


class _Clock:
    """Simulated time, so Smart Turn's wall-clock buffer trimming sees real-time audio."""

    now = 0.0

    @classmethod
    def monotonic(cls):
        return cls.now

    perf_counter = staticmethod(__import__("time").perf_counter)  # timing metrics stay real


def build_audio(cid, fragments, pauses) -> tuple[np.ndarray, list[float], float]:
    """Fragments at -26 dBFS with the given pauses; returns (audio, fragment starts, speech end)."""
    rng = np.random.default_rng(3)
    parts, starts, t = [np.zeros(int(0.5 * SR))], [], 0.5
    for i, _ in enumerate(fragments):
        x = sf.read(CACHE / f"{cid}_{i}.wav", dtype="float32")[0]
        x = x / (np.sqrt(np.mean(x[np.abs(x) > 0.01] ** 2)) + 1e-9) * 10 ** (-26 / 20)
        starts.append(t)
        parts.append(x)
        t += len(x) / SR
        if i < len(pauses):
            parts.append(np.zeros(int(pauses[i] * SR)))
            t += pauses[i]
    speech_end = t
    parts.append(np.zeros(int(6 * SR)))
    audio = np.concatenate(parts)
    audio += rng.standard_normal(len(audio)) * 10 ** (-68 / 20)
    return audio, starts, speech_end


async def vad_stop_times(audio: np.ndarray, stop_secs: float) -> list[float]:
    """When bot.py's Silero settings would emit VAD stops (and so flush Sarvam)."""
    vad = SileroVADAnalyzer(params=VADParams(confidence=0.7, start_secs=0.2, stop_secs=stop_secs, min_volume=0.6))
    vad.set_sample_rate(SR)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    stops, speaking = [], False
    for i in range(0, len(pcm) - FRAME, FRAME):
        state = await vad.analyze_audio(pcm[i : i + FRAME].tobytes())
        if state == VADState.SPEAKING and not speaking:
            speaking = True
        elif state == VADState.QUIET and speaking:
            speaking = False
            stops.append(round(i / SR, 2))
    return stops


async def run_case(cid, fragments, pauses, stop_secs: float):
    audio, starts, speech_end = build_audio(cid, fragments, pauses)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)

    smart_turn_base.time = _Clock  # test-only: simulated clock for the analyzer
    vad = SileroVADAnalyzer(params=VADParams(confidence=0.7, start_secs=0.2, stop_secs=stop_secs, min_volume=0.6))
    vad.set_sample_rate(SR)
    turn = LocalSmartTurnAnalyzerV3()
    turn.set_sample_rate(SR)
    turn.update_vad_start_secs(0.2)

    speaking, ended_at, verdicts = False, None, []
    pending_end: float | None = None
    for i in range(0, len(pcm) - FRAME, FRAME):
        now = i / SR
        _Clock.now = now
        chunk = pcm[i : i + FRAME].tobytes()
        state = await vad.analyze_audio(chunk)
        if state == VADState.SPEAKING and not speaking:
            speaking, pending_end = True, None
        elif state == VADState.QUIET and speaking:
            speaking = False
            eot, metrics = await turn.analyze_end_of_turn()
            verdicts.append((round(now, 2), eot == EndOfTurnState.COMPLETE, round(getattr(metrics, "probability", 0) or 0, 2)))
            if eot == EndOfTurnState.COMPLETE:
                pending_end = now + max(0.0, STT_P99 - stop_secs)
        if turn.append_audio(chunk, speaking) == EndOfTurnState.COMPLETE and not speaking:
            pending_end = now  # Smart Turn's silence fallback
        if pending_end is not None and now >= pending_end:
            ended_at = now
            break

    # Any turn end before the patient finished speaking is premature, including
    # one that lands a moment after they resumed but before VAD re-triggered.
    return {
        "case": cid,
        "premature": ended_at is not None and ended_at < speech_end,
        "end_after_speech_s": round(ended_at - speech_end, 2) if ended_at and ended_at >= speech_end else None,
        "ended_at": ended_at,
        "verdicts": verdicts,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop-secs", default="0.8", help="comma list of Silero stop_secs to compare")
    args = ap.parse_args()
    _synthesize()
    summary = {}
    for stop in (float(s) for s in args.stop_secs.split(",")):
        print(f"\nSilero stop_secs={stop} + Smart Turn v3")
        rows = [await run_case(*c, stop_secs=stop) for c in CASES]
        for r in rows:
            flag = "PREMATURE" if r["premature"] else "ok"
            print(f"  {r['case']:22s} {flag:9s} turn ends {r['end_after_speech_s']}s after speech | smart-turn (t, complete, p): {r['verdicts']}")
        done = [r["end_after_speech_s"] for r in rows if r["end_after_speech_s"] is not None]
        summary[stop] = {
            "premature": sum(r["premature"] for r in rows),
            "median_end_after_speech_s": round(float(np.median(done)), 2) if done else None,
            "max_end_after_speech_s": max(done) if done else None,
        }
    print("\n" + json.dumps(summary, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
