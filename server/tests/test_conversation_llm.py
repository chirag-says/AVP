r"""Conversation tests against the REAL LLM (Gemini), real prompt, real intake tools.

    .venv\Scripts\python.exe server\tests\test_conversation_llm.py

Text mode: the real GoogleLLMService, system prompt, IntakeSession tools and
Pipecat context aggregators; only speech (STT/TTS) and the database are
replaced (the database by an in-memory fake; test_supabase_intake.py covers the
real one). A scripted patient answers whatever the assistant actually asked,
so the test does not depend on question order. Uses a few dozen Gemini calls.
"""
import asyncio
import os
import pathlib
import re
import sys

SERVER = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402
from loguru import logger  # noqa: E402

load_dotenv(SERVER / ".env", override=True)
logger.remove()
logger.add(sys.stderr, level="ERROR")

from pipecat.frames.frames import (  # noqa: E402
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.worker import PipelineWorker  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor  # noqa: E402
from pipecat.services.google.llm import GoogleLLMService  # noqa: E402
from pipecat.workers.runner import WorkerRunner  # noqa: E402

from intake.engine import NONE, FieldStatus  # noqa: E402
from intake.prompts import build_system_prompt  # noqa: E402
from intake.reply_guard import ReplyGuard  # noqa: E402
from intake.session import GREETING, IntakeSession, Persistence, transcript_from_context  # noqa: E402
from tests.test_intake_session import FakeDB  # noqa: E402

results: list[tuple[str, bool]] = []
PATIENT_PAUSE_S = 4.0


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n        {detail}" if detail and not ok else ""))


class Collector(FrameProcessor):
    """End of the pipeline: gathers the assistant's words for each reply."""

    def __init__(self):
        super().__init__()
        self.text = ""
        self.replies: list[str] = []
        self.done = asyncio.Event()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            self.text += frame.text
        elif isinstance(frame, TTSSpeakFrame):  # spoken without the LLM (e.g. the empty-reply fallback)
            self.text += frame.text
            self.done.set()
        elif isinstance(frame, LLMFullResponseEndFrame):
            self.done.set()
        await self.push_frame(frame, direction)


class Conversation:
    def __init__(self):
        self.sent: list[dict] = []

        async def send(msg):
            self.sent.append(msg)

        self.db = FakeDB()
        self.context = LLMContext()
        self.intake = IntakeSession(send=send, persistence=Persistence(self.db.create_session, self.db.complete_session),
                                    session_id=self.db.create_session(),
                                    # The real builder over the real Gemini context (tool-call
                                    # messages included): the bug a stubbed transcript hid.
                                    get_transcript=lambda: transcript_from_context(self.context.messages))
        self.context = LLMContext(tools=self.intake.tools())
        self.context.add_message({"role": "assistant", "content": GREETING})
        self.intake.engine.mark_asked("personal.full_name")
        llm = GoogleLLMService(api_key=os.environ["GOOGLE_API_KEY"], settings=GoogleLLMService.Settings(
            model=os.getenv("GOOGLE_MODEL", "gemini-flash-lite-latest"), system_instruction=build_system_prompt()))
        _, assistant = LLMContextAggregatorPair(self.context)
        self.collector = Collector()
        # Between LLM and aggregator: the assistant aggregator consumes text frames.
        self.guard = ReplyGuard(self.context, is_open=lambda: self.intake.state == "open")  # same as bot.py
        self.worker = PipelineWorker(Pipeline([llm, self.guard, self.collector, assistant]), cancel_on_idle_timeout=False)
        self.transcript: list[tuple[str, str]] = [("assistant", GREETING)]

    async def start(self):
        self.runner = WorkerRunner(handle_sigint=False)
        await self.runner.add_workers(self.worker)
        self.task = asyncio.create_task(self.runner.run())
        await asyncio.sleep(0.5)

    async def say(self, text: str) -> str:
        """Patient says `text`; returns the assistant's full reply (after any tool calls)."""
        # A patient takes a few seconds to answer. Without this pause the test
        # fires ~30 LLM calls a minute and measures Gemini's free-tier 429
        # quota instead of the conversation.
        await asyncio.sleep(PATIENT_PAUSE_S)
        self.transcript.append(("patient", text))
        self.context.add_message({"role": "user", "content": text})
        self.collector.text, reply = "", ""
        await self.worker.queue_frames([LLMContextFrame(self.context)])
        # One reply may span several LLM runs (tool call -> result -> more text).
        for _ in range(6):
            self.collector.done.clear()
            try:
                await asyncio.wait_for(self.collector.done.wait(), timeout=40)
            except TimeoutError:
                break
            await asyncio.sleep(1.2)  # a follow-up run after a tool result starts quickly
            if not self.collector.done.is_set() and self.collector.text.strip():
                continue
            reply = self.collector.text.strip()
            if self.intake.state in ("saved", "save_failed") or (reply and not self._tool_pending()):
                break
        self.transcript.append(("assistant", reply))
        return reply

    def _tool_pending(self) -> bool:
        last = self.context.messages[-1] if self.context.messages else {}
        return last.get("role") == "tool" or bool(last.get("tool_calls"))

    async def stop(self):
        await self.worker.cancel()
        try:
            await asyncio.wait_for(self.task, 10)
        except Exception:
            pass

    def status(self, key):
        return self.intake.engine.status[key]


def patient_answer(reply: str, conv: Conversation, script: dict) -> str:
    """A cooperative patient answering what the assistant actually asked."""
    r = reply.lower()
    engine = conv.intake.engine
    for key, answer in script.get("confirmations", {}).items():
        if engine.status[key] == FieldStatus.NEEDS_CONFIRMATION:
            return answer.pop(0) if isinstance(answer, list) and len(answer) > 1 else (answer[0] if isinstance(answer, list) else answer)
    if re.search(r"\b(correct|right|confirm|is that|did you mean|accurate)\b", r) and "?" in r:
        return "Yes, that's correct."
    topics = [
        ("allerg", "allergies"), ("medication|medicine|tablet", "medications"),
        ("condition|illness|disease|health problem", "conditions"), ("how long|since when|duration", "duration"),
        ("symptom|brings you|problem|feeling|complaint|what brings", "symptoms"), ("address|where do you live|live", "address"),
        ("phone|mobile|number|contact", "phone"), ("gender", "gender"), ("age|old", "age"), ("name", "name"),
    ]
    for pattern, topic in topics:
        if re.search(pattern, r):
            return script[topic]
    return script.get("fallback", "Okay.")


async def run(script: dict, max_turns=24, stop_when=None) -> Conversation:
    conv = Conversation()
    await conv.start()
    reply = GREETING
    try:
        for _ in range(max_turns):
            text = script["first"] if len(conv.transcript) == 1 else patient_answer(reply, conv, script)
            if stop_when and stop_when(conv):
                break
            reply = await conv.say(text)
            if conv.intake.state in ("saved", "save_failed"):
                break
    finally:
        await conv.stop()
    return conv


BASE = {
    "first": "My name is Chirag Sharma.",
    "name": "Chirag Sharma.",
    "age": "I am twenty two years old.",
    "gender": "Male.",
    "phone": "My number is 9876543210.",
    "address": "12 MG Road, Jayanagar, Bangalore.",
    "symptoms": "I have a little cold and I'm not feeling well.",
    "duration": "It's been five or six years.",
    "allergies": "No allergies.",
    "conditions": "No.",
    "medications": "One.",
    "fallback": "Yes.",
    "confirmations": {"medical_history.current_medications": ["None.", "No, I'm not taking any medication."]},
}


def show(conv):
    return "\n".join(f"        {who}: {text}" for who, text in conv.transcript)


async def main() -> int:
    # --- Conversation 1: full intake, "no allergies", "one" then "none" meds, save ---------
    conv = await run(BASE)
    replies = [t for who, t in conv.transcript if who == "assistant"]
    after_allergies = []
    seen = False
    for who, text in conv.transcript:
        if who == "patient" and text == BASE["allergies"]:
            seen = True
        elif seen and who == "assistant":
            after_allergies.append(text.lower())
    e = conv.intake.engine
    check("T2 LLM: 'No allergies' -> allergies = none", e.get("medical_history.allergies") == NONE, show(conv))
    check("T2 LLM: never asks about 'other allergies' / 'besides none' afterwards",
          not any(re.search(r"other allerg|besides none|any more allerg|additional allerg", t) for t in after_allergies),
          show(conv))
    check("T4 LLM: medications ended as none after 'One.' was clarified",
          e.get("medical_history.current_medications") == NONE, show(conv))
    asked_med_confirm = any(re.search(r"(one medication|just to confirm|taking any|which one)", t.lower())
                            for t in replies)
    check("T4 LLM: the assistant asked to clarify instead of choosing", asked_med_confirm, show(conv))
    sym = e.get("visit.symptoms") or []
    check("T9 LLM: symptoms stored structured (no fragment 'five or six years' symptom)",
          sym and all(isinstance(s, dict) for s in sym)
          and not any("year" in s["description"].lower() for s in sym), str(sym))
    check("T1 LLM: conversation reaches a saved, completed intake",
          conv.intake.state == "saved" and any(m["type"] == "intake_complete" for m in conv.sent), show(conv))
    check("T1 LLM: completion announced only after the save (event order)",
          [m["type"] for m in conv.sent if m["type"].startswith("intake_")][-3:]
          == ["intake_finalizing", "intake_saving", "intake_complete"])
    no_premature = not any(re.search(r"intake is complete|been saved|successfully (saved|recorded)", t.lower())
                           for t in replies[:-1])
    check("T1 LLM: the assistant did not claim completion before the save", no_premature, show(conv))
    silent = [i for i, t in enumerate(replies[1:-1], 1) if not t.strip()]  # last one: finalize, spoken by TTS
    check("LLM: the assistant never goes silent mid-intake", not silent,
          f"silent replies at {silent}; guard retries={conv.guard.retries} fallbacks={conv.guard.fallbacks}")
    print(f"  empty-reply guard: retries={conv.guard.retries} fallbacks={conv.guard.fallbacks}")
    print("\n  transcript (conversation 1):\n" + show(conv) + "\n")

    # --- Conversation 2: unusual allergy -> clarification ---------------------------------
    script = dict(BASE, allergies="Actually I have allergy to Artificial Intelligence.", medications="No.",
                  confirmations={"medical_history.allergies": "No, I was just joking. I don't have any allergies."})
    stop_after_allergy = lambda c: c.intake.engine.status["medical_history.allergies"] in (  # noqa: E731
        FieldStatus.ANSWERED, FieldStatus.CONFIRMED) and c.intake.engine.get("medical_history.allergies") == NONE
    conv = await run(script, max_turns=20, stop_when=stop_after_allergy)
    ai_turn = next((i for i, (w, t) in enumerate(conv.transcript) if w == "patient" and "Artificial" in t), None)
    reply_to_ai = conv.transcript[ai_turn + 1][1].lower() if ai_turn is not None and ai_turn + 1 < len(conv.transcript) else ""
    check("T3 LLM: unusual allergy is not recorded as fact",
          "artificial" not in str(conv.intake.engine.get("medical_history.allergies") or "").lower(), show(conv))
    check("T3 LLM: the assistant asks the patient to clarify it",
          bool(re.search(r"confirm|did you mean|actual|seriously|really|to be sure|clarify", reply_to_ai)), show(conv))
    check("T3 LLM: after 'just joking', allergies = none",
          conv.intake.engine.get("medical_history.allergies") == NONE, show(conv))
    print("\n  transcript (conversation 2):\n" + show(conv) + "\n")

    failed = [n for n, ok in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    return len(failed)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
