r"""Tests for the no-transcript recovery and the headset-only transcript gate.

    .venv\Scripts\python.exe server\voice_eval\test_recovery_and_gate.py

Local only, no network.
"""
import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="WARNING")

from pipecat.frames.frames import (  # noqa: E402
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from pipecat.tests.utils import SleepFrame, run_test  # noqa: E402

from voice.echo_guard import BotSpeechTracker, EchoTranscriptFilter  # noqa: E402
from voice.recovery import RECOVERY_PROMPT, NoTranscriptRecovery  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")


def turn(rec: NoTranscriptRecovery, t0: float, speech_s: float, text: str | None) -> bool:
    """Feed one user turn to observe(); return whether recovery fired."""
    rec.observe(VADUserStartedSpeakingFrame(start_secs=0.2), t0)
    rec.observe(UserStartedSpeakingFrame(), t0 + 0.01)
    rec.observe(VADUserStoppedSpeakingFrame(stop_secs=0.2), t0 + speech_s)
    if text is not None:
        rec.observe(TranscriptionFrame(text, "", ""), t0 + speech_s + 0.3)
    return rec.observe(UserStoppedSpeakingFrame(), t0 + speech_s + 5)


def recovery_unit():
    rec = NoTranscriptRecovery()
    check("recovery: 1 s of speech, no transcript -> ask to repeat", turn(rec, 0, 1.0, None))
    rec = NoTranscriptRecovery()
    check("recovery: turn with a transcript -> nothing", not turn(rec, 0, 1.0, "I take metformin"))
    rec = NoTranscriptRecovery()
    check("recovery: 0.3 s blip (cough) -> nothing", not turn(rec, 0, 0.3, None))
    rec = NoTranscriptRecovery()
    fired = [turn(rec, 10 * i, 1.0, None) for i in range(3)]
    check("recovery: at most 2 prompts in a row (3rd empty turn stays quiet)", fired == [True, True, False])
    turn(rec, 40, 1.0, "yes")
    check("recovery: a real transcript resets the limit", turn(rec, 50, 1.0, None))
    rec = NoTranscriptRecovery()
    check("recovery: whitespace-only transcript is not an answer", turn(rec, 0, 1.0, "   "))


async def recovery_in_pipeline():
    frames = [
        VADUserStartedSpeakingFrame(start_secs=0.2),
        UserStartedSpeakingFrame(),
        SleepFrame(sleep=0.7),
        VADUserStoppedSpeakingFrame(stop_secs=0.2),
        UserStoppedSpeakingFrame(),
        SleepFrame(sleep=0.2),
    ]
    down, _ = await run_test(NoTranscriptRecovery(), frames_to_send=frames,
                             frames_to_send_direction=FrameDirection.UPSTREAM)
    speak = [f for f in down if isinstance(f, TTSSpeakFrame)]
    check("recovery in a Pipecat pipeline: pushes the spoken prompt downstream (to LLM/TTS)",
          len(speak) == 1 and speak[0].text == RECOVERY_PROMPT and speak[0].append_to_context)


def gate_unit():
    now = time.monotonic()

    tracker = BotSpeechTracker()
    tracker._spoken.append((now, "could you tell me which medications you are taking".split()))

    def filt(**state):
        f = EchoTranscriptFilter(tracker, echo_rules=state.pop("echo_rules", True),
                                 require_vad=state.pop("require_vad"))
        for k, v in state.items():
            setattr(f, f"_{k}", v)
        return f

    check("gate on: VAD active -> patient transcript accepted",
          filt(require_vad=True, vad_active=True, vad_last=now).echo_reason("my name is ramesh", now) is None)
    check("gate on: VAD stopped 1 s ago (STT finalizing) -> accepted",
          filt(require_vad=True, vad_last=now - 1.0).echo_reason("my name is ramesh", now) is None)
    check("gate on: no VAD for 4 s, even inside an open turn -> dropped (bystander in the pause)",
          filt(require_vad=True, vad_last=now - 4.0, user_turn=True).echo_reason("did you pay", now) == "no_vad_headset_gate")
    check("gate on: VAD never fired -> dropped",
          filt(require_vad=True).echo_reason("did you pay the parking fee", now) == "no_vad_headset_gate")
    check("gate off (laptop default): no VAD, bot silent -> accepted (soft-speech fallback unchanged)",
          filt(require_vad=False, bot_last_audio=now - 10).echo_reason("yes", now) is None)
    check("gate on + echo rules: bot's own words still dropped",
          filt(require_vad=True, bot_speaking=True, vad_active=True, vad_last=now)
          .echo_reason("which medications are you taking", now) == "bot_words")
    check("gate on, echo guard off: only the gate applies (bot words not checked)",
          filt(require_vad=True, echo_rules=False, bot_speaking=True, vad_active=True, vad_last=now)
          .echo_reason("which medications are you taking", now) is None)


async def recovery_real_turn():
    """Real Silero + Smart Turn + user aggregator, and an STT that returns nothing."""
    import numpy as np
    import soundfile as sf
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.frames.frames import InputAudioRawFrame, LLMContextFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair, LLMUserAggregatorParams)
    from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy
    from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
    from pipecat.turns.user_turn_strategies import UserTurnStrategies
    from voice.echo_guard import EchoAwareVADUserTurnStartStrategy

    clip = pathlib.Path(__file__).resolve().parent / ".cache" / "mix" / "clean" / "metformin.wav"
    audio = np.concatenate([sf.read(clip, dtype="float32")[0], np.zeros(7 * 16000, dtype=np.float32)])
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    frames = []
    for i in range(0, len(pcm) - 640, 640):
        frames += [InputAudioRawFrame(audio=pcm[i:i + 640], sample_rate=16000, num_channels=1), SleepFrame(sleep=0.02)]
    context = LLMContext()
    user_agg, _ = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(
            start=[EchoAwareVADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy()],
            stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())]),
        vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2))))
    # No STT in the chain = a silent STT drop: the patient speaks, no transcript arrives.
    down, _ = await run_test(Pipeline([NoTranscriptRecovery(), user_agg]), frames_to_send=frames)
    speak = [f for f in down if isinstance(f, TTSSpeakFrame)]
    check("recovery, real VAD + Smart Turn + aggregator, STT silent: bot asks to repeat once",
          len(speak) == 1 and speak[0].text == RECOVERY_PROMPT)
    check("recovery: nothing sent to the LLM from the empty turn (nothing can be saved)",
          not any(isinstance(f, LLMContextFrame) for f in down) and not any(
              m.get("role") == "user" for m in context.messages))


def main() -> int:
    recovery_unit()
    asyncio.run(recovery_in_pipeline())
    asyncio.run(recovery_real_turn())
    gate_unit()
    failed = sum(not ok for _, ok in results)
    print("\nALL PASSED" if not failed else f"\n{failed} FAILED")
    return failed


if __name__ == "__main__":
    sys.exit(main())
