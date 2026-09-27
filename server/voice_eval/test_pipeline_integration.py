"""Integration test: the intake bot's real voice-input chain, end to end, on live Sarvam.

    STT service built by voice.stt (live Sarvam WebSocket)
      -> EchoTranscriptFilter
      -> LLMUserAggregator with bot.py's turn strategies, Silero and Smart Turn

Feeds a synthetic patient answer in real time and checks that the words come
out as a user message in the LLM context. It exercises Pipecat's own frame
plumbing: our turn-start strategy inside the controller, the filter in the
chain, and the config-driven STT connection that bot.py uses.

    .venv\\Scripts\\python.exe server\\voice_eval\\test_pipeline_integration.py
    set SARVAM_STT_MODEL=saaras:v4 & set STT_KEYTERMS=true & ...   (to test those paths)

Costs a few seconds of Sarvam STT per run.
"""
import asyncio
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

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3  # noqa: E402
from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.audio.vad.vad_analyzer import VADParams  # noqa: E402
from pipecat.frames.frames import InputAudioRawFrame, LLMContextFrame, UserStartedSpeakingFrame  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.tests.utils import SleepFrame, run_test  # noqa: E402
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy  # noqa: E402
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy  # noqa: E402
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from voice.config import VoiceConfig  # noqa: E402
from voice.echo_guard import BotSpeechTracker, EchoAwareVADUserTurnStartStrategy, EchoTranscriptFilter  # noqa: E402
from voice.stt import build_sarvam_stt  # noqa: E402
from voice.telemetry import VoiceTelemetry  # noqa: E402

SR, FRAME = 16000, 320
CLIP = pathlib.Path(__file__).resolve().parent / ".cache" / "mix" / "clean" / "metformin.wav"


async def main() -> int:
    config = VoiceConfig.from_env()
    telemetry = VoiceTelemetry(session_label="integration-test")
    stt = build_sarvam_stt(os.environ["SARVAM_API_KEY"], config)
    connects = []

    @stt.event_handler("on_connected")
    async def on_connected(_service):
        connects.append(1)
        telemetry.note_stt_connected()

    bot_speech = BotSpeechTracker()
    context = LLMContext()
    user_agg, _ = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=[EchoAwareVADUserTurnStartStrategy(telemetry=telemetry), TranscriptionUserTurnStartStrategy()],
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())],
            ),
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=config.vad_stop_secs)),
        ),
    )
    echo_filter = EchoTranscriptFilter(bot_speech, telemetry=telemetry)
    telemetry.watch(input_transport=None, stt=stt, user_aggregator=user_agg, output_transport=None)

    audio = sf.read(CLIP, dtype="float32")[0]
    audio = np.concatenate([audio, np.zeros(int(3 * SR), dtype=np.float32)])  # time to end the turn
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    frames = []
    for i in range(0, len(pcm) - FRAME * 2, FRAME * 2):
        frames += [InputAudioRawFrame(audio=pcm[i : i + FRAME * 2], sample_rate=SR, num_channels=1), SleepFrame(sleep=0.02)]

    down, _up = await run_test(
        Pipeline([stt, echo_filter, user_agg]),
        frames_to_send=frames,
        observers=[telemetry, bot_speech],
    )

    user_messages = [m.get("content") for m in context.messages if m.get("role") == "user"]
    turn_starts = sum(isinstance(f, UserStartedSpeakingFrame) for f in down)
    context_pushes = sum(isinstance(f, LLMContextFrame) for f in down)
    summary = telemetry.summary()

    checks = {
        "STT connected via voice.stt": bool(connects),
        "user turn started": turn_starts >= 1,
        "context pushed to LLM": context_pushes >= 1,
        "patient words in user message": any("metformin" in (m or "").lower() for m in user_messages),
        "no transcripts dropped as echo (bot silent)": not any(k.startswith("transcript_dropped") for k in summary["counts"]),
    }
    print(f"model={config.stt_model} keyterms={config.stt_keyterms} vad_stop_secs={config.vad_stop_secs}")
    print(f"user message(s) reaching the LLM: {user_messages}")
    print(f"telemetry counts: {summary['counts']}")
    print(f"STT first transcript after VAD stop: {summary['stt_first_transcript_after_vad_stop']}")
    failures = 0
    for name, ok in checks.items():
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
