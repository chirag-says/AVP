"""Phase 0 smoke test: Supabase insert + select round-trip.
Requires SUPABASE_URL / SUPABASE_SERVICE_KEY in server/.env, and the
intake_sessions table created (see POC_GUIDE.md section 4 for the SQL).
Run: ..\\.venv\\Scripts\\python.exe smoke\\test_supabase.py
"""
import os
import pathlib
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")

def main():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_KEY missing in server/.env")

    client = create_client(url, key)

    inserted = client.table("intake_sessions").insert({
        "status": "completed",
        "patient": {"personal": {"full_name": "Smoke Test Patient"}},
        "transcript": [],
        "flags": [],
    }).execute()
    row_id = inserted.data[0]["id"]
    print(f"OK: inserted row {row_id}")

    fetched = client.table("intake_sessions").select("*").eq("id", row_id).execute()
    print(f"OK: fetched back -> {fetched.data[0]['patient']}")

    client.table("intake_sessions").delete().eq("id", row_id).execute()
    print("OK: cleaned up test row")

if __name__ == "__main__":
    main()
