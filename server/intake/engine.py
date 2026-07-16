"""IntakeEngine: the state machine the LLM operates through.

Design: the LLM never writes JSON directly and never decides when intake is
complete. It calls save_field()/finalize() as tools; this engine validates,
coerces, and is the sole authority on completeness. That split is what
guarantees no half-filled record ever reaches the database.
"""
import json
import re
from datetime import datetime, timezone

from .schema import FIELDS_BY_KEY, REQUIRED_KEYS, IntakeField

_LIST_FIELDS = {
    "visit.symptoms",
    "medical_history.existing_conditions",
    "medical_history.current_medications",
}

# The sentinel the system prompt tells the LLM to save when a patient refuses a
# required detail. It bypasses normalize/validate — otherwise the two validated
# fields (age, phone) are the only ones a patient can never decline.
DECLINED = "declined"

_DIGIT_WORDS = {
    "zero": "0", "nought": "0", "oh": "0", "o": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_REPEAT_WORDS = {"double": 2, "triple": 3, "treble": 3}


def _normalize_phone(raw: str) -> str:
    """Reduce a spoken mobile number to bare digits.

    Handles what whisper actually produces for a dictated number: digit words
    ("nine eight seven"), the Indian "double five" contraction, separators of
    every kind, and a +91/91/0 prefix. Scoped to the phone field by
    IntakeField.normalize because "oh" -> 0 is only safe among digits.
    """
    digits: list[str] = []
    repeat = 1
    for token in re.findall(r"[a-z]+|\d+", str(raw).lower()):
        if token in _REPEAT_WORDS:
            repeat = _REPEAT_WORDS[token]
            continue
        if token in _DIGIT_WORDS:
            digits.append(_DIGIT_WORDS[token] * repeat)
        elif token.isdigit():
            # "double 5" -> 55, but a multi-digit run is already literal.
            digits.append(token * repeat if len(token) == 1 else token)
        else:
            continue  # stray word ("my", "number", "is") — repeat still pending
        repeat = 1

    number = "".join(digits)
    if len(number) == 12 and number.startswith("91"):
        number = number[2:]  # +91 98765 43210
    elif len(number) == 11 and number.startswith("0"):
        number = number[1:]  # STD trunk prefix
    return number


_NORMALIZERS = {"phone": _normalize_phone}


def _get_nested(data: dict, dotted_key: str):
    node = data
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_nested(data: dict, dotted_key: str, value) -> None:
    parts = dotted_key.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _resolve_field(raw_key: str) -> IntakeField | None:
    field = FIELDS_BY_KEY.get(raw_key)
    if field:
        return field
    # Models (especially smaller ones) sometimes drop the section prefix,
    # e.g. "symptoms" instead of "visit.symptoms" — forgive that rather than
    # silently failing every save on that field for the rest of the call.
    suffix = raw_key.strip().lower()
    matches = [f for f in FIELDS_BY_KEY.values() if f.key.rsplit(".", 1)[-1] == suffix]
    return matches[0] if len(matches) == 1 else None


def _coerce_value(field: IntakeField, raw):
    # Defensive: the model's function-call schema advertises `value` as a
    # string, but a weaker model occasionally sends a real list/object
    # instead of a JSON-encoded string. Handle both rather than crashing the
    # tool call — an unhandled exception here surfaces to the model as an
    # opaque error and derails the conversation (observed with symptoms).
    if field.key in _LIST_FIELDS and isinstance(raw, list):
        return raw

    raw = str(raw).strip()
    if field.key in _LIST_FIELDS:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


class IntakeEngine:
    def __init__(self):
        self.data: dict = {}
        self.flags: list[dict] = []
        self.completed = False

    def save_field(self, key: str, value: str) -> dict:
        """Record one piece of patient information.

        Args:
            key: The field's dot-path, e.g. "personal.full_name" or "personal.phone".
                Must be one of the known intake fields.
            value: The value to store, as the patient stated it. For list fields
                (symptoms, existing conditions, current medications), pass a JSON
                array as a string, e.g. '["fever", "headache"]'.

        Returns:
            {"ok": true, "saved": key} on success, or {"error": "..."} if the
            key is unknown or the value fails validation — in which case, ask
            the patient to repeat or clarify and try again.
        """
        field = _resolve_field(key)
        if field is None:
            return {
                "error": (
                    f"unknown field '{key}'. Valid field keys are exactly: "
                    f"{', '.join(FIELDS_BY_KEY)}"
                )
            }

        try:
            coerced = _coerce_value(field, value)
        except Exception:
            return {
                "error": (
                    f"could not understand that value for {field.label}. "
                    f"Ask the patient to repeat it, one detail at a time."
                )
            }

        if isinstance(coerced, str) and coerced.lower() == DECLINED:
            _set_nested(self.data, field.key, DECLINED)
            return {"ok": True, "saved": field.key}

        if field.normalize:
            normalized = _NORMALIZERS[field.normalize](coerced)
            # An empty result means nothing usable was in there; keep the raw
            # value so the error below quotes what the patient actually said.
            coerced = normalized or coerced

        if field.validate and field.key not in _LIST_FIELDS:
            if not re.match(field.validate, str(coerced)):
                return {
                    "error": (
                        f"'{value}' is not a valid {field.label}. "
                        f"Ask the patient to repeat it clearly."
                    )
                }

        _set_nested(self.data, field.key, coerced)
        return {"ok": True, "saved": field.key}

    def missing_required(self) -> list[str]:
        missing = []
        for key in REQUIRED_KEYS:
            value = _get_nested(self.data, key)
            if value in (None, "", [], {}):
                missing.append(key)
        return missing

    def finalize(self) -> dict:
        """Attempt to close out the intake session.

        Returns:
            {"ok": true} if every required field has been saved — the
            conversation should end here. Otherwise {"error": "...", "missing":
            [...]} — keep asking about the missing fields and do not tell the
            patient the intake is complete.
        """
        missing = self.missing_required()
        if missing:
            labels = [FIELDS_BY_KEY[k].label for k in missing]
            return {"error": f"still missing: {', '.join(labels)}", "missing": missing}
        self.completed = True
        return {"ok": True}

    def get(self, key: str):
        return _get_nested(self.data, key)

    def flag(self, key: str, reason: str) -> None:
        self.flags.append({"field": key, "reason": reason})

    def as_tools(self) -> list:
        return [self.save_field, self.finalize]

    def to_record(self, language: str = "en") -> dict:
        return {
            **self.data,
            "meta": {
                "language": language,
                "completed": self.completed,
                "flags": self.flags,
                "captured_at": datetime.now(timezone.utc).isoformat(),
            },
        }
