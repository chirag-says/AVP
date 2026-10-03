"""Never leave the patient in silence when the bot owes them a reply.

Two ways the intake used to stall with the bot "listening" and nothing
happening until the patient spoke again:

1. Gemini answers a turn with nothing: no text and no tool call. In testing
   every case was a 429 (the free tier's per-minute quota), which Pipecat
   reports as an ordinary empty response.
2. Background noise opens a user turn at the wrong moment. Pipecat does not
   re-run the LLM after a tool result while the user is "speaking" (it expects
   the user's turn to do it), and a user turn starting cancels an LLM run in
   progress. A noise turn has no words, so it never runs the LLM, and the next
   question is never asked. Seen live right after the seventh field was saved.

Both leave the same state behind: the context ends with a patient message or a
tool result that no assistant message answers. This processor sits right after
the LLM and watches for that state while the pipeline is idle (the LLM is not
generating, no tool is running, neither side is speaking). After IDLE_S of
continuous idleness it re-runs the LLM on the same context; the wait also gives
a per-minute quota window a moment. If the bot still has not spoken after
MAX_NUDGES re-runs, it asks the patient to say it again. Any activity resets
the wait, so a real patient turn always runs the LLM itself first. It never
acts once the intake is finalizing or saved, and it records nothing.
"""
import asyncio
from collections.abc import Callable

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    FunctionCallCancelFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

EMPTY_REPLY_PROMPT = "Sorry, could you please say that again?"
IDLE_S = 2.0
MAX_NUDGES = 1  # LLM re-runs without the bot speaking before asking the patient

_STATE_FRAMES = (
    LLMFullResponseStartFrame, LLMFullResponseEndFrame, InterruptionFrame,
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame,
    UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
    FunctionCallInProgressFrame, FunctionCallResultFrame, FunctionCallCancelFrame,
)


def owes_reply(messages) -> bool:
    """True when the context ends with something the assistant has not answered."""
    for m in reversed(messages):
        if not isinstance(m, dict):  # LLMSpecificMessage, e.g. a Gemini thought
            continue
        role = m.get("role")
        if role in ("user", "tool"):
            return True
        if role == "assistant":
            return not m.get("content") and bool(m.get("tool_calls"))
        return False
    return False


class ReplyGuard(FrameProcessor):
    def __init__(self, context: LLMContext, is_open: Callable[[], bool] = lambda: True,
                 idle_s: float = IDLE_S, prompt: Callable[[], str] = lambda: EMPTY_REPLY_PROMPT, **kwargs):
        super().__init__(**kwargs)
        self._context = context
        self._is_open = is_open
        self._prompt = prompt
        self._idle_s = idle_s
        self._llm_busy = False
        self._bot_speaking = False
        self._user_speaking = False
        self._calls: set[str] = set()
        self._output = False       # text or a tool call in the current LLM response
        self._last_empty = False   # the last LLM response had neither
        self._nudges = 0
        self._timer: asyncio.Task | None = None
        self.retries = 0
        self.fallbacks = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (EndFrame, CancelFrame)):
            await self._cancel_timer()
        elif isinstance(frame, LLMTextFrame) and frame.text.strip():
            self._output = True
        elif isinstance(frame, FunctionCallsStartedFrame):
            self._output = True
        elif isinstance(frame, _STATE_FRAMES):
            self._track(frame)
        await self.push_frame(frame, direction)
        if isinstance(frame, _STATE_FRAMES):
            await self._rearm()

    def _track(self, frame: Frame):
        if isinstance(frame, LLMFullResponseStartFrame):
            self._llm_busy, self._output = True, False
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._llm_busy = False
            self._last_empty = not self._output
        elif isinstance(frame, InterruptionFrame):
            self._llm_busy = False  # an interruption cancels the LLM run in progress
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking, self._nudges = True, 0
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._user_speaking = True
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user_speaking = False
        elif isinstance(frame, FunctionCallInProgressFrame):
            self._calls.add(frame.tool_call_id)
        else:  # FunctionCallResultFrame, FunctionCallCancelFrame
            self._calls.discard(frame.tool_call_id)

    def _idle(self) -> bool:
        return not (self._llm_busy or self._bot_speaking or self._user_speaking or self._calls)

    async def _rearm(self):
        """Restart the idle countdown on every state change; start it only when idle."""
        await self._cancel_timer()
        if self._idle():
            self._timer = self.create_task(self._after_idle())

    async def _cancel_timer(self):
        if self._timer:
            await self.cancel_task(self._timer)
            self._timer = None

    async def _after_idle(self):
        await asyncio.sleep(self._idle_s)
        self._timer = None
        # Checked now, not when the countdown started: the assistant's reply
        # reaches the context after the bot's audio, at the end of the pipeline.
        if not self._idle() or not self._is_open() or not owes_reply(self._context.messages):
            return
        if self._nudges >= MAX_NUDGES:
            self._nudges = 0
            self.fallbacks += 1
            logger.warning("Still no reply after re-running the LLM; asking the patient to repeat")
            await self.push_frame(TTSSpeakFrame(self._prompt()), FrameDirection.DOWNSTREAM)
            return
        self._nudges += 1
        self.retries += 1
        cause = "the LLM returned an empty response" if self._last_empty else "the reply was cut off by an interruption"
        logger.warning(f"No reply to the patient after {self._idle_s:.1f}s idle ({cause}); running the LLM again")
        await self.push_frame(LLMContextFrame(self._context), FrameDirection.UPSTREAM)
