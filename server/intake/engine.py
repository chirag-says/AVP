"""IntakeEngine: the state machine the LLM operates through.

Design: the LLM never writes JSON directly and never decides when intake is
complete. It calls tools (save/confirm/flag/skip/finalize); this engine
validates, normalizes, tracks each field's state, and is the sole authority on
completeness. That split is what guarantees no half-filled or unconfirmed
record ever reaches the database.

Every field moves through explicit states, so the assistant is steered by what
the patient actually confirmed rather than by whether a value happens to be
non-empty:

    UNASKED -> ASKED -> ANSWERED ------------------------------> (complete)
                  \\         \\-> NEEDS_CONFIRMATION -> CONFIRMED -> (complete)
                   \\-> SKIPPED (optional field declined, or "declined")

NEEDS_CONFIRMATION values are held aside ("pending") and never written into
the record until the patient clarifies them.
"""
import json
import re
from datetime import datetime, timezone
from enum import StrEnum

from .schema import FIELDS_BY_KEY, INTAKE_FIELDS, REQUIRED_KEYS, IntakeField

_LIST_FIELDS = {
    "visit.symptoms",
    "medical_history.existing_conditions",
    "medical_history.current_medications",
}
# Fields where "no / none / nothing" is a complete, valid answer.
_NEGATABLE = {
    "medical_history.allergies",
    "medical_history.existing_conditions",
    "medical_history.current_medications",
}
NONE = "none"

# The sentinel the system prompt tells the LLM to save when a patient refuses a
# required detail. It bypasses normalize/validate — otherwise the two validated
# fields (age, phone) are the only ones a patient can never decline.
DECLINED = "declined"


class FieldStatus(StrEnum):
    UNASKED = "unasked"
    ASKED = "asked"
    ANSWERED = "answered"
    NEEDS_CONFIRMATION = "needs_confirmation"
    CONFIRMED = "confirmed"
    SKIPPED = "skipped"


_COMPLETE = {FieldStatus.ANSWERED, FieldStatus.CONFIRMED, FieldStatus.SKIPPED}

_DIGIT_WORDS = {
    "zero": "0", "nought": "0", "oh": "0", "o": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_REPEAT_WORDS = {"double": 2, "triple": 3, "treble": 3}


def _normalize_phone(raw: str) -> str:
    """Reduce a spoken mobile number to bare digits.

    Handles what STT actually produces for a dictated number: digit words
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

# --- negative / ambiguous answers for allergies, conditions, medications ------

_NEG_TOKENS = {"no", "none", "nothing", "nil", "nope", "na", "nah"}
# "no X" only counts as a negative when X is the category itself, so
# "No, penicillin" can never collapse to "none".
_CATEGORY = (r"(?:known |current |existing |other |regular |such )?"
             r"(?:allerg(?:y|ies)|medicines?|medications?|meds|tablets?|pills?|drugs?|"
             r"conditions?|diseases?|illness(?:es)?|problems?|health problems?)")
_NEGATIVE_PATTERNS = [re.compile(p) for p in (
    rf"^no {_CATEGORY}$",
    rf"^(?:there are |i have |i've got )?no {_CATEGORY}$",
    r"^(?:none|nothing) (?:that )?i know of$",
    r"^not that i know of$",
    rf"^(?:i am |i'm )?not aware of any(?: {_CATEGORY})?$",
    rf"^i (?:do not|don't|dont) (?:have|take|use) any(?:thing| {_CATEGORY})?$",
    r"^i (?:do not|don't|dont) take anything$",
    rf"^(?:i am |i'm )?not (?:on|taking) any(?:thing| {_CATEGORY})?$",
    rf"^(?:i am |i'm )?not on any {_CATEGORY}$",
)]
_NUMBER_ONLY = {"one", "two", "three", "four", "five", "a few", "few", "some", "many", "several"}
_AFFIRMATION_ONLY = {"yes", "yeah", "yep", "haan", "ha", "ji", "yes i do", "i do", "yes i am", "i am", "sure"}


def _plain(text: str) -> str:
    text = re.sub(r"[^\w\s'/]", " ", str(text).lower())
    return re.sub(r"\s+", " ", text).strip()


def is_negative(value) -> bool:
    """True when an allergy/condition/medication answer means "none".

    "one" is never "none": only whole negative phrases match.
    """
    if isinstance(value, list):
        # "No, none" arrives comma-split as ["No", "none"]; "No, penicillin" stays not-none.
        return bool(value) and all(is_negative(v) for v in value)
    if not isinstance(value, str):
        return False
    text = _plain(value)
    if not text:
        return False
    if all(tok in _NEG_TOKENS for tok in text.split()):
        return True
    return any(p.match(text) for p in _NEGATIVE_PATTERNS)


def ambiguity(value) -> str | None:
    """Why a negatable-field answer can't be recorded as-is, or None."""
    items = value if isinstance(value, list) else [value]
    texts = [_plain(i) for i in items if isinstance(i, str)]
    if not texts:
        return None
    if all(t in _NUMBER_ONLY or t.isdigit() for t in texts):
        return "count_only"  # "one" -> one what? (and "one" is not "none")
    if all(t in _AFFIRMATION_ONLY for t in texts):
        return "yes_only"  # "yes" -> which ones?
    return None


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


def _del_nested(data: dict, dotted_key: str) -> None:
    parts = dotted_key.split(".")
    node = data
    for part in parts[:-1]:
        node = node.get(part)
        if not isinstance(node, dict):
            return
    node.pop(parts[-1], None)


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
    if field.key in _LIST_FIELDS and isinstance(raw, dict):
        return [raw]

    raw = str(raw).strip()
    if field.key in _LIST_FIELDS:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            pass
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _symptom(item) -> dict | None:
    """One symptom as {description, duration, severity}; None if unusable."""
    if isinstance(item, str):
        item = {"description": item}
    if not isinstance(item, dict):
        return None
    desc = str(item.get("description") or item.get("name") or item.get("symptom") or "").strip()
    if not desc:
        return None

    def clean(v):
        v = None if v is None else str(v).strip()
        return v if v and v.lower() not in ("null", "none", "unknown", "n/a") else None

    return {"description": desc, "duration": clean(item.get("duration")), "severity": clean(item.get("severity"))}


def _merge_symptoms(existing: list[dict], incoming: list) -> list[dict]:
    """Merge by description: a later "five or six years" fills the duration of
    the symptom it belongs to instead of becoming a separate symptom."""
    merged = [dict(s) for s in existing]
    index = {s["description"].lower(): s for s in merged}
    for item in incoming:
        s = _symptom(item)
        if not s:
            continue
        current = index.get(s["description"].lower())
        if current is None:
            merged.append(s)
            index[s["description"].lower()] = s
        else:
            for k in ("duration", "severity"):
                if s[k]:
                    current[k] = s[k]
    return merged


class IntakeEngine:
    def __init__(self):
        self.data: dict = {}
        self.flags: list[dict] = []
        self.completed = False
        self.ready = False  # finalize passed its readiness check
        self.frozen = False
        self.status: dict[str, FieldStatus] = {f.key: FieldStatus.UNASKED for f in INTAKE_FIELDS}
        self.pending: dict[str, dict] = {}  # key -> {"value", "reason"} while NEEDS_CONFIRMATION
        self.language = None  # the session's LanguageState (language_follow.py), if any

    # --- tools the LLM calls (via intake/session.py) -------------------------

    def save_field(self, key: str, value: str) -> dict:
        """Record one answer. Negative answers ("no", "none", "I don't take
        anything") become "none"; ambiguous or contradictory ones are held for
        confirmation instead of being stored."""
        field, err = self._writable(key)
        if err:
            return err
        prep = self._prepare(field, value)
        if "error" in prep:
            return prep
        new = prep["value"]
        status = self.status[field.key]

        if prep.get("ambiguous"):
            return self._hold(field, new, prep["ambiguous"])

        if field.key in _NEGATABLE:
            old = self._current_or_pending(field.key)
            if old is not None and new != DECLINED and is_negative(old) != is_negative(new):
                # "one" then "none", or "none" then "penicillin": never pick silently.
                return self._hold(field, new, "contradiction", previous=old)

        if status == FieldStatus.CONFIRMED and new != self.get(field.key) and field.key != "visit.symptoms":
            return self._hold(field, new, "changes_confirmed", previous=self.get(field.key))
        if status == FieldStatus.NEEDS_CONFIRMATION:
            # A new answer while one is being clarified: confirm the new one too.
            return self._hold(field, new, "changed_while_confirming", previous=self.pending[field.key]["value"])

        old = self.get(field.key)
        if field.key in _LIST_FIELDS and field.key != "visit.symptoms" and isinstance(old, list) and isinstance(new, list):
            # "Metformin" then "also amlodipine": add, never silently drop the first.
            seen = {str(v).lower() for v in old}
            new = old + [v for v in new if str(v).lower() not in seen]
        return self._store(field, new, FieldStatus.SKIPPED if new == DECLINED else FieldStatus.ANSWERED)

    def confirm_field(self, key: str, value: str) -> dict:
        """The patient explicitly confirmed or clarified this answer."""
        field, err = self._writable(key)
        if err:
            return err
        prep = self._prepare(field, value)
        if "error" in prep:
            return prep
        if prep.get("ambiguous"):
            return self._hold(field, prep["value"], prep["ambiguous"])
        return self._store(field, prep["value"], FieldStatus.CONFIRMED)

    def flag_for_confirmation(self, key: str, value: str, reason: str = "") -> dict:
        """Hold an unusual, joking-sounding, garbled or low-confidence answer
        until the patient confirms it. Nothing is written to the record."""
        field, err = self._writable(key)
        if err:
            return err
        prep = self._prepare(field, value)
        if "error" in prep:
            return prep
        return self._hold(field, prep["value"], "unusual", note=reason)

    def skip_field(self, key: str) -> dict:
        """An optional field the patient does not want to (or cannot) answer."""
        field, err = self._writable(key)
        if err:
            return err
        if field.required:
            return {"error": f"{field.label} is required. If the patient refuses after you explain why "
                             f"it is needed, save the value '{DECLINED}'."}
        self.pending.pop(field.key, None)
        _del_nested(self.data, field.key)
        self.status[field.key] = FieldStatus.SKIPPED
        return self._ok(field, "Skipped. Do not ask about it again.")

    # --- state ------------------------------------------------------------------

    def mark_asked(self, key: str) -> None:
        if self.status.get(key) == FieldStatus.UNASKED:
            self.status[key] = FieldStatus.ASKED

    def next_field(self) -> IntakeField | None:
        """First field not yet complete, needs-confirmation first."""
        for f in INTAKE_FIELDS:
            if self.status[f.key] == FieldStatus.NEEDS_CONFIRMATION:
                return f
        for f in INTAKE_FIELDS:
            if self.status[f.key] not in _COMPLETE:
                return f
        return None

    def incomplete(self) -> dict:
        """What still blocks finalize: unconfirmed, missing required, unanswered optional."""
        needs = [k for k, s in self.status.items() if s == FieldStatus.NEEDS_CONFIRMATION]
        missing = [k for k in REQUIRED_KEYS if self.status[k] not in _COMPLETE]
        optional = [f.key for f in INTAKE_FIELDS if not f.required and self.status[f.key] not in _COMPLETE]
        return {"needs_confirmation": needs, "missing_required": missing, "unanswered_optional": optional}

    def missing_required(self) -> list[str]:
        return self.incomplete()["missing_required"]

    def finalize(self) -> dict:
        """Check readiness. Does not mark the intake complete: the session does
        that only after the record is safely in the database."""
        if self.frozen:
            return {"ok": True}
        blockers = self.incomplete()
        problems = []
        if blockers["needs_confirmation"]:
            problems.append("confirm first: " + ", ".join(FIELDS_BY_KEY[k].label for k in blockers["needs_confirmation"]))
        if blockers["missing_required"]:
            problems.append("still missing: " + ", ".join(FIELDS_BY_KEY[k].label for k in blockers["missing_required"]))
        if blockers["unanswered_optional"]:
            problems.append("ask (patient may say none or skip): "
                            + ", ".join(FIELDS_BY_KEY[k].label for k in blockers["unanswered_optional"]))
        if problems:
            return {"error": "; ".join(problems), "missing": blockers["missing_required"], **blockers}
        self.ready = True
        return {"ok": True}

    def freeze(self) -> dict:
        """Lock the record against further edits and return it."""
        self.frozen = True
        return self.to_record()

    def get(self, key: str):
        return _get_nested(self.data, key)

    def as_tools(self) -> list:
        """The engine's tools as plain methods (used by chat_cli's text mode)."""
        return [self.save_field, self.confirm_field, self.flag_for_confirmation, self.skip_field, self.finalize]

    def flag(self, key: str, reason: str) -> None:
        self.flags.append({"field": key, "reason": reason})

    def to_record(self) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        # Values are always English (prompts.py); these say what the patient heard.
        start = self.language.start.code if self.language else "en-IN"
        used = list(self.language.used) if self.language else [start]
        return {
            **json.loads(json.dumps(self.data)),  # detached copy
            "meta": {
                "language": start,
                "languages_used": used,
                "completed": self.completed,
                "flags": self.flags,
                "captured_at": now,
                "completed_at": now if self.completed else None,
                "field_status": {k: str(v) for k, v in self.status.items()},
            },
        }

    # --- internals ------------------------------------------------------------------

    def _writable(self, key: str):
        if self.frozen:
            return None, {"error": "This intake is already complete and saved. Do not change anything."}
        field = _resolve_field(key)
        if field is None:
            return None, {"error": f"unknown field '{key}'. Valid field keys are exactly: {', '.join(FIELDS_BY_KEY)}"}
        return field, None

    def _prepare(self, field: IntakeField, value) -> dict:
        try:
            coerced = _coerce_value(field, value)
        except Exception:
            return {"error": f"could not understand that value for {field.label}. "
                             f"Ask the patient to repeat it, one detail at a time."}
        if isinstance(coerced, str) and coerced.lower() == DECLINED:
            return {"value": DECLINED}
        if field.key in _NEGATABLE:
            if is_negative(coerced):
                return {"value": NONE}
            why = ambiguity(coerced)
            if why:
                return {"value": coerced, "ambiguous": why}
        if field.key == "visit.symptoms":
            symptoms = [s for s in (_symptom(i) for i in coerced) if s]
            if not symptoms:
                return {"error": "no symptom description in that answer. Ask what the problem is."}
            return {"value": symptoms}
        if field.normalize:
            normalized = _NORMALIZERS[field.normalize](coerced)
            # An empty result means nothing usable was in there; keep the raw
            # value so the error below quotes what the patient actually said.
            coerced = normalized or coerced
        if field.validate and field.key not in _LIST_FIELDS and not re.match(field.validate, str(coerced)):
            return {"error": f"'{value}' is not a valid {field.label}. Ask the patient to repeat it clearly."}
        if isinstance(coerced, str) and not coerced:
            return {"error": f"empty answer for {field.label}. Ask again."}
        return {"value": coerced}

    def _current_or_pending(self, key: str):
        if key in self.pending:
            return self.pending[key]["value"]
        return self.get(key)

    def _store(self, field: IntakeField, value, status: FieldStatus) -> dict:
        if field.key == "visit.symptoms" and isinstance(value, list):
            value = _merge_symptoms(self.get(field.key) or [], value)
        self.pending.pop(field.key, None)
        _set_nested(self.data, field.key, value)
        self.status[field.key] = status
        note = None
        if value == NONE:
            note = f"Recorded: no {field.label}. This answer is complete; never ask about other {field.label}."
        elif value == DECLINED:
            note = "Recorded as declined. Do not ask about it again."
        return self._ok(field, note)

    def _hold(self, field: IntakeField, value, reason: str, previous=None, note: str = "") -> dict:
        self.pending[field.key] = {"value": value, "reason": reason}
        self.status[field.key] = FieldStatus.NEEDS_CONFIRMATION
        label = field.label
        ask = {
            "count_only": f"The patient gave a number, not the name of any {label} (and 'one' is not 'none'). Ask, for example: "
                          f"'Just to confirm, are you taking one medication? Which one?'",
            "yes_only": f"The patient said yes but did not name the {label}. Ask which ones.",
            "contradiction": f"The patient first said '{_short(previous)}' and now '{_short(value)}'. Ask which is "
                             f"correct. Do not pick one yourself.",
            "changes_confirmed": f"This changes an already confirmed answer ('{_short(previous)}' to "
                                 f"'{_short(value)}'). Ask the patient to confirm the change.",
            "changed_while_confirming": f"While confirming '{_short(previous)}', the patient said "
                                        f"'{_short(value)}'. Confirm this new answer with them.",
            "unusual": f"Ask the patient to confirm '{_short(value)}' for {label} without judging it, e.g. whether "
                       f"they meant it seriously. {note}".strip(),
        }[reason]
        return {"ok": False, "status": str(FieldStatus.NEEDS_CONFIRMATION), "field": field.key,
                "not_saved": True, "instruction": ask + " After they answer, call confirm_field."}

    def _ok(self, field: IntakeField, note: str | None) -> dict:
        result = {"ok": True, "saved": field.key, "status": str(self.status[field.key])}
        if field.key == "visit.symptoms":
            gaps = [s["description"] for s in self.get(field.key) or [] if not s["duration"]]
            if gaps:
                result["ask_duration_for"] = gaps
        if note:
            result["note"] = note
        nxt = self.next_field()
        if nxt:
            self.mark_asked(nxt.key)
            result["next"] = nxt.label
        else:
            result["next"] = "all fields done: give a brief summary, then call finalize"
        return result


def _short(value) -> str:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        text = ", ".join(value)
    else:
        text = value if isinstance(value, str) else json.dumps(value)
    return text if len(text) <= 60 else text[:57] + "..."
