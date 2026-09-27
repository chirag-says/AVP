"""Content-free voice telemetry: one JSON line per session, no audio, no text.

Answers "where does bad input come from?" with numbers:
  - mic level by state: noise floor (nobody talking), echo (bot talking,
    patient not), speech (patient talking, bot not), double-talk (both).
    speech - noise is the SNR the STT gets; echo - noise is how much of the
    bot's voice survives browser echo cancellation.
  - turn starts by cause, interruptions, bot interruptions whose user turn
    ended with no transcript at all (false barge-ins), TTS stop latency.
  - transcript counts/latency, transcripts arriving around bot speech, what
    the echo guard dropped, Sarvam (re)connects.
  - the browser's actual mic processing settings, reported by the client.
  - in a development test session (VOICE_DEBUG_SCENARIO), a per-role report
    scored against the tester's patient/bystander key presses (voice/scenario.py).

Only counts, durations, levels and word counts are recorded. Transcript text,
audio and anything the patient says never enter this object.
"""
import json
import time

import numpy as np
from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection

from voice.scenario import Segment, score

_FLOOR_DB = -100
_STATES = ("noise", "echo", "speech", "double_talk")
# VAD speech at least this long with no transcript this soon after it counts as
# "speech without transcript": STT dropped it, or VAD accepted a non-speech
# sound (cough, door). A steady rate during real conversation means STT drops.
UNTRANSCRIBED_MIN_SPEECH_S = 0.4
UNTRANSCRIBED_WAIT_S = 4.0
# Client-reported mic settings we keep; anything else the client sends is ignored.
_MIC_SETTINGS = ("echoCancellation", "noiseSuppression", "autoGainControl", "sampleRate", "channelCount",
                 "voiceIsolation")


def _scalar(v: object) -> object:
    return v if isinstance(v, (bool, int, float)) or v is None else str(v)[:32]


def sanitize_client_mic(data: object) -> dict | None:
    """Keep only the known REQUESTED/ACTUAL/MISMATCH rows and a short device label."""
    if not isinstance(data, dict):
        return None
    rows = []
    for row in data.get("rows") or []:
        if isinstance(row, dict) and row.get("setting") in _MIC_SETTINGS:
            rows.append({
                "setting": row["setting"],
                "requested": _scalar(row.get("requested")),
                "actual": _scalar(row.get("actual")),
                "mismatch": bool(row.get("mismatch")),
            })
    label = data.get("deviceLabel")
    return {
        "rows": rows,
        "mismatch": [r["setting"] for r in rows if r["mismatch"]],
        "deviceLabel": label[:80] if isinstance(label, str) else None,
        "voiceIsolationSupported": bool(data.get("voiceIsolationSupported")),
        "explicitProcessing": bool(data.get("explicitProcessing")),
    }


def format_mic_table(mic: dict) -> str:
    lines = [f"{'SETTING':18s} {'REQUESTED':>14s} {'ACTUAL':>14s}  MISMATCH"]
    for r in mic["rows"]:
        lines.append(f"{r['setting']:18s} {str(r['requested']):>14s} {str(r['actual']):>14s}  {'YES' if r['mismatch'] else 'no'}")
    header = f"device={mic['deviceLabel']!r} voiceIsolationSupported={mic['voiceIsolationSupported']}"
    return "\n".join([header, *lines])


def frame_level_dbfs(audio: bytes) -> float:
    """RMS level of 16-bit PCM in dBFS, floored at -100."""
    samples = np.frombuffer(audio, dtype=np.int16)
    if samples.size == 0:
        return _FLOOR_DB
    rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2)) / 32768.0
    return max(_FLOOR_DB, 20 * np.log10(rms + 1e-12))


def _secs(ns: int) -> float:
    return ns / 1e9


class VoiceTelemetry(BaseObserver):
    def __init__(self, session_label: str | None = None, scenario: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._label = session_label
        self._scenario = scenario  # set only in development test sessions
        self._input = self._stt = self._user_agg = self._output = None
        self._hist = {s: np.zeros(-_FLOOR_DB + 1, dtype=np.int64) for s in _STATES}
        self._counts: dict[str, int] = {}
        self._audio = {"frames": 0, "sample_rate": None, "channels": None, "frame_ms": None}
        self._bot_speaking = False
        self._vad_speaking = False
        self._vad_started_at: float | None = None
        self._vad_stopped_at: float | None = None
        self._awaiting_first_transcript = False
        self._speech_secs: list[float] = []
        self._stt_latency: list[float] = []
        self._tts_stop_latency: list[float] = []
        self._interrupt_at: float | None = None
        self._bot_interrupts: list[float] = []  # times of interruptions during bot speech
        self._turn: dict | None = None  # {"interrupting": bool, "text": bool}
        self._false_interruptions = 0
        self._segments: list[Segment] = []
        self._pending_cause: str | None = None
        self._marks: list[tuple[float, str, bool]] = []
        self._tts_transcripts = {"from_stt": 0, "delivered_to_llm": 0}
        self._clock_offset = float("inf")  # monotonic() minus pipeline clock
        self._stt_connects = 0
        self._client_mic: dict | None = None
        self._untranscribed: list[float] = []  # VAD stop times still waiting for a transcript
        self._last_t = 0.0

    def watch(self, *, input_transport, stt, user_aggregator, output_transport):
        """Observers see every hop; count each event only where it originates."""
        self._input, self._stt = input_transport, stt
        self._user_agg, self._output = user_aggregator, output_transport

    # --- hooks for other components -------------------------------------------

    def count(self, name: str, n: int = 1):
        self._counts[name] = self._counts.get(name, 0) + n

    def note_turn_start_cause(self, cause: str, bot_speaking: bool):
        self._pending_cause = cause
        self.count(f"turn_start_{cause}")
        if bot_speaking:
            self.count("turn_start_during_bot_speech")

    def note_stt_connected(self):
        self._stt_connects += 1

    def note_client_mic(self, data: object) -> dict | None:
        mic = sanitize_client_mic(data)
        if mic:
            self._client_mic = mic
        return mic

    @property
    def scenario_enabled(self) -> bool:
        return self._scenario is not None

    def note_scenario_name(self, name: object):
        if self.scenario_enabled and isinstance(name, str) and name.strip():
            self._scenario = name.strip()[:40]

    def note_marker(self, role: object, down: object):
        """Tester's ground-truth key press (patient/bystander), timed on the pipeline clock."""
        if self.scenario_enabled and role in ("patient", "bystander") and self._clock_offset != float("inf"):
            self._marks.append((time.monotonic() - self._clock_offset, role, bool(down)))

    # --- observer -------------------------------------------------------------

    async def on_push_frame(self, data: FramePushed):
        f, src, t = data.frame, data.source, _secs(data.timestamp)
        self._last_t = max(self._last_t, t)
        # Observers run slightly behind the pipeline; the smallest gap seen is
        # the best estimate of the clock offset (used to time tester marks).
        self._clock_offset = min(self._clock_offset, time.monotonic() - t)

        if isinstance(f, InputAudioRawFrame):
            if src is self._input:
                self._on_audio(f)
            return
        if data.direction != FrameDirection.DOWNSTREAM:
            return

        if src is self._output and isinstance(f, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif src is self._output and isinstance(f, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            if self._interrupt_at is not None:
                self._tts_stop_latency.append(t - self._interrupt_at)
                self._interrupt_at = None
        elif src is self._user_agg and isinstance(f, VADUserStartedSpeakingFrame):
            self._vad_speaking, self._vad_started_at = True, t
            self.count("vad_starts")
            self._segments.append(Segment(start=t, bot_speaking=self._bot_speaking))
        elif src is self._user_agg and isinstance(f, VADUserStoppedSpeakingFrame):
            self._vad_speaking, self._vad_stopped_at = False, t
            self._awaiting_first_transcript = True
            if self._segments and self._segments[-1].end is None:
                self._segments[-1].end = t
            self._expire_untranscribed(t)
            if self._vad_started_at is not None:
                self._speech_secs.append(t - self._vad_started_at)
                if t - self._vad_started_at >= UNTRANSCRIBED_MIN_SPEECH_S:
                    self._untranscribed.append(t)
        elif src is self._user_agg and isinstance(f, UserStartedSpeakingFrame):
            self.count("user_turns")
            self._turn = {"interrupting": self._bot_speaking, "text": False}
            seg = self._segment_at(t)
            if seg:
                seg.started_turn, seg.turn_cause = True, self._pending_cause or "transcript"
            self._pending_cause = None
        elif src is self._user_agg and isinstance(f, UserStoppedSpeakingFrame):
            if self._turn and self._turn["interrupting"] and not self._turn["text"]:
                self._false_interruptions += 1  # bot cut off, and the turn produced no words
            self._turn = None
        elif src is self._user_agg and isinstance(f, InterruptionFrame):
            self.count("interruptions")
            if self._bot_speaking:
                self.count("interruptions_during_bot_speech")
                self._interrupt_at = t
                self._bot_interrupts.append(t)
        elif isinstance(f, TranscriptionFrame):
            if src is self._stt:
                self._untranscribed.clear()
                self.count("stt_transcripts")
                self.count("stt_words", len(f.text.split()))
                if self._bot_speaking:
                    self.count("stt_transcripts_during_bot_speech")
                    self._tts_transcripts["from_stt"] += 1
                if self._awaiting_first_transcript and self._vad_stopped_at is not None:
                    self._stt_latency.append(t - self._vad_stopped_at)
                    self._awaiting_first_transcript = False
            elif data.destination is self._user_agg:
                # Delivered to the aggregator (past the echo guard): goes to the LLM.
                if self._turn:
                    self._turn["text"] = True
                if self._bot_speaking:
                    self._tts_transcripts["delivered_to_llm"] += 1
                seg = self._segment_at(t, lookback_s=6.0)
                if seg:
                    seg.transcripts_delivered += 1
                    seg.words_delivered += len(f.text.split())

    def _segment_at(self, t: float, lookback_s: float = 3.0) -> Segment | None:
        """The segment in progress at t, else the latest one that ended within lookback_s."""
        for seg in reversed(self._segments):
            if seg.start <= t and (seg.end is None or t - seg.end <= lookback_s):
                return seg
        return None

    def _expire_untranscribed(self, now: float):
        expired = [x for x in self._untranscribed if now - x > UNTRANSCRIBED_WAIT_S]
        if expired:
            self.count("vad_speech_without_transcript", len(expired))
            self._untranscribed = [x for x in self._untranscribed if x not in expired]

    def _on_audio(self, frame: InputAudioRawFrame):
        a = self._audio
        a["frames"] += 1
        if a["sample_rate"] is None:
            a["sample_rate"], a["channels"] = frame.sample_rate, frame.num_channels
            a["frame_ms"] = round(1000 * len(frame.audio) / (2 * frame.sample_rate * frame.num_channels), 1)
        state = ("double_talk" if self._bot_speaking else "speech") if self._vad_speaking else (
            "echo" if self._bot_speaking else "noise")
        level = frame_level_dbfs(frame.audio)
        self._hist[state][int(-round(level))] += 1
        if self._vad_speaking and self._segments and self._segments[-1].end is None:
            self._segments[-1].levels_db.append(level)

    # --- report ----------------------------------------------------------------

    def _percentiles(self, state: str) -> dict | None:
        h = self._hist[state]
        total = int(h.sum())
        if not total:
            return None
        # Bins run 0 dB (index 0) down to -100 dB; walk from quiet to loud.
        levels = -np.arange(len(h))[::-1]
        cum = np.cumsum(h[::-1])
        pct = {p: int(levels[np.searchsorted(cum, total * p / 100)]) for p in (10, 50, 90)}
        return {"frames": total, "p10_dbfs": pct[10], "p50_dbfs": pct[50], "p90_dbfs": pct[90]}

    def summary(self) -> dict:
        self._expire_untranscribed(self._last_t)

        def stats(xs):
            return {"n": len(xs), "median_s": round(float(np.median(xs)), 3), "max_s": round(max(xs), 3)} if xs else None

        levels = {s: self._percentiles(s) for s in _STATES}
        noise, speech, echo = levels["noise"], levels["speech"], levels["echo"]
        report = {
            "session": self._label,
            "audio_in": self._audio,
            "client_mic": self._client_mic,
            "levels": levels,
            "snr_db_estimate": speech["p50_dbfs"] - noise["p50_dbfs"] if speech and noise else None,
            "echo_above_noise_db": echo["p50_dbfs"] - noise["p50_dbfs"] if echo and noise else None,
            "counts": self._counts,
            "false_interruptions": self._false_interruptions,
            "speech_segment": stats(self._speech_secs),
            "stt_first_transcript_after_vad_stop": stats(self._stt_latency),
            "tts_stop_after_interruption": stats(self._tts_stop_latency),
            "sarvam_stt_connects": self._stt_connects,
        }
        if self.scenario_enabled:
            dropped = sum(v for k, v in self._counts.items() if k.startswith("transcript_dropped"))
            report["scenario"] = score(self._scenario, self._segments, self._marks, self._bot_interrupts,
                                       {**self._tts_transcripts, "dropped_by_echo_guard": dropped}, self._last_t)
        return report

    def log_summary(self):
        logger.info("voice_telemetry " + json.dumps(self.summary(), default=str))
