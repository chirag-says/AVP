# HODDOCTOR voice intake: next-level plan

Date: 2026-10-03. Scope: reduce bot latency, raise accuracy, run 5 kiosks at once.

Basis: the full server and web code as of today's working tree (multilingual phase 1
included; 104 server and 15 web local tests pass), the evaluation numbers in
server/voice_eval/TEST_PLAN.md, the Gemini timings measured 2026-09-28, and two
measurements taken today on the target laptop (i5-12450H):

| Local model | Cost | How measured |
|---|---|---|
| Smart Turn v3 decision | 48 ms (1 thread), 31 ms (4 threads), independent of clip length | 5 runs each on 1 s, 3 s, 8 s audio |
| Silero VAD | 0.13 ms per 32 ms frame | 200 frames |
| Silero first load (cold import) | 9.4 s | once |

Conclusion from those: the local audio models are not where the time goes.

## 1. Where a turn's time goes today

"Turn" = the patient stops speaking until the bot's first audio.

| Stage | Time | Source |
|---|---|---|
| Silero silence before the end-of-turn check | 0.20 s | VAD_STOP_SECS (configured) |
| Smart Turn decision | 0.05 s | measured today |
| Smart Turn says "not finished" when the patient was finished (5 of 18 eval answers, mostly phone numbers said in groups) | up to 3.0 s more | Pipecat STOP_SECS=3 |
| Sarvam final transcript after VAD stop | 0.19 s median, p99 about 1.2 s | 2026-09-28, TEST_PLAN |
| Gemini call 1 (emits the save_field tool call) | about 2.5 s median | 2026-09-28, free tier |
| Gemini call 2 (writes the next question) | about 2.8 s median | 2026-09-28, free tier |
| Sarvam TTS first audio | not measured; estimate 0.3 to 0.8 s | |
| WebRTC playout | estimate 0.1 to 0.2 s | |

Median total: roughly 6 to 7 s, of which about 5.3 s is the two LLM calls.

Why two calls: Pipecat re-runs the LLM after every tool result (run_llm defaults to
true), so the question the patient hears always comes from a second round trip. One
intake has roughly 12 to 15 bot turns, so the LLM alone costs 60 to 80 s per patient.

Estimated intake length today: 3 to 3.5 minutes (estimate; Phase 0 measures it).

## 2. Targets

| Metric | Today | Target |
|---|---|---|
| Bot first audio after the patient stops, median | 6 to 7 s | 1.2 s |
| Same, p95 | 9 to 10 s | 2.5 s |
| Intake duration, typical 9-field patient | 3 to 3.5 min | under 2 min |
| LLM calls per intake | 25 to 30 | under 10 |
| Kiosks running at once | 1 | 5, with no shared-quota failure |
| Accuracy gates (validate.py final_v4 set) | phone 18/18, dates 12/12, medical 53/55, 0 wrong drug names | no regression |
| Wrong values saved by any non-LLM path | n/a | 0 |

Primary metric: median and p95 bot latency plus intake duration. Counter metrics:
the accuracy gates above, fast-path mistakes, bystander words in records.

## 3. Phases

### Phase 0: measure (half a day, no API cost beyond a few live intakes)

Add a per-turn timeline to voice/telemetry.py: VAD stop, turn end, LLM first token,
tool result, TTS first byte, bot audio start. Pipecat already emits TTFB metrics per
service (enable_metrics=True); an observer collects them into the existing
content-free JSON line. Also record session start (orb click to greeting audio) and
per-session memory. Chirag runs 5 live intakes on the laptop; that table is the
baseline every later phase is judged against.

### Phase 1: one LLM call per turn (1 to 2 days, saves about 2.5 to 2.8 s per turn)

The largest and cheapest win.

- Prompt: in the same reply, call the tool for what the patient said and ask the next
  question. Gemini streams the text first; Pipecat speaks it while the tool runs
  (checked in services/google/llm.py: text parts are pushed during the stream, the
  function calls run after it).
- session._field_tool returns run_llm=False when the engine accepted the value and the
  reply contained spoken text; run_llm=True otherwise (validation error, needs
  confirmation, or a tool-only reply with no text). The "spoke text" flag is a small
  per-response tracker; ReplyGuard already watches LLMTextFrame and can expose it.
- Tool results still land in the context, so the LLM sees the engine's "next: X"
  verdict one turn later. Out-of-order answers are handled the same way as today.
- Edge: tool error after the question was already spoken (a 9-digit phone). The
  run_llm=True path has the LLM correct itself ("that number seems short, could you
  repeat it?"). Rare; Phase 0 counts how often.
- Edge: Gemini returns the tool call with no text. Same as today (second call).
- Test: test_intake_session, test_conversation_llm (count Gemini calls per intake,
  target 1.2 per turn or less). Flag ONE_CALL_TURNS for rollback.

### Phase 2: instant fixed speech and warm start (1 day)

- Cached audio for fixed lines in all 5 languages: greeting, closing, "didn't catch
  that", "say that again", save-problem line, and the templated questions from
  Phase 3. Synthesize once with Sarvam (about 2,000 chars per language, roughly Rs 15
  total), store WAV files under server/voice/cache/<lang>/. A processor in front of
  the TTS serves a TTSSpeakFrame whose text matches a cached entry by emitting the
  audio frames itself (plus the TTSTextFrame so the transcript and echo guard still
  see the words). Removes TTS latency and TTS cost on those lines.
- Load the Silero and Smart Turn ONNX sessions once per process and share them
  across sessions (per-session state objects stay separate). Phase 0 says how much
  this is worth on a warm process; cold is 9.4 s today.
- The mic stays off between patients; "Start next patient" keeps its current flow.

### Phase 3: local fast path for simple answers (2 to 3 days, no LLM on about 40 to 50% of turns)

Data-driven, one engine: IntakeField gets an optional `parse` name. Parsers live in
engine.py next to the normalizers that already exist:

| Field | Parser | Accepts only |
|---|---|---|
| age | digits or number words, 1 to 120, 5 languages | the whole utterance is the number |
| gender | male/female/other synonyms, 5 languages | one synonym |
| phone | existing _normalize_phone | exactly 10 digits result |
| phone read-back | yes/no words, 5 languages | one yes or no |
| allergies, conditions, medications | existing is_negative | a pure "none" answer |

A processor between the user aggregator and the LLM: if the field just asked has a
parser and the transcript parses with nothing left over, it calls engine.save_field
directly, speaks the templated next question (cached audio from Phase 2), appends the
patient text and the spoken question to the LLM context so the history stays coherent,
and does not run the LLM. Anything else goes to the LLM exactly as today.

Turn latency on those fields: about 0.5 s (silence 0.2 s + Smart Turn 0.05 s + Sarvam
0.2 s + playout). The phone step drops from 4 LLM calls to 0.

Write contract (set by Chirag, 2026-10-03). A fast-path parser may NEVER write a value
unless all five hold:

1. The current field is exactly the field being asked: the field the engine marked
   ASKED when the bot spoke its last question, with no NEEDS_CONFIRMATION pending on
   any field and no interruption since that question.
2. The entire utterance matches: the parser consumes the whole transcript with nothing
   left over. "Thirty four" passes; "thirty four or thirty five" and "I think thirty
   four" do not.
3. The parser is certain: one unambiguous reading. Any alternative reading, any
   confidence below the parser's hard threshold, fails.
4. The value passes the field's validation (schema.py regex after normalization, the
   same check save_field applies).
5. No contradiction exists with the current state: the field is UNASKED or ASKED, not
   already ANSWERED, CONFIRMED or SKIPPED, and for negatable fields the new value does
   not flip an earlier positive or negative answer.

The gate is one pure function, `fast_path_allowed(engine, field, transcript)`, checked
before any engine call. If any condition fails, nothing is written and the turn goes
to the LLM exactly as today. Unit tests cover each condition failing on its own, per
parser, per language. The live counter metric is fast-path writes later corrected by
the patient or the LLM; the target is zero, and one occurrence disables the fast path
for that field until the cause is fixed.

### Phase 4: faster LLM (decision needed; half a day to switch; saves 1.5 to 2 s per LLM turn)

Today's Gemini free tier gives 1.2 to 2.7 s to first token from Chirag's laptop and
500 requests per day per model. Five kiosks at 25 intakes per hour each with about
7 calls per intake (after Phases 1 and 3) is around 900 calls per hour: the free
tier is not an option for 5 kiosks no matter what else is done.

| Option | Expected first token | Data | Risk |
|---|---|---|---|
| Gemini, same model, key with billing | 0.5 to 0.9 s (estimate, must measure) | Google, region not pinned | lowest: no code change |
| Vertex AI, asia-south1 (Mumbai) | similar or lower RTT | stays in India | Pipecat has a Vertex service; small change |
| Groq or Cerebras (US) | about 0.3 s | leaves India | tool-calling on this prompt unproven; sarvam-30b failed silently, so any switch must pass test_conversation_llm.py first |

Recommendation: Gemini with billing, or Vertex Mumbai if data residency matters.
Measure first-token latency in Phase 0 style before and after.

### Phase 5: endpointing per field (1 day, cuts the 3 s long tail)

Smart Turn's "not finished" verdict waits up to 3 s of silence. Make the maximum
silence a field property in schema.py: short fields (name, age, gender, yes/no)
1.0 s; phone, address, medications, symptoms 2.5 to 3 s because patients pause there.
The custom stop strategy reads the field the engine last asked. Fast-path fields can
skip Smart Turn entirely (plain VAD stop at 0.5 s). Validate with test_endpointing.py
(free, local).

### Phase 6: fewer turns (half a day, product decisions)

- Ask age and gender in one question; the engine already accepts several fields in
  one turn.
- Drop the address read-back; the screen shows it and the receptionist or patient
  corrects it visually. Keep the phone digit read-back.
- Drop the closing summary sentence; the screen shows the record. Finalize directly.
- Returning patients: look up by phone, pre-fill name, age, gender, address, confirm in
  one question. Needs an index on phone (additive migration) and a product decision;
  the bot reads back only after the patient has stated the number.

Estimate: 3 to 4 fewer bot turns, 30 to 45 s per intake.

### Phase 7: noise and accuracy (ongoing; mostly hardware and operations)

What the evaluation already proved: on a laptop mic a bystander 1 to 1.5 m away leaks
into the record 10/10 times and no software here can separate them; a headset boom at
3 cm leaks 4/10 and the VAD gate (HEADSET_MODE) rejects those; Chrome's echo
canceller cuts the patient by 24 to 34 dB while the bot talks, so talking over the bot
only works on headsets with echo cancellation off; RNNoise made Sarvam worse;
keyterms produced 7 wrong drug names.

- Hardware: every kiosk gets a close-talk mic (wired headset, or a gooseneck or desk
  mic within 10 cm), HEADSET_MODE=true and echo cancellation off for headsets. This is
  the single largest accuracy gain available and needs no code.
- Mic health on screen: live SNR and echo level from the existing telemetry shown to
  reception staff ("move the mic closer", "speaker too loud"); sessions with SNR under
  15 dB get a flag in the record. Small change.
- Denoiser experiment (optional, half a day): DeepFilterNet3 instead of RNNoise,
  compared with stt_ab.py. Costs about Rs 30 per hour of audio, so it needs Chirag's
  OK first. Kill rule: any WER loss on the clean or soft sets and it is dropped.
- Names: Sarvam cannot transcribe letter-by-letter spelling (eval), so "spell on
  retry" is weak. Replace it with an on-screen edit of the name field by the
  receptionist or patient, keeping the flag. Accuracy comes from confirmation on the
  screen, not from the microphone.
- Keyterms stay off.
- Multilingual phase 2 (pending): accuracy check of Indic names, numbers and dates
  before any kiosk runs in Kannada, Hindi, Tamil or Telugu. About Rs 50 of Sarvam and
  60 Gemini calls; needs the OK first.

### Phase 8: five kiosks (2 to 3 days plus hardware)

Recommended topology: one server (the i5 laptop or a small desktop) on the hospital
LAN, five kiosk machines running only the browser. CPU per session is tiny (numbers
above), there is one process to update and watch, one log, one set of keys. RAM per
session is measured in Phase 0; 5 sessions on a 7.7 GB machine should fit, verify.

Must be done before kiosks go live:

1. HTTPS. Chrome allows the microphone only on https or localhost; a kiosk opening
   http://<server-ip> gets no mic. Put Caddy in front of the web app and ports 7860
   and 7861 with its internal CA, install Caddy's root certificate on the 5 kiosks.
   One config file, no custom code.
2. Authentication on staff data. /api/records, /api/export/* and /api/consultations
   have no auth today and the runner's CORS default is "*". On a hospital LAN any
   device can read patient records. Minimum: a staff password that sets a signed,
   httpOnly, SameSite cookie checked on those routes, plus PIPECAT_ALLOWED_ORIGINS set
   to the kiosk origin. Not optional.
3. Sarvam concurrency. The eval harness hit WebSocket close 1003 at more than 2 or 3
   parallel streams on this key. Five kiosks mean up to 5 STT and 5 TTS streams.
   Confirm the plan's concurrent-stream limit with Sarvam before go-live; if it is
   lower, use a second key or a per-process semaphore that shows "please wait" on
   the 4th kiosk instead of failing mid-intake.
4. Paid LLM tier (Phase 4), for quota.
5. Process supervision: bot.py and scribe_bot.py as Windows services (NSSM) with
   restart on failure, log rotation, LOG_LEVEL=INFO; a status page that polls a
   health endpoint per service.
6. Kiosk identity: the kiosk's URL carries ?kiosk=3, the /start body passes it, one
   additive column on intake_sessions stores it; the records view and telemetry show
   which desk.
7. Per-kiosk mic settings through the URL flags that already exist (?ec=0&ns=1 ...),
   fixed in each kiosk's bookmark.

Throughput at target latency: about 25 to 30 patients per hour per kiosk.

Cost per intake (estimates; confirm rates on the Sarvam dashboard): STT about 2 min
at Rs 30/hour is Rs 1; TTS about 800 uncached characters is Rs 1.2 (cached fixed
lines cut this); Gemini flash-lite paid, 7 calls, under Rs 0.1. About Rs 2.5 per
intake, roughly Rs 350 per hour with all 5 kiosks busy.

## 4. Expected turn budget by phase (estimates until Phase 0 and each phase measure them)

| After phase | LLM turns | Simple-answer turns | Fixed lines |
|---|---|---|---|
| today | 6 to 7 s | 6 to 7 s | 0.5 to 1 s |
| 1 | 3.5 to 4 s | 3.5 to 4 s | same |
| 2 | same | same | about 0.2 s |
| 3 | same | 0.5 to 0.7 s | about 0.2 s |
| 4 | 1.3 to 1.8 s | same | same |
| 5 | p95 tail 3 s drops to about 1 s on short fields | | |

Overall: median about 1 s, p95 about 2.5 s, intake 1.5 to 2 min.

## 5. Order, effort, dependencies

| Order | Phase | Gain | Effort | Needs a decision |
|---|---|---|---|---|
| 1 | 0 measure | baseline | 0.5 d | no |
| 2 | 1 one call | 2.5 to 2.8 s per turn | 1 to 2 d | no |
| 3 | 4 faster LLM | 1.5 to 2 s per LLM turn, unblocks 5 kiosks | 0.5 d | yes: billing, provider |
| 4 | 2 cached speech, warm start | TTS latency and cost on fixed lines | 1 d | no |
| 5 | 3 fast path | no LLM on 40 to 50% of turns | 2 to 3 d | yes: templated questions OK? |
| 6 | 5 endpointing | p95 tail | 1 d | no |
| 7 | 6 fewer turns | 30 to 45 s per intake | 0.5 d | yes |
| 8 | 8 kiosks | scale | 2 to 3 d + hardware | yes: topology, mics |
| parallel | 7 hardware, mic health, auth | accuracy, security | ops + 1 d | yes: mics |

Each phase ships behind its own env flag and is judged against the Phase 0 table
before the next starts.

## 6. What would make this plan wrong

- The Gemini timings come from one day on one free account. If the paid tier is not
  faster from India, Phase 4 moves to Vertex Mumbai or another provider; Phase 0
  shows this before any switch.
- Phase 1 depends on Gemini emitting text and a tool call in one reply. If it prefers
  tool-only replies, the fallback is today's second call: no worse, just no gain.
  The call count per intake is the check.
- The fast path must never save a wrong value. Parsers accept only full-utterance
  matches; the counter metric is zero fast-path corrections in live sessions.
- The Sarvam concurrent-stream limit is unknown. Until confirmed, 5 kiosks is a
  plan, not a promise.
- Bystanders on open mics have no software fix. If kiosks ship with laptop mics,
  leakage stays and Phase 7's hardware line is the only remedy.

## 7. Decisions needed from Chirag

1. Topology: one central server plus 5 browser kiosks (recommended), or 5
   self-contained laptops each running the full stack?
2. LLM billing: OK to put billing on the Gemini key, or use Vertex AI Mumbai? Is any
   provider outside India unacceptable for patient data?
3. Conversation style: OK with templated questions on simple fields (age, gender,
   phone read-back, "none" answers) while the LLM handles everything else?
4. Microphone per kiosk: headset, gooseneck or desk mic, or the laptop mic? This
   decides HEADSET_MODE, echo-cancellation settings and how much bystander leakage
   remains.
5. Fewer turns: drop the address read-back and the closing summary? Is a returning-
   patient lookup by phone wanted?
6. Commit the current multilingual working tree first (all local tests pass today),
   so the latency work is a separate, reviewable change?
7. Who confirms the concurrent-stream limit with Sarvam?
