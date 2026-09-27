"""Scenario scoring for development test sessions (VOICE_DEBUG_SCENARIO).

Nothing in the voice pipeline knows WHO is speaking. In a test session the
tester supplies that ground truth by holding a key while the patient talks
(P) and another while the bystander talks (B). This module lines those marks
up with what the pipeline did and reports, per role: was the speech detected,
did it interrupt the bot, did it produce transcripts that reached the LLM, and
how loud each role's speech arrived (the level gap decides whether a
proximity-based gate could ever separate them on that device).

Input is timestamps, levels, booleans and word counts only: no audio, no text.
"""
from dataclasses import dataclass, field

import numpy as np

# Marks and VAD events are both human/VAD-timed; allow this much slop when
# deciding whether a speech segment overlaps a marked interval.
OVERLAP_SLOP_S = 0.3
MIN_MARK_S = 0.3  # ignore accidental key taps


@dataclass
class Segment:
    """One VAD speech segment as the pipeline saw it."""

    start: float
    end: float | None = None
    levels_db: list[float] = field(default_factory=list)
    bot_speaking: bool = False  # at segment start
    started_turn: bool = False
    turn_cause: str | None = None
    transcripts_delivered: int = 0
    words_delivered: int = 0


def _intervals(marks: list[tuple[float, str, bool]], role: str, end: float) -> list[tuple[float, float]]:
    out, opened = [], None
    for t, r, down in sorted(marks):
        if r != role:
            continue
        if down and opened is None:
            opened = t
        elif not down and opened is not None:
            out.append((opened, t))
            opened = None
    if opened is not None:
        out.append((opened, end))
    return [(a, b) for a, b in out if b - a >= MIN_MARK_S]


def _overlaps(a0: float, a1: float, spans: list[tuple[float, float]]) -> bool:
    return any(a0 < b1 + OVERLAP_SLOP_S and b0 - OVERLAP_SLOP_S < a1 for b0, b1 in spans)


def classify(seg: Segment, patient, bystander) -> str:
    # VAD fires start_secs (0.2 s) after onset; include that in the segment.
    s0, s1 = seg.start - 0.2, seg.end if seg.end is not None else seg.start + 0.5
    p, b = _overlaps(s0, s1, patient), _overlaps(s0, s1, bystander)
    return "both" if p and b else "patient" if p else "bystander" if b else "unmarked"


def score(name: str, segments: list[Segment], marks, interruptions_during_bot: list[float],
          transcripts_during_tts: dict, end: float) -> dict:
    patient, bystander = _intervals(marks, "patient", end), _intervals(marks, "bystander", end)
    bystander_only = [(a, b) for a, b in bystander if not _overlaps(a, b, patient)]
    classes = [classify(s, patient, bystander) for s in segments]

    def detected(spans):
        return sum(any(s.start - 0.2 < b + OVERLAP_SLOP_S and (s.end or s.start) > a - OVERLAP_SLOP_S
                       for s in segments) for a, b in spans)

    by_class: dict[str, dict] = {}
    for seg, cls in zip(segments, classes):
        c = by_class.setdefault(cls, {"segments": 0, "barge_ins": 0, "turn_starts": 0,
                                      "transcripts_to_llm": 0, "words_to_llm": 0, "levels": []})
        c["segments"] += 1
        c["turn_starts"] += seg.started_turn
        c["barge_ins"] += seg.started_turn and seg.bot_speaking
        c["transcripts_to_llm"] += seg.transcripts_delivered
        c["words_to_llm"] += seg.words_delivered
        if seg.levels_db:
            c["levels"].append(float(np.median(seg.levels_db)))
    for c in by_class.values():
        lv = c.pop("levels")
        c["median_level_dbfs"] = round(float(np.median(lv)), 1) if lv else None

    # Interruptions of the bot that no patient speech explains.
    false_ints = 0
    for t in interruptions_during_bot:
        owning = [cls for seg, cls in zip(segments, classes) if seg.start - 0.5 <= t <= (seg.end or t) + 0.5]
        if not any(cls in ("patient", "both") for cls in owning):
            false_ints += 1

    p_lvl = by_class.get("patient", {}).get("median_level_dbfs")
    b_lvl = by_class.get("bystander", {}).get("median_level_dbfs")
    return {
        "scenario": name,
        "marked_patient_utterances": len(patient),
        "marked_bystander_only_utterances": len(bystander_only),
        "patientSpeechDetected": f"{detected(patient)}/{len(patient)}",
        "bystanderSpeechDetected": f"{detected(bystander_only)}/{len(bystander_only)}",
        "bargeInTriggered": {cls: c["barge_ins"] for cls, c in by_class.items()},
        "transcriptReceived": {cls: c["transcripts_to_llm"] for cls, c in by_class.items()},
        "transcriptDuringTTS": transcripts_during_tts,
        "falseInterruption": false_ints,
        "by_segment_class": by_class,
        # > ~12 dB on a device means a level gate could separate the roles there;
        # a few dB means it cannot without dropping quiet patients.
        "patient_minus_bystander_level_db": round(p_lvl - b_lvl, 1) if p_lvl is not None and b_lvl is not None else None,
    }
