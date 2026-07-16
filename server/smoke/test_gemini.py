"""Phase 0 smoke test: Gemini free-tier responds to a hello.
Requires GOOGLE_API_KEY in server/.env (get one at https://aistudio.google.com/apikey).
Run: ..\\.venv\\Scripts\\python.exe smoke\\test_gemini.py
"""
import os
import pathlib
from dotenv import load_dotenv
from google import genai

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")

def main():
    key = os.getenv("GOOGLE_API_KEY")
    if not key:
        raise SystemExit("GOOGLE_API_KEY missing in server/.env")

    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model="gemini-flash-lite-latest",
        contents="Say hello in one short sentence, as a hospital receptionist would.",
    )
    print("OK:", resp.text.strip())

if __name__ == "__main__":
    main()
