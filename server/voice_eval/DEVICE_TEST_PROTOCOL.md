# Real-device test protocol (developer only)

Runs the actual app with real microphones and real rooms. Records only
content-free metrics and your notes. No audio is saved; use invented answers.

## Devices

| ID | Device | Page URL flags | Why |
|---|---|---|---|
| A | Laptop built-in mic + laptop speakers | `?ec=1` (default) | worst realistic case; AEC is required (acoustic echo) |
| B1 | Wired headset (mic + earphones) | `?ec=1` | compare against B2 |
| B2 | Wired headset | `?ec=0` | no acoustic echo path, so AEC can be off; see "why ec=0" |
| C | Bluetooth headset (if available) | `?ec=0` | Bluetooth mics usually run 16 kHz narrowband in call mode; check `sample_rate` |

**Why test ec=0 on headsets.** Measured in Chrome 153 (`chrome_aec_probe.py`):
with echo cancellation ON, the patient's voice is attenuated by 24–34 dB
whenever the bot is talking, even with no echo present. That is what makes
talking over the bot unreliable. A headset has no speaker-to-mic path, so AEC
has nothing to cancel; turning it off should restore barge-in. Confirm it here.
Never use `ec=0` with laptop speakers.

## Setup

1. Server, with scenario scoring on and logs captured (INFO level has no patient content):
   ```
   set VOICE_DEBUG_SCENARIO=1
   .venv\Scripts\python.exe server\bot.py -t webrtc 2>&1 | powershell -c "$input | Tee-Object server\voice_eval\results\live_<device>.log"
   ```
2. Web app: `npm --prefix web run dev`.
3. Open `http://localhost:5173/?scenario=<test>&ec=<0|1>` (test names below).
4. Check the browser console's `[mic] live track settings` table. Every
   requested row must say `MISMATCH no`. Note `ACTUAL sampleRate`.
5. One test = one conversation: click the orb, run the script, click the orb
   to end. The server prints one `voice_telemetry` line when it ends.
6. After each device: `..\.venv\Scripts\python.exe voice_eval\collect_telemetry.py results\live_<device>.log --csv results\device_matrix.csv`
   and fill the `notes` column from the checklist below.

## Marking (the ground truth)

The pipeline cannot tell people apart, so the tester (a third person, or
the bystander if needed) marks who is speaking: **hold P** while the patient
speaks, **hold B** while the other person speaks, release when they stop.
Hold both if both talk. Don't mark the bot. Marks are timing only.

## Tests (per device)

Patient answers from the script: "My name is Ramesh Kumar Sharma." /
"I take metformin five hundred milligrams twice a day." / "My number is
nine eight seven six five, four three two one zero." / "I am allergic to
penicillin." Speak at 30–50 cm (laptop) or normally (headset).

| # | `?scenario=` | Setup | What to do |
|---|---|---|---|
| 1 | `quiet` | quiet room | answer each question normally |
| 2 | `fan` | table fan 1–2 m, high | same |
| 3 | `ac` | AC running | same |
| 4 | `tv` | TV/music at conversation level, 2–3 m | same |
| 5 | `bystander` | second person 1–1.5 m | after each patient answer, bystander says an unrelated sentence in the pause ("Did you pay at the counter?") |
| 6 | `bystander_loud` | second person 0.5–1 m, normal-loud voice | same as 5 |
| 7 | `soft` | quiet room | patient answers at a near-whisper |
| 8 | `talk_over_bot` | quiet room | patient starts answering while the bot is still asking |
| 9 | `interrupt` | quiet room | 1 s into a bot question, patient says "Sorry, wait, I have a question" |
| 10 | `overlap` | second person 1 m | patient and bystander talk at the same time (bystander reads a newspaper aloud) |
| 11 | `echo_silent` | laptop speakers loud (device A only) | nobody speaks during two bot questions (Case A/D) |

Optional, per device: repeat tests 5 and 6 with `&vi=1` (browser/OS voice
isolation). Chrome lists the constraint as supported but did not apply it on a
fake device; the console table's `voiceIsolation ACTUAL` row shows whether your
device applies it. Adopt only if bystander leakage drops AND the patient's
medication names stay correct (voice isolation can distort speech).

Echo-guard cases map to: A = test 11, B = test 8/9, C = test 7 during bot speech,
D = test 11 at max volume, E = test 8 starting in the bot's last words.

## What to record (checklist → `notes`)

- speech detected? (patientSpeechDetected should be n/n)
- did TTS stop when the patient spoke? how fast did it feel?
- did the patient's words appear correctly in the transcript panel?
- did any bystander words appear? (transcripts_bystander > 0 = leak)
- were the bot's own words transcribed as the patient?
- was the turn cut early (bot answered mid-sentence) or late (> 2 s wait)?
- intake form: were name/phone/meds saved correctly?

## Pass criteria

| Metric | Target |
|---|---|
| mic `mismatch` | empty |
| patientSpeechDetected | n/n in tests 1–7 |
| falseInterruption_marked, false_interruptions_empty_turn | 0 (tests 1–7, 11) |
| bargeIn_patient in tests 8/9 | ≥ 1, `tts_stop_median_s` < 0.5 |
| transcripts_bystander | 0 in test 5 (headset), recorded for laptop |
| vad_speech_without_transcript | 0 |
| snr_db | > 20 dB (else move the mic / change device) |
| echo_above_noise_db (device A) | < 10 dB (else AEC or volume problem) |
| patient_minus_bystander_db | record; > 12 dB on a device means a proximity gate could work there |

## Reading the result

- Low `snr_db` with correct transcripts in quiet but errors with fan/AC →
  environment noise; fix placement/device.
- `transcripts_bystander > 0` with a few dB of `patient_minus_bystander_db` →
  the mic can't separate them; use a close mic.
- Barge-in fails on A and B1 but works on B2 → browser AEC double-talk
  suppression; use headsets with `VITE_MIC_ECHO_CANCELLATION=false`.
- `tts_transcripts_dropped > 0` → echo reached STT; check speaker volume/AEC.
