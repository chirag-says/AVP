"""Single source of truth for the intake questionnaire.

Add or change a field here and it automatically flows into: the system prompt,
the completeness check, server-side validation, and the live form on the
React client (via the /schema endpoint). Do not duplicate field lists anywhere else.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class IntakeField:
    key: str  # dot-path into the JSON record, e.g. "personal.full_name"
    label: str  # human name, used in prompts and read-back
    required: bool = True
    validate: str | None = None  # regex; None means no format constraint
    normalize: str | None = None  # named normalizer in engine._NORMALIZERS, run before validate
    confirm: str = "none"  # none | readback | digits | spell_on_retry
    hint: str = ""  # extra instruction for the LLM


INTAKE_FIELDS: list[IntakeField] = [
    IntakeField(
        key="personal.full_name",
        label="full name",
        confirm="spell_on_retry",
        hint="Ask for their full name first, before anything else.",
    ),
    IntakeField(
        key="personal.age",
        label="age",
        validate=r"^(?:[1-9]|[1-9][0-9]|1[01][0-9]|120)$",
        hint="A number between 1 and 120.",
    ),
    IntakeField(
        key="personal.gender",
        label="gender",
        hint="Accept male/female/other in any phrasing; store lowercase.",
    ),
    IntakeField(
        key="personal.phone",
        label="mobile number",
        validate=r"^[6-9]\d{9}$",
        # Speech never arrives regex-clean: whisper writes a spoken number as
        # "98765 43210", "+91 98765 43210", or "nine eight seven ...". Without
        # this the regex rejects even a word-perfect transcription.
        normalize="phone",
        confirm="digits",
        hint="Indian 10-digit mobile number starting with 6-9. Read it back digit by digit.",
    ),
    IntakeField(
        key="personal.address",
        label="address",
        confirm="readback",
    ),
    IntakeField(
        key="visit.symptoms",
        label="symptoms",
        hint=(
            "A list of symptoms. For each one, capture description, duration, and "
            "severity (mild/moderate/high). Ask follow-up questions until duration "
            "is known for every symptom mentioned."
        ),
    ),
    IntakeField(
        key="medical_history.allergies",
        label="allergies",
        hint=(
            "Safety-critical field. If the patient has none, still call save_field "
            "with the literal value 'none' — never leave this unset."
        ),
    ),
    IntakeField(
        key="medical_history.existing_conditions",
        label="existing medical conditions",
        required=False,
    ),
    IntakeField(
        key="medical_history.current_medications",
        label="current medications",
        required=False,
    ),
]

FIELDS_BY_KEY: dict[str, IntakeField] = {f.key: f for f in INTAKE_FIELDS}
REQUIRED_KEYS: list[str] = [f.key for f in INTAKE_FIELDS if f.required]
