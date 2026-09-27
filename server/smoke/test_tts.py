"""Quick smoke test: can Sarvam TTS actually produce audio?

Run:  .venv\Scripts\python.exe server\smoke\test_tts.py
"""
import asyncio
import json
import os
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=True)

async def test_tts():
    import websockets

    api_key = os.environ.get("SARVAM_API_KEY")
    if not api_key:
        print("❌ SARVAM_API_KEY not set in server/.env")
        return

    url = "wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v2&send_completion_event=true"
    headers = {"api-subscription-key": api_key}

    print(f"🔌 Connecting to {url} ...")
    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            print("✅ WebSocket connected")

            # Send config
            config = {
                "type": "config",
                "data": {
                    "target_language_code": "en-IN",
                    "speaker": "anushka",
                    "speech_sample_rate": "22050",
                    "enable_preprocessing": False,
                    "min_buffer_size": 50,
                    "max_chunk_length": 150,
                    "output_audio_codec": "linear16",
                    "output_audio_bitrate": "128k",
                    "pace": 1.0,
                    "model": "bulbul:v2",
                },
            }
            await ws.send(json.dumps(config))
            print("📤 Config sent")

            # Send text
            text_msg = {"type": "text", "data": {"text": "Hello, welcome to the hospital."}}
            await ws.send(json.dumps(text_msg))
            print("📤 Text sent: 'Hello, welcome to the hospital.'")

            # Wait for responses
            audio_chunks = 0
            got_final = False
            try:
                async for message in ws:
                    if isinstance(message, str):
                        msg = json.loads(message)
                        msg_type = msg.get("type")
                        if msg_type == "audio":
                            audio_chunks += 1
                            audio_b64 = msg.get("data", {}).get("audio", "")
                            print(f"  🔊 Audio chunk #{audio_chunks} ({len(audio_b64)} b64 chars)")
                        elif msg_type == "event":
                            event_type = msg.get("data", {}).get("event_type")
                            print(f"  📋 Event: {event_type}")
                            if event_type == "final":
                                got_final = True
                                break
                        elif msg_type == "error":
                            error_msg = msg.get("data", {}).get("message", str(msg))
                            print(f"  ❌ Error from Sarvam: {error_msg}")
                            break
                        else:
                            print(f"  ❓ Unknown message type: {msg_type} -> {msg}")
            except asyncio.TimeoutError:
                print("  ⏰ Timeout waiting for audio")

            if audio_chunks > 0:
                print(f"\n✅ SUCCESS: Got {audio_chunks} audio chunks. TTS is working!")
            else:
                print(f"\n❌ FAILURE: Got 0 audio chunks. TTS is NOT producing audio.")

    except Exception as e:
        print(f"❌ Connection failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(test_tts())
