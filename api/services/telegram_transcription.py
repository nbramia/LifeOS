"""Local speech-to-text for Telegram voice messages."""

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_MODEL = None
_MODEL_LOCK = threading.Lock()


def transcribe(path: str | Path) -> str:
    """Transcribe an audio file with a lazily loaded local Whisper model."""
    global _MODEL
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "Local transcription is not installed. Install the faster-whisper dependency."
        ) from exc

    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                model_name = os.getenv("LIFEOS_TELEGRAM_WHISPER_MODEL", "base")
                device = os.getenv("LIFEOS_TELEGRAM_WHISPER_DEVICE", "cpu")
                compute_type = os.getenv("LIFEOS_TELEGRAM_WHISPER_COMPUTE_TYPE", "int8")
                logger.info("Loading Telegram Whisper model %s (%s/%s)", model_name, device, compute_type)
                _MODEL = WhisperModel(model_name, device=device, compute_type=compute_type)

    segments, _info = _MODEL.transcribe(str(path), vad_filter=True)
    return " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
