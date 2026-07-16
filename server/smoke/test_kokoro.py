"""Phase 0 smoke test: Kokoro TTS speaks a sentence to a WAV file.
Run: ..\\.venv\\Scripts\\python.exe smoke\\test_kokoro.py
"""
import pathlib
import soundfile as sf
from kokoro_onnx import Kokoro

MODELS = pathlib.Path(__file__).resolve().parent.parent / "models"
OUT = pathlib.Path(__file__).resolve().parent / "out_kokoro.wav"

def main():
    kokoro = Kokoro(str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin"))
    samples, sample_rate = kokoro.create(
        "Hello, welcome to the hospital reception. Could you please tell me your full name?",
        voice="af_heart",
        speed=1.0,
        lang="en-us",
    )
    sf.write(str(OUT), samples, sample_rate)
    print(f"OK: wrote {OUT} ({len(samples) / sample_rate:.2f}s at {sample_rate}Hz)")

if __name__ == "__main__":
    main()
