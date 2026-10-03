"""The languages the intake speaks, and everything that differs between them.

One entry per language. Adding a language means adding an entry here (and its
UI strings in web/src/intake/languages.ts); nothing else branches on language.

The saved record is always English (see prompts.py), so the engine's "none"
and "one vs none" logic never needs per-language word lists. Only what the bot
says without the LLM lives here: the fixed lines below.

The non-English lines were written without a native speaker's review; have
them checked before patients hear them.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class IntakeLanguage:
    code: str      # BCP-47, as Sarvam STT/TTS expect it
    name: str      # English name, for the LLM prompt and logs
    script: str    # the script its replies are written in (see script_of)
    greeting: str
    closing: str   # {name} is ", <first name>" or ""
    save_problem: str
    repeat: str    # no transcript for a patient turn
    say_again: str  # the LLM produced no reply twice


ENGLISH = IntakeLanguage(
    code="en-IN", name="English", script="Latin",
    greeting="Welcome to AVP Hospital. I'm here to help you check in. Could you please tell me your full name?",
    closing="Thank you{name}. Your information has been saved. Please take a seat; the doctor will see you shortly.",
    save_problem="There's a problem saving your information. Please wait a moment.",
    repeat="Sorry, I didn't catch that. Could you please repeat that?",
    say_again="Sorry, could you please say that again?",
)

LANGUAGES: dict[str, IntakeLanguage] = {lang.code: lang for lang in (
    ENGLISH,
    IntakeLanguage(
        code="hi-IN", name="Hindi", script="Devanagari",
        greeting="AVP हॉस्पिटल में आपका स्वागत है। मैं चेक-इन में आपकी मदद करूँगी। कृपया अपना पूरा नाम बताइए।",
        closing="धन्यवाद{name}। आपकी जानकारी सेव हो गई है। कृपया बैठिए, डॉक्टर जल्द ही आपको देखेंगे।",
        save_problem="आपकी जानकारी सेव करने में समस्या आ रही है। कृपया थोड़ा इंतज़ार कीजिए।",
        repeat="माफ़ कीजिए, मैं ठीक से सुन नहीं पाई। कृपया फिर से बताइए।",
        say_again="माफ़ कीजिए, क्या आप फिर से बता सकते हैं?",
    ),
    IntakeLanguage(
        code="kn-IN", name="Kannada", script="Kannada",
        greeting="AVP ಆಸ್ಪತ್ರೆಗೆ ಸ್ವಾಗತ. ಚೆಕ್-ಇನ್ ಮಾಡಲು ನಾನು ನಿಮಗೆ ಸಹಾಯ ಮಾಡುತ್ತೇನೆ. ದಯವಿಟ್ಟು ನಿಮ್ಮ ಪೂರ್ಣ ಹೆಸರನ್ನು ಹೇಳಿ.",
        closing="ಧನ್ಯವಾದಗಳು{name}. ನಿಮ್ಮ ಮಾಹಿತಿಯನ್ನು ಉಳಿಸಲಾಗಿದೆ. ದಯವಿಟ್ಟು ಕುಳಿತುಕೊಳ್ಳಿ, ವೈದ್ಯರು ಶೀಘ್ರದಲ್ಲೇ ನಿಮ್ಮನ್ನು ನೋಡುತ್ತಾರೆ.",
        save_problem="ನಿಮ್ಮ ಮಾಹಿತಿಯನ್ನು ಉಳಿಸುವಲ್ಲಿ ಸಮಸ್ಯೆ ಆಗಿದೆ. ದಯವಿಟ್ಟು ಸ್ವಲ್ಪ ಕಾಯಿರಿ.",
        repeat="ಕ್ಷಮಿಸಿ, ನನಗೆ ಸರಿಯಾಗಿ ಕೇಳಿಸಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ.",
        say_again="ಕ್ಷಮಿಸಿ, ದಯವಿಟ್ಟು ಇನ್ನೊಮ್ಮೆ ಹೇಳುತ್ತೀರಾ?",
    ),
    IntakeLanguage(
        code="ta-IN", name="Tamil", script="Tamil",
        greeting="AVP மருத்துவமனைக்கு வரவேற்கிறோம். செக்-இன் செய்ய நான் உங்களுக்கு உதவுகிறேன். தயவுசெய்து உங்கள் முழுப் பெயரைச் சொல்லுங்கள்.",
        closing="நன்றி{name}. உங்கள் தகவல்கள் சேமிக்கப்பட்டன. தயவுசெய்து அமருங்கள், மருத்துவர் விரைவில் உங்களைப் பார்ப்பார்.",
        save_problem="உங்கள் தகவல்களைச் சேமிப்பதில் சிக்கல் உள்ளது. தயவுசெய்து சிறிது நேரம் காத்திருங்கள்.",
        repeat="மன்னிக்கவும், எனக்குச் சரியாகக் கேட்கவில்லை. தயவுசெய்து மீண்டும் சொல்லுங்கள்.",
        say_again="மன்னிக்கவும், தயவுசெய்து மீண்டும் சொல்ல முடியுமா?",
    ),
    IntakeLanguage(
        code="te-IN", name="Telugu", script="Telugu",
        greeting="AVP హాస్పిటల్‌కు స్వాగతం. చెక్-ఇన్ చేయడానికి నేను మీకు సహాయం చేస్తాను. దయచేసి మీ పూర్తి పేరు చెప్పండి.",
        closing="ధన్యవాదాలు{name}. మీ సమాచారం సేవ్ చేయబడింది. దయచేసి కూర్చోండి, డాక్టర్ త్వరలో మిమ్మల్ని చూస్తారు.",
        save_problem="మీ సమాచారాన్ని సేవ్ చేయడంలో సమస్య ఉంది. దయచేసి కొంచెం సేపు వేచి ఉండండి.",
        repeat="క్షమించండి, నాకు సరిగ్గా వినపడలేదు. దయచేసి మళ్ళీ చెప్పండి.",
        say_again="క్షమించండి, దయచేసి మళ్ళీ చెప్పగలరా?",
    ),
)}

# Unicode blocks of the scripts above. Anything else (digits, punctuation,
# another script) doesn't vote.
_SCRIPT_RANGES = (
    ("Devanagari", 0x0900, 0x097F),
    ("Tamil", 0x0B80, 0x0BFF),
    ("Telugu", 0x0C00, 0x0C7F),
    ("Kannada", 0x0C80, 0x0CFF),
)
_BY_SCRIPT = {lang.script: lang for lang in LANGUAGES.values()}


def resolve_language(code) -> IntakeLanguage:
    """The language for a client-supplied code; anything unknown is English."""
    return LANGUAGES.get(code, ENGLISH) if isinstance(code, str) else ENGLISH


def _char_script(ch: str) -> str | None:
    if ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
        return "Latin"
    cp = ord(ch)
    for script, lo, hi in _SCRIPT_RANGES:
        if lo <= cp <= hi:
            return script
    return None


def script_of(text: str) -> str | None:
    """The script most of the text's letters are in, or None if it has no letters.

    A Kannada sentence with "AVP" or an English name in it is still Kannada.
    """
    counts: dict[str, int] = {}
    for ch in text:
        script = _char_script(ch)
        if script:
            counts[script] = counts.get(script, 0) + 1
    return max(counts, key=counts.get) if counts else None


def language_of(text: str) -> IntakeLanguage | None:
    """The intake language a reply is written in, judged by its script."""
    script = script_of(text)
    return _BY_SCRIPT.get(script) if script else None
