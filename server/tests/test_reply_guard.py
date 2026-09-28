r"""ReplyGuard: the bot never goes silent when it owes the patient a reply.

    .venv\Scripts\python.exe server\tests\test_reply_guard.py

Local only: frames through Pipecat's test pipeline, no Gemini, no Sarvam.
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
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext, LLMSpecificMessage  # noqa: E402
from pipecat.tests.utils import SleepFrame, run_test  # noqa: E402

from intake.reply_guard import EMPTY_REPLY_PROMPT, ReplyGuard, owes_reply  # noqa: E402

IDLE = 0.2
WAIT = SleepFrame(sleep=IDLE + 0.25)
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))


GREETING = {"role": "assistant", "content": "Could you please tell me your full name?"}
ANSWER = {"role": "user", "content": "No allergies."}
TOOL_CALL = {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
                                                  "function": {"name": "save_field", "arguments": "{}"}}]}
TOOL_RESULT = {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true, "next": "existing medical conditions"}'}
QUESTION = {"role": "assistant", "content": "Do you have any existing medical conditions?"}


def call_frames():
    args = dict(function_name="save_field", tool_call_id="c1", arguments={})
    return FunctionCallInProgressFrame(**args), FunctionCallResultFrame(**args, result={"ok": True})


async def run(messages, frames, is_open=lambda: True):
    guard = ReplyGuard(LLMContext(messages=list(messages)), is_open=is_open, idle_s=IDLE)
    down, up = await run_test(guard, frames_to_send=frames)
    nudges = sum(isinstance(f, LLMContextFrame) for f in up)
    spoken = [f.text for f in down if isinstance(f, TTSSpeakFrame)]
    return guard, nudges, spoken


def owes_reply_tests():
    check("owes: tool result last", owes_reply([GREETING, ANSWER, TOOL_CALL, TOOL_RESULT]))
    check("owes: patient message last", owes_reply([GREETING, ANSWER]))
    check("owes: tool call without a result", owes_reply([GREETING, ANSWER, TOOL_CALL]))
    check("not owed: assistant question last", not owes_reply([GREETING, ANSWER, TOOL_CALL, TOOL_RESULT, QUESTION]))
    thought = LLMSpecificMessage(llm="google", message={"type": "thought", "text": "", "signature": None})
    check("not owed: Gemini thought after the question is skipped", not owes_reply([ANSWER, QUESTION, thought]))
    check("owes: Gemini thought after a tool result is skipped", owes_reply([ANSWER, TOOL_CALL, TOOL_RESULT, thought]))
    check("not owed: empty context", not owes_reply([]))


async def guard_tests():
    in_progress, result = call_frames()
    owed = [GREETING, ANSWER, TOOL_CALL, TOOL_RESULT]

    # The live bug: noise opens a user turn while save_field runs, so Pipecat
    # skips the follow-up; the noise turn has no words and never runs the LLM.
    _, nudges, spoken = await run(owed, [in_progress, UserStartedSpeakingFrame(), result, WAIT,
                                         UserStoppedSpeakingFrame(), WAIT])
    check("noise turn swallowed the follow-up: LLM re-run once after the noise ends", nudges == 1 and not spoken,
          f"nudges={nudges} spoken={spoken}")

    _, nudges, _ = await run(owed, [in_progress, UserStartedSpeakingFrame(), result, WAIT, WAIT])
    check("never re-runs while someone is speaking", nudges == 0, f"nudges={nudges}")

    _, nudges, _ = await run([GREETING, ANSWER, TOOL_CALL], [in_progress, WAIT])
    check("never re-runs while a tool is still running", nudges == 0, f"nudges={nudges}")

    _, nudges, _ = await run([GREETING, ANSWER, TOOL_CALL, TOOL_RESULT, QUESTION],
                             [LLMFullResponseStartFrame(), LLMTextFrame("Do you have any existing medical conditions?"),
                              LLMFullResponseEndFrame(), BotStartedSpeakingFrame(), BotStoppedSpeakingFrame(), WAIT])
    check("normal reply: nothing happens", nudges == 0, f"nudges={nudges}")

    _, nudges, _ = await run(owed, [LLMFullResponseStartFrame(), WAIT])
    check("never re-runs while the LLM is generating", nudges == 0, f"nudges={nudges}")

    _, nudges, _ = await run(owed, [BotStartedSpeakingFrame(), WAIT])
    check("never re-runs while the bot is speaking", nudges == 0, f"nudges={nudges}")

    # A real patient turn runs the LLM itself within milliseconds; the guard stays out of it.
    _, nudges, _ = await run([GREETING, ANSWER], [UserStartedSpeakingFrame(), UserStoppedSpeakingFrame(),
                                                  LLMFullResponseStartFrame(), WAIT])
    check("real patient turn: the guard does not double-run the LLM", nudges == 0, f"nudges={nudges}")

    # Idleness must be continuous: activity inside the window restarts the countdown.
    _, nudges, _ = await run(owed, [LLMFullResponseEndFrame(), SleepFrame(sleep=IDLE / 2),
                                    UserStartedSpeakingFrame(), WAIT])
    check("countdown restarts on activity", nudges == 0, f"nudges={nudges}")

    # Empty responses (Gemini 429): re-run once, then ask the patient.
    guard, nudges, spoken = await run([GREETING, ANSWER], [LLMFullResponseStartFrame(), LLMFullResponseEndFrame(), WAIT,
                                                           LLMFullResponseStartFrame(), LLMFullResponseEndFrame(), WAIT])
    check("empty response: re-run once, then ask the patient to repeat",
          nudges == 1 and spoken == [EMPTY_REPLY_PROMPT] and guard.retries == 1 and guard.fallbacks == 1,
          f"nudges={nudges} spoken={spoken}")

    _, nudges, spoken = await run([GREETING, ANSWER], [LLMFullResponseStartFrame(), LLMFullResponseEndFrame(), WAIT,
                                                       BotStartedSpeakingFrame(), BotStoppedSpeakingFrame(), WAIT])
    check("the bot speaking resets the re-run budget", nudges == 2 and not spoken, f"nudges={nudges} spoken={spoken}")

    # finalize returns run_llm=False and the system speaks the closing line: the
    # context ends with a tool result, but the guard must stay quiet.
    _, nudges, spoken = await run(owed, [LLMFullResponseEndFrame(), WAIT], is_open=lambda: False)
    check("finalizing or saved: never re-runs or speaks", nudges == 0 and not spoken, f"nudges={nudges} spoken={spoken}")


def main() -> int:
    owes_reply_tests()
    asyncio.run(guard_tests())
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
