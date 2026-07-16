"""Consultation Scribe — a listen-only voice pipeline plus a summary API.

A separate process from bot.py (the reception chatbot) by design: it runs on
its own port, never shares a runner, and this file never imports or edits
bot.py. The two modules only meet at the Supabase client and the Sarvam key.

Run (alongside the chatbot, which stays on 7860):

    .venv\\Scripts\\python.exe server\\scribe_bot.py -t webrtc --port 7861

The pipeline only transcribes: transport.input() -> Sarvam STT -> transport
output. No LLM, no TTS — the AI never speaks, it listens to the doctor and
patient in the room. PipelineWorker auto-adds RTVI and forwards user
transcription frames to the browser by default, so the transcript builds live
via onUserTranscript, exactly as the chatbot's frontend already consumes it.

The clinical summary is produced once, on demand, when the browser POSTs the
collected transcript to /api/consultation/summarize at the end of the visit.
"""
import os
import pathlib
import sys

# Same Windows console fix as bot.py: force UTF-8 so the runner's emoji banner
# can't crash startup on the default cp1252 codepage.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.runner.run import app as runner_app
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner
from pydantic import BaseModel

from scribe.summarizer import SUMMARY_MODEL, summarize
from services.consult_db import list_notes, save_note

load_dotenv(pathlib.Path(__file__).resolve().parent / ".env", override=True)

HAVE_SUPABASE = bool(os.getenv("SUPABASE_URL"))

# STT provider mirrors bot.py's default (Sarvam, Indic-tuned streaming). Unlike
# intake there is no LLM leg, so none of bot.py's tool-calling caveats apply
# here — the scribe only needs faithful transcription.
STT_PROVIDER = os.getenv("STT_PROVIDER", "sarvam").lower()
MODELS_DIR = pathlib.Path(__file__).resolve().parent / "models"


def _sarvam_key() -> str:
    key = os.getenv("SARVAM_API_KEY")
    if not key:
        raise RuntimeError(
            "SARVAM_API_KEY is not set. Create a key at https://dashboard.sarvam.ai "
            "and add it to server/.env, or set STT_PROVIDER=whisper to run the scribe "
            "on local Whisper instead."
        )
    return key


def _build_stt():
    if STT_PROVIDER == "sarvam":
        return SarvamSTTService(
            api_key=_sarvam_key(),
            settings=SarvamSTTService.Settings(
                model=os.getenv("SARVAM_STT_MODEL", "saaras:v3"),
                language=Language.EN_IN,
            ),
        )
    return WhisperSTTService(
        device=os.getenv("WHISPER_DEVICE", "cpu"),
        settings=WhisperSTTService.Settings(model="small"),
        compute_type="int8",
    )


# --- Summary API -----------------------------------------------------------
# Registered on the runner's own FastAPI app, per process. bot.py registers
# /api/records on its port the same way; these live only on the scribe's port.


class Segment(BaseModel):
    text: str
    ts: float | None = None  # seconds from session start; optional, for display


class SummarizeRequest(BaseModel):
    transcript: list[Segment]
    duration_s: int | None = None


@runner_app.post("/api/consultation/summarize")
async def summarize_consultation(req: SummarizeRequest):
    """Summarise a finished consultation and (if configured) persist the note."""
    transcript = [seg.model_dump() for seg in req.transcript]

    try:
        # genai's client is synchronous; keep it off the event loop.
        summary = await run_in_threadpool(summarize, transcript)
    except ValueError as exc:
        # Empty/blank transcript — a client error, not a server fault.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Consultation summary failed")
        raise HTTPException(status_code=502, detail="Could not generate the summary.")

    note_id = None
    if HAVE_SUPABASE:
        try:
            note_id = await run_in_threadpool(
                save_note, transcript, summary, req.duration_s, SUMMARY_MODEL
            )
        except Exception:
            # The clinician still gets their note on screen; only the archive
            # copy failed, and that shouldn't 500 the request.
            logger.exception("Saving consultation note failed")

    return {"id": note_id, "summary": summary, "model": SUMMARY_MODEL}


@runner_app.get("/api/consultations")
async def get_consultations():
    """Recent consultation notes, most recent first."""
    if not HAVE_SUPABASE:
        return []
    try:
        return await run_in_threadpool(list_notes)
    except Exception as exc:
        msg = str(exc)
        # The table hasn't been created yet — the one-time setup step. Say so
        # plainly instead of a bare 500, so the UI can guide the clinician.
        if "consultation_notes" in msg or "PGRST205" in msg or "does not exist" in msg:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Past notes aren't set up yet. Run server/consult_schema.sql "
                    "once in the Supabase SQL editor to enable saved notes."
                ),
            )
        logger.exception("Listing consultation notes failed")
        raise HTTPException(status_code=500, detail="Could not load past notes.")


# --- Voice pipeline --------------------------------------------------------


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    logger.info(f"Starting consultation scribe (stt={STT_PROVIDER})")
    stt = _build_stt()

    # Only transcribe: audio in -> STT -> out. No context aggregator, LLM, or
    # TTS. PipelineWorker wraps this with RTVI, which forwards STT transcription
    # frames to the client as user-transcription messages by default.
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            transport.output(),
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[],
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Clinician connected — listening")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Clinician disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Entry point discovered and run per session by the dev runner."""
    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            # The scribe never talks back, so no outbound audio track.
            audio_out_enabled=False,
            # Natural in-room speech has long pauses (a patient describing
            # symptoms, thinking). 0.8s keeps utterances from fragmenting on
            # every breath while still segmenting turns — the same value intake
            # settled on, for the same reason.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.8)),
        ),
    }

    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
