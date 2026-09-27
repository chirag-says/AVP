"""Offline test of the TTS echo guard with the real Silero VAD on real speech.

No network, no Sarvam cost. Needs the TTS cache from `stt_ab.py generate`.

    .venv\\Scripts\\python.exe server\\voice_eval\\test_echo_guard.py

Drives EchoAwareVADUserTurnStartStrategy frame by frame exactly as the user
aggregator would (20 ms InputAudioRawFrames, VAD start/stop frames from the
same Silero settings as bot.py, bot speaking frames), for echo levels from
"browser AEC working" to "AEC failed, speaker loud", and checks when a user
turn would start. Also unit-tests EchoTranscriptFilter's drop rules.
"""
import asyncio
import pathlib
import sys

import numpy as np
import soundfile as sf

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="WARNING")

from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.audio.vad.vad_analyzer import VADParams, VADState  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)

from voice.echo_guard import BotSpeechTracker, EchoAwareVADUserTurnStartStrategy, EchoTranscriptFilter  # noqa: E402
from voice.telemetry import VoiceTelemetry  # noqa: E402

SR, FRAME = 16000, 320
TTS = pathlib.Path(__file__).resolve().parent / ".cache" / "tts"
ROOM_FLOOR_DBFS = -68


def _load(name: str, dbfs: float) -> np.ndarray:
    x = sf.read(TTS / f"{name}.wav", dtype="float32")[0]
    frames = x[: len(x) // FRAME * FRAME].reshape(-1, FRAME)
    e = np.sqrt(np.mean(frames**2, axis=1))
    active = np.sqrt(np.mean(e[e > e.max() * 0.05] ** 2))
    return x * 10 ** ((dbfs - 20 * np.log10(active)) / 20)


async def simulate(echo_dbfs: float | None, patient_dbfs: float | None, patient_at: float = 1.5, guard: bool = True):
    """Bot speaks from t=0; patient (optional) starts at `patient_at`. Returns events."""
    rng = np.random.default_rng(7)
    bot = _load("bot", echo_dbfs) if echo_dbfs is not None else np.zeros(int(4.5 * SR))
    bot_end = len(bot) / SR
    n = int((max(bot_end, patient_at + 5) + 1.5) * SR)
    mic = rng.standard_normal(n) * 10 ** (ROOM_FLOOR_DBFS / 20)
    mic[: len(bot)] += bot
    patient_start = None
    if patient_dbfs is not None:
        p = _load("metformin", patient_dbfs)
        s = int(patient_at * SR)
        mic[s : s + len(p)] += p[: n - s]
        patient_start = patient_at
    pcm = (np.clip(mic, -1, 1) * 32767).astype(np.int16)

    vad = SileroVADAnalyzer(params=VADParams(confidence=0.7, start_secs=0.2, stop_secs=0.8, min_volume=0.6))
    vad.set_sample_rate(SR)
    telemetry = VoiceTelemetry()
    strategy = EchoAwareVADUserTurnStartStrategy(telemetry=telemetry) if guard else None
    events: dict = {"turn_starts": [], "vad_starts": []}
    t = 0.0

    async def on_start(_s, _params):
        events["turn_starts"].append(round(t, 2))

    if strategy:
        strategy.add_event_handler("on_user_turn_started", on_start)
        if echo_dbfs is not None:
            await strategy.process_frame(BotStartedSpeakingFrame())

    # The server marks the bot stopped ~0.35 s after its last audio leaves.
    bot_stop_at = bot_end + 0.35 if echo_dbfs is not None else 0.0
    bot_stopped = echo_dbfs is None
    speaking = False
    for i in range(0, len(pcm) - FRAME, FRAME):
        t = i / SR
        if not bot_stopped and t >= bot_stop_at:
            bot_stopped = True
            if strategy:
                await strategy.process_frame(BotStoppedSpeakingFrame())
        chunk = pcm[i : i + FRAME].tobytes()
        if strategy:
            await strategy.process_frame(InputAudioRawFrame(audio=chunk, sample_rate=SR, num_channels=1))
        state = await vad.analyze_audio(chunk)
        if state == VADState.SPEAKING and not speaking:
            speaking = True
            events["vad_starts"].append(round(t, 2))
            if strategy:
                await strategy.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
            else:
                events["turn_starts"].append(round(t, 2))  # stock: every VAD start is a turn start
        elif state == VADState.QUIET and speaking:
            speaking = False
            if strategy:
                await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.8))
    events["counts"] = telemetry.summary()["counts"]
    events["patient_start"] = patient_start
    events["bot_audio_end"] = round(bot_end, 2)
    return events


# name, echo level (None = bot silent), patient level (None = no patient),
# max seconds from patient onset to turn start (None = no turn allowed at all)
CASES = [
    ("patient answers, bot silent", None, -26, 0.5),
    ("echo only, AEC working (-50 dBFS)", -50, None, None),
    ("echo only, AEC weak (-36 dBFS)", -36, None, None),
    ("echo only, AEC failed (-26 dBFS)", -26, None, None),
    ("barge-in over AEC-working echo", -50, -26, 0.5),
    ("barge-in over weak echo", -36, -26, 0.5),
    ("soft barge-in over AEC-working echo", -50, -40, 0.5),
    # Patient no louder than the echo: level can't separate them, so the VAD
    # path must NOT fire (it would equally fire on echo alone). The transcript
    # path starts this turn instead (EchoTranscriptFilter rules below).
    ("barge-in, echo as loud as patient", -26, -26, "transcript_path"),
]


async def main():
    print("EchoAwareVADUserTurnStartStrategy vs stock VAD start (real Silero, bot.py VAD params)\n")
    failures = 0
    for name, echo, patient, max_delay in CASES:
        guarded = await simulate(echo, patient)
        stock = await simulate(echo, patient, guard=False)
        onset = guarded["patient_start"]

        def verdict(starts):
            if max_delay is None:
                return not starts, "no turn"
            if max_delay == "transcript_path":
                return not starts, "no VAD turn (transcript path starts it)"
            early = [s for s in starts if s < onset]
            return (not early and bool(starts) and starts[0] - onset <= max_delay,
                    f"turn within {max_delay}s of patient onset, none before")

        ok, expect = verdict(guarded["turn_starts"])
        stock_ok, _ = verdict(stock["turn_starts"])
        failures += not ok
        delay = f", {guarded['turn_starts'][0] - onset:+.2f}s after patient onset" if onset and guarded["turn_starts"] else ""
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: expect {expect}")
        print(f"       stock {'ok ' if stock_ok else 'BAD'} turn starts {stock['turn_starts'][:3]} | guarded {guarded['turn_starts'][:3]}{delay}")
        print(f"       vad starts {guarded['vad_starts'][:3]}  counts {guarded['counts']}")

    print("\nEchoTranscriptFilter drop rules")
    tracker = BotSpeechTracker()
    tracker._spoken.append((10**9, "thank you could you tell me which medications you are currently taking and since when".split()))
    import time as _time

    def fresh(**state):
        f = EchoTranscriptFilter(tracker)
        for k, v in state.items():
            setattr(f, f"_{k}", v)
        return f

    now = _time.monotonic()
    tracker._spoken[0] = (now, tracker._spoken[0][1])
    rules = [
        ("bot silent long ago, no VAD (soft speech fallback)", fresh(bot_last_audio=now - 10), "i take metformin", None),
        ("bot speaking, no VAD", fresh(bot_speaking=True), "which medications you are taking", "no_vad"),
        ("bot speaking, VAD active, bot's words", fresh(bot_speaking=True, vad_active=True, vad_last=now), "which medications are you currently taking", "bot_words"),
        ("bot speaking, VAD active, patient words", fresh(bot_speaking=True, vad_active=True, vad_last=now), "metformin and amlodipine", None),
        ("bot stopped 1 s ago, echo tail, no VAD", fresh(bot_last_audio=now - 1), "since when", "no_vad"),
        ("user turn active", fresh(bot_speaking=True, user_turn=True), "which medications", None),
    ]
    for name, f, text, expected in rules:
        got = f.echo_reason(text, now)
        ok = got == expected
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {got!r} (expected {expected!r})")

    f = fresh()
    f.update_state(UserStartedSpeakingFrame(), now)
    assert f._user_turn  # state frames update the filter

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
