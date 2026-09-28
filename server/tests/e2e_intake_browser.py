r"""End-to-end: real browser, real bot, Gemini, Sarvam TTS and REAL Supabase.

    .venv\Scripts\python.exe server\tests\e2e_intake_browser.py

Starts bot.py (Supabase ON), the Vite dev server and a headless Chrome with an
isolated temporary profile. The patient's answers are typed into the live
session with the Pipecat client's sendText (the dev-build handle), because a
recorded microphone can't adapt to the assistant's questions; everything after
the transcript is the production path: Gemini + intake tools + finalize +
Supabase write + intake_complete + completion modal. Then it clicks "Start
Next Patient" and checks the new session starts clean.

Writes one clearly labelled test row ("Test Patient Echo", phone 9000000003).
Costs: a few dozen Gemini calls, a little Sarvam TTS/STT.
"""
import asyncio
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

import numpy as np
import soundfile as sf
import websockets

HERE = pathlib.Path(__file__).resolve().parent
SERVER = HERE.parent
WEB = SERVER.parent / "web"
sys.path.insert(0, str(SERVER))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from voice_eval.browser_e2e import CHROME, _CDP, _kill, _wait_http  # noqa: E402

BOT_PORT, WEB_PORT, CDP_PORT = 7873, 5198, 9336
GREETING = "Welcome to AVP Hospital. I'm here to help you check in. Could you please tell me your full name?"
results: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n        {detail}" if detail and not ok else ""), flush=True)


ANSWERS = [
    (r"\b(correct|right|confirm|is that|did you mean)\b.*\?", "Yes, that's correct."),
    (r"allerg", "No allergies."),
    (r"medication|medicine|tablet", "No, I'm not taking any medications."),
    (r"condition|illness|disease", "No."),
    (r"how long|since when|duration", "About three days."),
    (r"sever", "Mild."),
    (r"symptom|brings you|problem|feeling|complaint", "I have a mild fever and a sore throat."),
    (r"address|where do you live", "3 Test Street, Testville."),
    (r"phone|mobile|number|contact", "My number is 9000000003."),
    (r"gender", "Female."),
    (r"\bage\b|how old", "I am thirty years old."),
    (r"name", "My name is Test Patient Echo."),
]


def answer_for(reply: str) -> str:
    r = reply.lower()
    for pattern, answer in ANSWERS:
        if re.search(pattern, r):
            return answer
    return "Yes."


JS_STATE = """(() => {
  const bot = [...document.querySelectorAll('span')].filter(s => s.textContent === 'Assistant')
    .map(s => s.nextElementSibling ? s.nextElementSibling.textContent : '');
  const dialog = document.querySelector('[role=dialog]');
  return JSON.stringify({
    bot,
    dialog: dialog ? dialog.innerText : null,
    live: !!document.body.innerText.includes('Live'),
    badge: [...document.querySelectorAll('[data-slot=badge]')].map(b => b.textContent),
    form: (document.querySelector('main') || document.body).innerText,
    hasClient: !!window.__intakeClient,
  });
})()"""


async def state(cdp) -> dict:
    r = await cdp.call("Runtime.evaluate", expression=JS_STATE)
    return json.loads(r["result"]["result"]["value"])


async def wait_for(cdp, pred, timeout=60, step=0.5):
    t = time.time()
    while time.time() - t < timeout:
        s = await state(cdp)
        if pred(s):
            return s
        await asyncio.sleep(step)
    return await state(cdp)


async def drive() -> dict:
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list"))
    page = next(t for t in targets if t["type"] == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
        cdp = _CDP(ws)
        pump = asyncio.create_task(cdp.pump())
        await cdp.call("Runtime.enable")
        await cdp.call("Page.navigate", url=f"http://localhost:{WEB_PORT}/")
        await asyncio.sleep(4)
        await cdp.call("Runtime.evaluate", expression="document.querySelector('button.orb-button').click()")

        s = await wait_for(cdp, lambda s: s["bot"] and s["hasClient"], timeout=90)
        check("E2E: session starts with the fixed greeting", bool(s["bot"]) and s["bot"][0].strip() == GREETING, s["bot"][:1])

        transcript = [("assistant", " ".join(s["bot"]))]
        seen = len(s["bot"])
        for turn in range(30):
            if s["dialog"]:
                break
            text = answer_for(transcript[-1][1])
            await asyncio.sleep(4)  # patient think time; also keeps Gemini under its free-tier rate limit
            await cdp.call("Runtime.evaluate", expression=f"window.__intakeClient.sendText({json.dumps(text)})")
            transcript.append(("patient", text))
            # Wait for the assistant's reply (new bubbles that then stop growing) or the modal.
            s = await wait_for(cdp, lambda st: st["dialog"] or len(st["bot"]) > seen, timeout=60)
            last = -1
            while not s["dialog"] and len(s["bot"]) != last:
                last = len(s["bot"])
                await asyncio.sleep(2.5)
                s = await state(cdp)
            new = s["bot"][seen:]
            seen = len(s["bot"])
            transcript.append(("assistant", " ".join(new)))
        final = s
        print("\n  transcript:\n" + "\n".join(f"    {w}: {t}" for w, t in transcript), flush=True)

        check("E2E: completion modal appears", bool(final["dialog"]), final["dialog"])
        dialog = final["dialog"] or ""
        check("E2E: modal says 'Intake Complete', thanks the patient, offers Start Next Patient",
              "Intake Complete" in dialog and "Thank you, Test." in dialog and "successfully recorded" in dialog
              and "Start Next Patient" in dialog, dialog)
        check("E2E: intake panel shows 'Saved' (only after the DB confirmed)", "Saved" in final["badge"], final["badge"])
        check("E2E: no raw JSON on screen", "{\"" not in final["form"] and "description" not in final["form"])
        check("E2E: 'none' answers shown as 'None'", "None" in final["form"])

        # Let the closing line play, then start the next patient.
        await asyncio.sleep(6)
        await cdp.call("Runtime.evaluate", expression="""[...document.querySelectorAll('[role=dialog] button')]
            .find(b => b.textContent.includes('Start Next Patient')).click()""")
        nxt = await wait_for(cdp, lambda st: not st["dialog"] and len(st["bot"]) >= 1 and st["hasClient"], timeout=90)
        await asyncio.sleep(4)
        nxt = await state(cdp)
        check("E2E next patient: modal closed and a fresh greeting (only one bot turn)",
              not nxt["dialog"] and nxt["bot"] == [GREETING], nxt["bot"])
        leaked = [x for x in ("Test Patient Echo", "9000000003", "3 Test Street", "fever", "Thank you, Test")
                  if x in nxt["form"] or x in " ".join(nxt["bot"])]
        check("E2E next patient: nothing from the previous patient on screen", not leaked, leaked)
        check("E2E next patient: intake panel reset to 0 collected", any(b.startswith("0 of") for b in nxt["badge"]), nxt["badge"])
        # Close the second session cleanly (it is an empty, abandoned session).
        await cdp.call("Runtime.evaluate", expression="window.__intakeClient.disconnect()")
        await asyncio.sleep(2)
        pump.cancel()
        return {"transcript": transcript}


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="hoddoctor_e2e_intake_"))
    silence = tmp / "silence.wav"
    sf.write(silence, np.zeros(16000 * 5, dtype=np.float32), 16000, subtype="PCM_16")  # the patient types, not talks
    bot = web = chrome = None
    try:
        bot = subprocess.Popen([sys.executable, str(SERVER / "bot.py"), "-t", "webrtc", "--port", str(BOT_PORT)],
                               cwd=SERVER, stdout=open(tmp / "bot.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT)
        web = subprocess.Popen(f"npx vite --port {WEB_PORT} --strictPort", shell=True, cwd=WEB,
                               env={**os.environ, "VITE_START_ENDPOINT": f"http://localhost:{BOT_PORT}/start"},
                               stdout=open(tmp / "vite.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT)
        if not (_wait_http(f"http://localhost:{BOT_PORT}/api/records", 90) and _wait_http(f"http://localhost:{WEB_PORT}/", 60)):
            sys.exit(f"servers did not start; logs in {tmp}")
        chrome = subprocess.Popen([
            CHROME, "--headless=new", f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={tmp / 'profile'}",
            "--no-first-run", "--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream", f"--use-file-for-fake-audio-capture={silence}", "about:blank"])
        _wait_http(f"http://127.0.0.1:{CDP_PORT}/json/version", 30)
        asyncio.run(drive())
    finally:
        _kill(chrome)
        _kill(web)
        _kill(bot)
        time.sleep(1)

    # --- verify in Supabase what the browser was told ------------------------------
    log = re.sub("\x1b\\[[0-9;]*m", "", (tmp / "bot.log").read_text(encoding="utf-8", errors="replace"))
    saved_ids = re.findall(r"Intake ([0-9a-f-]{36}) saved", log)
    session_ids = re.findall(r"Intake session ([0-9a-f-]{36})", log)
    from dotenv import load_dotenv

    load_dotenv(SERVER / ".env", override=True)
    from services.db import get_client

    rows = [get_client().table("intake_sessions").select("*").eq("id", i).execute().data for i in session_ids]
    rows = [r[0] for r in rows if r]
    completed = [r for r in rows if r["status"] == "completed"]
    check("E2E DB: exactly one completed row for the finished patient", len(completed) == 1 and len(saved_ids) == 1,
          f"saved={saved_ids} statuses={[r['status'] for r in rows]}")
    if completed:
        p = completed[0]["patient"]
        print(f"  completed row id: {completed[0]['id']}")
        check("E2E DB: record holds the patient's answers",
              p["personal"]["full_name"].lower().startswith("test patient echo") and p["personal"]["phone"] == "9000000003"
              and p["medical_history"]["allergies"] == "none" and p["meta"]["completed_at"])
        check("E2E DB: symptoms stored structured",
              isinstance(p["visit"]["symptoms"], list) and all(isinstance(x, dict) for x in p["visit"]["symptoms"]))
    others = [r for r in rows if r["status"] != "completed"]
    check("E2E DB: the next patient's session is a different row with no previous-patient data",
          len(session_ids) == 2 and all("Echo" not in json.dumps(r["patient"]) for r in others),
          f"sessions={len(session_ids)}")
    warn = [line for line in log.splitlines() if re.search(r"\| (ERROR)", line)]
    check("E2E: no server errors", not warn, "\n".join(w[-200:] for w in warn[:5]))
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    print(f"  logs: {tmp}")
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
