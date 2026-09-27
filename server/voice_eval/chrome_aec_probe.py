"""Does Chrome's own echo canceller attenuate the patient while the bot is talking?

With a fake capture device there is no acoustic path from speaker to mic, so
there is no echo to cancel. Any drop in the mic level while the page plays
audio is therefore the browser's processing acting on near-end speech during
far-end playback ("double-talk"): exactly what happens to a patient who talks
over the bot.

The probe page captures the fake mic (a looping patient recording) with the
same constraints the app requests, plays the bot's TTS through an <audio>
element for a few seconds, and samples the captured level every 50 ms.

    .venv\\Scripts\\python.exe server\\voice_eval\\chrome_aec_probe.py

With --agc it answers a second question instead: does AGC shrink the loudness
gap between a close patient and a quieter bystander talking in the pauses?

Local only: no Sarvam, no bot, isolated temporary Chrome profile.
"""
import asyncio
import functools
import http.server
import json
import pathlib
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import numpy as np
import soundfile as sf
import websockets

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE / ".cache"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HTTP_PORT, CDP_PORT = 8765, 9334
SR = 16000

PAGE = """<!doctype html><meta charset=utf-8><body><audio id=bot src="/probe_bot.wav"></audio><script>
async function probe(ec, ns, agc, playBot) {
  const stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: ec, noiseSuppression: ns, autoGainControl: agc, channelCount: 1}});
  const track = stream.getAudioTracks()[0];
  const ctx = new AudioContext(); await ctx.resume();
  const an = ctx.createAnalyser(); an.fftSize = 2048;
  ctx.createMediaStreamSource(stream).connect(an);
  const buf = new Float32Array(an.fftSize), samples = [];
  const bot = document.getElementById('bot'); bot.currentTime = 0;
  const t0 = performance.now(); let playing = false;
  await new Promise(done => {
    const timer = setInterval(() => {
      const t = (performance.now() - t0) / 1000;
      if (playBot && !playing && t >= 4) { playing = true; bot.play(); }
      an.getFloatTimeDomainData(buf);
      let s = 0; for (const v of buf) s += v * v;
      samples.push({t, db: 10 * Math.log10(s / buf.length + 1e-12), bot: !bot.paused && !bot.ended});
      if (t > 14) { clearInterval(timer); done(); }
    }, 50);
  });
  const settings = track.getSettings();
  track.stop(); await ctx.close(); bot.pause();
  return JSON.stringify({settings: {ec: settings.echoCancellation, ns: settings.noiseSuppression, agc: settings.autoGainControl}, samples});
}
</script>"""


def _serve(root: pathlib.Path):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def _run(configs, play_bot=True):
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list"))
    page = next(t for t in targets if t["type"] == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
        n = 0

        async def call(method, **params):
            nonlocal n
            n += 1
            await ws.send(json.dumps({"id": n, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n:
                    return msg

        await call("Page.navigate", url=f"http://localhost:{HTTP_PORT}/probe.html")
        await asyncio.sleep(2)
        results = {}
        for name, (ec, ns, agc) in configs.items():
            r = await call("Runtime.evaluate", expression=f"probe({str(ec).lower()}, {str(ns).lower()}, {str(agc).lower()}, {str(play_bot).lower()})",
                           awaitPromise=True, timeout=30000)
            results[name] = json.loads(r["result"]["result"]["value"])
        return results


def _level(x: np.ndarray, dbfs: float) -> np.ndarray:
    return x / np.sqrt(np.mean(x[np.abs(x) > 0.01] ** 2)) * 10 ** (dbfs / 20)


def _two_clusters(db: list[float]) -> tuple[float, float]:
    """1-D 2-means: centroids of the loud (patient) and quiet (bystander) frames."""
    x = np.array(db)
    lo, hi = np.percentile(x, 20), np.percentile(x, 80)
    for _ in range(30):
        loud = np.abs(x - hi) < np.abs(x - lo)
        lo, hi = x[~loud].mean(), x[loud].mean()
    return float(hi), float(lo)


def main():
    agc_mode = "--agc" in sys.argv
    root = pathlib.Path(tempfile.mkdtemp(prefix="aec_probe_"))
    (root / "probe.html").write_text(PAGE, encoding="utf-8")
    shutil.copy(CACHE / "tts" / "bot.wav", root / "probe_bot.wav")
    # Patient: continuous speech (phrases back to back) at -24 dBFS, so it overlaps the bot.
    parts = []
    for pid in ("metformin", "allergy", "statin", "thyroid"):
        x = sf.read(CACHE / "tts" / f"{pid}.wav", dtype="float32")[0]
        parts += [x / np.sqrt(np.mean(x[np.abs(x) > 0.01] ** 2)) * 10 ** (-24 / 20), np.zeros(int(0.15 * SR))]
    mic = np.concatenate(parts).astype(np.float32)
    if agc_mode:
        # Close patient at -24 dBFS, then a bystander 20 dB quieter in the pause.
        patient = _level(sf.read(CACHE / "tts" / "metformin.wav", dtype="float32")[0], -24)
        bystander = _level(sf.read(CACHE / "tts" / "background.wav", dtype="float32")[0][: 5 * SR], -44)
        gap = np.zeros(int(0.3 * SR))
        mic = np.concatenate([patient, gap, bystander, gap]).astype(np.float32)
    sf.write(root / "patient.wav", mic, SR, subtype="PCM_16")

    srv = _serve(root)
    chrome = subprocess.Popen([
        CHROME, "--headless=new", f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={root / 'profile'}",
        "--no-first-run", "--autoplay-policy=no-user-gesture-required",
        "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={root / 'patient.wav'}", "about:blank",
    ])
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        configs = {
            "ec=1 ns=1 agc=1 (app default)": (True, True, True),
            "ec=1 ns=1 agc=0": (True, True, False),
        } if agc_mode else {
            "ec=1 ns=1 agc=1 (app default)": (True, True, True),
            "ec=0 ns=1 agc=1": (False, True, True),
            "ec=1 ns=0 agc=0": (True, False, False),
            "ec=0 ns=0 agc=0 (raw)": (False, False, False),
        }
        results = asyncio.run(_run(configs, play_bot=not agc_mode))
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(chrome.pid)], capture_output=True)
        srv.shutdown()

    if agc_mode:
        print("Close patient (-24 dBFS) vs bystander 20 dB quieter in the pauses, as delivered by Chrome")
        for name, r in results.items():
            frames = [x["db"] for x in r["samples"] if x["t"] > 2 and x["db"] > -70]
            hi, lo = _two_clusters(frames)
            print(f"{name:32s} patient {hi:6.1f} dB | bystander {lo:6.1f} dB | gap {hi - lo:5.1f} dB (20 dB in the source)")
        shutil.rmtree(root, ignore_errors=True)
        return
    print("Patient speech level captured by Chrome, while the page is silent vs while it plays the bot's TTS")
    print("(fake mic: there is NO acoustic echo; any drop is browser processing acting on the patient)\n")
    for name, r in results.items():
        s = r["samples"]
        # Skip the first 2 s (AGC/NS settling) and compare speech-level frames.
        quiet = [x["db"] for x in s if not x["bot"] and x["t"] > 2 and x["db"] > -60]
        during = [x["db"] for x in s if x["bot"]]
        loud_during = [d for d in during if d > -60]
        q, d = statistics.median(quiet), statistics.median(during) if during else float("nan")
        print(f"{name:32s} settings={r['settings']}")
        print(f"    bot silent: median {q:6.1f} dB | bot playing: median {d:6.1f} dB "
              f"-> drop {q - d:5.1f} dB | speech-level frames while bot plays: {len(loud_during)}/{len(during)}")
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
