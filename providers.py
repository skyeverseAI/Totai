import os
import logging

from livekit.plugins import openai, deepgram, elevenlabs, sarvam

import config

logger = logging.getLogger("mia-dancing-cow")


def build_stt():
    if config.STT_PROVIDER == "deepgram":
        logger.info("STT: Deepgram nova-3")
        return deepgram.STT(
            model=config.DEEPGRAM_STT_MODEL,
            language=config.DEEPGRAM_STT_LANGUAGE,
            api_key=os.getenv("DEEPGRAM_API_KEY"),
        )
    else:
        logger.info("STT: Sarvam saaras:v3")
        return sarvam.STT(
            model=config.SARVAM_STT_MODEL,
            language=config.SARVAM_STT_LANGUAGE,
            api_key=os.getenv("SARVAM_API_KEY"),
            high_vad_sensitivity=False,
        )


def build_tts():
    if config.TTS_PROVIDER == "elevenlabs":
        logger.info(f"TTS: ElevenLabs {config.ELEVENLABS_MODEL}")
        return elevenlabs.TTS(
            voice_id=config.ELEVENLABS_VOICE_ID,
            model=config.ELEVENLABS_MODEL,
            api_key=os.getenv("ELEVEN_API_KEY"),
        )
    else:
        logger.info("TTS: Sarvam bulbul:v3")
        return sarvam.TTS(
            model=config.SARVAM_TTS_MODEL,
            speaker=config.SARVAM_TTS_VOICE,
            target_language_code=config.SARVAM_TTS_LANGUAGE,
        )


def build_llm():
    logger.info(f"LLM: {config.LLM_PROVIDER} | {config.LLM_MODEL}")
    return openai.LLM(
        model=config.LLM_MODEL,
        api_key=config.LLM_API_KEY,
        base_url=config.LLM_BASE_URL,
        temperature=config.LLM_TEMPERATURE,
    )
