"""Audio noise gate — silences frames below a volume threshold.

Sits before STT in the pipeline and replaces quiet/noisy audio with silence
so the speech-to-text engine never even sees background chatter, fan hum, etc.
"""
import struct

from loguru import logger
from pipecat.frames.frames import AudioRawFrame, Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class NoiseGateProcessor(FrameProcessor):
    """Passes audio through only when volume exceeds a threshold.

    Unlike VAD min_volume (which merely decides whether VAD *triggers*), this
    processor actually *replaces* below-threshold audio with silence so STT
    never receives noisy frames at all.

    Args:
        volume_threshold: RMS volume (0.0–1.0 normalised) below which audio
            is replaced with silence.  Start with 0.01–0.03 and raise if
            background noise persists.
        gate_open_frames: How many consecutive loud frames are needed before
            the gate opens (prevents short noise spikes from getting through).
    """

    def __init__(self, *, volume_threshold: float = 0.02, gate_open_frames: int = 3, **kwargs):
        super().__init__(**kwargs)
        self._threshold = volume_threshold
        self._open_frames_required = gate_open_frames
        self._consecutive_loud = 0
        self._gate_open = False

    @staticmethod
    def _rms_volume(audio: bytes) -> float:
        """Compute RMS volume normalised to 0.0–1.0 for 16-bit PCM."""
        if len(audio) < 2:
            return 0.0
        n_samples = len(audio) // 2
        samples = struct.unpack(f"<{n_samples}h", audio[:n_samples * 2])
        sum_sq = sum(s * s for s in samples)
        rms = (sum_sq / n_samples) ** 0.5
        return rms / 32768.0  # normalise to 0–1

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, AudioRawFrame):
            vol = self._rms_volume(frame.audio)

            if vol >= self._threshold:
                self._consecutive_loud += 1
                if self._consecutive_loud >= self._open_frames_required:
                    self._gate_open = True
            else:
                self._consecutive_loud = 0
                self._gate_open = False

            if self._gate_open:
                # Audio is loud enough — pass through unchanged
                await self.push_frame(frame, direction)
            else:
                # Replace with silence (same length) so the pipeline timing
                # stays intact but STT receives nothing useful.
                silence = b"\x00" * len(frame.audio)
                silent_frame = AudioRawFrame(
                    audio=silence,
                    sample_rate=frame.sample_rate,
                    num_channels=frame.num_channels,
                )
                await self.push_frame(silent_frame, direction)
        else:
            # Non-audio frames (control, text, etc.) pass through untouched.
            await self.push_frame(frame, direction)
