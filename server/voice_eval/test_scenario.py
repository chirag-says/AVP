r"""Unit test for voice/scenario.py: scoring pipeline events against tester marks.

    .venv\Scripts\python.exe server\voice_eval\test_scenario.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from voice.scenario import Segment, score  # noqa: E402

# Patient answers twice, bystander talks once alone and once over the patient,
# an accidental key tap, then the patient barges in while the bot speaks.
MARKS = [(1.0, "patient", True), (3.0, "patient", False),
         (6.0, "bystander", True), (8.0, "bystander", False),
         (10.0, "patient", True), (13.0, "patient", False), (11.0, "bystander", True), (12.0, "bystander", False),
         (15.0, "patient", True), (15.1, "patient", False),
         (21.0, "patient", True), (23.0, "patient", False)]
SEGMENTS = [
    Segment(1.2, 3.1, [-25] * 50, started_turn=True, turn_cause="vad", transcripts_delivered=1, words_delivered=6),
    Segment(6.2, 8.1, [-38] * 40, started_turn=True, turn_cause="vad", transcripts_delivered=1, words_delivered=5),
    Segment(10.2, 13.1, [-24] * 60, started_turn=True, transcripts_delivered=1, words_delivered=9),
    Segment(18.0, 18.5, [-50] * 10, bot_speaking=True, started_turn=True, turn_cause="vad_barge_in"),
    Segment(21.2, 23.1, [-27] * 40, bot_speaking=True, started_turn=True, turn_cause="vad_barge_in",
            transcripts_delivered=1, words_delivered=4),
]

r = score("unit", SEGMENTS, MARKS, [18.1, 21.25], {"from_stt": 2, "delivered_to_llm": 1, "dropped_by_echo_guard": 1}, 30)
checks = {
    "patient utterances detected 3/3 (tap ignored)": r["patientSpeechDetected"] == "3/3",
    "bystander-only utterance detected 1/1": r["bystanderSpeechDetected"] == "1/1",
    "bystander transcript leak counted": r["transcriptReceived"]["bystander"] == 1,
    "unmarked barge-in is a false interruption": r["falseInterruption"] == 1,
    "patient barge-in attributed to patient": r["bargeInTriggered"]["patient"] == 1,
    "level gap = median(-25,-27) - (-38) = 12 dB": r["patient_minus_bystander_level_db"] == 12.0,
}
for name, ok in checks.items():
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
sys.exit(sum(not ok for ok in checks.values()))
