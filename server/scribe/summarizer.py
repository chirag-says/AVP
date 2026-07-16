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


def summarize(transcript: list[dict]) -> dict:
    """Summarise an ordered list of {text, ...} utterance segments.

    Returns the structured note as a dict matching _SCHEMA. Raises on an empty
    transcript or an unparseable model reply — the caller turns those into a
    clean HTTP error rather than storing a broken note.
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

    # The flash models throw a transient 503 ("high demand") often enough that a
    # single attempt would drop a finished consultation on the floor. Retry the
    # server-side failures a few times with a short backoff; a 4xx (bad key,
    # bad request) is not retryable and surfaces immediately.
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model=SUMMARY_MODEL, contents=contents, config=config
            )
            return _extract_json(resp.text or "")
        except genai_errors.ServerError as exc:
            last_exc = exc
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc  # type: ignore[misc]
