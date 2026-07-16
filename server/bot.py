"""Hospital intake voice bot — Pipecat cascade pipeline.

Scaffolded via `pipecat init` (see server/PIPECAT_REFERENCE.md for the
framework's own guidance) and adapted in place per its golden rule: modify
the generated pipeline, don't rebuild it. Swaps the scaffold's generic
assistant prompt for the intake questionnaire and wires save_field/finalize
as tools.

Run: .venv\\Scripts\\python.exe server\\bot.py -t webrtc
"""
import os
import pathlib
import sys

# Windows terminals default to a legacy codepage (cp1252) that can't encode
# the emoji Pipecat's runner prints on startup — crashes before the server
# even binds. Force UTF-8 on stdout/stderr rather than relying on the
# PYTHONUTF8 env var being set in whatever shell this gets run from.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndWorkerFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.run import app as runner_app
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.sarvam.llm import SarvamLLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

from intake.engine import IntakeEngine
from intake.prompts import build_system_prompt
from services.db import create_session, list_sessions, save_session

load_dotenv(pathlib.Path(__file__).resolve().parent / ".env", override=True)

MODELS_DIR = pathlib.Path(__file__).resolve().parent / "models"
HAVE_SUPABASE = bool(os.getenv("SUPABASE_URL"))

# Each leg of the cascade picks its own provider.
#
# Speech runs on Sarvam (Indic-tuned, streaming). The LLM does NOT, and that is
# a measured decision, not a preference: across a 5-turn intake with full
# conversation history, sarvam-30b emitted ZERO save_field tool calls and
# produced an empty record, while gemini-flash-lite on the identical schema and
# turns saved 4/5 with correct keys. sarvam-30b can call tools single-shot but
# stops once history accumulates. That failure is silent — no crash, just blank
# patient records, which is exactly what IntakeEngine exists to prevent.
# Re-run server/smoke/test_sarvam_tools.py before ever flipping this to sarvam.
STT_PROVIDER = os.getenv("STT_PROVIDER", "sarvam").lower()
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "sarvam").lower()
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "google").lower()


def _sarvam_key() -> str:
    key = os.getenv("SARVAM_API_KEY")
    if not key:
        raise RuntimeError(
            "SARVAM_API_KEY is not set. Create a key at https://dashboard.sarvam.ai "
            "and add it to server/.env. To run the previous stack instead, set "
            "STT_PROVIDER=whisper TTS_PROVIDER=kokoro LLM_PROVIDER=google."
        )
    return key


def _build_stt():
    if STT_PROVIDER == "sarvam":
        return SarvamSTTService(
            api_key=_sarvam_key(),
            settings=SarvamSTTService.Settings(
                # saaras:v3 is the only model exposing per-connection VAD tuning,
                # and unlike saaras:v2.5 it transcribes rather than translating.
                model=os.getenv("SARVAM_STT_MODEL", "saaras:v3"),
                language=Language.EN_IN,
            ),
        )
    # See the WHISPER_DEVICE note below: "auto" detects CUDA and then crashes on
    # Windows without the full Toolkit, so CPU stays the default here too.
    return WhisperSTTService(
        device=os.getenv("WHISPER_DEVICE", "cpu"),
        settings=WhisperSTTService.Settings(model="small"),
        compute_type="int8",
    )


def _build_tts():
    if TTS_PROVIDER == "sarvam":
        # The WebSocket service (not SarvamHttpTTSService) — it's an
        # InterruptibleTTSService, which is what keeps barge-in working.
        return SarvamTTSService(
            api_key=_sarvam_key(),
            settings=SarvamTTSService.Settings(
                model=os.getenv("SARVAM_TTS_MODEL", "bulbul:v2"),
                voice=os.getenv("SARVAM_VOICE_ID", "anushka"),
                language=Language.EN_IN,
            ),
        )
    return KokoroTTSService(
        model_path=str(MODELS_DIR / "kokoro-v1.0.onnx"),
        voices_path=str(MODELS_DIR / "voices-v1.0.bin"),
        settings=KokoroTTSService.Settings(voice=os.getenv("KOKORO_VOICE_ID", "af_heart")),
    )


def _build_llm():
    if LLM_PROVIDER == "sarvam":
        return SarvamLLMService(
            api_key=_sarvam_key(),
            settings=SarvamLLMService.Settings(
                model=os.getenv("SARVAM_MODEL", "sarvam-30b"),
                system_instruction=build_system_prompt(),
            ),
        )
    return GoogleLLMService(
        api_key=os.environ["GOOGLE_API_KEY"],
        settings=GoogleLLMService.Settings(
            model=os.getenv("GOOGLE_MODEL", "gemini-flash-lite-latest"),
            system_instruction=build_system_prompt(),
        ),
    )


@runner_app.get("/api/records")
async def get_records():
    """Receptionist view: recent intake sessions, most recent first."""
    return list_sessions()


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    logger.info("Starting hospital intake bot")

    engine = IntakeEngine()
    session_id = create_session() if HAVE_SUPABASE else None
    if session_id:
        logger.info(f"Intake session {session_id}")

    logger.info(f"Stack: stt={STT_PROVIDER} llm={LLM_PROVIDER} tts={TTS_PROVIDER}")
    stt = _build_stt()
    tts = _build_tts()
    llm = _build_llm()

    async def save_field_tool(params: FunctionCallParams, key: str, value: str):
        """Record one piece of patient information.

        Args:
            key: The field's dot-path, e.g. "personal.full_name" or "personal.phone".
                Must be one of the known intake fields.
            value: The value to store, as the patient stated it. For list fields
                (symptoms, existing conditions, current medications), pass a JSON
                array as a string, e.g. '["fever", "headache"]'.
        """
        result = engine.save_field(key, value)
        if result.get("ok"):
            canonical_key = result["saved"]
            await worker.rtvi.send_server_message(
                {"type": "field_update", "key": canonical_key, "value": engine.get(canonical_key)}
            )
        await params.result_callback(result)

    async def finalize_tool(params: FunctionCallParams):
        """Attempt to close out the intake session.

        Call this only once every required field has been collected and
        confirmed with the patient.
        """
        result = engine.finalize()
        await params.result_callback(result)
        if not result.get("ok"):
            return

        record = engine.to_record()
        transcript = [
            {"role": m.get("role"), "text": m.get("content")}
            for m in context.messages
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
        ]
        if session_id:
            save_session(session_id, record, transcript, status="completed")
        await worker.rtvi.send_server_message({"type": "intake_complete"})
        await params.llm.push_frame(EndWorkerFrame())

    context = LLMContext(tools=[save_field_tool, finalize_tool])
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # Pipecat's 0.2s stop_secs default assumes a smart-turn analyzer is
            # deciding end-of-turn; with VAD alone it ends the turn on any
            # ordinary pause. Intake is full of them — nobody reads out a
            # 10-digit mobile number without a breath ("98765... 43210"), and
            # at 0.2s whisper only ever sees half the digits. 0.8s spans a
            # mid-number pause while still feeling responsive.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.8)),
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=[],
    )

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        context.add_message(
            {
                "role": "developer",
                "content": (
                    "Start the conversation now: warmly greet the patient and "
                    "ask for their full name."
                ),
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        if session_id and not engine.completed:
            save_session(session_id, engine.to_record(), [], status="abandoned")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point — discovered and run per session by the dev runner."""
    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    }

    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
