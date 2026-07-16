"""Phase 1 verification harness: text-only intake conversation, no audio.

Talk to the bot in the terminal exactly like the voice pipeline will let a
patient talk to it. Confirms the engine/prompts/validation loop works before
any WebRTC/STT/TTS complexity is added.

Run: .venv\\Scripts\\python.exe server\\chat_cli.py
"""
import os
import pathlib
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from intake.engine import IntakeEngine
from intake.prompts import build_system_prompt
from services.db import create_session, save_session

load_dotenv(pathlib.Path(__file__).resolve().parent / ".env")


def _retry_delay_seconds(err: genai_errors.ClientError) -> float:
    try:
        for detail in err.details["error"]["details"]:
            if detail.get("@type", "").endswith("RetryInfo"):
                return float(detail["retryDelay"].rstrip("s"))
    except (KeyError, TypeError, ValueError):
        pass
    return 15.0


def send_with_retry(chat, text: str, max_retries: int = 4):
    """The free tier is 15 req/min — a real spoken conversation stays well
    under that (human speech + STT/TTS latency throttles it naturally), but
    scripted/automated testing can burn through it fast. Retry once or twice
    on 429 rather than crashing and losing the in-progress intake."""
    for attempt in range(max_retries + 1):
        try:
            return chat.send_message(text)
        except genai_errors.ClientError as err:
            if err.code == 429 and attempt < max_retries:
                delay = _retry_delay_seconds(err)
                print(f"[rate limited, waiting {delay:.0f}s before retrying...]")
                time.sleep(delay + 1)
                continue
            raise


def main():
    key = os.getenv("GOOGLE_API_KEY")
    if not key:
        raise SystemExit("GOOGLE_API_KEY missing in server/.env")

    engine = IntakeEngine()
    client = genai.Client(api_key=key)
    chat = client.chats.create(
        model="gemini-flash-lite-latest",
        config=types.GenerateContentConfig(
            system_instruction=build_system_prompt(),
            tools=engine.as_tools(),
        ),
    )

    transcript: list[dict] = []
    session_id = None
    if os.getenv("SUPABASE_URL"):
        session_id = create_session()
        print(f"[session {session_id}]")

    print("Bot: Hello! Welcome to the hospital. Could I get your full name, please?")
    transcript.append({"role": "bot", "text": "Hello! Welcome to the hospital. Could I get your full name, please?"})

    while True:
        try:
            user_text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_text:
            continue
        if user_text.lower() in {"quit", "exit"}:
            break

        transcript.append({"role": "patient", "text": user_text})
        try:
            response = send_with_retry(chat, user_text)
        except genai_errors.ClientError:
            print("[Gemini call failed after retries — saving partial intake and stopping]")
            if session_id:
                save_session(session_id, engine.to_record(), transcript, status="abandoned")
                print(f"[partial session saved as abandoned: {session_id}]")
            raise
        bot_text = response.text or "(...)"
        print(f"Bot: {bot_text}")
        transcript.append({"role": "bot", "text": bot_text})

        if engine.completed:
            record = engine.to_record()
            print("\n--- INTAKE COMPLETE ---")
            import json
            print(json.dumps(record, indent=2))
            if session_id:
                save_session(session_id, record, transcript, status="completed")
                print(f"[saved to Supabase: session {session_id}]")
            break

    if not engine.completed and session_id:
        save_session(session_id, engine.to_record(), transcript, status="abandoned")
        print(f"[partial session saved as abandoned: {session_id}]")


if __name__ == "__main__":
    main()
