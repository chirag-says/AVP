# HODDOCTOR — Setup

Two voice services (reception chatbot + consultation scribe) plus a React web app.

## Prerequisites

- **Python 3.13** (required — some pinned deps are 3.13-only). Check: `python --version`
- **Node.js 18+** and npm. Check: `node --version`
- A **Supabase** project (free tier is fine)
- API keys:
  - **Sarvam AI** — https://dashboard.sarvam.ai (speech-to-text/text-to-speech; paid, small trial credit)
  - **Google AI Studio** — https://aistudio.google.com/apikey (free tier; powers the LLM + summaries)

> Tested on Windows + PowerShell. Commands below are PowerShell.

## 1. Backend (Python)

From the project root (`HODDOCTOR`):

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r server\requirements.txt
```

## 2. Frontend (React)

```powershell
cd web
npm install
cd ..
```

## 3. Environment variables

Copy the template and fill in real keys:

```powershell
Copy-Item server\.env.example server\.env
notepad server\.env
```

Fill in `SARVAM_API_KEY`, `GOOGLE_API_KEY`, `SUPABASE_URL`, and `SUPABASE_SERVICE_KEY`
(Supabase dashboard → Settings → API; use the **service_role** key).

The web app needs no env file for local dev — it defaults to `localhost:7860` (chatbot)
and `localhost:7861` (scribe).

## 4. Database (Supabase)

In your Supabase project's **SQL editor**, run both files once:

- `server\supabase_schema.sql` — the reception chatbot's `intake_sessions` table
- `server\consult_schema.sql` — the scribe's `consultation_notes` table

## 5. Run it (three terminals)

```powershell
# Terminal 1 — reception chatbot (port 7860)
.venv\Scripts\python.exe server\bot.py -t webrtc

# Terminal 2 — consultation scribe (port 7861)
.venv\Scripts\python.exe server\scribe_bot.py -t webrtc --port 7861

# Terminal 3 — web app (port 5173)
cd web ; npm run dev
```

Then open **http://localhost:5173**:
- `/` — reception chatbot (intake)
- `/consultation.html` — consultation scribe
- `/export.html` — export data (CSV / Excel)

(The navbar links between all three.)

## Notes

- **Model files not needed** by default: the stack runs on Sarvam (cloud STT/TTS), so the
  large `server\models\*.onnx` / `*.bin` files are only required if you switch to the local
  fallback stack (`STT_PROVIDER=whisper TTS_PROVIDER=kokoro` in `.env`).
- **`.mcp.json`** is only for Claude Code's tooling, not for running the app — safe to ignore.
- **Free-tier quota:** the Google key allows ~20 summary/LLM requests per day per model.
  For heavier use, enable billing on the Google API key.
