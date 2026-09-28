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
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.evals.transport import EvalTransportParams
from pipecat.frames.frames import TTSSpeakFrame
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
from pipecat.services.sarvam.llm import SarvamLLMService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy, VADUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from intake.prompts import build_system_prompt
from intake.reply_guard import ReplyGuard
from intake.session import GREETING, IntakeSession, Persistence, transcript_from_context
from services.db import complete_session, create_session, list_sessions, save_session
from voice.config import VoiceConfig
from voice.echo_guard import BotSpeechTracker, EchoAwareVADUserTurnStartStrategy, EchoTranscriptFilter
from voice.recovery import NoTranscriptRecovery
from voice.stt import build_sarvam_stt
from voice.telemetry import VoiceTelemetry, format_mic_table

load_dotenv(pathlib.Path(__file__).resolve().parent / ".env", override=True)
VOICE = VoiceConfig.from_env()

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
        # Model, language, keyterms and Sarvam's server VAD profile come from
        # VoiceConfig; see server/voice/stt.py for why each default was chosen.
        return build_sarvam_stt(_sarvam_key(), VOICE)
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
                model=os.getenv("SARVAM_TTS_MODEL", "bulbul:v3"),
                voice=os.getenv("SARVAM_VOICE_ID", "priya"),
                language=Language.EN_IN,
                pace=1.3,  # slightly faster speech (v3 range: 0.5–2.0)
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

    # Everything patient-specific lives in this session and dies with this
    # connection: the next patient gets a new connection, session, engine,
    # LLM context and database row.
    session_id = None
    if HAVE_SUPABASE:
        try:
            session_id = create_session()
        except Exception as e:
            # The session retries creating the row at finalize; completion is
            # never announced unless the record is actually saved.
            logger.warning(f"Supabase unavailable at call start: {type(e).__name__}")
    if session_id:
        logger.info(f"Intake session {session_id}")

    logger.info(f"Stack: stt={STT_PROVIDER} llm={LLM_PROVIDER} tts={TTS_PROVIDER}")
    logger.info(f"Voice: echo_guard={VOICE.echo_guard} headset_mode={VOICE.headset_mode} "
                f"no_transcript_recovery={VOICE.no_transcript_recovery} keyterms={VOICE.stt_keyterms}")
    stt = _build_stt()
    tts = _build_tts()
    llm = _build_llm()

    async def send_to_client(message: dict):
        await worker.rtvi.send_server_message(message)

    def transcript() -> list[dict]:
        return transcript_from_context(context.messages)

    intake = IntakeSession(
        send=send_to_client,
        persistence=Persistence(create_session, complete_session) if HAVE_SUPABASE else None,
        session_id=session_id,
        get_transcript=transcript,
    )

    telemetry = VoiceTelemetry(session_label=intake.session_id, scenario=VOICE.debug_scenario) if VOICE.telemetry else None
    bot_speech = BotSpeechTracker() if VOICE.echo_guard else None

    # Turn-taking, stated explicitly (these were Pipecat's implicit defaults,
    # except the echo-aware VAD start):
    #   start: VAD, or a transcript Silero missed (soft speech fallback)
    #   stop:  Smart Turn v3, a local audio model, decides whether a pause ends
    #          the turn. VAD alone does NOT end turns; if Smart Turn judges the
    #          patient mid-sentence it keeps waiting (up to 3 s of silence).
    vad_start = (
        EchoAwareVADUserTurnStartStrategy(telemetry=telemetry)
        if VOICE.echo_guard
        else VADUserTurnStartStrategy()
    )
    turn_strategies = UserTurnStrategies(
        start=[vad_start, TranscriptionUserTurnStartStrategy()],
        stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())],
    )

    context = LLMContext(tools=intake.tools())
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=turn_strategies,
            # Silero decides *whether someone is speaking*. It is not noise
            # suppression: audio it calls speech can still carry fan noise or
            # a bystander, and it never alters what Sarvam receives.
            #
            # stop_secs (VAD_STOP_SECS, default 0.2) is when Smart Turn is
            # asked "is the patient done?". It used to be 0.8 to keep Whisper
            # from cutting dictated numbers, but with Smart Turn in the loop
            # 0.8 s of trailing silence biases the model toward "complete"
            # (the same "My date of birth is..." pause scored p=0.02 at 0.2 s
            # and p=0.94 at 0.8 s). Measured in server/voice_eval: 0.2 s ends
            # fewer turns early (3 vs 5 of 12), with the same ~1.1 s median
            # end-of-turn delay and the same Sarvam accuracy on paused answers
            # (the extra flushes split segments, which the aggregator rejoins).
            # The other values are Pipecat's defaults, written out for clarity.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(
                confidence=0.7,
                start_secs=0.2,
                stop_secs=VOICE.vad_stop_secs,
                min_volume=0.6,
            )),
        ),
    )

    processors = [transport.input(), stt]
    if VOICE.echo_guard or VOICE.headset_mode:
        # Between STT and the aggregator: drops transcripts of the bot's own
        # voice (echo guard) and, on headset kiosks only, any transcript local
        # VAD did not hear, before they can start a user turn or reach the LLM.
        processors.append(EchoTranscriptFilter(
            bot_speech, telemetry=telemetry, echo_rules=VOICE.echo_guard, require_vad=VOICE.headset_mode))
    if VOICE.no_transcript_recovery:
        # After the filter, so a dropped transcript does not count as patient text.
        processors.append(NoTranscriptRecovery(telemetry=telemetry))
    # ReplyGuard: if the patient is owed a reply and everything goes quiet (empty LLM
    # response, or a noise turn swallowed the follow-up), re-runs the LLM, then asks them to repeat.
    reply_guard = ReplyGuard(context, is_open=lambda: intake.state == "open")
    processors += [user_aggregator, llm, reply_guard, tts, transport.output(), assistant_aggregator]
    pipeline = Pipeline(processors)

    if telemetry:
        telemetry.watch(
            input_transport=transport.input(),
            stt=stt,
            user_aggregator=user_aggregator,
            output_transport=transport.output(),
        )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=[o for o in (telemetry, bot_speech) if o],
    )

    @worker.rtvi.event_handler("on_client_message")
    async def on_client_message(rtvi, msg):
        data = msg.data if isinstance(msg.data, dict) else {}
        if msg.type == "retry_save":
            await intake.retry_save()
        elif telemetry:
            if msg.type == "scenario_marker":  # ignored unless VOICE_DEBUG_SCENARIO is set
                telemetry.note_marker(data.get("role"), data.get("down"))
            elif msg.type == "scenario_name":
                telemetry.note_scenario_name(data.get("name"))
            elif msg.type == "mic_settings":
                mic = telemetry.note_client_mic(msg.data)
                if mic:
                    logger.info("Client mic, live track settings:\n" + format_mic_table(mic))

    if telemetry and STT_PROVIDER == "sarvam":
        @stt.event_handler("on_connected")
        async def on_stt_connected(service):
            telemetry.note_stt_connected()

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        # A fixed greeting (added to the LLM context as the assistant's first
        # turn), so every patient starts the same way.
        intake.engine.mark_asked("personal.full_name")
        await worker.queue_frames([TTSSpeakFrame(GREETING)])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        if intake.session_id and intake.state not in ("saved", "saving"):
            try:
                save_session(intake.session_id, intake.engine.to_record(), transcript(), status="abandoned")
            except Exception as e:
                logger.warning(f"Failed to save abandoned session: {type(e).__name__}")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    try:
        await runner.run()
    finally:
        if telemetry:
            telemetry.log_summary()


def _audio_in_filter():
    """Optional server-side denoiser, applied before VAD, Smart Turn and STT.

    Off by default, on evidence: browser noise suppression already runs on the
    mic, and in the server/voice_eval A/B RNNoise in front of Sarvam made
    recognition WORSE (fan WER 0.014 -> 0.062, quiet-bystander 0.26 -> 0.40):
    it strips noise but also speech detail Sarvam uses, and it cannot remove a
    bystander's voice, which is speech too. ~2.4 ms CPU per 20 ms frame.
    SERVER_DENOISER=rnnoise needs `pip install pyrnnoise` (not in
    requirements.txt); without it Pipecat logs an error and passes audio
    through unchanged.
    """
    if VOICE.server_denoiser == "rnnoise":
        from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter

        return RNNoiseFilter()
    return None


def _apply_log_level():
    """Keep patient data out of the server log by default.

    Pipecat's dev runner logs at DEBUG, where its own services print raw
    Sarvam transcripts, the full LLM context and save_field arguments: names,
    phone numbers, medications. INFO has none of that. LOG_LEVEL=DEBUG brings
    the detail back for local debugging. Re-applied per session because the
    runner installs its DEBUG handler after this module is imported.
    """
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    if level not in ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"):
        level = "INFO"
    logger.remove()
    logger.add(sys.stderr, level=level)


async def bot(runner_args: RunnerArguments):
    """Main bot entry point — discovered and run per session by the dev runner."""
    _apply_log_level()
    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_filter=_audio_in_filter(),
        ),
        # Headless boot for `pipecat eval` and server/voice_eval/boot_check.py.
        "eval": lambda: EvalTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_filter=_audio_in_filter(),
        ),
    }

    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
