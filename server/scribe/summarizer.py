"""Turn a raw consultation transcript into a structured clinical note.

The scribe pipeline is deliberately dumb: it only transcribes. All the
clinical structuring happens here, in one Gemini call at the end of the visit.
That split mirrors the intake module's design — the transcription layer never
decides anything, a single well-scoped model call does.

Why Gemini and not Sarvam's LLM: the same measured reason bot.py documents for
intake — the Google models follow a strict output contract reliably across a
long context, and a consultation transcript is long. Here the contract is a
fixed JSON shape rather than tool calls, but the failure mode of a weaker model
(dropping fields, inventing structure) is the same, and just as silent.
"""
import json
import os
import re
import time

from google import genai
from google.genai import errors as genai_errors

# gemini-flash (not -lite) by default: summarising a full consultation is a
# heavier reasoning task than intake's one-field-at-a-time extraction, and the
# note is read by a clinician, so accuracy earns the larger model. Overridable
# for cost/quota via env, same knob style as bot.py.
SUMMARY_MODEL = os.getenv("GOOGLE_SUMMARY_MODEL", "gemini-flash-latest")

# Flash-latest throws transient 503s ("high demand") in bursts that can outlast a
# handful of retries and lose a finished consultation. flash-lite is a lighter,
# far-more-available model (it's what the intake bot runs on) — fall back to it
# so a busy primary degrades to a slightly simpler note instead of no note.
FALLBACK_MODEL = os.getenv("GOOGLE_SUMMARY_FALLBACK_MODEL", "gemini-flash-lite-latest")

# The exact shape scribe_bot.py stores and ConsultationScribe.tsx renders. Kept
# here as the single source of truth; the prompt reproduces it verbatim so the
# model's keys can never drift from the reader's expectations.
_SCHEMA = """{
  "chief_complaint": "string — the main reason for the visit, one line",
  "history_of_present_illness": "string — a clinical narrative paragraph of how the problem developed (onset, duration, character, aggravating/relieving factors, associated symptoms), in the third person",
  "symptoms": ["string — each symptom the patient reported"],
  "vitals_and_exam": ["string — any measurements or examination findings stated aloud (e.g. 'BP 140/90', 'chest clear')"],
  "past_history": ["string — pre-existing conditions, past surgeries, allergies, family history mentioned"],
  "current_medications": ["string — medicines the patient is already taking, with dose if stated"],
  "assessment": ["string — the doctor's provisional diagnosis or clinical impression(s)"],
  "investigations_ordered": ["string — tests, scans, or labs the doctor ordered"],
  "medications_prescribed": [
    {"name": "string", "dosage": "string or empty", "frequency": "string or empty", "duration": "string or empty"}
  ],
  "advice_and_plan": ["string — lifestyle advice, procedures, referrals, and management steps"],
  "follow_up": "string — when/whether to return, or empty if not discussed",
  "red_flags": ["string — warning signs the patient was told to watch for and return urgently"],
  "patient_summary": "string — a short, warm, plain-language recap the patient could read to understand what was said and what to do next"
}"""

_SYSTEM = f"""You are a clinical scribe. You are given the raw, unlabelled \
speech-to-text transcript of a single in-person consultation between a doctor \
and a patient (and occasionally a caregiver). The transcript has no speaker \
labels — infer from context who is speaking, but never state a role you are \
not sure of.

Produce ONE JSON object with exactly these keys and no others:

{_SCHEMA}

Hard rules:
- Record ONLY what is actually present in the transcript. Never invent a \
symptom, diagnosis, dose, or instruction that was not spoken. Fabrication in a \
medical note is the worst possible failure.
- If something was not discussed, use an empty string "" or an empty array [] \
— do not write "unknown" or guess.
- Do not give any medical opinion of your own. You are transcribing structure, \
not diagnosing. The `assessment` field is the DOCTOR's stated impression, not \
yours.
- Speech-to-text makes errors, especially on drug names and numbers. If a \
value is clearly garbled, keep your best faithful reading and do not \
"correct" it into something that was never said.
- Output raw JSON only. No markdown, no code fences, no commentary before or \
after."""


def _extract_json(text: str) -> dict:
    """Parse the model's reply into a dict, tolerating stray fences/prose.

    response_mime_type asks for bare JSON, but a belt-and-braces parse keeps a
    single formatting slip from turning a good summary into a 500.
    """
    text = text.strip()
    # Strip a ```json ... ``` fence if the model added one anyway.
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)

    # raw_decode parses the first complete JSON value and ignores anything after
    # it — so a stray trailing newline, a second object, or a word of commentary
    # the model appended can't turn a good summary into a 500. Start from the
    # first "{" to skip any preamble.
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in model reply")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


def summarize(transcript: list[dict]) -> tuple[dict, str]:
    """Summarise an ordered list of {text, ...} utterance segments.

    Returns (note, model_used) — the structured note matching _SCHEMA, and which
    model actually produced it (primary or fallback), so the stored record is
    honest about that. Raises on an empty transcript, an unparseable reply, or
    every model being unavailable — the caller turns those into a clean HTTP
    error rather than storing a broken note.
    """
    segments = [str(s.get("text", "")).strip() for s in transcript]
    body = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(segments) if t)
    if not body:
        raise ValueError("empty transcript")

    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    config = {
        "system_instruction": _SYSTEM,
        "response_mime_type": "application/json",
        # Low but non-zero: the note should be faithful and stable, not
        # creative, but a hard 0 can make the model loop on odd input.
        "temperature": 0.2,
    }
    contents = f"Transcript (one numbered utterance per line):\n\n{body}"

    # Two transient failure modes, handled differently:
    #  - 503 ServerError ("high demand"): retry the same model a couple of times
    #    with a short backoff.
    #  - 429 ClientError (RESOURCE_EXHAUSTED): the free tier caps each model at
    #    ~20 requests/day, and that quota is PER MODEL — so retrying the same
    #    model is pointless, but the fallback model has its own separate quota.
    #    Skip straight to it.
    # Any other 4xx (bad key, bad request) is a real error and surfaces at once.
    models = [SUMMARY_MODEL]
    if FALLBACK_MODEL and FALLBACK_MODEL != SUMMARY_MODEL:
        models.append(FALLBACK_MODEL)

    last_exc: Exception | None = None
    for model in models:
        for attempt in range(2):
            try:
                resp = client.models.generate_content(
                    model=model, contents=contents, config=config
                )
                return _extract_json(resp.text or ""), model
            except genai_errors.ClientError as exc:
                if getattr(exc, "code", None) == 429:
                    last_exc = exc
                    break  # daily quota for this model is gone — try the next model
                raise
            except genai_errors.ServerError as exc:
                last_exc = exc
                time.sleep(1.0 + attempt)  # 1s, then 2s, before giving up on this model
    raise last_exc  # type: ignore[misc]
