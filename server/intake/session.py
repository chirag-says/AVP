"""One patient's intake: the LLM's tools, finalization, persistence, completion.

Everything patient-specific lives in one IntakeSession, created per WebRTC
connection. The next patient gets a new connection, so a new IntakeSession,
IntakeEngine, LLM context and database row: nothing carries over.

Finalization order (nothing is announced before the database confirms):

    finalize tool -> engine readiness check -> freeze record
      -> "intake_saving" -> Supabase write, verified -> state "saved"
      -> "intake_complete" -> closing sentence spoken -> session ends

If the write fails the patient hears "There's a problem saving your
information. Please wait a moment.", the UI gets "intake_save_failed", and the
frozen record stays ready for a retry (the Retry button, or finalize again).
Finalize is idempotent: a second call after a successful save changes nothing.
"""
import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger
from pipecat.frames.frames import EndWorkerFrame, TTSSpeakFrame
from pipecat.frames.frames import FunctionCallResultProperties
from pipecat.services.llm_service import FunctionCallParams

from .engine import FieldStatus, IntakeEngine
from .language_follow import LanguageState
from .languages import ENGLISH

# The English lines; each language's own are in languages.py.
GREETING = ENGLISH.greeting
CLOSING = ENGLISH.closing
SAVE_PROBLEM = ENGLISH.save_problem

Send = Callable[[dict], Awaitable[None]]
_NO_REPLY = FunctionCallResultProperties(run_llm=False)


def transcript_from_context(messages) -> list[dict]:
    """Patient/assistant text turns from an LLM context, for the saved record.

    The context also holds provider-specific objects (Pipecat's
    LLMSpecificMessage for Gemini tool calls), which are not dicts; skip
    anything that isn't a plain text turn instead of crashing the save.
    """
    turns = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            turns.append({"role": m["role"], "text": m["content"]})
    return turns


class Persistence:
    """The two database calls a session needs (injectable for tests)."""

    def __init__(self, create_session, complete_session):
        self.create_session = create_session
        self.complete_session = complete_session


class IntakeSession:
    def __init__(self, *, send: Send, persistence: Persistence | None, session_id: str | None,
                 get_transcript: Callable[[], list[dict]], language: LanguageState | None = None):
        self.language = language or LanguageState(ENGLISH)
        self.engine = IntakeEngine()
        self.engine.language = self.language
        self.session_id = session_id
        self.state = "open"  # open | saving | saved | save_failed
        self._send = send
        self._db = persistence
        self._get_transcript = get_transcript
        self._lock = asyncio.Lock()
        self._record: dict | None = None
        self._llm = None  # the LLM processor, to speak the closing lines

    # --- tools (Pipecat direct functions; their docstrings are what the LLM sees) ---

    def tools(self) -> list:
        session = self

        async def save_field(params: FunctionCallParams, key: str, value: str):
            """Record a detail the patient stated clearly.

            Use for ordinary, unambiguous answers. If the answer is unusual,
            sounds like a joke, is garbled, or you are unsure you heard it
            right, use flag_for_confirmation instead. "No", "none", "nothing",
            "I don't take any" for allergies, conditions or medications are
            complete answers: save them as given and move on.

            Args:
                key: The field's dot-path, e.g. "personal.full_name" or "medical_history.allergies".
                value: The value exactly as the patient stated it (never corrected or guessed).
                    For symptoms pass a JSON array of objects like
                    '[{"description": "cold", "duration": "5 years", "severity": null}]';
                    a later answer about one symptom merges into it by description.
                    For existing conditions or current medications pass a JSON array of names.
            """
            await session._field_tool(params, session.engine.save_field(key, value))

        async def confirm_field(params: FunctionCallParams, key: str, value: str):
            """Record an answer the patient has just explicitly confirmed or clarified.

            Use after asking the patient to confirm a flagged, ambiguous or
            contradictory answer, or after reading back a phone number or address.

            Args:
                key: The field's dot-path.
                value: The final value the patient confirmed, as they stated it.
            """
            await session._field_tool(params, session.engine.confirm_field(key, value))

        async def flag_for_confirmation(params: FunctionCallParams, key: str, value: str, reason: str):
            """Hold an answer that needs the patient's confirmation before it is recorded.

            Use when an answer is unusual, sounds casual or joking, contradicts
            something earlier, or is a medical word you may have misheard. Do
            not judge whether it is medically real; just ask the patient.

            Args:
                key: The field's dot-path.
                value: What the patient said, unchanged.
                reason: Short reason, e.g. "unusual allergy" or "unclear medication name".
            """
            await session._field_tool(params, session.engine.flag_for_confirmation(key, value, reason))

        async def skip_field(params: FunctionCallParams, key: str):
            """Mark an OPTIONAL field the patient does not want to or cannot answer.

            Not for "none": a patient saying they have no conditions or take no
            medications is an answer; save it with save_field.

            Args:
                key: The optional field's dot-path.
            """
            await session._field_tool(params, session.engine.skip_field(key))

        async def finalize(params: FunctionCallParams):
            """Save the completed intake. Call once, after your brief summary.

            Only the system announces that the intake is complete and saved;
            do not say so yourself.
            """
            await session.finalize(params)

        return [save_field, confirm_field, flag_for_confirmation, skip_field, finalize]

    # --- finalize / persist ----------------------------------------------------

    async def finalize(self, params: FunctionCallParams):
        async with self._lock:  # a doubled finalize call waits, then sees "saved"
            if self.state == "saved":
                await params.result_callback({"ok": True, "already_saved": True}, properties=_NO_REPLY)
                return
            if self.state == "open":
                check = self.engine.finalize()
                if not check.get("ok"):
                    await params.result_callback(check)  # the LLM keeps collecting
                    return
                self.engine.completed = True
                self._record = self.engine.freeze()
            self._llm = params.llm
            await self._send({"type": "intake_finalizing"})
            saved = await self._persist()
            if saved:
                await params.result_callback({"ok": True, "saved": True}, properties=_NO_REPLY)
                await self._announce_complete()
            else:
                await params.result_callback(
                    {"ok": False, "error": "The record could not be saved. The system has told the patient to "
                                           "wait. Do not say the intake is complete."},
                    properties=_NO_REPLY)
                await self._announce_save_problem()

    async def retry_save(self):
        """Retry after a failed save (the UI's Retry button)."""
        async with self._lock:
            if self.state != "save_failed":
                return
            if await self._persist():
                await self._announce_complete()
            else:
                await self._send({"type": "intake_save_failed", "message": SAVE_PROBLEM})

    async def _persist(self) -> bool:
        self.state = "saving"
        await self._send({"type": "intake_saving"})
        try:
            if self._db is None:
                raise RuntimeError("Supabase is not configured")
            if self.session_id is None:  # the row couldn't be created when the call started
                self.session_id = await asyncio.to_thread(self._db.create_session)
            try:
                transcript = self._get_transcript()
            except Exception as e:
                # The patient's record matters more than the audit transcript:
                # never let a transcript problem block saving the intake.
                logger.warning(f"Transcript unavailable for intake {self.session_id}: {type(e).__name__}")
                transcript = []
            outcome = await asyncio.to_thread(self._db.complete_session, self.session_id, self._record, transcript)
            logger.info(f"Intake {self.session_id} {outcome}")
            self.state = "saved"
            return True
        except Exception as e:
            # Log the failure type only: the record itself is patient data.
            logger.error(f"Saving intake {self.session_id} failed: {type(e).__name__}: {e}")
            self.state = "save_failed"
            return False

    async def _announce_complete(self):
        name = self.first_name()
        await self._send({"type": "intake_complete", "session_id": self.session_id, "patient_name": name})
        if self._llm is not None:
            await self._llm.push_frame(TTSSpeakFrame(self.language.current.closing.format(name=f", {name}" if name else "")))
            # Downstream after the closing line, so it is spoken before the session ends.
            await self._llm.push_frame(EndWorkerFrame())

    async def _announce_save_problem(self):
        await self._send({"type": "intake_save_failed", "message": SAVE_PROBLEM})
        if self._llm is not None:
            await self._llm.push_frame(TTSSpeakFrame(self.language.current.save_problem))

    def first_name(self) -> str | None:
        name = self.engine.get("personal.full_name")
        if not isinstance(name, str) or name.lower() == "declined":
            return None
        return name.split()[0].strip(" ,.").title() if name.split() else None

    # --- shared tool plumbing ------------------------------------------------------

    async def _field_tool(self, params: FunctionCallParams, result: dict):
        key = result.get("saved") or result.get("field")
        if key and result.get("ok"):
            await self._send({"type": "field_update", "key": key, "value": self.engine.get(key),
                              "status": str(self.engine.status[key])})
        elif key and result.get("status") == str(FieldStatus.NEEDS_CONFIRMATION):
            # The unconfirmed value stays server-side; the panel only shows the field is being confirmed.
            await self._send({"type": "field_update", "key": key, "value": self.engine.get(key),
                              "status": str(FieldStatus.NEEDS_CONFIRMATION)})
        await params.result_callback(result)
