"""Voice-input settings for the intake bot, read once from the environment.

Every experimental audio feature is a flag here so it can be switched off
without a code change (see server/.env.example for the rollback recipe).

Responsibilities stay separate on purpose, because they fail differently:
  - noise suppression cleans the signal (browser NS, optional server denoiser)
  - VAD decides *whether* someone is speaking; it never cleans audio, and a
    VAD "speech" decision can still be patient + fan + a bystander
  - the echo guard decides whether detected speech/transcripts are the bot's
    own voice coming back through the mic
  - STT turns whatever audio reaches it into text; it cannot tell voices apart
None of these identify *which person* is speaking.
"""
import os
from dataclasses import dataclass


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError:
        return default
    return min(max(value, lo), hi)


@dataclass(frozen=True)
class VoiceConfig:
    # Suppress barge-ins and transcripts that are the bot's own TTS leaking
    # back through the mic. Off = stock Pipecat turn-start behavior.
    echo_guard: bool
    # Headset kiosks only: accept a transcript as patient speech only when local
    # VAD also heard speech. Never for laptop/desk mics (VAD hears bystanders there).
    headset_mode: bool
    # When a patient turn ends with no transcript, ask the patient to repeat
    # instead of silently stalling.
    no_transcript_recovery: bool
    # Content-free audio/turn metrics, one JSON log line per session.
    telemetry: bool
    # Development test sessions only: scenario label that enables the per-role
    # report scored against the tester's patient/bystander key presses.
    debug_scenario: str | None
    # Server-side denoiser applied before VAD and STT: "none" or "rnnoise".
    server_denoiser: str
    # Silero silence before a VAD stop (which also flushes Sarvam and asks
    # Smart Turn whether the patient finished).
    vad_stop_secs: float
    # Sarvam STT. Defaults are the A/B winners, see server/voice_eval/TEST_PLAN.md.
    stt_model: str
    stt_language: str
    stt_mode: str
    stt_keyterms: bool
    stt_keyterm_limit: int
    stt_vocabulary_extra_file: str | None
    # "default" leaves Sarvam's server VAD at its documented defaults;
    # "permissive" restores the pre-fix hand-tuned block (see stt.py).
    sarvam_vad_profile: str

    @classmethod
    def from_env(cls) -> "VoiceConfig":
        denoiser = os.getenv("SERVER_DENOISER", "none").strip().lower()
        vad_profile = os.getenv("SARVAM_VAD_PROFILE", "default").strip().lower()
        return cls(
            echo_guard=_flag("ENABLE_TTS_ECHO_GUARD", True),
            headset_mode=_flag("HEADSET_MODE", False),
            no_transcript_recovery=_flag("STT_NO_TRANSCRIPT_RECOVERY", True),
            telemetry=_flag("VOICE_TELEMETRY", True),
            debug_scenario=(os.getenv("VOICE_DEBUG_SCENARIO") or "").strip()[:40] or None,
            server_denoiser=denoiser if denoiser in ("none", "rnnoise") else "none",
            vad_stop_secs=_float("VAD_STOP_SECS", 0.2, lo=0.1, hi=2.0),
            stt_model=os.getenv("SARVAM_STT_MODEL", "saaras:v4"),
            stt_language=os.getenv("SARVAM_STT_LANGUAGE", "en-IN"),
            stt_mode=os.getenv("SARVAM_STT_MODE", "transcribe"),
            stt_keyterms=_flag("STT_KEYTERMS", False),
            stt_keyterm_limit=max(0, min(_int("STT_KEYTERM_LIMIT", 50), 50)),
            stt_vocabulary_extra_file=os.getenv("STT_VOCABULARY_EXTRA_FILE") or None,
            sarvam_vad_profile=vad_profile if vad_profile in ("default", "permissive") else "default",
        )
