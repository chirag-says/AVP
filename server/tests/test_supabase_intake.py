r"""Real Supabase verification of intake finalization (writes clearly labelled TEST rows).

    .venv\Scripts\python.exe server\tests\test_supabase_intake.py

Uses the same create_session / complete_session the bot uses, against the
project's intake_sessions table. Every row it creates is a fake patient named
"Test Patient ..." with a 900000000x phone number and a transcript marked as an
automated test; the row ids are printed. Nothing is deleted or modified
outside these rows. No credentials are printed.
"""
import asyncio
import json
import pathlib
import sys
import uuid

SERVER = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402
from loguru import logger  # noqa: E402

load_dotenv(SERVER / ".env", override=True)
logger.remove()

from intake.session import IntakeSession, Persistence  # noqa: E402
from services.db import complete_session, create_session, get_client  # noqa: E402
from tests.test_intake_session import FakeLLM, FakeParams  # noqa: E402

TEST_MARK = [{"role": "system", "text": "AUTOMATED TEST RECORD (server/tests/test_supabase_intake.py), not a patient"}]
results: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))


def session(session_id=None, create=True):
    sent = []

    async def send(m):
        sent.append(m)

    s = IntakeSession(send=send, persistence=Persistence(create_session, complete_session),
                      session_id=session_id if session_id or not create else create_session(),
                      get_transcript=lambda: TEST_MARK)
    return s, sent


def row(sid):
    rows = get_client().table("intake_sessions").select("*").eq("id", sid).execute().data
    return rows[0] if rows else None


ALPHA = [("personal.full_name", "Test Patient Alpha"), ("personal.age", "22"), ("personal.gender", "male"),
         ("personal.phone", "9000000001"), ("personal.address", "1 Test Street, Testville"),
         ("visit.symptoms", '[{"description": "test cold", "duration": "3 days", "severity": "mild"}]'),
         ("medical_history.allergies", "none"), ("medical_history.existing_conditions", "no"),
         ("medical_history.current_medications", "I don't take anything")]
BRAVO = [("personal.full_name", "Test Patient Bravo"), ("personal.age", "41"), ("personal.gender", "female"),
         ("personal.phone", "9000000002"), ("personal.address", "2 Test Street, Testville"),
         ("visit.symptoms", '[{"description": "test headache", "duration": "1 week"}]'),
         ("medical_history.allergies", "penicillin")]


async def main() -> int:
    # --- Patient A: normal intake -> save -> completion ----------------------------------
    a, sent_a = session()
    for k, v in ALPHA:
        assert a.engine.save_field(k, v).get("ok"), k
    await a.finalize(FakeParams(FakeLLM()))
    ra = row(a.session_id)
    print(f"  patient A row id: {a.session_id}")
    check("T1 DB: record exists and is completed", ra is not None and ra["status"] == "completed")
    p = ra["patient"]
    check("T1 DB: correct patient fields stored",
          p["personal"]["full_name"] == "Test Patient Alpha" and p["personal"]["phone"] == "9000000001"
          and p["visit"]["symptoms"][0] == {"description": "test cold", "duration": "3 days", "severity": "mild"})
    check("T1 DB: completion timestamp present", bool(p["meta"].get("completed_at")) and p["meta"]["completed"] is True)
    check("T8 DB: optional 'none' answers stored as none",
          p["medical_history"]["existing_conditions"] == "none" and p["medical_history"]["current_medications"] == "none")
    check("DB: field states stored with the record",
          p["meta"]["field_status"]["medical_history.allergies"] in ("answered", "confirmed"))
    check("T1 DB: intake_complete sent only after the save, with this row's id",
          [m["type"] for m in sent_a if m["type"].startswith("intake_")] == ["intake_finalizing", "intake_saving", "intake_complete"]
          and sent_a[-1]["session_id"] == a.session_id)

    # --- T6: finalize again, and a direct second write -> still one row ----------------------
    await a.finalize(FakeParams(FakeLLM()))
    await asyncio.gather(a.finalize(FakeParams(FakeLLM())), a.finalize(FakeParams(FakeLLM())))
    second = complete_session(a.session_id, {"personal": {"full_name": "SHOULD NOT OVERWRITE"}, "meta": {}}, [])
    ra2 = row(a.session_id)
    same_name = get_client().table("intake_sessions").select("id").eq("status", "completed") \
        .eq("patient->personal->>full_name", "Test Patient Alpha").eq("id", a.session_id).execute().data
    check("T6 DB: repeated finalize / direct re-write -> 'already_saved', record not overwritten",
          second == "already_saved" and ra2["patient"]["personal"]["full_name"] == "Test Patient Alpha")
    check("T6 DB: exactly one completed row for this session", len(same_name) == 1)
    check("T6 DB: only one intake_complete event", sum(m["type"] == "intake_complete" for m in sent_a) == 1)

    # --- T5: real save failure (row that does not exist) -> no success -----------------------
    ghost, sent_g = session(session_id=str(uuid.uuid4()), create=False)
    for k, v in ALPHA:
        ghost.engine.save_field(k, v)
    await ghost.finalize(FakeParams(FakeLLM()))
    check("T5 DB: failed write -> no intake_complete, save_failed reported",
          not any(m["type"] == "intake_complete" for m in sent_g)
          and any(m["type"] == "intake_save_failed" for m in sent_g) and ghost.state == "save_failed")
    check("T5 DB: nothing inserted for the failed session", row(ghost.session_id) is None)

    # --- Patient B after A: zero leakage; optional fields skipped still completes ---------
    b, sent_b = session()
    check("T10 DB: patient B gets a new row", b.session_id != a.session_id)
    check("T10: patient B's engine starts empty", b.engine.data == {} and not b.engine.frozen)
    for k, v in BRAVO:
        assert b.engine.save_field(k, v).get("ok"), k
    b.engine.skip_field("medical_history.existing_conditions")
    b.engine.skip_field("medical_history.current_medications")
    await b.finalize(FakeParams(FakeLLM()))
    rb = row(b.session_id)
    print(f"  patient B row id: {b.session_id}")
    blob = json.dumps(rb)
    check("T8 DB: optional fields skipped -> intake completes",
          rb["status"] == "completed" and rb["patient"]["meta"]["field_status"]["medical_history.current_medications"] == "skipped"
          and "current_medications" not in rb["patient"]["medical_history"])
    check("T10 DB: no patient A value anywhere in patient B's row",
          not any(x in blob for x in ("Alpha", "9000000001", "test cold", "1 Test Street")))
    check("T10 DB: B's completion event names B", sent_b[-1].get("patient_name") == "Test")
    ra3 = row(a.session_id)
    check("T10 DB: patient A's row unchanged after B", ra3["patient"] == ra["patient"])

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    print(f"  test rows written: {a.session_id}, {b.session_id} (named 'Test Patient Alpha/Bravo')")
    return len(failed)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
