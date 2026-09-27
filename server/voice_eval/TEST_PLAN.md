# Voice input test plan (developer only)

Two layers:

1. **Offline suite**: repeatable, no microphone. Synthetic Indian-English
   patients (Sarvam TTS) mixed with generated interference, streamed to Sarvam
   the way Pipecat streams it. Use it to rank configurations.
2. **Live protocol**: a real mic, a real room, a real speaker. The only way to
   judge browser echo cancellation, AGC, mic placement and real bystanders.
   Offline numbers are a proxy and do not replace this.

No step records or stores patient audio. Use invented answers only.

## 1. Offline suite

Run from the repo root with the project venv.

| Script | What it answers | Cost |
|---|---|---|
| `server/voice_eval/stt_ab.py generate / render / run / report` | STT accuracy per config under fan, AC, bystanders, echo, soft speech | Sarvam STT ≈ ₹30/audio-hour |
| `server/voice_eval/test_echo_guard.py` | Does the bot's own echo trigger barge-in? How fast is a real barge-in? | free, local |
| `server/voice_eval/test_endpointing.py --stop-secs 0.2,0.8` | Do pauses end the turn early? How long after the patient stops? | free after first TTS |
| `server/voice_eval/test_flush_fragmentation.py` | Does flushing Sarvam at every VAD stop hurt paused answers? | small |
| `server/voice_eval/test_pipeline_integration.py` | Real chain (Sarvam → echo filter → aggregator) produces the user message | seconds of STT |
| `server/voice_eval/boot_check.py` (run from `server/`) | bot.py assembles its pipeline headless; Supabase disabled | seconds |
| `server/voice_eval/denoise_rnnoise.py` | Server denoiser A/B (runs in a throwaway venv) | free |

Keep `--concurrency` at 2–3: Sarvam rate-limits concurrent streams per key
(WS close 1003), and rate-limited runs also produced silent empty results
for saaras:v4. Re-run failures with `run --retry-errors`.

### Conditions (`CONDITIONS` in stt_ab.py)

Patient speech at −26 dBFS (−46 for *soft*), −68 dBFS room hiss always on.

| Condition | Interference |
|---|---|
| clean | none |
| fan | pink noise, 12 Hz blade flutter, 50 Hz hum, −10 dB vs speech |
| ac | brown noise + 100/200 Hz hum, −5 dB |
| babble_quiet / babble_loud / overlap | another person talking continuously at −15 / −6 / 0 dB |
| echo | bot TTS at −10 dB (weak AEC), patient barges in at 1.2 s |
| echo_only | bot TTS alone: every word transcribed is a phantom patient |
| soft | patient at −46 dBFS |

### Results (2026-09-26, 14 labelled phrases, 20 medical-term instances)

WER (lower is better), two independent runs agree for v3:

| Config | clean | fan | AC | quiet bystander | soft |
|---|---|---|---|---|---|
| v3, Sarvam default VAD (**chosen**) | 0.007 | 0.014 | 0.021 | 0.26 | 0.027 |
| v3, old hand-tuned VAD (working tree) | 0.007 | 0.014 | 0.021 | 0.26 | **0.26 (2 missed)** |
| v4 (second run) | 0.000 | 0.021 | 0.000 | **0.007** | 0.027 |
| v3 + RNNoise before Sarvam | 0.007 | 0.062 | 0.034 | 0.397 | 0.021 |

Findings that set the defaults:

- **Old Sarvam VAD tuning dropped soft speakers**: its −45 dB floor sits
  above a quiet patient. Removed (rollback: `SARVAM_VAD_PROFILE=permissive`).
- **saaras:v4 ignores a quieter bystander far better** (0 leaked words vs 5),
  but returned **nothing at all** for some utterances: 17/116 in one run, 4/16
  and 2/4 in others, 0 in the rest. v3: 0 of ~350. Stays opt-in.
- **Keyterms**: v4-only (Sarvam rejects them for v3), JSON array only. Listed
  drugs 28/32 vs 19/32 without, but unlisted look-alikes were rewritten:
  *amiodarone → amlodipine* in 4/4 trials; *atorvastatin → "AVP Hospital"*
  once. Off by default.
- **RNNoise** removed ~50 dB of fan/AC in gaps but made recognition worse and
  cannot touch a bystander (speech is speech). Off. ~2.4 ms CPU / 20 ms frame.
- Loud bystander (−6 dB) and simultaneous speech (0 dB): every configuration
  fails (WER 0.7–1.4). Software here cannot separate them; see limitations.
- Echo at −10 dB: every configuration transcribes the bot (echo_only: 45
  words). Only browser AEC and the echo guard stand between that and the LLM.

Echo guard (`test_echo_guard.py`, real Silero, bot.py VAD settings):

| Scenario | Stock Pipecat | With guard |
|---|---|---|
| Echo only, AEC working (−50 dBFS) | false barge-in at 1.1 s | none |
| Echo only, weak AEC (−36) | false barge-in | none |
| Echo only, AEC failed (−26) | false barge-in | none |
| Patient barges in over echo (−50 / −36) | "barge-in" fired *before* the patient spoke | fires 0.06–0.08 s after patient onset |
| Patient as loud as echo (−26 / −26) | false barge-in at 0.34 s | fires 0.96 s after onset |

Endpointing (`test_endpointing.py`, 12 answers, Smart Turn v3 verdicts):

| Silero stop_secs | Ended early | Median end after speech | Worst |
|---|---|---|---|
| 0.2 (**chosen**) | 3 / 12 | 1.11 s | 3.0 s |
| 0.8 (old) | 5 / 12 | 1.09 s | 3.6 s |

Early endings were phone numbers split by a pause and a "thinking" pause, where
TTS spoke each half with final intonation; real speakers trail off less
cleanly, so treat counts as relative. Paused answers transcribe identically at
0.2 and 0.8 (WER 0.038 both). Letter-by-letter spelling fails at both
("S", "H" heard as "Yes", "Itch"): a Sarvam limit, so spell-on-retry is weak.

## 2. Live protocol

### Setup

- Chrome, laptop speakers (worst realistic case) unless a test says headset.
- Start the bot (`bot.py -t webrtc`) and web app; open DevTools console.
- After connecting, the console prints `[mic] processing in effect {...}`.
  Record `echoCancellation`, `noiseSuppression`, `autoGainControl`,
  `sampleRate`, `channelCount`. `mismatch: []` means the browser honoured the
  request. The server logs the same as `Client mic processing: ...`.
- At the end of each session the server logs one `voice_telemetry {...}` line.
  Copy it into the results sheet.

### Reading `voice_telemetry`

| Field | Meaning | Healthy |
|---|---|---|
| `snr_db_estimate` | patient speech p50 − noise floor p50 | > 20 dB |
| `echo_above_noise_db` | bot's voice left in the mic after AEC | < 10 dB (AEC working) |
| `levels.speech.p50_dbfs` | how loud the patient reaches the server | −35 to −20 |
| `counts.turn_start_vad_barge_in` | real barge-ins | matches what you did |
| `counts.barge_in_suppressed_as_echo` / `barge_in_unconfirmed_when_bot_stopped` | echo caught by the guard | any number is fine; it's echo |
| `counts.transcript_dropped_*` | bot-voice transcripts kept from the LLM | should be ≈ 0 if AEC works |
| `false_interruptions` | bot cut off with no patient words after | 0 |
| `counts.vad_speech_without_transcript` | speech heard, no transcript | ≈ 0 (else STT drops) |
| `tts_stop_after_interruption.median_s` | server-side stop after barge-in | < 0.1 s |
| `stt_first_transcript_after_vad_stop.median_s` | STT finalisation delay | < 1.2 s |

Diagnosis: low SNR → mic/placement/gain. High `echo_above_noise_db` →
AEC or speaker volume. High `vad_speech_without_transcript` → STT. Many
`transcript_dropped_*` → echo reaching STT. Early cut-offs → turn detection.

### AGC A/B

Same speaker, same phrases, same mic distance. Load the page with `?agc=1`,
run tests 1, 2, 6, 7; reload with `?agc=0`, repeat. Compare WER, term hits,
`levels.speech.p50_dbfs`, and `snr_db_estimate`. Keep AGC on unless `?agc=0`
wins on soft speech *and* doesn't lose on normal speech. AGC on is the current
default because it is the browser default the system already ran with.

### Scripted answers

Say these exactly; they are the labelled references.

1. "My name is Ramesh Kumar Sharma."
2. "I take metformin five hundred milligrams twice a day and amlodipine at night."
3. "My mobile number is nine eight four five zero, one two three six seven."
4. "My date of birth is fourteenth March, nineteen eighty two."
5. "I am allergic to penicillin and sulfa drugs."
6. "Yes, I have diabetes for… (pause) …about five years."
7. "I have asthma and use a salbutamol inhaler."

### Tests

For every test record: speech detected? TTS stopped? patient words reached
Sarvam (server log / transcript panel)? background words included? bot's own
words transcribed? final transcript correct (WER, terms)? turn ended early?
late? added latency (felt, plus telemetry).

| # | Scenario | How | Pass criteria |
|---|---|---|---|
| 1 | Quiet room | answers 1–7 at 30–50 cm | WER < 5%, all terms right |
| 2 | Fan | table fan 1–2 m away, on high | WER < 10% |
| 3 | AC | run AC; answers 1–7 | WER < 10% |
| 4 | Quiet bystander | second person reads a newspaper aloud softly 2 m away | no bystander words in transcripts |
| 5 | Loud bystander | second person talks normally 1 m away | record leakage; expect some (limitation) |
| 6 | Soft patient | answers at a near-whisper, 50 cm | no missed answers |
| 7 | Talk over the bot | start answer 2 while the bot is still asking | TTS stops < 0.5 s, full answer transcribed, no bot words |
| 8 | Interrupt after 1 s | say "Sorry, wait" 1 s into a bot question | bot stops, then responds to you |
| 9 | 1 s pause | answer 6 with a 1 s pause | one turn, not two |
| 10 | 2 s pause | answer 6 with a 2 s pause | ideally one turn; if split, bot must recover |
| 11 | Simultaneous speech | both people speak at once | record; expect garbled (limitation) |
| 12 | Medication names | answers 2, 5, 7 plus "rabeprazole, vildagliptin, amiodarone" | term accuracy; watch for *substituted* drugs |
| 13 | Phone number | answer 3, also with a pause mid-number | exact 10 digits saved |
| 14 | Date of birth | answer 4 | correct date saved |
| 15 | Hindi/English mix | "Mujhe teen din se bukhar hai aur sar mein dard bhi hai" | meaning preserved (romanized) |
| 16 | Echo stress | laptop speakers at max, stay silent through a bot question | no phantom user turn, `false_interruptions` = 0 |
| 17 | Headset | repeat 4, 5, 7 with a wired headset | compare leakage vs laptop mic |

### A/B procedure (baseline vs improved)

Baseline = previous behavior, via flags (no code checkout needed):

```
server/.env:     ENABLE_TTS_ECHO_GUARD=false  VAD_STOP_SECS=0.8  SARVAM_VAD_PROFILE=permissive
web/.env.local:  VITE_VOICE_AUDIO_PROCESSING=false
```

Improved = defaults (remove those lines). Restart both. Run the same tests
with the same speaker in the same room, then compare per test: WER, term
accuracy, missed answers, bystander words, bot words, false interruptions,
early/late turn ends, and the telemetry latencies.

WER = (substitutions + insertions + deletions) / reference words, numbers
compared digit by digit. Medical-term accuracy = reference terms that appear
exactly in the transcript / all reference terms. A *wrong drug* counts as a
miss and must be noted separately: it is worse than a misspelling.

## 3. Validation pass (2026-09-27)

New tools: `browser_e2e.py` (headless Chrome, real app, fake mic),
`chrome_aec_probe.py` (browser processing on patient speech; `--agc`),
`test_echo_guard_matrix.py`, `test_scenario.py`, `validate.py`
(`reliability | keyterms | numbers | bystander`), `collect_telemetry.py`,
and `DEVICE_TEST_PROTOCOL.md` (the real-device matrix, with P/B tester marks
scored by `VOICE_DEBUG_SCENARIO`).

Findings:

- Live browser track (Chrome 153, fake device) reports exactly what the app
  requests: EC/NS/AGC true, channelCount 1, sampleRate 48000, no mismatch.
- **Chrome's echo canceller attenuates the patient by 24–34 dB whenever the
  bot is talking**, even with no echo present (EC off: 4–5 dB, i.e. natural
  variation). This, not the echo guard, is what blocks talking over the bot on
  speakers. Headsets can run with EC off.
- AGC does not narrow the patient/bystander level gap (22.1 vs 22.8 dB from a
  20 dB source gap); it adds ~6.6 dB overall. Kept on.
- Echo guard: fixed a false barge-in when the echo was loud enough to trip VAD
  in the first 0.3 s of a reply (reference built from one quiet frame). Now
  0/6 false turns at echo -60…-20 dBFS; patient barge-in detected via VAD in
  6/24 matrix cells (the rest are level-indistinguishable from the echo and go
  through the transcript path). Margin sweep: +3 dB remains the best trade-off.
- Endpointing (18 answers, corrected "premature" = ended before the patient's
  last word): 0.2 s 5/18 vs 0.8 s 9/18, same ~1.1 s median. At 0.2 s, 4 of 5
  early endings are phone numbers dictated in groups with a pause.
- Reliability (paired, 178 clips each; credits ran out mid-medical group):
  v3 1 empty, v4 1 empty (same "Yes." clip). WER short 0.065 vs 0.006 (v3
  hears "Male" as "Mail" 10/10), noisy 0.037 vs 0.020, medical terms 54/56 both.
  v4 was reliable today but dropped utterances in 3 of 6 runs the day before:
  episodic, so it stays opt-in until several separate days are clean.
- Not run (Sarvam credits exhausted): keyterm-safety rerun, numbers/dates STT,
  bystander geometry. Each is one `validate.py` command once credits are back.
- LOG_LEVEL now defaults to INFO: a real session produced 0 DEBUG lines and no
  patient content in the server log.

### Completed after credits were restored (2026-09-27)

Keyterm safety (23 terms x clean/fan x 2 runs; `validate.py keyterms`):

| Config | Correct | Wrong medical substitution | Missing/misspelled |
|---|---|---|---|
| v3 | 66/92 | 0 | 26 |
| v4 | 84/92 | 0 | 8 |
| v4 + keyterms | 78/92 | 7 | 7 |
| v4 + keyterms + "AVP Hospital" | 86/92 | 4 | 2 |

Substitutions only ever happened with keyterms: amiodarone -> amlodipine (5),
glipizide -> glimepiride (2), clonidine -> clopidogrel, valsartan -> "AVP
Hospital". Keyterms stay off. v3's misses are mostly visible misspellings
("Glymeride", "Clope Dogrel"); one near-miss outside the lexicon: sumatriptan
-> "Sumatropin" (resembles somatropin).

Numbers and dates (`validate.py numbers`, clean/fan/soft x 3):

| Answer | v3 | v4 |
|---|---|---|
| "nine eight seven six five four three two one zero" | 0/9, "98765432110" (duplicated 1) every time | 9/9 |
| same, grouped with a pause | 0/9, "987654320" (dropped 1) every time | 9/9 |
| "nine eight double seven, six five four three two one" | 8/9 | 9/9 |
| dates (2 phrasings), age, durations (2) | 9/9 each | 9/9 each |
| "500 milligrams twice a day" | 8/9 | 7/9 ("twice a day" lost in fan) |

v3 is deterministic, so its phone failures are one consistent error per
phrasing, not random. Intake validation (`^[6-9]\d{9}$`) rejects both wrong
lengths, so nothing wrong is saved, but that patient cannot get the number in.

Bystander vs mic geometry (`validate.py bystander`, v3, 10 answers each):

| Geometry | Bystander vs patient | Bystander in the patient's pause | Bystander over the patient |
|---|---|---|---|
| laptop 50 cm | -6 dB | 10/10 add their sentence | 64% patient words kept |
| lapel 20 cm | -14 dB | 10/10 | 98% kept, 2/10 stray words |
| desk mic 10 cm | -20 dB | 10/10 | 99% kept, clean |
| headset boom 3 cm | -30 dB | 4/10 | 99% kept, clean |

Silero VAD fired on the pause-bystander in 10/10 clips at -6/-14/-20 dB and
0/10 at -30 dB. With a headset, "no transcript without VAD evidence" would
have rejected every bystander leak here; with a laptop mic it cannot.

### Final v4 gate and production switch (2026-09-27, fresh session)

`validate.py final_v4`: 198 clips (short 50, noisy 40, medical 40, phone 18,
date 12, other numbers 12, paused answers with per-pause flushes 26).
0 empties, 0 connection failures, 0 rate-limited. Phone 18/18, dates 12/12,
medical terms 53/55 with 0 wrong substitutions. Pass bar (set before the run):
no connection failures and at most 2 empties. PASS, so the default became
saaras:v4 (keyterms still off). Added the same day: no-transcript recovery
(voice/recovery.py, on by default) and the headset-only VAD transcript gate
(HEADSET_MODE, off by default). Tests: test_recovery_and_gate.py.
