r"""Multilingual intake (phase 1): language table, reply-language following, session lines.

    .venv\Scripts\python.exe server\tests\test_languages.py

Local only: no network, no Sarvam, no Gemini, no Supabase.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger  # noqa: E402

logger.remove()

from pipecat.frames.frames import (  # noqa: E402
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.tests.utils import SleepFrame, run_test  # noqa: E402
from pipecat.transcriptions.language import Language  # noqa: E402

from intake.language_follow import LanguageState, ReplyLanguageFollower  # noqa: E402
from intake.languages import ENGLISH, LANGUAGES, language_of, resolve_language, script_of  # noqa: E402
from intake.prompts import build_system_prompt  # noqa: E402
from intake.session import CLOSING, GREETING, IntakeSession, Persistence  # noqa: E402
from tests.test_intake_session import FakeDB, FakeLLM, FakeParams, fill  # noqa: E402
from voice.echo_guard import _words  # noqa: E402

KN, HI = LANGUAGES["kn-IN"], LANGUAGES["hi-IN"]
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))


def table_tests():
    check("table: English plus Kannada, Hindi, Tamil, Telugu",
          sorted(LANGUAGES) == ["en-IN", "hi-IN", "kn-IN", "ta-IN", "te-IN"], sorted(LANGUAGES))
    for lang in LANGUAGES.values():
        lines = [lang.greeting, lang.closing, lang.save_problem, lang.repeat, lang.say_again]
        # Every fixed line is in its own script, so the voice speaks it in that language.
        wrong = [line for line in lines if language_of(line) is not lang]
        check(f"table {lang.code}: every fixed line is written in {lang.script} script", not wrong, wrong)
        check(f"table {lang.code}: closing has the name slot", "{name}" in lang.closing)
        check(f"table {lang.code}: a code Pipecat and Sarvam know", Language(lang.code).value == lang.code)
    check("English lines unchanged", GREETING.startswith("Welcome to AVP Hospital.") and CLOSING == ENGLISH.closing)


def resolve_tests():
    check("resolve: known code", resolve_language("kn-IN") is KN)
    check("resolve: unknown code falls back to English", resolve_language("fr-FR") is ENGLISH)
    check("resolve: missing or non-string falls back to English",
          resolve_language(None) is ENGLISH and resolve_language(["kn-IN"]) is ENGLISH and resolve_language({}) is ENGLISH)


def script_tests():
    check("script: English", script_of("How old are you?") == "Latin")
    check("script: Kannada sentence starting with English letters is Kannada",
          language_of("AVP ಆಸ್ಪತ್ರೆಗೆ ಸ್ವಾಗತ, Ravi.") is KN)
    check("script: Hindi with a danda", language_of("आपकी उम्र क्या है।") is HI)
    check("script: digits and punctuation only have no language", language_of("9876 5432 10.") is None)


def prompt_tests():
    en, kn = build_system_prompt(), build_system_prompt(language=KN)
    check("prompt: English sessions start in English", "The greeting was in English," in en)
    check("prompt: Kannada sessions start in Kannada", "The greeting was in Kannada," in kn and "Keep speaking\n  Kannada." in kn)
    check("prompt: an English answer alone doesn't switch the language",
          "are NOT a reason to switch" in kn and "two questions in a row" in kn)
    check("prompt: tool values are always English", "Tool values are ALWAYS in English" in kn)
    check("prompt: native script required", "Kannada in Kannada script" in kn and "Hindi in Devanagari script" in kn)
    check("prompt: names transliterated, never translated", "Never translate, correct or complete them" in kn)


def echo_tests():
    words = _words(KN.greeting)
    check("echo guard: Kannada words are seen (the bot's echo can be recognized)", len(words) >= 10, words)
    check("echo guard: English tokenization unchanged", _words("Hello, AVP! I'm here.") == ["hello", "avp", "i'm", "here"])
    check("echo guard: danda is punctuation, not part of a word", "है" in _words("नाम क्या है।"))


async def follow(start, frames):
    state = LanguageState(start)
    down, _ = await run_test(ReplyLanguageFollower(state), frames_to_send=frames)
    return state, down


def switches(down):
    return [f.delta.language for f in down if isinstance(f, TTSUpdateSettingsFrame)]


def texts(down):
    return "".join(f.text for f in down if isinstance(f, LLMTextFrame))


async def follower_tests():
    state, down = await follow(ENGLISH, [LLMFullResponseStartFrame(), LLMTextFrame("ನಿಮ್ಮ"), LLMTextFrame(" ವಯಸ್ಸು"),
                                         LLMTextFrame(" ಎಷ್ಟು?"), LLMFullResponseEndFrame()])
    first_text = next(i for i, f in enumerate(down) if isinstance(f, LLMTextFrame))
    first_switch = next(i for i, f in enumerate(down) if isinstance(f, TTSUpdateSettingsFrame))
    check("follow: patient switched to Kannada -> the voice switches before the sentence",
          switches(down) == [Language.KN_IN] and first_switch < first_text, switches(down))
    check("follow: text passes through whole and in order", texts(down) == "ನಿಮ್ಮ ವಯಸ್ಸು ಎಷ್ಟು?")
    check("follow: languages used recorded", state.used == ["en-IN", "kn-IN"] and state.current is KN, state.used)

    _, down = await follow(ENGLISH, [LLMTextFrame("AVP"), LLMTextFrame(" ಆಸ್ಪತ್ರೆಗೆ ಸ್ವಾಗತ.")])
    check("follow: a Kannada sentence starting with 'AVP' never flips the voice to English",
          switches(down) == [Language.KN_IN], switches(down))

    _, down = await follow(KN, [LLMTextFrame("ಧನ್ಯವಾದಗಳು."), LLMTextFrame(" ನಿಮ್ಮ ವಯಸ್ಸು?")])
    check("follow: same language -> no settings update", switches(down) == [], switches(down))

    state, down = await follow(KN, [TTSSpeakFrame(HI.say_again)])
    order = [type(f).__name__ for f in down if isinstance(f, (TTSUpdateSettingsFrame, TTSSpeakFrame))]
    check("follow: a fixed line in Hindi switches the voice first",
          order == ["TTSUpdateSettingsFrame", "TTSSpeakFrame"] and state.current is HI, order)

    _, down = await follow(ENGLISH, [LLMTextFrame("How old"), LLMTextFrame(" are you"), LLMFullResponseEndFrame()])
    end = next(i for i, f in enumerate(down) if isinstance(f, LLMFullResponseEndFrame))
    last_text = max(i for i, f in enumerate(down) if isinstance(f, LLMTextFrame))
    check("follow: unfinished sentence released before the response ends", last_text < end and switches(down) == [])

    # The pause lets the text be held first; a system frame would otherwise overtake it in the queue.
    _, down = await follow(ENGLISH, [LLMTextFrame("ನಿಮ್ಮ ವಯಸ್ಸು"), SleepFrame(sleep=0.05), InterruptionFrame(),
                                     LLMFullResponseEndFrame()])
    check("follow: an interruption drops the held text (it must not be spoken)", texts(down) == "", texts(down))


async def session_tests():
    db = FakeDB()
    sent = []

    async def send(msg):
        sent.append(msg)

    languages = LanguageState(KN)
    s = IntakeSession(send=send, persistence=Persistence(db.create_session, db.complete_session),
                      session_id=db.create_session(), get_transcript=lambda: [], language=languages)
    fill(s.engine)
    languages.switch(HI)  # the patient moved to Hindi during the intake
    llm = FakeLLM()
    await s.finalize(FakeParams(llm))
    check("session: closing spoken in the language the bot is speaking now",
          isinstance(llm.pushed[0], TTSSpeakFrame) and llm.pushed[0].text == HI.closing.format(name=", Chirag"),
          llm.pushed[0].text if llm.pushed else None)
    meta = db.rows[s.session_id]["patient"]["meta"]
    check("session: record notes the starting language and every language used",
          meta["language"] == "kn-IN" and meta["languages_used"] == ["kn-IN", "hi-IN"], meta)
    check("session: record values stay as saved (English)", db.rows[s.session_id]["patient"]["personal"]["gender"] == "male")

    db = FakeDB(fail=1)
    s = IntakeSession(send=send, persistence=Persistence(db.create_session, db.complete_session),
                      session_id=db.create_session(), get_transcript=lambda: [], language=LanguageState(KN))
    fill(s.engine)
    llm = FakeLLM()
    sent.clear()
    await s.finalize(FakeParams(llm))
    failed = next(m for m in sent if m["type"] == "intake_save_failed")
    check("session: save problem spoken in Kannada, on-screen message stays English for staff",
          llm.pushed[0].text == KN.save_problem and failed["message"] == ENGLISH.save_problem)

    s = IntakeSession(send=send, persistence=None, session_id=None, get_transcript=lambda: [])
    check("session: no language given -> English", s.language.start is ENGLISH
          and s.engine.to_record()["meta"]["languages_used"] == ["en-IN"])


def main() -> int:
    table_tests()
    resolve_tests()
    script_tests()
    prompt_tests()
    echo_tests()
    asyncio.run(follower_tests())
    asyncio.run(session_tests())
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
