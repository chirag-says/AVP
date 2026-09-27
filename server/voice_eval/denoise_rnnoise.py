"""Apply RNNoise to the rendered voice_eval clips, the way Pipecat's RNNoiseFilter would.

Runs in a throwaway venv so pyrnnoise (which drags in matplotlib) never
enters the project venv unless the A/B justifies it:

    python -m venv %TEMP%\\rnn_eval_venv
    %TEMP%\\rnn_eval_venv\\Scripts\\pip install pyrnnoise==0.4.5 soundfile soxr numpy
    %TEMP%\\rnn_eval_venv\\Scripts\\python server\\voice_eval\\denoise_rnnoise.py
    .venv\\Scripts\\python server\\voice_eval\\stt_ab.py run --configs v4_keyterms \\
        --audio-dir server\\voice_eval\\.cache\\mix_rnnoise --tag rnnoise

Audio is fed in 20 ms frames through streaming resamplers, as the transport
would, and the per-frame CPU cost is reported.
"""
import pathlib
import time

import numpy as np
import soundfile as sf
import soxr
from pyrnnoise import RNNoise

HERE = pathlib.Path(__file__).resolve().parent
SRC, DST = HERE / ".cache" / "mix", HERE / ".cache" / "mix_rnnoise"
SR, FRAME = 16000, 320


def denoise(audio: np.ndarray) -> tuple[np.ndarray, float]:
    rn = RNNoise(sample_rate=48000)
    up = soxr.ResampleStream(SR, 48000, 1, dtype="int16", quality="QQ")
    down = soxr.ResampleStream(48000, SR, 1, dtype="int16", quality="QQ")
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    out, cpu = [], 0.0
    for i in range(0, len(pcm), FRAME):
        t = time.perf_counter()
        hi = up.resample_chunk(pcm[i : i + FRAME])
        for _prob, frame in rn.denoise_chunk(hi):
            frame = np.asarray(frame).reshape(-1)
            if np.issubdtype(frame.dtype, np.floating):
                frame = (np.clip(frame, -1, 1) * 32767).astype(np.int16)
            out.append(down.resample_chunk(frame.astype(np.int16)))
        cpu += time.perf_counter() - t
    y = np.concatenate(out).astype(np.float32) / 32767 if out else np.zeros(0, np.float32)
    # Keep clip length (and so the flush timing) identical to the original.
    y = np.pad(y, (0, max(0, len(audio) - len(y))))[: len(audio)]
    return y, cpu


def main():
    total_cpu = total_frames = 0
    for cond_dir in sorted(p for p in SRC.iterdir() if p.is_dir()):
        (DST / cond_dir.name).mkdir(parents=True, exist_ok=True)
        for wav in cond_dir.glob("*.wav"):
            audio = sf.read(wav, dtype="float32")[0]
            y, cpu = denoise(audio)
            sf.write(DST / cond_dir.name / wav.name, y, SR, subtype="PCM_16")
            total_cpu += cpu
            total_frames += len(audio) // FRAME
    (DST / "manifest.json").write_text((SRC / "manifest.json").read_text())
    print(f"RNNoise: {total_frames} frames, {1000 * total_cpu / total_frames:.3f} ms CPU per 20 ms frame")


if __name__ == "__main__":
    main()
