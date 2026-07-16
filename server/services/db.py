"""Supabase access. Server-only — uses the service_role key, never expose this
client or the key to the browser."""
import os
from functools import lru_cache

from supabase import Client, create_client


@lru_cache(maxsize=1)
def get_client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


def create_session() -> str:
    row = get_client().table("intake_sessions").insert({"status": "in_progress"}).execute()
    return row.data[0]["id"]


def save_session(session_id: str, record: dict, transcript: list[dict], status: str) -> None:
    get_client().table("intake_sessions").update({
        "status": status,
        "patient": record,
        "transcript": transcript,
        "flags": record.get("meta", {}).get("flags", []),
    }).eq("id", session_id).execute()


def list_sessions(limit: int = 50) -> list[dict]:
    rows = (
        get_client()
        .table("intake_sessions")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return rows.data
