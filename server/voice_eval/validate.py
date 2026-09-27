"""Focused validation runs: STT reliability, keyterm safety, numbers and dates.

    .venv\\Scripts\\python.exe server\\voice_eval\\validate.py generate
    .venv\\Scripts\\python.exe server\\voice_eval\\validate.py reliability   # v3 vs v4, 200 utterances each
    .venv\\Scripts\\python.exe server\\voice_eval\\validate.py keyterms      # correct / wrong substitution / missing
    .venv\\Scripts\\python.exe server\\voice_eval\\validate.py numbers       # phone, date, age, dosage, duration
    .venv\\Scripts\\python.exe server\\voice_eval\\validate.py report

Same streaming protocol as stt_ab.py (and Pipecat). Models are interleaved
clip by clip so a bad minute on Sarvam's side hits both equally. Results are
saved without audio; transcripts of the invented test phrases are kept in
results/ (gitignored) for inspection.
"""
import argparse
import asyncio
import json
import math
import os
import pathlib
import sys
import tempfile
import time

import numpy as np
import soundfile as sf

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from voice_eval.stt_ab import (  # noqa: E402
    CONDITIONS, RESULTS_DIR, SR, TTS_DIR, _mix, _stream_clip, _tts,
)
from voice_eval.scoring import canon, contains_phrase, number_tokens, word_errors  # noqa: E402
from voice.vocabulary import DEFAULT_VOCABULARY_FILE, load_keyterms  # noqa: E402
from intake.engine import _normalize_phone  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
SETS = json.loads((HERE / "validation_sets.json").read_text(encoding="utf-8"))
PHRASES = json.loads((HERE / "phrases.json").read_text(encoding="utf-8"))
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


def _url(model: str, keyterms: list[str] | None = None) -> str:
    import urllib.parse

    params = {"language-code": "en-IN", "model": model, "mode": "transcribe",
              "sample_rate": str(SR), "flush_signal": "true"}
    if keyterms:
        params["keyterms"] = json.dumps(keyterms)
    return "wss://api.sarvam.ai/speech-to-text/ws?" + urllib.parse.urlencode(params)


def _clip(tts_id: str, condition: str, seed: int) -> tuple[np.ndarray, float]:
    speech = sf.read(TTS_DIR / f"{tts_id}.wav", dtype="float32")[0]
    voices = {n: sf.read(TTS_DIR / f"{n}.wav", dtype="float32")[0] for n in ("bot", "background")}
    return _mix(speech, CONDITIONS[condition], voices, np.random.default_rng(seed))


async def _run(jobs: list[dict], concurrency: int) -> list[dict]:
    """jobs: {url, tts_id, condition, seed, ...meta}. Returns jobs + outcome."""
    sem = asyncio.Semaphore(concurrency)

    async def one(job):
        if "audio" in job:  # pre-mixed (bystander geometry test)
            audio, flush_at = job["audio"], job["flush_at"]
        else:
            audio, flush_at = _clip(job["tts_id"], job["condition"], job["seed"])
        async with sem:
            outcome = "ok"
            for attempt in range(3):
                try:
                    res = await _stream_clip(job["url"], audio, flush_at, tuple(job.get("mid_flushes", ())))
                    if not res["hyp"].strip():
                        outcome = "empty"
                    if any("Rate limit" in e for e in res["errors"]):
                        outcome = "rate_limited"
                        await asyncio.sleep(3 * (attempt + 1))
                        continue
                    break
                except Exception as e:
                    res = {"hyp": "", "segments": 0, "final_latency_s": None, "errors": [f"{type(e).__name__}: {e}"[:200]]}
                    outcome = "rate_limited" if "Rate limit" in str(e) else "connection_failure"
                    await asyncio.sleep(3 * (attempt + 1))
            await asyncio.sleep(0.3)  # stay under the per-key stream limit
        return {**{k: v for k, v in job.items() if k not in ("url", "audio", "mid_flushes")}, **res, "outcome": outcome}

    return list(await asyncio.gather(*(one(j) for j in jobs)))


def _save(name: str, results: list[dict]):
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / f"validate_{name}.json").write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")


def _wilson(k: int, n: int) -> str:
    if not n:
        return "-"
    z, p = 1.96, k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return f"{100 * p:.1f}% [{100 * max(0, centre - half):.1f}-{100 * min(1, centre + half):.1f}]"


# --- generate -------------------------------------------------------------


def cmd_generate(_args):
    from sarvamai import SarvamAI

    client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])
    voices = PHRASES["patient_voices"]
    items = SETS["short"] + SETS["numbers"] + SETS["keyterm_safety"]
    for i, item in enumerate(items):
        out = TTS_DIR / f"{item['id']}.wav"
        if not out.exists():
            sf.write(out, _tts(client, item["say"], voices[i % len(voices)], "en-IN"), SR)
            print("  synthesized", item["id"])


# --- reliability ------------------------------------------------------------


def _medical_ids() -> list[str]:
    ids = [p["id"] for p in PHRASES["phrases"] if p.get("terms")]
    return ids


def cmd_reliability(args):
    phrase_ref = {p["id"]: p for p in PHRASES["phrases"]}
    short_ref = {s["id"]: s for s in SETS["short"]}
    items = []
    for rep in range(10):
        for s in SETS["short"]:
            items.append({"group": "short", "tts_id": s["id"], "condition": "clean", "seed": rep})
    noisy_pool = [p["id"] for p in PHRASES["phrases"] if not p.get("qualitative") and not p["id"].startswith("kt_")]
    for i in range(50):
        cond = ("fan", "ac", "babble_quiet", "soft")[i % 4]
        items.append({"group": "noisy", "tts_id": noisy_pool[i % len(noisy_pool)], "condition": cond, "seed": 100 + i})
    med = _medical_ids()
    for i in range(50):
        items.append({"group": "medical", "tts_id": med[i % len(med)], "condition": "clean", "seed": 200 + i})

    jobs = []
    for item in items:  # interleave: v3 and v4 of the same clip run back to back
        for model in ("saaras:v3", "saaras:v4"):
            jobs.append({**item, "model": model, "url": _url(model)})
    t0 = time.time()
    results = asyncio.run(_run(jobs, args.concurrency))
    for r in results:
        ref = (phrase_ref.get(r["tts_id"], {}).get("ref") or short_ref.get(r["tts_id"], {}).get("ref"))
        r["ref"], r["terms"] = ref, phrase_ref.get(r["tts_id"], {}).get("terms", [])
    _save("reliability", results)
    print(f"reliability: {len(results)} clips in {time.time() - t0:.0f}s")
    report_reliability(results)


def report_reliability(results):
    print(f"\n{'model':10s} {'group':8s} {'n':>4s} {'ok':>4s} {'empty':>6s} {'conn_fail':>9s} {'rate_lim':>8s}  empty-rate [95% CI]      WER    terms   median/p90 latency")
    for model in ("saaras:v3", "saaras:v4"):
        for group in ("short", "noisy", "medical", "ALL"):
            rs = [r for r in results if r["model"] == model and (group == "ALL" or r["group"] == group)]
            if not rs:
                continue
            count = {k: sum(r["outcome"] == k for r in rs) for k in ("ok", "empty", "connection_failure", "rate_limited")}
            errs = words = hits = terms = 0
            for r in rs:
                if r["ref"]:
                    ref, hyp = canon(r["ref"]), canon(r["hyp"])
                    errs, words = errs + word_errors(ref, hyp), words + len(ref)
                for t in r["terms"]:
                    terms += 1
                    hits += contains_phrase(canon(r["hyp"]), t)
            lat = sorted(r["final_latency_s"] for r in rs if r["final_latency_s"] is not None)
            lat_s = f"{np.median(lat):.2f}/{np.percentile(lat, 90):.2f}s" if lat else "-"
            print(f"{model:10s} {group:8s} {len(rs):4d} {count['ok']:4d} {count['empty']:6d} {count['connection_failure']:9d} "
                  f"{count['rate_limited']:8d}  {_wilson(count['empty'], len(rs)):22s} {errs / max(words, 1):6.3f} "
                  f"{(f'{hits}/{terms}' if terms else '-'):>7s}   {lat_s}")


# --- keyterm safety ---------------------------------------------------------------


def _lexicon() -> list[str]:
    vocab = json.loads(DEFAULT_VOCABULARY_FILE.read_text(encoding="utf-8"))["categories"]
    terms = {t.lower() for ts in vocab.values() for t in ts}
    terms |= {t.lower() for t in SETS["medical_lexicon_extra"]}
    terms |= {k["term"] for k in SETS["keyterm_safety"]}
    return sorted(terms)


def classify_term(hyp: str, term: str, lexicon: list[str], say: str) -> tuple[str, list[str]]:
    """correct | wrong_substitution (another real medical term took its place) | missing."""
    h = canon(hyp)
    if contains_phrase(h, term):
        return "correct", []
    said = canon(say)
    wrong = [t for t in lexicon if t != term and contains_phrase(h, t) and not contains_phrase(said, t)]
    return ("wrong_substitution", wrong) if wrong else ("missing", [])


def cmd_keyterms(args):
    listed = load_keyterms(50)
    site_file = pathlib.Path(tempfile.gettempdir()) / "hoddoctor_site_terms.json"
    site_file.write_text(json.dumps(["AVP Hospital"]), encoding="utf-8")
    with_site = load_keyterms(50, str(site_file))
    configs = {
        "v3": _url("saaras:v3"),
        "v4": _url("saaras:v4"),
        "v4+keyterms": _url("saaras:v4", listed),
        "v4+keyterms+site": _url("saaras:v4", with_site),
    }
    jobs = []
    for rep in range(2):
        for cond in ("clean", "fan"):
            for item in SETS["keyterm_safety"]:
                for name, url in configs.items():
                    jobs.append({"config": name, "url": url, "tts_id": item["id"], "condition": cond, "seed": rep,
                                 "term": item["term"], "kind": item["kind"], "say": item["say"]})
    results = asyncio.run(_run(jobs, args.concurrency))
    lex = _lexicon()
    for r in results:
        r["verdict"], r["substituted_with"] = classify_term(r["hyp"], r["term"], lex, r["say"])
        if r["outcome"] != "ok":
            r["verdict"] = f"stt_{r['outcome']}"
    _save("keyterms", results)
    report_keyterms(results)


def report_keyterms(results):
    print(f"\n{'config':18s} {'kind':11s} {'n':>3s} {'correct':>8s} {'WRONG SUB':>9s} {'missing':>8s} {'stt_fail':>8s}")
    for name in dict.fromkeys(r["config"] for r in results):
        for kind in ("in_list", "look_alike", "hospital", "ALL"):
            rs = [r for r in results if r["config"] == name and (kind == "ALL" or r["kind"] == kind)]
            v = [r["verdict"] for r in rs]
            print(f"{name:18s} {kind:11s} {len(rs):3d} {v.count('correct'):8d} {v.count('wrong_substitution'):9d} "
                  f"{v.count('missing'):8d} {sum(x.startswith('stt_') for x in v):8d}")
    subs = [(r["config"], r["condition"], r["term"], r["substituted_with"]) for r in results if r["verdict"] == "wrong_substitution"]
    print("\nwrong medical substitutions (config, condition, said, heard):")
    for s in subs:
        print("  ", s)


# --- numbers / dates -------------------------------------------------------------


def score_number(kind: str, expect, hyp: str) -> tuple[str, str]:
    toks = number_tokens(hyp)
    nums = [int(t) for t in toks if t.isdigit()]
    if kind == "phone":
        got = _normalize_phone(hyp)
        if got == expect:
            return "correct", got
        if not got:
            return "no_digits", got
        if len(got) < len(expect):
            return "digit_omission", got
        if len(got) > len(expect):
            dup = any(got[i] == got[i + 1] and expect.count(got[i] * 2) < got.count(got[i] * 2) for i in range(len(got) - 1))
            return ("digit_duplication" if dup else "extra_digits"), got
        return "digit_substitution", got
    if kind == "date":
        day = next((n for n in nums if 1 <= n <= 31), None)
        month = next((MONTHS[t] for t in toks if t in MONTHS), None)
        year = next((n for n in nums if 1900 <= n <= 2030), None)
        for a, b in zip(nums, nums[1:]):  # "nineteen eighty two" -> 19, 82
            if year is None and a in (19, 20) and b < 100:
                year = a * 100 + b
        got = {"day": day, "month": month, "year": year}
        wrong = [k for k in expect if got[k] != expect[k]]
        return ("correct" if not wrong else "incorrect_" + "+".join(wrong)), json.dumps(got)
    if kind == "age":
        return ("correct" if expect in nums else "incorrect"), str(nums)
    if kind == "dosage":
        ok_amount = expect["amount"] in nums
        ok_unit = any(t in ("mg", "milligram", "milligrams") for t in toks)
        ok_freq = expect["frequency"] in toks
        bad = [n for n, ok in (("amount", ok_amount), ("unit", ok_unit), ("frequency", ok_freq)) if not ok]
        return ("correct" if not bad else "incorrect_" + "+".join(bad)), str(nums)
    if kind == "duration":
        ok_v = expect["value"] in nums
        ok_u = any(t.startswith(expect["unit"]) for t in toks)
        return ("correct" if ok_v and ok_u else "incorrect"), str(nums)
    raise ValueError(kind)


def cmd_numbers(args):
    jobs = []
    for rep in range(3):
        for cond in ("clean", "fan", "soft"):
            for item in SETS["numbers"]:
                for model in ("saaras:v3", "saaras:v4"):
                    jobs.append({"model": model, "url": _url(model), "tts_id": item["id"], "condition": cond,
                                 "seed": rep, "kind": item["kind"], "expect": item["expect"]})
    results = asyncio.run(_run(jobs, args.concurrency))
    for r in results:
        r["verdict"], r["parsed"] = score_number(r["kind"], r["expect"], r["hyp"])
        if r["outcome"] != "ok":
            r["verdict"] = f"stt_{r['outcome']}"
    _save("numbers", results)
    report_numbers(results)


def report_numbers(results):
    from collections import Counter

    for model in ("saaras:v3", "saaras:v4"):
        print(f"\n{model}")
        for item in SETS["numbers"]:
            rs = [r for r in results if r["model"] == model and r["tts_id"] == item["id"]]
            c = Counter(r["verdict"] for r in rs)
            print(f"  {item['id']:18s} {c['correct']}/{len(rs)} correct  {dict((k, v) for k, v in c.items() if k != 'correct')}")
    print("\nfailures (model, id, condition, verdict, parsed | transcript):")
    for r in results:
        if r["verdict"] != "correct":
            print(f"  {r['model']} {r['tts_id']} {r['condition']} {r['verdict']} {r['parsed']} | {r['hyp'][:80]}")


# --- bystander geometry --------------------------------------------------------

# Bystander level relative to the patient, per mic geometry (inverse-square law,
# bystander ~1 m away; real rooms add reverberation, which narrows laptop gaps
# further, while a boom mic's near-field advantage mostly survives):
GEOMETRY = {
    "laptop (patient 50 cm)": -6,
    "lapel (~20 cm)": -14,
    "desk mic close (~10 cm)": -20,
    "headset boom (~3 cm)": -30,
}


def _bystander_clip(patient_id: str, rel_db: float, mode: str, seed: int) -> tuple[np.ndarray, float]:
    """Patient at -26 dBFS; bystander either talks in the patient's pause or over them."""
    rng = np.random.default_rng(seed)
    patient = sf.read(TTS_DIR / f"{patient_id}.wav", dtype="float32")[0]
    patient = patient / np.sqrt(np.mean(patient[np.abs(patient) > 0.01] ** 2)) * 10 ** (-26 / 20)
    other = sf.read(TTS_DIR / "background.wav", dtype="float32")[0]
    other = other / np.sqrt(np.mean(other[np.abs(other) > 0.01] ** 2)) * 10 ** ((-26 + rel_db) / 20)
    lead = int(0.6 * SR)
    if mode == "pause":  # the common case: someone answers/chats right after the patient
        other = other[: 3 * SR]
        start_other = lead + len(patient) + int(0.3 * SR)
        speech_end = start_other + len(other)
    else:  # talking over the patient the whole time
        start_other = 0
        speech_end = lead + len(patient)
        other = np.tile(other, 3)[: speech_end]
    n = speech_end + int(4.8 * SR)
    out = rng.standard_normal(n) * 10 ** (-68 / 20)
    out[lead : lead + len(patient)] += patient
    out[start_other : start_other + len(other)] += other[: n - start_other]
    return np.clip(out, -1, 1).astype(np.float32), speech_end / SR + 0.8


def cmd_bystander(args):
    patients = [p["id"] for p in PHRASES["phrases"] if not p.get("qualitative") and not p["id"].startswith("kt_")][:10]
    jobs = []
    for geo, rel in GEOMETRY.items():
        for mode in ("pause", "overlap"):
            for i, pid in enumerate(patients):
                audio, flush_at = _bystander_clip(pid, rel, mode, i)
                jobs.append({"model": "saaras:v3", "url": _url("saaras:v3"), "geometry": geo, "rel_db": rel,
                             "mode": mode, "tts_id": pid, "audio": audio, "flush_at": flush_at})
    results = asyncio.run(_run(jobs, args.concurrency))
    _save("bystander", results)
    report_bystander(results)


def _lcs(a: list[str], b: list[str]) -> int:
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def report_bystander(results):
    """Patient recall = patient words recovered in order (LCS); extra words = everything
    else in the transcript, i.e. what the bystander contributed to the patient's answer."""
    phrase_ref = {p["id"]: p for p in PHRASES["phrases"]}
    print(f"\n{'geometry':26s} {'bystander':>9s} {'mode':8s} {'patient words kept':>18s} "
          f"{'clips w/ extra words':>20s} {'extra words':>11s}")
    for geo in dict.fromkeys(r["geometry"] for r in results):
        for mode in ("pause", "overlap"):
            rs = [r for r in results if r["geometry"] == geo and r["mode"] == mode]
            kept = total = extra = extra_clips = 0
            for r in rs:
                ref, hyp = canon(phrase_ref[r["tts_id"]]["ref"]), canon(r["hyp"])
                common = _lcs(ref, hyp)
                kept, total = kept + common, total + len(ref)
                n_extra = len(hyp) - common
                extra += n_extra
                extra_clips += n_extra > 1  # tolerate one stray token
            print(f"{geo:26s} {rs[0]['rel_db']:>7d}dB {mode:8s} {100 * kept / max(total, 1):17.0f}% "
                  f"{f'{extra_clips}/{len(rs)}':>20s} {extra:11d}")


# --- final v4 reliability gate ------------------------------------------------------

# Pass bar, fixed before the run: no connection failures and at most this many
# silent empties (v3's measured rate was 1 in 178; this allows chance, not a regression).
FINAL_MAX_EMPTIES = 2


def cmd_final_v4(args):
    """One independent saaras:v4 session across every category that matters for intake."""
    from voice_eval.test_endpointing import CASES as PAUSE_CASES, build_audio, vad_stop_times

    url = _url("saaras:v4")
    phrase_ref = {p["id"]: p for p in PHRASES["phrases"]}
    jobs = []
    for rep_ in range(5):
        for s_ in SETS["short"]:
            jobs.append({"category": "short", "tts_id": s_["id"], "condition": "clean", "seed": 300 + rep_, "ref": s_["ref"]})
    pool = [p["id"] for p in PHRASES["phrases"] if not p.get("qualitative") and not p["id"].startswith("kt_")]
    for i in range(40):
        pid = pool[i % len(pool)]
        jobs.append({"category": "noisy", "tts_id": pid, "condition": ("fan", "ac", "babble_quiet", "soft")[i % 4],
                     "seed": 400 + i, "ref": phrase_ref[pid]["ref"], "terms": phrase_ref[pid]["terms"]})
    for item in SETS["keyterm_safety"]:
        jobs.append({"category": "medical", "tts_id": item["id"], "condition": "clean", "seed": 500,
                     "term": item["term"], "say": item["say"]})
    for p in PHRASES["phrases"]:
        if p.get("terms"):
            jobs.append({"category": "medical", "tts_id": p["id"], "condition": "clean", "seed": 501,
                         "ref": p["ref"], "terms": p["terms"]})
    for item in SETS["numbers"]:
        for cond in ("clean", "fan", "soft"):
            for rep_ in range(2 if item["kind"] in ("phone", "date") else 1):
                cat = "phone" if item["kind"] == "phone" else "date" if item["kind"] == "date" else "numbers_other"
                jobs.append({"category": cat, "tts_id": item["id"], "condition": cond, "seed": 600 + rep_,
                             "kind": item["kind"], "expect": item["expect"]})
    for cid, fragments, pauses in PAUSE_CASES:
        if not pauses:
            continue
        audio, _, speech_end = build_audio(cid, fragments, pauses)
        stops = asyncio.run(vad_stop_times(audio, 0.2))  # bot.py's VAD: Pipecat flushes Sarvam at each stop
        final = speech_end + 0.2
        for rep_ in range(2):
            jobs.append({"category": "pauses", "tts_id": cid, "condition": "clean", "seed": rep_,
                         "ref": " ".join(fragments), "audio": audio[: int((final + 4) * SR)], "flush_at": final,
                         "mid_flushes": tuple(t for t in stops if t < final - 0.05)})
    for j in jobs:
        j["url"] = url
    t0 = time.time()
    results = asyncio.run(_run(jobs, args.concurrency))
    lex = _lexicon()
    for r in results:
        if r.get("kind"):
            r["verdict"], r["parsed"] = score_number(r["kind"], r["expect"], r["hyp"])
        elif r.get("term"):
            r["verdict"], r["substituted_with"] = classify_term(r["hyp"], r["term"], lex, r["say"])
    _save("final_v4", results)
    print(f"final_v4: {len(results)} clips in {time.time() - t0:.0f}s")
    report_final_v4(results)


def report_final_v4(results):
    from collections import Counter

    outcomes = Counter(r["outcome"] for r in results)
    print(f"\n{'category':14s} {'n':>4s} {'empty':>6s} {'conn_fail':>9s} {'rate_lim':>8s}  accuracy                      median/p90 latency")
    for cat in dict.fromkeys(r["category"] for r in results):
        rs = [r for r in results if r["category"] == cat]
        c = Counter(r["outcome"] for r in rs)
        if cat in ("phone", "date", "numbers_other"):
            acc = f"{sum(r['verdict'] == 'correct' for r in rs)}/{len(rs)} exact"
        else:
            errs = words = hits = terms = subs = 0
            for r in rs:
                if r.get("ref"):
                    ref, hyp = canon(r["ref"]), canon(r["hyp"])
                    errs, words = errs + word_errors(ref, hyp), words + len(ref)
                for t in r.get("terms") or []:
                    terms += 1
                    hits += contains_phrase(canon(r["hyp"]), t)
                if r.get("term"):
                    terms += 1
                    hits += r["verdict"] == "correct"
                    subs += r["verdict"] == "wrong_substitution"
            acc = f"WER {errs / max(words, 1):.3f}" + (f", terms {hits}/{terms}, wrong subs {subs}" if terms else "")
        lat = sorted(r["final_latency_s"] for r in rs if r["final_latency_s"] is not None)
        lat_s = f"{np.median(lat):.2f}/{np.percentile(lat, 90):.2f}s" if lat else "-"
        print(f"{cat:14s} {len(rs):4d} {c['empty']:6d} {c['connection_failure']:9d} {c['rate_limited']:8d}  {acc:30s} {lat_s}")
    empties = outcomes["empty"]
    passed = outcomes["connection_failure"] == 0 and empties <= FINAL_MAX_EMPTIES
    print(f"\nTOTAL {len(results)} clips: empty {empties} ({_wilson(empties, len(results))}), "
          f"connection failures {outcomes['connection_failure']}, rate-limited {outcomes['rate_limited']}")
    print(f"PASS BAR (no connection failures, <= {FINAL_MAX_EMPTIES} empties): {'PASS' if passed else 'FAIL'}")
    for r in results:
        if r["outcome"] != "ok" or r.get("verdict") not in (None, "correct"):
            print(f"  {r['category']:8s} {r['tts_id']:20s} {r['condition']:12s} {r['outcome']:8s} {r.get('verdict', '')} | {r['hyp'][:70]}")


def cmd_report(_args):
    for name, fn in (("reliability", report_reliability), ("keyterms", report_keyterms),
                     ("numbers", report_numbers), ("bystander", report_bystander), ("final_v4", report_final_v4)):
        path = RESULTS_DIR / f"validate_{name}.json"
        if path.exists():
            print(f"\n######## {name}")
            fn(json.loads(path.read_text(encoding="utf-8")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["generate", "reliability", "keyterms", "numbers", "bystander", "final_v4", "report"])
    ap.add_argument("--concurrency", type=int, default=2)
    args = ap.parse_args()
    {"generate": cmd_generate, "reliability": cmd_reliability, "keyterms": cmd_keyterms,
     "numbers": cmd_numbers, "bystander": cmd_bystander, "final_v4": cmd_final_v4, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
