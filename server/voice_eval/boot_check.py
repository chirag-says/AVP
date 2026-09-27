"""Run the real intake bot with Supabase disabled, for checks and browser tests.

Exercises bot.py's run_bot() wiring (config, STT factory, echo guard, turn
strategies, telemetry, observers) exactly as a live session would. Supabase is
switched off in-process so a check never writes a row to the real database.

    cd server
    ..\.venv\Scripts\python.exe voice_eval\boot_check.py                          # headless eval transport
    ..\.venv\Scripts\python.exe voice_eval\boot_check.py --transport webrtc --port 7872   # for browser_e2e.py
"""
import argparse
import pathlib
import sys

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

import bot as intake_bot  # noqa: E402  (not named `bot`: the runner would call the module)

intake_bot.HAVE_SUPABASE = False  # never touch the real intake_sessions table from a check

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", default="eval")
    ap.add_argument("--port", default="7871")
    args = ap.parse_args()

    from pipecat.runner.run import main

    sys.argv = ["bot.py", "-t", args.transport, "--port", args.port]
    main()
