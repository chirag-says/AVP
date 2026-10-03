"""Keep the voice speaking the language the bot is replying in.

The LLM replies in whatever language the patient last spoke (prompts.py), so
the TTS language has to follow each reply, not the language picked at the
start. The reply's script says which language it is (Kannada script is
Kannada, Devanagari is Hindi, English letters are English), so no extra model
call is needed to know.

This sits between the LLM and the TTS. LLM text streams in token-sized frames;
a sentence like "AVP ಆಸ್ಪತ್ರೆಗೆ ..." starts with English letters, so judging by
the first frame would flip the voice to English for a Kannada sentence. It
holds each sentence's frames until the sentence ends, judges the whole
sentence, sends a TTS language update if it changed, then releases the frames.
That adds no delay: the TTS waits for a full sentence before speaking anyway.
"""
from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMTextFrame,
    SystemFrame,
    TTSSpeakFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.settings import TTSSettings
from pipecat.transcriptions.language import Language

from .languages import IntakeLanguage, language_of

_SENTENCE_END = (".", "?", "!", "।", "\n")


class LanguageState:
    """The session's languages: where it started, what the bot speaks now, all it used."""

    def __init__(self, start: IntakeLanguage):
        self.start = start
        self.current = start
        self.used: list[str] = [start.code]

    def switch(self, lang: IntakeLanguage) -> bool:
        if lang.code == self.current.code:
            return False
        self.current = lang
        if lang.code not in self.used:
            self.used.append(lang.code)
        return True


class ReplyLanguageFollower(FrameProcessor):
    def __init__(self, state: LanguageState, **kwargs):
        super().__init__(**kwargs)
        self._state = state
        self._held: list[LLMTextFrame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
        elif isinstance(frame, LLMTextFrame):
            self._held.append(frame)
            if frame.text.rstrip().endswith(_SENTENCE_END):
                await self._release()
        elif isinstance(frame, InterruptionFrame):
            self._held = []  # the reply is cancelled; its text must not be spoken
            await self.push_frame(frame, direction)
        elif isinstance(frame, TTSSpeakFrame):
            await self._release()
            await self._follow(frame.text)
            await self.push_frame(frame, direction)
        elif isinstance(frame, SystemFrame):
            await self.push_frame(frame, direction)  # out-of-band; never waits for text
        else:
            await self._release()  # keep order: held text goes before e.g. the response end
            await self.push_frame(frame, direction)

    async def _release(self):
        if not self._held:
            return
        held, self._held = self._held, []
        await self._follow("".join(f.text for f in held))
        for f in held:
            await self.push_frame(f, FrameDirection.DOWNSTREAM)

    async def _follow(self, text: str):
        lang = language_of(text)
        if lang and self._state.switch(lang):
            logger.info(f"Bot language -> {lang.code}")
            await self.push_frame(TTSUpdateSettingsFrame(delta=TTSSettings(language=Language(lang.code))),
                                  FrameDirection.DOWNSTREAM)
