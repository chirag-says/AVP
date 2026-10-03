"""TTS echo guard: stop the bot's own voice from interrupting it or becoming patient text.

The mic stays open while the bot speaks, so natural barge-in works. The cost
is that whatever browser echo cancellation misses comes straight back as
input. Two independent symptoms, two guards:

1. EchoAwareVADUserTurnStartStrategy replaces Pipecat's VADUserTurnStartStrategy.
   With the bot silent it behaves identically: VAD start means turn start,
   immediately. With the bot speaking, a VAD start only interrupts if the mic
   level over the speech onset is clearly above the loudest recent echo. A
   patient close to the mic passes on the very frame VAD fires (no added
   latency); a VAD start caused by echo alone does not. If the patient is no
   louder than the echo, the turn starts the moment the bot stops instead.

2. EchoTranscriptFilter sits between STT and the user aggregator. Pipecat's
   aggregator appends *every* transcript to the next user message and
   TranscriptionUserTurnStartStrategy turns any transcript into a user turn,
   so an echo transcript would both interrupt the bot and be fed to the LLM as
   the patient's words. Around bot speech, a transcript is dropped unless VAD
   heard speech and the words are not the bot's own recent words.

Neither guard is noise suppression or speaker identification: they only
separate "the bot's voice" from "everyone else". A bystander who speaks up
while the bot is talking still counts as a person speaking.
"""
import re
import time
from collections import deque

import numpy as np
from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start import BaseUserTurnStartStrategy

from voice.telemetry import VoiceTelemetry, frame_level_dbfs

# Barge-in during bot speech must beat the loudest recent echo by this much.
BARGE_IN_MARGIN_DB = 3.0
# Window of mic audio scored as the speech onset (Silero needs 0.2 s to fire).
ONSET_WINDOW_S = 0.2
# Frames younger than this may be the patient's own onset, so they are held
# back from the echo estimate until VAD has had time to decide.
ECHO_HOLDBACK_S = 0.3
# The echo reference only counts once built from this much bot-only audio: a
# single quiet frame at the start of a TTS reply is not an echo estimate.
ECHO_MIN_REFERENCE_S = 0.5
# The echo peak relaxes while the bot talks, so one loud syllable can't
# block barge-in for the rest of the utterance.
ECHO_PEAK_DECAY_DB_PER_S = 3.0

# A transcript this soon after bot audio is judged by the echo rules. Covers
# Sarvam's finalization latency (p99 ~1.2 s) with margin.
ECHO_TRANSCRIPT_WINDOW_S = 2.0
# How much bot text to remember, and how much of a transcript must be the
# bot's own words for it to count as echo.
BOT_WORDS_MEMORY_S = 20.0
# Headset gate: a transcript counts as patient speech only if VAD heard speech
# this recently. Covers Sarvam's finalization after a flush (median ~0.3 s,
# worst seen ~2 s).
VAD_GATE_WINDOW_S = 2.5
BOT_LIKE_RATIO = 0.6

# English letters, plus the Indic blocks from Devanagari to Malayalam (their
# vowel signs are part of the word) and the zero-width joiners Kannada and
# Telugu spell with. The danda sentence marks (U+0964/5) are punctuation.
_WORD = re.compile(r"[a-z0-9'\u0900-\u0963\u0966-\u0D7F\u200C\u200D]+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class BotSpeechTracker(BaseObserver):
    """Remembers the words the bot has recently spoken (TTSTextFrame text)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._spoken: deque[tuple[float, list[str]]] = deque()
        self._seen_ids: deque[int] = deque(maxlen=64)

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        # Observers see a frame at every hop; count each TTS sentence once.
        if isinstance(frame, TTSTextFrame) and frame.id not in self._seen_ids:
            self._seen_ids.append(frame.id)
            self._spoken.append((time.monotonic(), _words(frame.text)))

    def recent_words(self) -> set[str]:
        cutoff = time.monotonic() - BOT_WORDS_MEMORY_S
        while self._spoken and self._spoken[0][0] < cutoff:
            self._spoken.popleft()
        return {w for _, words in self._spoken for w in words}


class EchoAwareVADUserTurnStartStrategy(BaseUserTurnStartStrategy):
    """VAD turn start that ignores VAD triggers explained by the bot's own echo."""

    def __init__(self, telemetry: VoiceTelemetry | None = None, **kwargs):
        super().__init__(**kwargs)
        self._telemetry = telemetry
        self._bot_speaking = False
        self._vad_speaking = False
        self._pending = False  # VAD fired during bot speech, not yet confirmed
        self._recent: deque[tuple[float, float, bool]] = deque()  # (secs, dbfs, bot_only)
        self._recent_secs = 0.0
        self._echo_peak_db: float | None = None
        self._echo_ref_secs = 0.0  # bot-only audio behind the reference, kept across replies

    async def reset(self):
        self._pending = False

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            if self._pending:
                # Don't start a turn just because VAD is still "speaking": the
                # server marks the bot stopped before the browser finishes
                # playing, and Silero holds its speaking state 0.8 s past the
                # last echo. Real speech that overlapped the bot reaches the
                # turn through its transcript instead (EchoTranscriptFilter
                # passes it if the words aren't the bot's).
                self._pending = False
                if self._telemetry:
                    self._telemetry.count("barge_in_unconfirmed_when_bot_stopped")
        elif isinstance(frame, InputAudioRawFrame):
            await self._on_audio(frame)
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad_speaking = True
            if not self._bot_speaking:
                await self._start("vad")
            else:
                self._pending = True
                await self._evaluate_barge_in()
            return ProcessFrameResult.STOP
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._vad_speaking = False
            if self._pending:
                self._pending = False
                if self._telemetry:
                    self._telemetry.count("barge_in_suppressed_as_echo")
                logger.debug("Echo guard: VAD speech during bot audio did not beat the echo level")
        return ProcessFrameResult.CONTINUE

    async def _start(self, cause: str):
        if self._telemetry:
            self._telemetry.note_turn_start_cause(cause, bot_speaking=self._bot_speaking)
        await self.trigger_user_turn_started()

    async def _on_audio(self, frame: InputAudioRawFrame):
        secs = len(frame.audio) / (2 * frame.sample_rate * frame.num_channels)
        bot_only = self._bot_speaking and not self._vad_speaking
        self._recent.append((secs, frame_level_dbfs(frame.audio), bot_only))
        self._recent_secs += secs
        # Frames older than the holdback are safe to treat as echo evidence.
        while self._recent_secs > ECHO_HOLDBACK_S:
            old_secs, old_db, old_bot_only = self._recent.popleft()
            self._recent_secs -= old_secs
            if old_bot_only and not self._pending:
                peak = old_db if self._echo_peak_db is None else self._echo_peak_db
                self._echo_peak_db = max(old_db, peak - ECHO_PEAK_DECAY_DB_PER_S * old_secs)
                self._echo_ref_secs += old_secs
        if self._pending:
            await self._evaluate_barge_in()

    async def _evaluate_barge_in(self):
        onset, secs = [], 0.0
        for frame_secs, db, _ in reversed(self._recent):
            onset.append(db)
            secs += frame_secs
            if secs >= ONSET_WINDOW_S:
                break
        if not onset or self._echo_peak_db is None or self._echo_ref_secs < ECHO_MIN_REFERENCE_S:
            # No trustworthy echo reference yet (VAD fired before 0.5 s of
            # bot-only audio could be measured, e.g. an echo loud enough to hold
            # VAD open), so a loud echo and the patient look the same. Don't
            # interrupt on VAD alone; a transcript that isn't the bot's own
            # words still starts the turn.
            return
        onset_db = 10 * np.log10(np.mean(10 ** (np.array(onset) / 10)))  # energy mean
        if onset_db >= self._echo_peak_db + BARGE_IN_MARGIN_DB:
            self._pending = False
            await self._start("vad_barge_in")


class EchoTranscriptFilter(FrameProcessor):
    """Drops transcripts that are not the patient, before the user aggregator sees them.

    Two independently switched rules:
      - echo rules (ENABLE_TTS_ECHO_GUARD): around bot speech, drop the bot's
        own voice (see the module docstring).
      - headset gate (HEADSET_MODE): drop ANY transcript our local VAD did not
        also hear. With a close (headset) mic the patient always trips VAD and
        a bystander ~1 m away does not (measured: 0/10 VAD triggers at -30 dB,
        while Sarvam still transcribed the bystander in 4/10 of the patient's
        pauses). NOT for laptop or desk mics: there VAD hears the bystander too
        (10/10), and the gate would only remove the soft-speech fallback.
    """

    def __init__(
        self,
        bot_speech: BotSpeechTracker | None,
        telemetry: VoiceTelemetry | None = None,
        *,
        echo_rules: bool = True,
        require_vad: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._bot_speech = bot_speech
        self._telemetry = telemetry
        self._echo_rules = echo_rules and bot_speech is not None
        self._require_vad = require_vad
        self._bot_speaking = False
        self._bot_last_audio = float("-inf")
        self._user_turn = False
        self._user_turn_started = float("-inf")
        self._vad_active = False
        self._vad_last = float("-inf")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        now = time.monotonic()
        self.update_state(frame, now)
        if isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            reason = self.echo_reason(frame.text, now)
            if reason:
                if self._telemetry:
                    self._telemetry.count(f"transcript_dropped_{reason}")
                # Word count only: transcript text is patient data.
                logger.debug(f"Echo guard: dropped a {len(_words(frame.text))}-word transcript ({reason})")
                return
        await self.push_frame(frame, direction)

    def update_state(self, frame: Frame, now: float):
        # State frames reach us travelling upstream (broadcast by the output
        # transport and the user aggregator); they pass through untouched.
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._bot_last_audio = now
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._user_turn, self._user_turn_started = True, now
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user_turn = False
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad_active, self._vad_last = True, now
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._vad_active, self._vad_last = False, now

    def echo_reason(self, text: str, now: float) -> str | None:
        if self._require_vad and not (self._vad_active or now - self._vad_last < VAD_GATE_WINDOW_S):
            return "no_vad_headset_gate"  # applies inside a turn too: a bystander can talk in its pauses
        if not self._echo_rules:
            return None
        if self._user_turn or now - self._user_turn_started < ECHO_TRANSCRIPT_WINDOW_S:
            return None  # a confirmed patient turn owns this transcript
        bot_recent = self._bot_speaking or now - self._bot_last_audio < ECHO_TRANSCRIPT_WINDOW_S
        if not bot_recent:
            return None  # unchanged behavior away from bot speech (soft-speech fallback)
        if not (self._vad_active or now - self._vad_last < ECHO_TRANSCRIPT_WINDOW_S):
            return "no_vad"
        words = _words(text)
        bot_words = self._bot_speech.recent_words()
        if words and sum(w in bot_words for w in words) / len(words) >= BOT_LIKE_RATIO:
            return "bot_words"
        return None
