"""Recover when the patient spoke but no transcript came back.

Without this, a turn that VAD opened but STT never filled (a silent STT drop,
seen intermittently with saaras:v4, or speech STT could not decode) is closed
by Pipecat's 5 s stop-timeout watchdog with an empty message, nothing reaches
the LLM, and the bot sits silent: the intake stalls.

This processor sits right before the user aggregator. When a user turn ends
with no transcript at all, after at least MIN_SPEECH_S of detected speech, it
has the bot ask the patient to repeat. The request is a fixed sentence spoken
by TTS (no LLM call), so nothing is saved from the empty turn; it is appended
to the LLM context so the model knows it asked. The patient's next answer is
an ordinary turn. At most MAX_CONSECUTIVE requests in a row, so a noise source
that keeps tripping VAD cannot make the bot repeat itself indefinitely.
"""
import time

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.telemetry import VoiceTelemetry

RECOVERY_PROMPT = "Sorry, I didn't catch that. Could you please repeat that?"
# Less speech than this (a cough, a chair scrape) is not worth interrupting for.
MIN_SPEECH_S = 0.5
MAX_CONSECUTIVE = 2


class NoTranscriptRecovery(FrameProcessor):
    def __init__(self, telemetry: VoiceTelemetry | None = None, prompt: str = RECOVERY_PROMPT, **kwargs):
        super().__init__(**kwargs)
        self._telemetry = telemetry
        self._prompt = prompt
        self._turn_open = False
        self._got_text = False
        self._speech_s = 0.0
        self._vad_started: float | None = None
        self._consecutive = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        recover = self.observe(frame, time.monotonic())
        await self.push_frame(frame, direction)
        if recover:
            if self._telemetry:
                self._telemetry.count("no_transcript_recovery_prompts")
            logger.info("No transcript for a patient turn; asking the patient to repeat")
            await self.push_frame(TTSSpeakFrame(self._prompt), FrameDirection.DOWNSTREAM)

    def observe(self, frame: Frame, now: float) -> bool:
        """Track the current user turn; True when it just ended with no transcript."""
        # Turn and VAD frames reach this position travelling upstream (broadcast
        # by the user aggregator); transcripts travel downstream from STT.
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad_started = now
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            if self._vad_started is not None:
                self._speech_s += now - self._vad_started
                self._vad_started = None
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._turn_open, self._got_text = True, False
            # VAD start usually arrives just before the turn opens; keep it.
            self._speech_s = 0.0
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            if frame.text.strip():
                self._got_text = True
                self._consecutive = 0
        elif isinstance(frame, UserStoppedSpeakingFrame) and self._turn_open:
            self._turn_open = False
            speech = self._speech_s + (now - self._vad_started if self._vad_started is not None else 0.0)
            if not self._got_text and speech >= MIN_SPEECH_S and self._consecutive < MAX_CONSECUTIVE:
                self._consecutive += 1
                return True
        return False
