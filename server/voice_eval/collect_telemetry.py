r"""Turn the server's voice_telemetry log lines into one CSV row per test session.

    ..\.venv\Scripts\python.exe voice_eval\collect_telemetry.py results\live_laptop.log [more logs...] --csv results\device_matrix.csv

The log lines are content-free (levels, counts, timings, booleans), so the CSV
is too. Add your manual observations in the CSV's `notes` column.
"""
import argparse
import csv
import json
import pathlib
import re
import sys

ANSI = re.compile("\x1b\\[[0-9;]*m")

COLUMNS = [
    "log", "device", "scenario", "ec", "ns", "agc", "sample_rate", "channels", "mismatch",
    "snr_db", "echo_above_noise_db", "speech_p50_dbfs", "noise_p50_dbfs",
    "patientSpeechDetected", "bystanderSpeechDetected",
    "bargeIn_patient", "bargeIn_bystander", "bargeIn_unmarked",
    "transcripts_patient", "transcripts_bystander", "transcripts_unmarked",
    "tts_transcripts_from_stt", "tts_transcripts_to_llm", "tts_transcripts_dropped",
    "falseInterruption_marked", "false_interruptions_empty_turn",
    "patient_minus_bystander_db", "vad_speech_without_transcript",
    "tts_stop_median_s", "stt_latency_median_s", "notes",
]


def _row(log: str, t: dict) -> dict:
    mic = t.get("client_mic") or {}
    actual = {r["setting"]: r["actual"] for r in mic.get("rows", [])}
    lv = t.get("levels") or {}
    sc = t.get("scenario") or {}
    counts = t.get("counts") or {}
    med = lambda k: (t.get(k) or {}).get("median_s")  # noqa: E731
    return {
        "log": log, "device": mic.get("deviceLabel"), "scenario": sc.get("scenario"),
        "ec": actual.get("echoCancellation"), "ns": actual.get("noiseSuppression"), "agc": actual.get("autoGainControl"),
        "sample_rate": actual.get("sampleRate"), "channels": actual.get("channelCount"),
        "mismatch": ",".join(mic.get("mismatch", [])),
        "snr_db": t.get("snr_db_estimate"), "echo_above_noise_db": t.get("echo_above_noise_db"),
        "speech_p50_dbfs": (lv.get("speech") or {}).get("p50_dbfs"), "noise_p50_dbfs": (lv.get("noise") or {}).get("p50_dbfs"),
        "patientSpeechDetected": sc.get("patientSpeechDetected"), "bystanderSpeechDetected": sc.get("bystanderSpeechDetected"),
        **{f"bargeIn_{k}": (sc.get("bargeInTriggered") or {}).get(k, 0) for k in ("patient", "bystander", "unmarked")},
        **{f"transcripts_{k}": (sc.get("transcriptReceived") or {}).get(k, 0) for k in ("patient", "bystander", "unmarked")},
        "tts_transcripts_from_stt": (sc.get("transcriptDuringTTS") or {}).get("from_stt"),
        "tts_transcripts_to_llm": (sc.get("transcriptDuringTTS") or {}).get("delivered_to_llm"),
        "tts_transcripts_dropped": (sc.get("transcriptDuringTTS") or {}).get("dropped_by_echo_guard"),
        "falseInterruption_marked": sc.get("falseInterruption"), "false_interruptions_empty_turn": t.get("false_interruptions"),
        "patient_minus_bystander_db": sc.get("patient_minus_bystander_level_db"),
        "vad_speech_without_transcript": counts.get("vad_speech_without_transcript", 0),
        "tts_stop_median_s": med("tts_stop_after_interruption"), "stt_latency_median_s": med("stt_first_transcript_after_vad_stop"),
        "notes": "",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--csv", help="append rows to this CSV")
    args = ap.parse_args()
    rows = []
    for path in args.logs:
        for line in pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            line = ANSI.sub("", line)
            if "voice_telemetry " in line:
                rows.append(_row(pathlib.Path(path).name, json.loads(line.split("voice_telemetry ", 1)[1])))
    if not rows:
        sys.exit("no voice_telemetry lines found (is VOICE_TELEMETRY on, and did the session end?)")
    for r in rows:
        print(" | ".join(f"{k}={r[k]}" for k in COLUMNS if r[k] is not None and r[k] != ""))
    if args.csv:
        out = pathlib.Path(args.csv)
        new = not out.exists()
        with out.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            if new:
                w.writeheader()
            w.writerows(rows)
        print(f"\n{len(rows)} row(s) appended to {out}")


if __name__ == "__main__":
    main()
