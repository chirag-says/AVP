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
    """Save a non-final state (e.g. "abandoned"). Never overwrites a completed intake."""
    get_client().table("intake_sessions").update({
        "status": status,
        "patient": record,
        "transcript": transcript,
        "flags": record.get("meta", {}).get("flags", []),
    }).eq("id", session_id).neq("status", "completed").execute()


def complete_session(session_id: str, record: dict, transcript: list[dict]) -> str:
    """Mark this session's row completed, idempotently, and verify it.

    One row per session (created by create_session when the call starts), so
    the write is an UPDATE by primary key: a repeated finalize, a retry or a
    reconnect can never add a second patient row. The status guard means the
    first completed record wins and is never overwritten.

    Returns "saved", or "already_saved" if this session was completed before.
    Raises if the row is missing or the write did not take.
    """
    client = get_client()
    rows = (
        client.table("intake_sessions")
        .update({
            "status": "completed",
            "patient": record,
            "transcript": transcript,
            "flags": record.get("meta", {}).get("flags", []),
        })
        .eq("id", session_id)
        .neq("status", "completed")
        .execute()
        .data
    )
    if rows:
        if len(rows) != 1 or rows[0].get("status") != "completed":
            raise RuntimeError("completion write not applied")
        return "saved"
    existing = client.table("intake_sessions").select("id,status").eq("id", session_id).execute().data
    if existing and existing[0].get("status") == "completed":
        return "already_saved"
    raise RuntimeError("intake session row not found")


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
