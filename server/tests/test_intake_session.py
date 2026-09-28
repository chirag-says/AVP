r"""Intake engine + session tests with a fake database and fake LLM plumbing.

    .venv\Scripts\python.exe server\tests\test_intake_session.py

Local only: no network, no Supabase, no Gemini.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger  # noqa: E402

logger.remove()

from pipecat.frames.frames import EndWorkerFrame, TTSSpeakFrame  # noqa: E402

from intake.engine import NONE, FieldStatus, IntakeEngine  # noqa: E402
from intake.session import CLOSING, SAVE_PROBLEM, IntakeSession, Persistence  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))


class FakeLLM:
    def __init__(self):
        self.pushed = []

    async def push_frame(self, frame, direction=None):
        self.pushed.append(frame)


class FakeParams:
    def __init__(self, llm):
        self.llm = llm
        self.results = []

    async def result_callback(self, result, *, properties=None):
        self.results.append((result, properties))


class FakeDB:
    def __init__(self, fail: int = 0):
        self.rows: dict[str, dict] = {}
        self.fail = fail  # number of calls that raise before succeeding
        self.complete_calls = 0

    def create_session(self):
        sid = f"sess-{len(self.rows) + 1}"
        self.rows[sid] = {"status": "in_progress", "patient": {}}
        return sid

    def complete_session(self, sid, record, transcript):
        self.complete_calls += 1
        if self.fail:
            self.fail -= 1
            raise ConnectionError("simulated Supabase outage")
        if self.rows[sid]["status"] == "completed":
            return "already_saved"
        self.rows[sid] = {"status": "completed", "patient": record, "transcript": transcript}
        return "saved"


PATIENT_A = [
    ("personal.full_name", "Chirag Sharma"),
    ("personal.age", "22"),
    ("personal.gender", "male"),
    ("personal.phone", "9876543210"),
    ("personal.address", "12 MG Road, Bangalore"),
    ("visit.symptoms", '[{"description": "cold", "duration": "3 days", "severity": "mild"}]'),
    ("medical_history.allergies", "none"),
    ("medical_history.existing_conditions", "no"),
    ("medical_history.current_medications", "I don't take anything"),
]


def fill(engine: IntakeEngine, answers=PATIENT_A):
    for key, value in answers:
        r = engine.save_field(key, value)
        assert r.get("ok"), (key, r)


def new_session(db: FakeDB | None):
    sent = []

    async def send(msg):
        sent.append(msg)

    session = IntakeSession(send=send, persistence=Persistence(db.create_session, db.complete_session) if db else None,
                            session_id=db.create_session() if db else None,
                            get_transcript=lambda: [{"role": "assistant", "text": "(test)"}])
    return session, sent


# --- engine ------------------------------------------------------------------


def engine_tests():
    e = IntakeEngine()
    r = e.save_field("medical_history.allergies", "No allergies.")
    check("T2 engine: 'No allergies' -> allergies = none, answer complete",
          e.get("medical_history.allergies") == NONE and r["status"] == "answered")
    check("T2 engine: tool result tells the assistant never to ask about other allergies",
          "never ask about other allergies" in r.get("note", ""))
    check("T2 engine: next question moves on (not allergies again)", r.get("next") != "allergies")

    e = IntakeEngine()
    r1 = e.save_field("medical_history.current_medications", "One.")
    check("T4 engine: 'One.' is held for confirmation, not stored",
          r1["status"] == "needs_confirmation" and e.get("medical_history.current_medications") is None)
    r2 = e.save_field("medical_history.current_medications", "None.")
    check("T4 engine: 'None.' right after 'One.' is a contradiction, still not stored",
          r2["status"] == "needs_confirmation" and "Ask which is correct" in r2["instruction"])
    e.confirm_field("medical_history.current_medications", "No, none")
    check("T4 engine: after the patient confirms, medications = none (confirmed)",
          e.get("medical_history.current_medications") == NONE
          and e.status["medical_history.current_medications"] == FieldStatus.CONFIRMED)

    e = IntakeEngine()
    r = e.flag_for_confirmation("medical_history.allergies", "Artificial Intelligence", "unusual allergy")
    check("T3 engine: unusual allergy is held (not recorded) until confirmed",
          r["status"] == "needs_confirmation" and e.get("medical_history.allergies") is None)
    check("T3 engine: finalize is blocked while an answer awaits confirmation",
          "confirm first: allergies" in e.finalize().get("error", ""))
    # The LLM interprets "I was just joking" and records the real answer the patient gives.
    e.confirm_field("medical_history.allergies", "none")
    check("T3 engine: clarified joke -> allergies = none", e.get("medical_history.allergies") == NONE)
    e2 = IntakeEngine()
    e2.flag_for_confirmation("medical_history.allergies", "Artificial Intelligence", "unusual")
    e2.confirm_field("medical_history.allergies", "Artificial Intelligence")
    check("T3 engine: a confirmed unusual answer is preserved as stated",
          e2.get("medical_history.allergies") == "Artificial Intelligence")

    e = IntakeEngine()
    fill(e, [a for a in PATIENT_A if "existing_conditions" not in a[0] and "current_medications" not in a[0]])
    check("T8 engine: finalize asks about unanswered optional fields first",
          "ask (patient may say none or skip)" in e.finalize().get("error", ""))
    e.skip_field("medical_history.existing_conditions")
    e.skip_field("medical_history.current_medications")
    check("T8 engine: optional fields skipped -> intake can finalize", e.finalize() == {"ok": True})
    check("T8 engine: a required field cannot be skipped",
          "required" in e.skip_field("personal.age").get("error", "") if not e.frozen else True)

    e = IntakeEngine()
    e.save_field("visit.symptoms", '[{"description": "cold"}, {"description": "not feeling well"}]')
    r = e.save_field("visit.symptoms", '[{"description": "Cold", "duration": "five or six years"}]')
    sym = e.get("visit.symptoms")
    check("T9 engine: two symptoms from one answer stay two structured symptoms",
          [s["description"] for s in sym] == ["cold", "not feeling well"])
    check("T9 engine: a later duration attaches to its symptom (no fragment symptom)",
          sym[0]["duration"] == "five or six years" and len(sym) == 2)
    check("T9 engine: only the missing duration is asked for", r.get("ask_duration_for") == ["not feeling well"])

    e = IntakeEngine()
    e.save_field("medical_history.current_medications", '["metformin"]')
    e.save_field("medical_history.current_medications", '["amlodipine"]')
    check("engine: a second medication is added, the first is kept",
          e.get("medical_history.current_medications") == ["metformin", "amlodipine"])
    check("engine: 'No, penicillin' is an allergy, not none",
          IntakeEngine().save_field("medical_history.allergies", "No, penicillin").get("ok") is True)


# --- session (finalize / persistence) ----------------------------------------------------


async def session_tests():
    db = FakeDB()
    s, sent = new_session(db)
    fill(s.engine)
    llm = FakeLLM()
    params = FakeParams(llm)
    await s.finalize(params)
    types = [m["type"] for m in sent if m["type"].startswith("intake_")]
    check("T1 session: order is finalizing -> saving -> complete",
          types == ["intake_finalizing", "intake_saving", "intake_complete"], types)
    row = db.rows[s.session_id]
    check("T1 session: record written as completed with the patient's fields",
          row["status"] == "completed" and row["patient"]["personal"]["full_name"] == "Chirag Sharma"
          and row["patient"]["meta"]["completed"] is True and row["patient"]["meta"]["completed_at"])
    complete_msg = next(m for m in sent if m["type"] == "intake_complete")
    check("T1 session: completion event carries the session id and first name",
          complete_msg["session_id"] == s.session_id and complete_msg["patient_name"] == "Chirag")
    check("T1 session: closing line spoken, then the session ends",
          isinstance(llm.pushed[0], TTSSpeakFrame) and llm.pushed[0].text == CLOSING.format(name=", Chirag")
          and isinstance(llm.pushed[1], EndWorkerFrame))
    check("T1 session: the LLM is not asked for another reply after finalize",
          params.results[-1][1] is not None and params.results[-1][1].run_llm is False)
    check("T1 session: record is frozen after saving",
          s.engine.save_field("personal.age", "30").get("error", "").startswith("This intake is already complete"))

    # T5: Supabase failure -> no success, retry works
    db = FakeDB(fail=1)
    s, sent = new_session(db)
    fill(s.engine)
    llm = FakeLLM()
    await s.finalize(FakeParams(llm))
    check("T5 session: save failure -> no intake_complete sent",
          not any(m["type"] == "intake_complete" for m in sent))
    check("T5 session: the UI is told saving failed, with the patient-facing message",
          any(m["type"] == "intake_save_failed" and m["message"] == SAVE_PROBLEM for m in sent))
    check("T5 session: patient hears the save problem, the session is NOT ended",
          [type(f).__name__ for f in llm.pushed] == ["TTSSpeakFrame"] and llm.pushed[0].text == SAVE_PROBLEM)
    check("T5 session: nothing written to the database", db.rows[s.session_id]["status"] == "in_progress")
    await s.retry_save()
    check("T5 session: Retry saves and only then completes",
          db.rows[s.session_id]["status"] == "completed" and sent[-1]["type"] == "intake_complete")

    # T6: finalize twice (even concurrently) -> one write, one completion
    db = FakeDB()
    s, sent = new_session(db)
    fill(s.engine)
    llm = FakeLLM()
    p1, p2 = FakeParams(llm), FakeParams(llm)
    await asyncio.gather(s.finalize(p1), s.finalize(p2))
    await s.finalize(FakeParams(llm))
    check("T6 session: three finalize calls -> exactly one database write", db.complete_calls == 1, db.complete_calls)
    check("T6 session: exactly one completion event and one closing line",
          sum(m["type"] == "intake_complete" for m in sent) == 1
          and sum(isinstance(f, TTSSpeakFrame) for f in llm.pushed) == 1)
    check("T6 session: later calls report already_saved",
          p2.results[-1][0].get("already_saved") is True or p1.results[-1][0].get("already_saved") is True)

    # Incomplete intake: finalize refuses, nothing saved, nothing announced
    db = FakeDB()
    s, sent = new_session(db)
    s.engine.save_field("personal.full_name", "Chirag")
    params = FakeParams(FakeLLM())
    await s.finalize(params)
    check("session: incomplete intake -> finalize refused, no save, no events",
          db.complete_calls == 0 and not sent[-1:] or sent[-1]["type"] == "field_update"
          and "error" in params.results[-1][0])

    # No Supabase configured -> not a success
    s, sent = new_session(None)
    fill(s.engine)
    await s.finalize(FakeParams(FakeLLM()))
    check("session: no database configured -> save failure, never 'complete'",
          not any(m["type"] == "intake_complete" for m in sent) and s.state == "save_failed")

    # T10 (server side): patient B's session shares nothing with patient A's
    db = FakeDB()
    a, _ = new_session(db)
    fill(a.engine)
    await a.finalize(FakeParams(FakeLLM()))
    b, sent_b = new_session(db)
    fresh = all(b.engine.get(k) is None for k, _ in PATIENT_A)
    check("T10 session: patient B starts with every field empty and UNASKED",
          fresh and set(b.engine.status.values()) == {FieldStatus.UNASKED} and b.engine.data == {})
    check("T10 session: patient B has its own database row", b.session_id != a.session_id)
    check("T10 session: patient B is not frozen/completed", b.state == "open" and not b.engine.frozen)
    b.engine.save_field("personal.full_name", "Priya Rao")
    await b.finalize(FakeParams(FakeLLM()))
    check("T10 session: A's completed row is untouched by B",
          db.rows[a.session_id]["patient"]["personal"]["full_name"] == "Chirag Sharma")


def transcript_tests():
    from pipecat.processors.aggregators.llm_context import LLMSpecificMessage

    from intake.session import transcript_from_context

    # A real Gemini context holds LLMSpecificMessage objects for tool calls:
    # this is exactly what crashed every save before the fix.
    messages = [
        {"role": "assistant", "content": "Welcome to AVP Hospital."},
        {"role": "user", "content": "My name is Chirag."},
        LLMSpecificMessage(llm="google", message={"role": "model", "parts": [{"function_call": {"name": "save_field"}}]}),
        {"role": "tool", "content": "{\"ok\": true}", "tool_call_id": "x"},
        {"role": "assistant", "content": "How old are you?"},
    ]
    turns = transcript_from_context(messages)
    check("transcript: tool-call objects in the context no longer crash the save",
          turns == [{"role": "assistant", "text": "Welcome to AVP Hospital."},
                    {"role": "user", "text": "My name is Chirag."},
                    {"role": "assistant", "text": "How old are you?"}], turns)


async def transcript_failure_still_saves():
    db = FakeDB()
    s, sent = new_session(db)
    s._get_transcript = lambda: (_ for _ in ()).throw(AttributeError("simulated transcript bug"))
    fill(s.engine)
    await s.finalize(FakeParams(FakeLLM()))
    check("transcript: a transcript error never blocks saving the patient record",
          db.rows[s.session_id]["status"] == "completed" and db.rows[s.session_id]["transcript"] == []
          and sent[-1]["type"] == "intake_complete")


def main() -> int:
    transcript_tests()
    asyncio.run(transcript_failure_still_saves())
    engine_tests()
    asyncio.run(session_tests())
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
