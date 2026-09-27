"""Does flushing Sarvam at every VAD stop hurt paused answers (phone numbers, lists)?

Pipecat's SarvamSTTService flushes Sarvam on every VAD stop, finalizing that
segment. A shorter Silero stop_secs means more flushes inside one answer.
This streams the paused answers from test_endpointing.py to Sarvam with a
flush at every VAD stop Silero would emit, for each stop_secs, and scores
the joined transcript (a turn's segments are concatenated by the user
aggregator, so the joined text is what the LLM sees).

    .venv\\Scripts\\python.exe server\\voice_eval\\test_flush_fragmentation.py --stop-secs 0.2,0.8

Costs a little Sarvam STT credit (~1-2 minutes of audio per stop_secs value).
"""
import argparse
import asyncio
import pathlib
import sys

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from voice_eval.scoring import canon, word_errors  # noqa: E402
from voice_eval.stt_ab import CONFIGS, _stream_clip, _ws_url  # noqa: E402
from voice_eval.test_endpointing import CASES, _synthesize, build_audio, vad_stop_times  # noqa: E402

PAUSED = [c for c in CASES if c[2]]  # only answers that contain pauses


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop-secs", default="0.2,0.8")
    ap.add_argument("--config", default="v4_keyterms")
    args = ap.parse_args()
    _synthesize()
    url = _ws_url(CONFIGS[args.config], "json")
    for stop in (float(s) for s in args.stop_secs.split(",")):
        errs = words = 0
        print(f"\nstop_secs={stop}, config={args.config}")
        for cid, fragments, pauses in PAUSED:
            audio, _, speech_end = build_audio(cid, fragments, pauses)
            stops = await vad_stop_times(audio, stop)
            final = speech_end + stop
            mids = tuple(t for t in stops if t < final - 0.05)
            res = await _stream_clip(url, audio[: int((final + 4) * 16000)], final, mid_flushes=mids)
            ref = canon(" ".join(fragments))
            hyp = canon(res["hyp"])
            e = word_errors(ref, hyp)
            errs, words = errs + e, words + len(ref)
            print(f"  {cid:22s} flushes={len(mids) + 1} segments={res['segments']} errors={e}/{len(ref)} | {res['hyp'][:80]}")
        print(f"  WER over paused answers: {errs / words:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
