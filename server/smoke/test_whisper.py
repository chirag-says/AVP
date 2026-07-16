"""Phase 0 smoke test: faster-whisper transcribes out_kokoro.wav (run test_kokoro.py first).
Run: ..\\.venv\\Scripts\\python.exe smoke\\test_whisper.py
"""
import pathlib
from faster_whisper import WhisperModel

WAV = pathlib.Path(__file__).resolve().parent / "out_kokoro.wav"

def main():
    if not WAV.exists():
        raise SystemExit(f"{WAV} not found — run test_kokoro.py first")

    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(WAV), beam_size=5)

    print(f"Detected language: {info.language} (p={info.language_probability:.2f})")
    text = " ".join(seg.text.strip() for seg in segments)
    print(f"Transcript: {text}")

if __name__ == "__main__":
    main()
