// Languages the intake offers, and the patient-facing screen text for each.
// The codes must match server/intake/languages.py (checked by
// languages.test.ts); the server treats any other code as English. What the
// bot SAYS lives on the server; this is only what the patient READS.
// Staff-facing text (Start Next Patient, Retry saving, the records view)
// stays English.
//
// The non-English strings were written without a native speaker's review.

export interface UiText {
  tapToStart: string;
  connecting: string;
  listening: string;
  speaking: string;
  goAhead: string;
  tryAgain: string;
  saving: string;
  completeTitle: string;
  thankYou: (name: string | null) => string;
  recorded: string;
}

export interface IntakeLanguage {
  code: string; // BCP-47, as sent to the server
  label: string; // the language's own name, in its own script
  ui: UiText;
}

export const LANGUAGES: IntakeLanguage[] = [
  {
    code: "en-IN",
    label: "English",
    ui: {
      tapToStart: "Tap to start",
      connecting: "Connecting…",
      listening: "Listening…",
      speaking: "Speaking…",
      goAhead: "Go ahead",
      tryAgain: "Tap to try again",
      saving: "Saving your information…",
      completeTitle: "Intake Complete",
      thankYou: (name) => (name ? `Thank you, ${name}.` : "Thank you."),
      recorded: "Your information has been successfully recorded.",
    },
  },
  {
    code: "kn-IN",
    label: "ಕನ್ನಡ",
    ui: {
      tapToStart: "ಪ್ರಾರಂಭಿಸಲು ಟ್ಯಾಪ್ ಮಾಡಿ",
      connecting: "ಸಂಪರ್ಕಿಸಲಾಗುತ್ತಿದೆ…",
      listening: "ಕೇಳಿಸಿಕೊಳ್ಳುತ್ತಿದ್ದೇನೆ…",
      speaking: "ಮಾತನಾಡುತ್ತಿದ್ದೇನೆ…",
      goAhead: "ಮಾತನಾಡಿ",
      tryAgain: "ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಲು ಟ್ಯಾಪ್ ಮಾಡಿ",
      saving: "ನಿಮ್ಮ ಮಾಹಿತಿಯನ್ನು ಉಳಿಸಲಾಗುತ್ತಿದೆ…",
      completeTitle: "ಚೆಕ್-ಇನ್ ಪೂರ್ಣಗೊಂಡಿದೆ",
      thankYou: (name) => (name ? `ಧನ್ಯವಾದಗಳು, ${name}.` : "ಧನ್ಯವಾದಗಳು."),
      recorded: "ನಿಮ್ಮ ಮಾಹಿತಿಯನ್ನು ಯಶಸ್ವಿಯಾಗಿ ದಾಖಲಿಸಲಾಗಿದೆ.",
    },
  },
  {
    code: "hi-IN",
    label: "हिन्दी",
    ui: {
      tapToStart: "शुरू करने के लिए टैप करें",
      connecting: "कनेक्ट हो रहा है…",
      listening: "सुन रही हूँ…",
      speaking: "बोल रही हूँ…",
      goAhead: "बोलिए",
      tryAgain: "फिर से कोशिश करने के लिए टैप करें",
      saving: "आपकी जानकारी सेव हो रही है…",
      completeTitle: "चेक-इन पूरा हुआ",
      thankYou: (name) => (name ? `धन्यवाद, ${name}।` : "धन्यवाद।"),
      recorded: "आपकी जानकारी सफलतापूर्वक दर्ज कर ली गई है।",
    },
  },
  {
    code: "ta-IN",
    label: "தமிழ்",
    ui: {
      tapToStart: "தொடங்க தட்டவும்",
      connecting: "இணைக்கிறது…",
      listening: "கேட்டுக்கொண்டிருக்கிறேன்…",
      speaking: "பேசிக்கொண்டிருக்கிறேன்…",
      goAhead: "பேசுங்கள்",
      tryAgain: "மீண்டும் முயற்சிக்க தட்டவும்",
      saving: "உங்கள் தகவல்கள் சேமிக்கப்படுகின்றன…",
      completeTitle: "செக்-இன் முடிந்தது",
      thankYou: (name) => (name ? `நன்றி, ${name}.` : "நன்றி."),
      recorded: "உங்கள் தகவல்கள் வெற்றிகரமாகப் பதிவு செய்யப்பட்டன.",
    },
  },
  {
    code: "te-IN",
    label: "తెలుగు",
    ui: {
      tapToStart: "ప్రారంభించడానికి నొక్కండి",
      connecting: "కనెక్ట్ అవుతోంది…",
      listening: "వింటున్నాను…",
      speaking: "మాట్లాడుతున్నాను…",
      goAhead: "చెప్పండి",
      tryAgain: "మళ్ళీ ప్రయత్నించడానికి నొక్కండి",
      saving: "మీ సమాచారం సేవ్ అవుతోంది…",
      completeTitle: "చెక్-ఇన్ పూర్తయింది",
      thankYou: (name) => (name ? `ధన్యవాదాలు, ${name}.` : "ధన్యవాదాలు."),
      recorded: "మీ సమాచారం విజయవంతంగా నమోదు చేయబడింది.",
    },
  },
];

export const DEFAULT_LANGUAGE = "en-IN";

export function languageFor(code: string): IntakeLanguage {
  return LANGUAGES.find((l) => l.code === code) ?? LANGUAGES[0];
}
