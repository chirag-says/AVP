"""Speech-recognition vocabulary (Sarvam `keyterms`) for the intake bot.

The term list is data, not code: server/voice/vocabulary.json holds generic
medical terms, and an optional site file (STT_VOCABULARY_EXTRA_FILE) holds
hospital-specific names. Site terms are taken first because they are the ones
no general-purpose model has ever heard.
"""
import json
import pathlib

from loguru import logger

DEFAULT_VOCABULARY_FILE = pathlib.Path(__file__).resolve().parent / "vocabulary.json"

# Sarvam documents 50 keyterms of at most 64 characters each for saaras:v4.
MAX_TERM_CHARS = 64


def _terms_from_file(path: pathlib.Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):  # a site file may just be a flat list
        return [str(t) for t in data]
    terms: list[str] = []
    for category_terms in data.get("categories", {}).values():
        terms.extend(str(t) for t in category_terms)
    return terms


def load_keyterms(
    limit: int,
    extra_file: str | None = None,
    base_file: pathlib.Path = DEFAULT_VOCABULARY_FILE,
) -> list[str]:
    """Return up to `limit` unique keyterms, site-specific terms first.

    A missing or malformed file is logged and skipped rather than raised: a
    broken vocabulary must never stop the voice bot from starting.
    """
    sources = [pathlib.Path(extra_file)] if extra_file else []
    sources.append(base_file)

    seen: set[str] = set()
    keyterms: list[str] = []
    for path in sources:
        try:
            terms = _terms_from_file(path)
        except (OSError, ValueError) as e:
            logger.warning(f"Skipping STT vocabulary file {path.name}: {e}")
            continue
        for term in terms:
            term = term.strip()
            key = term.lower()
            if not term or len(term) > MAX_TERM_CHARS or key in seen:
                continue
            seen.add(key)
            keyterms.append(term)
            if len(keyterms) >= limit:
                return keyterms
    return keyterms
