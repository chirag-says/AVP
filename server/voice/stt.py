"""Sarvam STT for the intake bot, on Pipecat's SarvamSTTService.

Defaults come from the A/B in server/voice_eval (TEST_PLAN.md has the numbers):

  - saaras:v4 is the model. It is more accurate on the fields that matter:
    phone numbers 27/27 vs 9/27 (v3 consistently adds or drops a digit in
    "...three two one zero"), medical terms 84/92 vs 66/92, "Male" 10/10 vs
    0/10 ("Mail"), quiet-bystander WER ~0.03 vs 0.26. On 2026-09-26 it
    silently returned nothing for some utterances in 3 of 6 runs; since then
    it matched v3 (1 empty in ~350) and passed a final independent gate on
    2026-09-27 (0 empties, 0 connection failures in 198 clips across short,
    noisy, medical, phone, date and paused answers). If a silent drop recurs,
    voice/recovery.py asks the patient to repeat instead of stalling.
    Rollback: SARVAM_STT_MODEL=saaras:v3.
  - Sarvam's server VAD stays at its documented defaults. The hand-tuned block
    that used to live in bot.py lowered every threshold, and its -45 dB volume
    floor dropped soft-spoken patients outright (soft-speech WER 0.03 -> 0.26).
  - keyterms are off. Sarvam accepts them only for saaras:v4 (it rejects v3),
    as a JSON array (a comma list is rejected). They did help listed drugs
    (28/32 vs 19/32), but also rewrote unlisted drugs into listed look-alikes:
    "amiodarone" came back as "amlodipine" in 4 of 4 trials. A wrong real drug
    name in a record is worse than a misspelled one, and no list covers every
    drug, so they stay opt-in (STT_KEYTERMS=true) for site names, not drugs.

Pipecat 1.5.0 knows neither saaras:v4 nor keyterms; both are added here
without touching vendored code: v4 is registered in Pipecat's model table with
v3's capabilities (same endpoint and parameters per Sarvam's API reference),
and keyterms ride the Sarvam SDK's documented
request_options["additional_query_parameters"].
"""
import json

from loguru import logger
from pipecat.services.sarvam import stt as sarvam_stt
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.transcriptions.language import Language

from voice.config import VoiceConfig
from voice.vocabulary import load_keyterms

sarvam_stt.MODEL_CONFIGS.setdefault("saaras:v4", sarvam_stt.MODEL_CONFIGS["saaras:v3"])

# The hand-tuned block that lived in bot.py before this change. It lowered
# Sarvam's speech thresholds below the server defaults (0.7 / 0.45 / 8 first-turn
# frames), so Sarvam's VAD accepts more noise as speech, and its -45 dB volume
# floor discards soft voices. Kept only as a rollback profile
# (SARVAM_VAD_PROFILE=permissive).
_PERMISSIVE_VAD = {
    "positive_speech_threshold": 0.5,
    "negative_speech_threshold": 0.2,
    "min_speech_frames": 3,
    "first_turn_min_speech_frames": 3,
    "start_speech_volume_threshold": -45,
    "num_initial_ignored_frames": 2,
}


class KeytermSarvamSTTService(SarvamSTTService):
    """SarvamSTTService that can send `keyterms` and reports its connections."""

    def __init__(self, *, keyterms: list[str] | None = None, **kwargs):
        super().__init__(**kwargs)
        if not keyterms:
            return
        extra = {"keyterms": json.dumps(keyterms)}
        streaming = self._sarvam_client.speech_to_text_streaming
        connect = streaming.connect

        def connect_with_keyterms(**connect_kwargs):
            options = dict(connect_kwargs.pop("request_options", None) or {})
            options["additional_query_parameters"] = {
                **(options.get("additional_query_parameters") or {}),
                **extra,
            }
            return connect(**connect_kwargs, request_options=options)

        # Scoped to this service's own SDK client instance, not the library.
        streaming.connect = connect_with_keyterms

    async def _connect(self):
        # Pipecat 1.5.0 documents an on_connected event for this service but
        # never emits it; emit it so (re)connects are observable.
        await super()._connect()
        if self._socket_client is not None:
            await self._call_event_handler("on_connected")


def build_sarvam_stt(api_key: str, config: VoiceConfig) -> SarvamSTTService:
    keyterms = []
    if config.stt_keyterms and config.stt_model == "saaras:v4" and config.stt_keyterm_limit:
        keyterms = load_keyterms(config.stt_keyterm_limit, config.stt_vocabulary_extra_file)
    elif config.stt_keyterms:
        logger.warning(f"STT keyterms skipped: Sarvam accepts them only for saaras:v4 (model={config.stt_model})")

    vad = _PERMISSIVE_VAD if config.sarvam_vad_profile == "permissive" else {}
    try:
        language = Language(config.stt_language) if config.stt_language != "unknown" else None
    except ValueError:
        logger.warning(f"Unknown SARVAM_STT_LANGUAGE={config.stt_language!r}; using en-IN")
        language = Language.EN_IN

    logger.info(
        f"Sarvam STT: model={config.stt_model} language={config.stt_language} "
        f"mode={config.stt_mode} keyterms={len(keyterms)} vad_profile={config.sarvam_vad_profile}"
    )
    return KeytermSarvamSTTService(
        api_key=api_key,
        mode=config.stt_mode,
        keyterms=keyterms,
        settings=SarvamSTTService.Settings(model=config.stt_model, language=language, **vad),
    )
