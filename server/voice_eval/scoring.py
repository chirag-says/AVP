"""Transcript scoring for voice_eval: WER, medical-term hits, phone accuracy.

Numbers are compared digit by digit so "forty two", "42" and "4 2" all score
the same: formatting is not what this harness measures, recognition is.
"""
import re

_UNITS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_ORDINALS = {
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10",
    "eleventh": "11", "twelfth": "12", "thirteenth": "13", "fourteenth": "14",
    "fifteenth": "15", "sixteenth": "16", "seventeenth": "17", "eighteenth": "18",
    "nineteenth": "19", "twentieth": "20", "thirtieth": "30",
}


def _words_to_numbers(tokens: list[str]) -> list[str]:
    """Collapse runs of number words into digit strings ("five hundred" -> "500")."""
    out: list[str] = []
    current: int | None = None  # number being built from the current run
    last_kind = ""  # "unit" | "tens" | "hundred" for deciding group boundaries

    def flush():
        nonlocal current, last_kind
        if current is not None:
            out.append(str(current))
        current, last_kind = None, ""

    for tok in tokens:
        if tok in _UNITS:
            value = _UNITS[tok]
            if current is not None and last_kind == "tens" and value < 10:
                current += value  # "forty two"
            elif current is not None and last_kind == "hundred":
                current += value  # "five hundred twelve"
            else:
                flush()  # "nine eight" is two digits, not 17
                current = value
            last_kind = "unit"
        elif tok in _TENS:
            if current is not None and last_kind == "hundred":
                current += _TENS[tok]
            else:
                flush()  # "nineteen eighty" -> 19, 80
                current = _TENS[tok]
            last_kind = "tens"
        elif tok == "hundred" and current is not None:
            current *= 100
            last_kind = "hundred"
        elif tok == "thousand" and current is not None:
            current *= 1000  # "two thousand four" -> 2004
            last_kind = "hundred"
        else:
            flush()
            out.append(_ORDINALS.get(tok, tok))
    flush()
    return out


def number_tokens(text: str | None) -> list[str]:
    """Lowercased word tokens with spoken numbers collapsed, NOT split into digits."""
    if not text:
        return []
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text.lower().replace("-", " "))
    text = re.sub(r"(\d)[,](\d{3})\b", r"\1\2", text)  # "2,004" -> "2004"
    return _words_to_numbers([t.replace("'", "") for t in re.findall(r"[a-z0-9']+", text)])


def canon(text: str | None) -> list[str]:
    """Lowercase, strip punctuation, numbers as single digits."""
    if not text:
        return []
    text = text.lower().replace("-", " ")
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text)
    tokens = re.findall(r"[a-z0-9']+", text)
    tokens = [t.replace("'", "") for t in tokens]
    tokens = _words_to_numbers(tokens)
    digits_split: list[str] = []
    for tok in tokens:
        if tok.isdigit():
            digits_split.extend(tok)  # "1982" -> 1 9 8 2
        else:
            digits_split.append(tok)
    return digits_split


def word_errors(ref: list[str], hyp: list[str]) -> int:
    """Levenshtein distance over tokens (substitutions + insertions + deletions)."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1]


def contains_phrase(hyp: list[str], phrase: str) -> bool:
    needle = canon(phrase)
    n = len(needle)
    return any(hyp[i : i + n] == needle for i in range(len(hyp) - n + 1))


def leaked_words(hyp: list[str], ref: list[str], intruder_text: str) -> int:
    """Count hypothesis words that come from an intruding voice (bot or bystander).

    Only words distinctive to the intruder (absent from the reference, longer
    than 3 letters) count, so shared filler like "the" can't inflate leakage.
    """
    ref_set = set(ref)
    intruder = {w for w in canon(intruder_text) if w not in ref_set and len(w) > 3}
    return sum(1 for w in hyp if w in intruder)
