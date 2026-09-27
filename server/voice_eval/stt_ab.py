"""A/B harness: how well does Sarvam STT hear a patient under realistic interference?

Developer tool, not part of the runtime. It streams synthetic patient audio to
Sarvam over the same WebSocket protocol Pipecat's SarvamSTTService uses (20 ms
base64 PCM frames at 16 kHz, flush after 0.8 s of trailing silence, exactly
like the intake bot's Silero stop), so config comparisons carry over.

    # 1. synthesize the labelled phrases once (Sarvam TTS, cached)
    .venv\\Scripts\\python.exe server\\voice_eval\\stt_ab.py generate
    # 2. mix them into interference conditions (deterministic, cached WAVs)
    .venv\\Scripts\\python.exe server\\voice_eval\\stt_ab.py render
    # 3. stream a condition set through one or more STT configs
    .venv\\Scripts\\python.exe server\\voice_eval\\stt_ab.py run --configs v3_worktree,v4_keyterms
    # 4. compare saved runs
    .venv\\Scripts\\python.exe server\\voice_eval\\stt_ab.py report

Synthetic audio is a proxy: TTS voices are cleaner than people, and the noise
is generated, not recorded. Use it to rank configurations, then confirm the
winner with the live test plan in server/voice_eval/TEST_PLAN.md.

Costs Sarvam credit (STT is billed per audio hour). No patient data is used.
"""
import argparse
import asyncio
import base64
import json
import os
import pathlib
import sys
import time
import urllib.parse
import zlib

import numpy as np
import soundfile as sf

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from dotenv import load_dotenv  # noqa: E402

from intake.engine import _normalize_phone  # noqa: E402
from voice.vocabulary import load_keyterms  # noqa: E402
from voice_eval.scoring import canon, contains_phrase, leaked_words, word_errors  # noqa: E402

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(SERVER_DIR / ".env", override=True)

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE / ".cache"
TTS_DIR = CACHE / "tts"
RESULTS_DIR = HERE / "results"
SR = 16000
FRAME = SR // 50  # 20 ms, the frame size aiortc hands Pipecat
FLUSH_AFTER_S = 0.8  # intake Silero stop_secs: Pipecat flushes Sarvam here
WAIT_AFTER_FLUSH_S = 4.0

SPEECH_DBFS = -26.0  # a patient near the mic after browser AGC
SOFT_DBFS = -46.0  # a quiet speaker / distant mic
ROOM_FLOOR_DBFS = -68.0  # never digital silence: real rooms hiss

# Interference relative to the patient's speech level, in dB.
CONDITIONS = {
    "clean": {},
    "fan": {"noise": "fan", "rel_db": -10},
    "ac": {"noise": "ac", "rel_db": -5},
    "babble_quiet": {"talker": "background", "rel_db": -15},
    "babble_loud": {"talker": "background", "rel_db": -6},
    "overlap": {"talker": "background", "rel_db": 0},
    "echo": {"talker": "bot", "rel_db": -10, "patient_starts_s": 1.2},
    "soft": {"speech_dbfs": SOFT_DBFS},
}
# Bot audio with no patient at all: any transcript here is a phantom user turn.
ECHO_ONLY_REPEATS = 3

CONFIGS = {
    # HEAD (31ac190): no Sarvam VAD params, server defaults.
    "v3_committed": {"model": "saaras:v3"},
    # Working tree before this change: the "gentle" block in bot.py.
    "v3_worktree": {
        "model": "saaras:v3",
        "positive_speech_threshold": "0.5",
        "negative_speech_threshold": "0.2",
        "min_speech_frames": "3",
        "first_turn_min_speech_frames": "3",
        "start_speech_volume_threshold": "-45",
        "num_initial_ignored_frames": "2",
    },
    # keyterms on saaras:v3 is rejected by Sarvam (WS close 4000: "only supported
    # by model saaras:v4"), despite the docs listing both models.
    "v4": {"model": "saaras:v4"},
    "v4_keyterms": {"model": "saaras:v4", "keyterms": True},
}


def _dbfs_rms(x: np.ndarray) -> float:
    return 20 * np.log10(np.sqrt(np.mean(x**2)) + 1e-12)


def _active_rms_db(x: np.ndarray) -> float:
    """RMS over the voiced part only, so leading/trailing silence doesn't skew levels."""
    frames = x[: len(x) // FRAME * FRAME].reshape(-1, FRAME)
    energies = np.sqrt(np.mean(frames**2, axis=1))
    active = energies[energies > energies.max() * 0.05]
    return 20 * np.log10(np.sqrt(np.mean(active**2)) + 1e-12)


def _scale_to(x: np.ndarray, target_db: float) -> np.ndarray:
    return x * 10 ** ((target_db - _active_rms_db(x)) / 20)


# --- step 1: synthesize ------------------------------------------------------


def _tts(client, text: str, voice: str, language: str, pace: float = 1.0) -> np.ndarray:
    resp = client.text_to_speech.convert(
        text=text,
        target_language_code=language,
        speaker=voice,
        model="bulbul:v3",
        pace=pace,
        speech_sample_rate=SR,
    )
    wav_bytes = base64.b64decode(resp.audios[0])
    import io

    audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SR:
        import soxr

        audio = soxr.resample(audio, sr, SR)
    return audio


def cmd_generate(_args):
    from sarvamai import SarvamAI

    spec = json.loads((HERE / "phrases.json").read_text(encoding="utf-8"))
    client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])
    TTS_DIR.mkdir(parents=True, exist_ok=True)

    jobs = [("bot", spec["bot"]["say"], spec["bot"]["voice"], "en-IN", spec["bot"]["pace"])]
    jobs.append(("background", spec["background"]["say"], spec["background"]["voice"], "en-IN", 1.0))
    voices = spec["patient_voices"]
    for i, p in enumerate(spec["phrases"]):
        jobs.append((p["id"], p["say"], voices[i % len(voices)], p.get("tts_language", "en-IN"), 1.0))

    for name, text, voice, lang, pace in jobs:
        out = TTS_DIR / f"{name}.wav"
        if out.exists():
            continue
        audio = _tts(client, text, voice, lang, pace)
        sf.write(out, audio, SR)
        print(f"  synthesized {name} ({voice}, {len(audio) / SR:.1f}s)")
    print(f"TTS cache: {TTS_DIR}")


# --- step 2: mix ---------------------------------------------------------------


def _pink(n: int, rng) -> np.ndarray:
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.maximum(np.fft.rfftfreq(n, 1 / SR), 20)
    return np.fft.irfft(spectrum / np.sqrt(freqs), n)


def _brown(n: int, rng) -> np.ndarray:
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.maximum(np.fft.rfftfreq(n, 1 / SR), 20)
    return np.fft.irfft(spectrum / freqs, n)


def _noise(kind: str, n: int, rng) -> np.ndarray:
    t = np.arange(n) / SR
    if kind == "fan":
        # Broadband airflow with blade-rate amplitude flutter and a motor hum.
        x = _pink(n, rng) * (1 + 0.25 * np.sin(2 * np.pi * 12 * t))
        x += 0.15 * np.std(x) * np.sin(2 * np.pi * 50 * t)
    else:  # ac: rumbly low end, compressor hum, a bit of hiss
        x = _brown(n, rng) + 0.5 * _pink(n, rng) * np.std(_brown(n, rng)) / np.std(_pink(n, rng))
        x += 0.3 * np.std(x) * (np.sin(2 * np.pi * 100 * t) + 0.5 * np.sin(2 * np.pi * 200 * t))
    return x / (np.sqrt(np.mean(x**2)) + 1e-12)  # unit RMS


def _loop_to(x: np.ndarray, n: int) -> np.ndarray:
    reps = int(np.ceil(n / len(x)))
    return np.tile(x, reps)[:n]


def _mix(speech: np.ndarray, cond: dict, voices: dict[str, np.ndarray], rng) -> tuple[np.ndarray, float]:
    """Return (mixed audio, flush time in seconds)."""
    speech_db = cond.get("speech_dbfs", SPEECH_DBFS)
    lead = cond.get("patient_starts_s", 0.6)
    speech = _scale_to(speech, speech_db)
    flush_at = lead + len(speech) / SR + FLUSH_AFTER_S
    n = int((flush_at + WAIT_AFTER_FLUSH_S) * SR)

    out = np.zeros(n, dtype=np.float64)
    start = int(lead * SR)
    out[start : start + len(speech)] += speech
    out += rng.standard_normal(n) * 10 ** (ROOM_FLOOR_DBFS / 20)

    if "noise" in cond:
        level = 10 ** ((speech_db + cond["rel_db"]) / 20)
        out += _noise(cond["noise"], n, rng) * level
    if "talker" in cond:
        other = _scale_to(voices[cond["talker"]], speech_db + cond["rel_db"])
        if cond["talker"] == "background":
            out += _loop_to(other, n)  # a bystander who keeps talking, gaps included
        else:
            out[: min(n, len(other))] += other[:n]  # bot audio playing as the patient barges in
    return np.clip(out, -1, 1).astype(np.float32), flush_at


def cmd_render(_args):
    spec = json.loads((HERE / "phrases.json").read_text(encoding="utf-8"))
    voices = {name: sf.read(TTS_DIR / f"{name}.wav", dtype="float32")[0] for name in ("bot", "background")}
    manifest = []
    for cond_name, cond in CONDITIONS.items():
        cond_dir = CACHE / "mix" / cond_name
        cond_dir.mkdir(parents=True, exist_ok=True)
        for p in spec["phrases"]:
            if p.get("qualitative") and cond_name != "clean":
                continue
            rng = np.random.default_rng(zlib.crc32(f"{cond_name}:{p['id']}".encode()))
            speech = sf.read(TTS_DIR / f"{p['id']}.wav", dtype="float32")[0]
            mixed, flush_at = _mix(speech, cond, voices, rng)
            sf.write(cond_dir / f"{p['id']}.wav", mixed, SR, subtype="PCM_16")
            manifest.append({"condition": cond_name, "phrase": p["id"], "flush_at": round(flush_at, 3)})

    echo_dir = CACHE / "mix" / "echo_only"
    echo_dir.mkdir(parents=True, exist_ok=True)
    for i in range(ECHO_ONLY_REPEATS):
        rng = np.random.default_rng(1000 + i)
        bot = _scale_to(voices["bot"], SPEECH_DBFS + CONDITIONS["echo"]["rel_db"])
        n = len(bot) + int((FLUSH_AFTER_S + WAIT_AFTER_FLUSH_S) * SR)
        out = np.zeros(n)
        out[: len(bot)] = bot
        out += rng.standard_normal(n) * 10 ** (ROOM_FLOOR_DBFS / 20)
        sf.write(echo_dir / f"echo_only_{i}.wav", out.astype(np.float32), SR, subtype="PCM_16")
        manifest.append({"condition": "echo_only", "phrase": f"echo_only_{i}", "flush_at": round(len(bot) / SR + FLUSH_AFTER_S, 3)})

    (CACHE / "mix" / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"Rendered {len(manifest)} clips into {CACHE / 'mix'}")


# --- step 3: stream to Sarvam ----------------------------------------------------


def _ws_url(cfg: dict, keyterms_format: str) -> str:
    params = {
        "language-code": "en-IN",
        "model": cfg["model"],
        "mode": "transcribe",
        "sample_rate": str(SR),
        "flush_signal": "true",
    }
    for k, v in cfg.items():
        if k not in ("model", "keyterms"):
            params[k] = v
    if cfg.get("keyterms"):
        terms = load_keyterms(limit=50)
        params["keyterms"] = json.dumps(terms) if keyterms_format == "json" else ",".join(terms)
    return "wss://api.sarvam.ai/speech-to-text/ws?" + urllib.parse.urlencode(params)


async def _stream_clip(url: str, audio: np.ndarray, flush_at: float, mid_flushes: tuple[float, ...] = ()) -> dict:
    """Stream one clip in real time; flush at `flush_at` (and at any `mid_flushes`,
    to reproduce Pipecat flushing Sarvam on every VAD stop inside a turn)."""
    import websockets

    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    frame_bytes = FRAME * 2
    flush_byte = int(flush_at * SR) * 2
    pending_mid = sorted(int(t * SR) * 2 for t in mid_flushes if t < flush_at)
    messages: list[dict] = []
    errors: list[str] = []
    t0 = time.monotonic()
    flush_sent_at: float | None = None

    async with websockets.connect(url, additional_headers={"Api-Subscription-Key": os.environ["SARVAM_API_KEY"]}) as ws:

        async def receive():
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "data":
                    messages.append({"t": time.monotonic() - t0, "text": msg["data"].get("transcript", "")})
                elif msg.get("type") == "error":
                    errors.append(str(msg.get("data")))

        receiver = asyncio.create_task(receive())
        try:
            for offset in range(0, len(pcm), frame_bytes):
                while pending_mid and offset >= pending_mid[0]:
                    pending_mid.pop(0)
                    await ws.send(json.dumps({"type": "flush"}))
                if flush_sent_at is None and offset >= flush_byte:
                    await ws.send(json.dumps({"type": "flush"}))
                    flush_sent_at = time.monotonic() - t0
                chunk = pcm[offset : offset + frame_bytes]
                await ws.send(json.dumps({"audio": {"data": base64.b64encode(chunk).decode(), "sample_rate": SR, "encoding": "audio/wav"}}))
                # Real-time pacing: Sarvam's VAD sees the same timing it would live.
                await asyncio.sleep(max(0.0, t0 + (offset + frame_bytes) / (SR * 2) - time.monotonic()))
                if flush_sent_at is not None:
                    since_flush = time.monotonic() - t0 - flush_sent_at
                    # Sarvam often finalizes on its own VAD before our flush; if
                    # nothing is pending it sends nothing more, so cap the wait.
                    if any(m["t"] > flush_sent_at for m in messages) or since_flush > 3.5:
                        break
        finally:
            await asyncio.sleep(0.2)
            receiver.cancel()

    # Latency from the end of patient speech (not from the flush) to the last
    # transcript: what the patient actually waits for, before turn logic.
    speech_end = flush_at - FLUSH_AFTER_S
    texts = [m for m in messages if m["text"].strip()]
    return {
        "hyp": " ".join(m["text"] for m in texts),
        "segments": len(texts),
        "final_latency_s": round(texts[-1]["t"] - speech_end, 3) if texts else None,
        "errors": errors,
    }


async def _run_config(name: str, cfg: dict, manifest: list[dict], audio_root: pathlib.Path, concurrency: int, keyterms_format: str) -> list[dict]:
    url = _ws_url(cfg, keyterms_format)
    sem = asyncio.Semaphore(concurrency)

    async def one(item):
        path = audio_root / item["condition"] / f"{item['phrase']}.wav"
        if not path.exists():
            return None
        audio = sf.read(path, dtype="float32")[0]
        async with sem:
            for attempt in range(3):
                try:
                    res = await _stream_clip(url, audio, item["flush_at"])
                    break
                except Exception as e:  # network blips shouldn't sink a whole run
                    res = {"hyp": "", "segments": 0, "final_latency_s": None, "errors": [f"{type(e).__name__}: {e}"]}
                    await asyncio.sleep(3 * (attempt + 1))  # rate-limit backoff
        return {"config": name, **item, **res}

    results = [r for r in await asyncio.gather(*(one(i) for i in manifest)) if r]
    print(f"  {name}: {len(results)} clips")
    return results


def cmd_run(args):
    manifest = json.loads((CACHE / "mix" / "manifest.json").read_text())
    if args.conditions:
        wanted = set(args.conditions.split(","))
        manifest = [m for m in manifest if m["condition"] in wanted]
    if args.phrases:
        wanted = set(args.phrases.split(","))
        manifest = [m for m in manifest if m["phrase"] in wanted or m["condition"] == "echo_only"]
    audio_root = pathlib.Path(args.audio_dir) if args.audio_dir else CACHE / "mix"
    RESULTS_DIR.mkdir(exist_ok=True)
    for name in args.configs.split(","):
        tag = f"{name}{'_' + args.tag if args.tag else ''}"
        out = RESULTS_DIR / f"{tag}.json"
        if args.retry_errors:
            # Re-stream only clips that failed (e.g. rate limited) and merge.
            previous = json.loads(out.read_text(encoding="utf-8"))
            failed = {(r["condition"], r["phrase"]) for r in previous if r["errors"]}
            todo = [m for m in manifest if (m["condition"], m["phrase"]) in failed]
            redone = {(r["condition"], r["phrase"]): r for r in asyncio.run(
                _run_config(name, CONFIGS[name], todo, audio_root, args.concurrency, args.keyterms_format))}
            results = [redone.get((r["condition"], r["phrase"]), r) for r in previous]
        else:
            results = asyncio.run(_run_config(name, CONFIGS[name], manifest, audio_root, args.concurrency, args.keyterms_format))
        out.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    cmd_report(args)


# --- step 4: score ---------------------------------------------------------------


def _score(results: list[dict], spec: dict) -> dict:
    phrases = {p["id"]: p for p in spec["phrases"]}
    by_cond: dict[str, dict] = {}
    for r in results:
        c = by_cond.setdefault(r["condition"], {"err": 0, "words": 0, "terms": 0, "term_hits": 0, "phone": [], "leak": 0, "echo_words": 0, "lat": [], "errors": 0, "missed": 0})
        c["errors"] += bool(r["errors"])
        if r["final_latency_s"] is not None:
            c["lat"].append(r["final_latency_s"])
        hyp = canon(r["hyp"])
        if r["condition"] == "echo_only":
            c["echo_words"] += len(hyp)
            continue
        p = phrases[r["phrase"]]
        if p.get("qualitative"):
            continue
        c["missed"] += not hyp  # patient spoke, nothing came back: the worst failure
        ref = canon(p["ref"])
        c["err"] += word_errors(ref, hyp)
        c["words"] += len(ref)
        c["terms"] += len(p["terms"])
        c["term_hits"] += sum(contains_phrase(hyp, t) for t in p["terms"])
        if p.get("phone"):
            c["phone"].append(p["phone"] in _normalize_phone(r["hyp"]))
        intruder = {"echo": spec["bot"]["say"]}.get(r["condition"], spec["background"]["say"])
        if r["condition"] != "clean":
            c["leak"] += leaked_words(hyp, ref, intruder)
    summary = {}
    for cond, c in by_cond.items():
        summary[cond] = {
            "wer": round(c["err"] / c["words"], 3) if c["words"] else None,
            "term_acc": round(c["term_hits"] / c["terms"], 3) if c["terms"] else None,
            "phone_ok": all(c["phone"]) if c["phone"] else None,
            "leaked_words": c["leak"],
            "echo_only_words": c["echo_words"] if cond == "echo_only" else None,
            "median_final_latency_s": round(float(np.median(c["lat"])), 2) if c["lat"] else None,
            "missed": c["missed"],
            "clips_with_errors": c["errors"],
        }
    return summary


def cmd_report(args):
    spec = json.loads((HERE / "phrases.json").read_text(encoding="utf-8"))
    files = sorted(RESULTS_DIR.glob("*.json"))
    if getattr(args, "only", None):
        keep = set(args.only.split(","))
        files = [f for f in files if f.stem in keep]
    scored = {f.stem: _score(json.loads(f.read_text(encoding="utf-8")), spec) for f in files}
    conds = list(CONDITIONS) + ["echo_only"]
    for metric in ("wer", "term_acc", "missed", "leaked_words", "median_final_latency_s"):
        print(f"\n{metric}")
        print("config".ljust(24) + "".join(c[:12].rjust(13) for c in conds))
        for name, s in scored.items():
            row = []
            for c in conds:
                v = s.get(c, {}).get("echo_only_words" if (c == "echo_only" and metric == "leaked_words") else metric)
                row.append(("-" if v is None else str(v)).rjust(13))
            print(name[:23].ljust(24) + "".join(row))
    print("\nphone digits exact (per condition):")
    for name, s in scored.items():
        print(" ", name, {c: v["phone_ok"] for c, v in s.items() if v["phone_ok"] is not None})
    errs = {name: sum(v["clips_with_errors"] for v in s.values()) for name, s in scored.items()}
    print("clips with Sarvam/network errors:", errs)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate")
    sub.add_parser("render")
    run = sub.add_parser("run")
    run.add_argument("--configs", required=True, help=f"comma list of {', '.join(CONFIGS)}")
    run.add_argument("--conditions", help="comma list; default all")
    run.add_argument("--phrases", help="comma list of phrase ids; default all")
    run.add_argument("--audio-dir", help="alternative mix root, e.g. denoised copies")
    run.add_argument("--tag", help="suffix for the results file")
    run.add_argument("--concurrency", type=int, default=3, help="Sarvam rate-limits concurrent streams per key")
    run.add_argument("--retry-errors", action="store_true", help="re-run only failed clips of an existing result")
    run.add_argument("--keyterms-format", choices=["json", "comma"], default="json")
    rep = sub.add_parser("report")
    rep.add_argument("--only", help="comma list of result names")
    args = ap.parse_args()
    {"generate": cmd_generate, "render": cmd_render, "run": cmd_run, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
