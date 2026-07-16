"""Gate test: does an LLM reliably emit save_field tool calls across a real intake?

This exists because sarvam-30b does not. Measured 2026-07-16: across the five
turns below, with full conversation history, sarvam-30b emitted ZERO tool calls
and produced an empty record; gemini-flash-lite saved 4/5 with correct keys.
(sarvam-30b does call tools single-shot — it stops once history accumulates.)

The failure is silent: no crash, no error frame, just blank patient records. So
run this before pointing LLM_PROVIDER at any new model, and read the record it
prints — a passing tool-call count with unresolvable keys is still a failure.

Run: ..\\.venv\\Scripts\\python.exe smoke\\test_sarvam_tools.py
"""
import json
import os
import pathlib
import sys

from dotenv import load_dotenv
from openai import OpenAI

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from intake.engine import IntakeEngine
from intake.prompts import build_system_prompt

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=True)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_field",
            "description": "Record one piece of patient information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": (
                            "Field dot-path, e.g. personal.full_name or "
                            "personal.phone. Must be one of the known intake fields."
                        ),
                    },
                    "value": {
                        "type": "string",
                        "description": "The value to store, as the patient stated it.",
                    },
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize",
            "description": "Close out intake once every required field is collected.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

TURNS = [
    "Hi, my name is Chirag Sharma",
    "I am 28 years old",
    "male",
    "my number is 98765 43210",
    "I live at 12 MG Road, Bangalore",
]

PROVIDERS = {
    "sarvam-30b": ("SARVAM_API_KEY", "https://api.sarvam.ai/v1", "sarvam-30b"),
    "gemini": (
        "GOOGLE_API_KEY",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        os.getenv("GOOGLE_MODEL", "gemini-flash-lite-latest"),
    ),
}


def run(label: str, key_env: str, base_url: str, model: str) -> None:
    api_key = os.getenv(key_env)
    if not api_key:
        print(f"  {label}: skipped — {key_env} not set\n")
        return

    client = OpenAI(api_key=api_key, base_url=base_url)
    engine = IntakeEngine()
    messages = [{"role": "system", "content": build_system_prompt()}]
    saved = rejected = 0

    for utterance in TURNS:
        messages.append({"role": "user", "content": utterance})
        message = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS
        ).choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        if not message.tool_calls:
            print(f"    MISS  {utterance[:30]!r:32} -> no tool call")
            continue

        for call in message.tool_calls:
            if call.function.name == "save_field":
                args = json.loads(call.function.arguments or "{}")
                # Score against the real engine: a tool call carrying a key the
                # engine can't resolve is a miss wearing a success costume.
                result = engine.save_field(args.get("key", ""), args.get("value", ""))
                ok = bool(result.get("ok"))
                saved += ok
                rejected += not ok
                print(f"    {'SAVE' if ok else 'REJ '}  {utterance[:30]!r:32} -> key={args.get('key')!r}")
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps({"ok": True})}
            )

    print(f"  == {label}: {saved}/{len(TURNS)} saved, {rejected} rejected")
    print(f"     record: {json.dumps(engine.data)}\n")


def main():
    for label, (key_env, base_url, model) in PROVIDERS.items():
        print(f"--- {label} ({model}) ---")
        run(label, key_env, base_url, model)


if __name__ == "__main__":
    main()
