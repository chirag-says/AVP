"""Supabase access for the Consultation Scribe module.

Kept separate from services/db.py (the reception chatbot's store) so the two
modules never share write paths — but it reuses the same lru_cached client,
which is server-only and holds the service_role key. Never expose this to the
browser.
"""
from services.db import get_client


def save_note(transcript: list[dict], summary: dict, duration_s: int | None, model: str) -> str:
    row = (
        get_client()
        .table("consultation_notes")
        .insert(
            {
                "status": "summarized",
                "duration_s": duration_s,
                "transcript": transcript,
                "summary": summary,
                "model": model,
            }
        )
        .execute()
    )
    return row.data[0]["id"]


def list_notes(limit: int = 50) -> list[dict]:
    rows = (
        get_client()
        .table("consultation_notes")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return rows.data
