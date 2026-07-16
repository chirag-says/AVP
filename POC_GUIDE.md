# Hospital Voice Intake Assistant — PoC Build Guide

> **Goal of the PoC:** In a 10-minute demo, a "patient" talks to the assistant, it collects
> a complete intake record through natural conversation, validates and confirms the details,
> and the record appears in Supabase as clean JSON — with zero typing by a receptionist.
>
> **Success criteria (define these BEFORE building — this is what you show the client):**
> 1. 100% of *required* fields captured before the bot says goodbye (completeness — their #1 pain).
> 2. Name and phone number confirmed via read-back before saving (accuracy — their #2 pain).
> 3. Record lands in DB as structured JSON within 2 seconds of conversation end.
> 4. A receptionist-facing table view shows the record with any low-confidence flags.

---

## 1. Stack verdict

| Layer | Your pick | Verdict | Notes |
|---|---|---|---|
| Voice framework | Pipecat | ✅ Correct | Purpose-built for exactly this. Alternatives (LiveKit Agents, Vocode) are heavier or less active. Keep it. |
| STT | faster-whisper | ✅ Correct | Free, local, private. Use `small` (int8) on CPU, `distil-large-v3` if you have an NVIDIA GPU. |
| LLM | Gemini free tier / Ollama | ✅ with a decision | **Use Gemini `gemini-2.5-flash` (or whatever current flash model your key supports — check with `client.models.list()`, older names get their free-tier quota zeroed out over time) as primary** — you need reliable *function calling* for slot-filling, and 8B local models are flaky at it. Keep Ollama (`llama3.1:8b`) only as an offline-demo fallback. |
| TTS | Kokoro | ✅ Correct | 82M params, Apache-2.0, near-real-time on CPU via ONNX. Best free local TTS right now. |
| Backend | FastAPI | ✅ | Pipecat's own runner is FastAPI-based; they merge naturally. |
| DB | Supabase | ✅ | Free tier + native `jsonb` = perfect fit for "store JSON in the DB". |
| Frontend | React | ✅ | Pipecat ships an official JS client SDK. |
| **Missing piece** | **Transport: `SmallWebRTCTransport`** | ⚠️ You must add this | Browser mic → server audio needs WebRTC. Pipecat's default examples often use Daily (paid service). **SmallWebRTCTransport is Pipecat's free, serverless P2P WebRTC transport — use it.** |
| **Missing piece** | **VAD: Silero** | ⚠️ You must add this | Detects when the patient stops talking. Free, bundled as a Pipecat extra. Without it turn-taking doesn't work. |

**Platform note (you're on Windows 11):** Pipecat + aiortc + faster-whisper generally work on
native Windows, but if you hit binary/dependency pain (esp. `av`/aiortc), move the *server* into
**WSL2** and keep React on Windows. Budget 30 minutes for this contingency, don't fight native
Windows for a day.

---

## 2. Architecture

```
┌─────────────────────────┐         WebRTC (audio both ways)
│  React (Vite)           │◄───────────────────────────────────┐
│  - Mic + connect button │                                    │
│  - Live transcript      │      ┌─────────────────────────────▼──────────────┐
│  - LIVE INTAKE FORM     │◄─────│  FastAPI + Pipecat pipeline (1 per session)│
│    (fills as bot        │ RTVI │                                            │
│     captures fields)    │ msgs │  transport.input()                         │
└─────────────────────────┘      │    → Silero VAD (end-of-speech)            │
                                 │    → faster-whisper STT                    │
                                 │    → user context aggregator               │
                                 │    → Gemini LLM ──function calls──┐        │
                                 │    → Kokoro TTS                   │        │
                                 │    → transport.output()           ▼        │
                                 │                          ┌──────────────┐  │
                                 │                          │ IntakeEngine │  │
                                 │                          │ (slot-fill + │  │
                                 │                          │  validators) │  │
                                 │                          └──────┬───────┘  │
                                 └─────────────────────────────────┼──────────┘
                                                                   ▼
                                                        Supabase (jsonb record)
```

**The design principle (one engine, many uses):** the intake questionnaire is **data, not code**.
Fields, prompts, validation rules, and confirmation policies live in one table (`schema.py`).
The client will 100% ask "can we add a field for insurance number?" in the meeting —
your answer is "yes, it's one line of config," and that answer wins the project.

---

## 3. Two hard truths to design around (and to tell the client honestly)

### Truth 1: Voice does NOT automatically fix spelling
The client's pain is misspelled names and wrong numbers. STT will *also* mangle Indian names
("Chirag" → "Cheerag", "Shirag"). The accuracy win comes from the **confirmation loop**, not the mic:

- **Read-back**: bot repeats name and phone digit-by-digit; patient confirms or corrects.
- **Spell-out mode**: if patient corrects twice, bot asks them to spell it letter by letter.
- **Hybrid screen**: the live form on screen shows what was captured — patients catch errors
  visually that they miss aurally. This is your killer demo moment.
- **Confidence flags**: any field the bot isn't sure about is flagged in the JSON for the
  receptionist to verify in 5 seconds — human-in-the-loop, not human-out-of-the-loop.

### Truth 2: Latency budget is ~2–3s per turn on free/local tools
VAD stop (~200ms) → whisper-small on CPU (~1–2s) → Gemini flash first token (~500–900ms) →
Kokoro (~300ms–1s). It's fine for a PoC — set expectations: "the paid stack (streaming
STT/TTS) brings this under 800ms."

**GPU note, corrected after testing on the actual target laptop:** a GTX 1650 being *present*
and *detected* (`nvidia-smi` works, `ctranslate2.get_cuda_device_count()` returns 1) is not
the same as being *usable* — on Windows, ctranslate2 also needs the full NVIDIA **CUDA
Toolkit** installed (specifically `cublas64_12.dll`), not just the display driver. Without it,
`bot.py` crashed mid-conversation with `Library cublas64_12.dll is not found or cannot be
loaded` the moment VAD triggered STT — non-fatal to the pipeline, but the resulting barge-in
interruption cut off the bot's greeting before it played, which looked like "nothing is
speaking." `bot.py` now **defaults `WHISPER_DEVICE` to `cpu`**, which is what Phase 0 actually
proved word-for-word accurate. Set the `WHISPER_DEVICE=cuda` env var to opt back into GPU
*after* installing the CUDA Toolkit (not just the driver) — untested here, verify on your
machine before relying on it for a demo.

---

## 4. Data design

### The JSON record (what lands in the DB)

```json
{
  "personal": {
    "full_name": "Chirag Sharma",
    "age": 34,
    "gender": "male",
    "phone": "9876543210",
    "address": "12 MG Road, Bengaluru"
  },
  "visit": {
    "symptoms": [
      { "description": "fever", "duration": "3 days", "severity": "high" },
      { "description": "headache", "duration": "2 days", "severity": "moderate" }
    ],
    "prior_treatment": "took paracetamol, no relief"
  },
  "medical_history": {
    "existing_conditions": ["diabetes"],
    "current_medications": ["metformin 500mg"],
    "allergies": ["penicillin"]
  },
  "meta": {
    "language": "en",
    "completed": true,
    "flags": [
      { "field": "personal.address", "reason": "low_confidence_transcription" }
    ]
  }
}
```

### Supabase setup (SQL editor → run once)

```sql
create table intake_sessions (
  id          uuid primary key default gen_random_uuid(),
  created_at  timestamptz not null default now(),
  status      text not null default 'in_progress',   -- in_progress | completed | abandoned
  patient     jsonb not null default '{}'::jsonb,
  transcript  jsonb not null default '[]'::jsonb,     -- full turn-by-turn log (audit + debugging)
  flags       jsonb not null default '[]'::jsonb
);

-- PoC talks to DB only from the backend with the service_role key,
-- so enable RLS and add no public policies (deny-all from browsers).
alter table intake_sessions enable row level security;
```

**Rules even in a PoC:** service key lives in server `.env` only, never in React.
Never demo with real patient data — use fictional patients. (For production you'll need a
DPDP Act conversation with the client — see §10.)

---

## 5. The intake engine (the actual brain)

### 5.1 Field schema — `server/intake/schema.py`

```python
from dataclasses import dataclass, field

@dataclass
class IntakeField:
    key: str                    # dot-path into the JSON record
    label: str                  # human name, used in prompts and read-back
    required: bool = True
    validate: str | None = None # regex, applied server-side (never trust the LLM)
    confirm: str = "none"       # none | readback | digits | spell_on_retry
    hint: str = ""              # extra instruction for the LLM

INTAKE_FIELDS: list[IntakeField] = [
    IntakeField("personal.full_name", "full name", confirm="spell_on_retry"),
    IntakeField("personal.age", "age", validate=r"^(?:[1-9]|[1-9][0-9]|1[01][0-9]|120)$"),
    IntakeField("personal.gender", "gender",
                hint="Accept male/female/other in any phrasing; store lowercase."),
    IntakeField("personal.phone", "mobile number",
                validate=r"^[6-9]\d{9}$", confirm="digits",
                hint="Indian 10-digit mobile. Read back digit by digit."),
    IntakeField("personal.address", "address", confirm="readback"),
    IntakeField("visit.symptoms", "symptoms",
                hint="A list. For each symptom get description, duration, severity. "
                     "Ask follow-ups until duration is known for each."),
    IntakeField("medical_history.allergies", "allergies",
                hint="Safety-critical. If none, store the literal value 'none' — "
                     "never leave empty."),
    IntakeField("medical_history.existing_conditions", "existing conditions", required=False),
    IntakeField("medical_history.current_medications", "current medications", required=False),
]
```

This one list drives: the system prompt (auto-generated), the completeness check,
validation, the read-back step, and the live form in React. Add a field → everything updates.

### 5.2 How the LLM fills slots — function calling, not free text

Give Gemini two tools:

- `save_field(key: str, value: json)` — called whenever the patient provides a detail.
  Your handler runs the regex validator; on failure it returns an error string and the LLM
  re-asks naturally ("Sorry, that number seems short — could you repeat it?").
- `finalize_intake()` — the LLM may only call this when every required field is filled.
  Your handler enforces it: if something's missing it returns
  `{"error": "missing: personal.phone"}` and the conversation continues.
  **The LLM never decides completeness — your code does.** That's the completeness guarantee.

On successful `finalize_intake()`: write the record to Supabase, push a `intake_complete`
message to the React client, bot says goodbye.

### 5.3 System prompt skeleton — `server/intake/prompts.py`

```python
def build_system_prompt(fields) -> str:
    field_lines = "\n".join(
        f"- {f.label} ({'required' if f.required else 'optional'}). {f.hint}"
        for f in fields
    )
    return f"""You are a warm, patient hospital intake assistant at the reception desk.
Your ONLY job is to collect the following details through natural conversation:
{field_lines}

Rules:
- Ask ONE question at a time. Keep every reply under 25 words (it will be spoken aloud).
- Never give medical advice or diagnosis. If asked, say the doctor will help with that.
- Call save_field the moment the patient provides any detail.
- Before finishing: read the full name back and ask if it is correct; read the phone
  number back digit by digit and ask if it is correct.
- If the patient corrects a spelling twice, ask them to spell it letter by letter.
- If the patient refuses a required detail, explain why it is needed once; if they still
  refuse, save the value "declined" and move on.
- When everything is collected and confirmed, call finalize_intake, then thank them
  and tell them to take a seat.
- Speak plainly. No markdown, no emojis, no lists — your words are converted to speech."""
```

---

## 6. Phase plan

Build in this order. **Each phase has a verification gate — do not advance until it passes.**
The order deliberately isolates the risky part (voice plumbing) from the smart part (the engine).

### Phase 0 — Environment & component smoke tests (half a day)
```powershell
# Python 3.12 recommended (3.13 still has dependency gaps in this ecosystem)
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install "pipecat-ai[silero,whisper,google,webrtc]" fastapi "uvicorn[standard]" supabase python-dotenv kokoro-onnx
```
> Pin whatever `pipecat-ai` version you install (`pip freeze`) — the API moves fast.

Verify each component **standalone** before combining anything:
1. faster-whisper transcribes a WAV you record (`from faster_whisper import WhisperModel`).
2. Kokoro speaks a sentence to a WAV file (kokoro-onnx needs the model + voices files —
   download per its README).
3. Gemini responds to a hello (AI Studio key in `.env` as `GOOGLE_API_KEY`).
4. Supabase insert + select works with the service key.

**Gate:** all four run clean. Any failure here costs 10x more to debug inside the pipeline.

### Phase 1 — Text-only intake brain (1–2 days) ← the real IP
Build `IntakeEngine` + prompts + validators + Supabase write, driven from a **plain CLI chat
loop** (or a `/chat` endpoint). No audio at all.

**Gate:** 5 scripted conversations all produce complete, valid JSON in Supabase:
happy path · user corrects their name · invalid phone given twice · user rambles
symptoms before being asked · user refuses address.

*If the demo deadline is tight, Phase 1 alone is already demoable as a text chatbot.*

### Phase 2 — Voice pipeline (2–3 days)
Don't hand-roll the WebRTC plumbing or guess class names — Pipecat ships a CLI scaffolder
that generates a *runnable* bot for your exact pinned version:

```powershell
pip install "pipecat-ai[cli]"
pipecat init . --bot-type web -t smallwebrtc -m cascade `
  --stt whisper_stt --llm google_gemini_llm --tts kokoro_tts `
  --client-framework none --no-deploy-to-cloud
```
This writes a working `bot.py` (transport + STT + LLM + TTS already wired, correct
imports for your installed version) plus an `AGENTS.md` explaining the framework's current
conventions in detail — read it before touching anything. **Modify the generated pipeline
in place; don't rebuild it.**

As of pipecat-ai 1.5.0, the two things worth knowing before you edit:
- **Kokoro, Whisper, and Gemini all ship as built-in services** (`pipecat.services.kokoro.tts.KokoroTTSService`,
  `.whisper.stt.WhisperSTTService`, `.google.llm.GoogleLLMService`) — no custom TTS wrapper needed.
- **Function calling is just `LLMContext(tools=[save_field_tool, finalize_tool])`** — plain
  async functions; name, type hints, and docstring become the schema automatically. No
  separate `register_function` call. See `server/bot.py` for the working version — it wraps
  `IntakeEngine.save_field`/`.finalize` as two small adapter functions and broadcasts a
  `field_update` RTVI server-message after every successful save (the React form's data source).

**Gate:** full spoken conversation → record in Supabase. Run `python server/bot.py -t webrtc`
and either talk to it via the React client (Phase 3) or Pipecat's own browser debug UI at
`http://localhost:7860` — the latter needs `pip install pipecat-ai-prebuilt` (not pulled in
by the `[cli]`/base extras; the runner falls back to "Prebuilt frontend not available" in
its logs without it, but still serves `/api/offer` etc. fine either way). Handy for a quick
sanity check without the full React app running.

**Windows gotcha already fixed in `server/bot.py`:** the runner prints an emoji on startup
(`🚀 Bot ready!`) that crashes with `UnicodeEncodeError` on a default Windows terminal
(cp1252 codepage) before the server even binds. `bot.py` forces UTF-8 on stdout/stderr at
the top of the file for this reason — don't remove that block, and don't rely on remembering
`$env:PYTHONUTF8=1` in every new terminal instead.

### Phase 3 — React client with live form (2 days)
`npm create vite@latest web` then `npm i @pipecat-ai/client-js @pipecat-ai/small-webrtc-transport`.

Three components:
1. **Connect/mic button** + bot audio playback (SDK handles the audio element).
2. **Live transcript** (SDK emits user/bot transcript events).
3. **Live intake form** — read-only fields, one per `INTAKE_FIELDS` entry, that fill in as
   the server captures them. Server pushes `{type:"field_update", key, value}` via the
   RTVI server-message channel (or, simplest possible: a tiny WebSocket alongside, where
   the server broadcasts engine state after every `save_field`). Green tick per field;
   the demo magic is watching the form complete itself while the patient just talks.

**Gate:** stranger-test — someone who isn't you completes an intake without instructions.

### Phase 4 — Demo hardening (1–2 days)
- Handle: mic permission denied · WebRTC disconnect mid-session (save as `abandoned`,
  keep partial JSON — partial data beats lost data) · Gemini rate-limit/timeout (bot says
  "bear with me a moment" and retries once).
- Barge-in (patient interrupts bot): Pipecat + Silero handles this — verify it works, demo it.
- A dead-simple `/records` page: table of intake_sessions, flags highlighted. This is the
  "receptionist view" and closes the story.
- Rehearse the demo script (§8) three times, including once on hotel/phone-hotspot wifi.

**Total: roughly 6–9 working days.**

---

## 7. Verifying without a live mic — Pipecat's eval harness

Pipecat ships a scripted eval runner (`pipecat.evals`) that drives your *running* bot over
text or synthesized audio and asserts on what it does — useful for regression-checking the
intake logic (completeness, validation, tool calls) without a live voice call every time.
Scaffold with `--eval` to get starter scenarios in `server/evals/`, then:
```powershell
python server/bot.py -t eval          # boots a headless eval server
pipecat eval run server/evals/starter_text.yaml -v   # drives it from a second terminal
```
Worth adding once Phase 2 works, as a fast way to re-check prompt changes before every demo
rehearsal — text mode is nearly instant since it skips STT/VAD/TTS entirely.

---

## 8. Demo script for the client meeting

Run three scenarios, in this order:

1. **Happy path** (2 min): fictional patient, smooth intake, form fills live,
   end on the Supabase row appearing in the records page.
2. **The correction** (2 min): give a name the bot mishears, let it read back wrong,
   correct it, show spell-out mode fix it. *This scenario answers their actual complaint —
   it's the most important two minutes of the meeting.*
3. **The incomplete patient** (1 min): try to end early / refuse the phone number —
   show the bot politely insisting, and the record only saving as complete when it is.
   This kills their "patients don't fill the form fully" problem on camera.

Then show the JSON in Supabase and say the sentence: *"Adding a new field — say insurance
number — is one line of configuration."*

---

## 9. Free-tier limits you must respect during the demo

| Tool | Free limit | Demo impact |
|---|---|---|
| Gemini (AI Studio) | Model-dependent and drifts — **measured live**: `gemini-2.0-flash` is now fully deprecated (0 free-tier quota); `gemini-2.5-flash` is only 5 req/min; `gemini-flash-lite-latest` (currently resolving to `gemini-3.1-flash-lite`) is 15 req/min. **Use `gemini-flash-lite-latest`** — the `-latest` alias also means it won't go stale like a dated model name did on us mid-build. Re-check with `client.models.list()` if you hit 429s. | One conversation ≈ 10–20 requests. 15 req/min is enough for one live conversation but not back-to-back automated tests — don't rerun test scripts in a tight loop, and don't do a live 5-kiosk trial on it. |
| Supabase | 500MB DB, project pauses after ~1 week inactivity | **Wake the project the day before the demo.** |
| faster-whisper / Kokoro / Pipecat | None — local | Concurrency limited only by your hardware (~2–3 parallel sessions on a laptop CPU). |

---

## 10. Limitations & production roadmap (say these BEFORE the client asks)

Being first to name the limits is what makes the pitch credible:

1. **Language**: PoC is English. Real reception traffic is Hindi/Kannada/mixed. Roadmap:
   Whisper large is decent at Hindi; production wants an Indian-language stack
   (Sarvam AI / Bhashini APIs) — this is a paid-phase line item, and a strong one.
2. **Noise**: a hospital lobby is loud. PoC assumes a quiet corner / handheld or headset mic.
   Production = kiosk with a directional mic.
3. **Scale**: 1000s of patients/day ≠ one laptop. Production needs GPU workers or paid
   streaming STT/TTS (Deepgram/Cartesia/Sarvam ≈ $0.02–0.06 per intake-minute) + paid Gemini.
   Give them a per-patient cost estimate in the proposal, not vague "we'll scale it".
4. **Compliance**: patient data in India = DPDP Act 2023 — consent capture, retention policy,
   encryption at rest, audit log. PoC uses fictional data; production needs a compliance
   workstream. Clients respect you for raising this first.
5. **Not a triage bot**: it collects, it never advises. Keep that line bright — it's a
   safety and liability boundary. The system prompt enforces it; say it out loud too.
6. **Human-in-the-loop**: position it as "receptionist reviews flagged fields in seconds"
   not "replaces the receptionist" — accuracy story and change-management story in one.

---

## 11. Project structure

```
D:\HODDOCTOR\
├── POC_GUIDE.md               ← this file
├── .claude\launch.json        # preview_start config for the web dev server
├── server\
│   ├── bot.py                 # Pipecat pipeline (STT/LLM/TTS wiring) + /api/records route
│   ├── chat_cli.py            # Phase 1 harness: text-only conversation, no audio
│   ├── PIPECAT_REFERENCE.md   # framework's own AGENTS.md, saved from `pipecat init` scaffold
│   ├── supabase_schema.sql    # run once in the Supabase SQL editor
│   ├── intake\
│   │   ├── schema.py          # INTAKE_FIELDS — the single source of truth
│   │   ├── engine.py          # IntakeEngine: state, validators, save_field/finalize handlers
│   │   └── prompts.py         # build_system_prompt()
│   ├── services\
│   │   └── db.py              # supabase client (service key, server-only)
│   ├── smoke\                 # Phase 0 standalone component checks
│   │   ├── test_kokoro.py
│   │   ├── test_whisper.py
│   │   ├── test_gemini.py
│   │   └── test_supabase.py
│   ├── models\                 # kokoro-v1.0.onnx + voices-v1.0.bin (downloaded, gitignore-worthy)
│   ├── .env                    # GOOGLE_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY — never commit
│   ├── .env.example
│   └── requirements.txt        # pip freeze — pinned to what Phase 0 actually verified working
└── web\                        # Vite + React, `npm run build`/`tsc --noEmit` both verified clean
    └── src\
        ├── App.tsx              # connect button, layout, PipecatClient wiring
        ├── Transcript.tsx       # live user/bot transcript
        ├── IntakeForm.tsx       # live-filling form driven by field_update RTVI messages
        ├── Records.tsx          # receptionist table view (hits /api/records)
        └── fields.ts            # mirrors server/intake/schema.py field list
```

**Run it** (from `D:\HODDOCTOR`):
```powershell
.venv\Scripts\python.exe server\bot.py -t webrtc     # backend, port 7860
npm --prefix web run dev                              # frontend, port 5173
```

*— Guide written 2026-07-15, updated through Phase 2 build. Pipecat's API moves fast: when
a snippet here and the installed package disagree, trust the installed package
(`server/PIPECAT_REFERENCE.md` and reading the source under `.venv/Lib/site-packages/pipecat/`
directly resolved every mismatch found during this build).
