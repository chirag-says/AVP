"""Real-browser end-to-end check of the intake voice path, with a scripted "microphone".

Starts the intake bot (Supabase disabled), the Vite dev server, and a headless
Chrome with an isolated temporary profile. Chrome's fake capture device plays a
WAV file as the microphone, so the audio goes through Chrome's real
getUserMedia, Chrome's own audio processing (AEC/NS/AGC), daily-js, WebRTC,
aiortc, Sarvam, Gemini and Sarvam TTS: the whole production path except the
physical mic and room.

Answers two questions a synthetic STT harness can't:
  - what does the LIVE browser track report for the requested processing
    (REQUESTED / ACTUAL / MISMATCH, printed by the page and the server)?
  - what reaches the server after Chrome's processing (voice_telemetry levels)?

    .venv\\Scripts\\python.exe server\\voice_eval\\browser_e2e.py --audio path.wav [--query "agc=0&ns=0"] [--seconds 40]

A fake device is not a laptop microphone: acoustic echo, room reverberation and
mic placement are NOT tested here. Use TEST_PLAN.md's live protocol for those.
Costs a little Sarvam STT/TTS and one Gemini session per run.
"""
import argparse
import asyncio
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

HERE = pathlib.Path(__file__).resolve().parent
SERVER_DIR = HERE.parent
WEB_DIR = SERVER_DIR.parent / "web"
CHROME = os.getenv("CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
BOT_PORT, WEB_PORT, CDP_PORT = 7872, 5199, 9333

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def _wait_http(url: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _kill(proc: subprocess.Popen | None):
    if proc and proc.poll() is None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)


class _CDP:
    def __init__(self, ws):
        self.ws, self.next_id, self.pending, self.console = ws, 0, {}, []

    async def pump(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self.pending:
                self.pending.pop(msg["id"]).set_result(msg)
            elif msg.get("method") == "Runtime.consoleAPICalled":
                self.console.append(_render_console(msg["params"]))
            elif msg.get("method") == "Runtime.exceptionThrown":
                self.console.append("EXCEPTION " + json.dumps(msg["params"]["exceptionDetails"])[:300])

    async def call(self, method: str, **params):
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = fut
        await self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        return await asyncio.wait_for(fut, 15)


def _render_console(p: dict) -> str:
    parts = []
    for a in p.get("args", []):
        if "value" in a:
            parts.append(str(a["value"]))
        elif p.get("type") == "table" and a.get("preview"):
            # console.table: render each row object from the preview.
            for row in a["preview"].get("properties", []):
                cells = {c["name"]: c.get("value") for c in (row.get("valuePreview") or {}).get("properties", [])}
                parts.append("\n    " + "  ".join(f"{k}={v}" for k, v in cells.items()))
        else:
            parts.append(a.get("description", a.get("type", "")))
    return f"[{p.get('type')}] " + " ".join(parts)


async def _key(cdp, key: str, down: bool):
    # A DOM KeyboardEvent reaches the app's window listener regardless of
    # headless window focus (CDP Input events need a focused window).
    kind = "keydown" if down else "keyup"
    r = await cdp.call("Runtime.evaluate", expression=f"window.dispatchEvent(new KeyboardEvent('{kind}', {{key: '{key}'}}))")
    state = await cdp.call("Runtime.evaluate", expression=(
        "JSON.stringify([...document.querySelectorAll('button')].filter(b => /speaking \(/.test(b.textContent))"
        ".map(b => ({text: b.textContent, disabled: b.disabled, active: !b.className.includes('border')})))"))
    print(f"  [harness] {kind} {key} dispatched={r.get('result', {}).get('result', {}).get('value')} "
          f"panel={state.get('result', {}).get('result', {}).get('value')}")


async def _drive(url: str, seconds: float, hold_key: str | None = None) -> list[str]:
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list"))
    page = next(t for t in targets if t["type"] == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
        cdp = _CDP(ws)
        pump = asyncio.create_task(cdp.pump())
        await cdp.call("Runtime.enable")
        await cdp.call("Page.enable")
        await cdp.call("Page.navigate", url=url)
        await asyncio.sleep(4)
        await cdp.call("Runtime.evaluate", expression="document.querySelector('button.orb-button').click()")
        if hold_key:  # tester's scenario mark (e.g. P = patient) held for the session
            waited = 0.0
            while waited < 60:  # the panel only accepts marks once the bot is ready
                r = await cdp.call("Runtime.evaluate", expression=(
                    "[...document.querySelectorAll('button')].some(b => /speaking \(/.test(b.textContent) && !b.disabled)"))
                if r.get("result", {}).get("result", {}).get("value"):
                    break
                await asyncio.sleep(0.5)
                waited += 0.5
            await _key(cdp, hold_key, True)
            await asyncio.sleep(max(5.0, seconds - 7 - waited))
            await _key(cdp, hold_key, False)
            await asyncio.sleep(2)
        else:
            await asyncio.sleep(seconds)
        await cdp.call("Runtime.evaluate", expression="document.querySelector('button.orb-button').click()")
        await asyncio.sleep(4)
        pump.cancel()
        return cdp.console


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True, help="WAV played as the microphone (loops)")
    ap.add_argument("--query", default="", help='page query string, e.g. "agc=0&ns=0"')
    ap.add_argument("--seconds", type=float, default=40)
    ap.add_argument("--bot-env", action="append", default=[], help="KEY=VALUE for the bot process (repeatable)")
    ap.add_argument("--hold-key", help="scenario mark key to hold during the session, e.g. p")
    args = ap.parse_args()

    audio = pathlib.Path(args.audio).resolve()
    logs = pathlib.Path(tempfile.mkdtemp(prefix="hoddoctor_e2e_"))
    profile = logs / "chrome-profile"  # isolated: never the user's Chrome profile
    bot = web = chrome = None
    try:
        bot = subprocess.Popen(
            [sys.executable, str(HERE / "boot_check.py"), "--transport", "webrtc", "--port", str(BOT_PORT)],
            cwd=SERVER_DIR, stdout=open(logs / "bot.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT,
            env={**os.environ, **dict(kv.split("=", 1) for kv in args.bot_env)},
        )
        web = subprocess.Popen(
            f"npx vite --port {WEB_PORT} --strictPort", shell=True, cwd=WEB_DIR,
            env={**os.environ, "VITE_START_ENDPOINT": f"http://localhost:{BOT_PORT}/start"},
            stdout=open(logs / "vite.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT,
        )
        if not (_wait_http(f"http://localhost:{BOT_PORT}/api/records", 90) and _wait_http(f"http://localhost:{WEB_PORT}/", 60)):
            sys.exit(f"servers did not start; logs in {logs}")
        chrome = subprocess.Popen([
            CHROME, "--headless=new", f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required",
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={audio}", "about:blank",
        ])
        if not _wait_http(f"http://127.0.0.1:{CDP_PORT}/json/version", 30):
            sys.exit("chrome did not start")
        query = f"?{args.query}" if args.query else ""
        console = asyncio.run(_drive(f"http://localhost:{WEB_PORT}/{query}", args.seconds, args.hold_key))
    finally:
        _kill(chrome)
        _kill(web)
        _kill(bot)
        time.sleep(1)

    print("=== browser console ([mic] lines and errors) ===")
    for line in console:
        if "[mic]" in line or "table" in line[:8] or "EXCEPTION" in line or "[error]" in line:
            print(line)
    ansi = re.compile("\[[0-9;]*m")  # loguru colour codes
    bot_log = ansi.sub("", (logs / "bot.log").read_text(encoding="utf-8", errors="replace"))
    print("\n=== server: live track settings reported by the page ===")
    m = re.search(r"Client mic, live track settings:\n((?:.*\n){1,8})", bot_log)
    print(m.group(1) if m else "(not received)")
    print("=== server: voice_telemetry ===")
    for line in bot_log.splitlines():
        if "voice_telemetry" in line:
            print(json.dumps(json.loads(line.split("voice_telemetry ", 1)[1]), indent=1))
    errors = [line for line in bot_log.splitlines() if re.search(r"\| (ERROR|WARNING)", line)]
    print("=== server warnings/errors ===\n" + ("\n".join(e[-220:] for e in errors[:15]) or "(none)"))
    shutil.rmtree(profile, ignore_errors=True)
    print(f"\nfull logs: {logs}")


if __name__ == "__main__":
    main()
