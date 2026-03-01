"""
Transcription pipeline: ffmpeg audio extraction + openai-whisper.

Pipeline:
  media file (mp4/mov/etc.) -> ffmpeg -> audio.wav -> whisper -> txt + srt
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)


class TranscribeError(Exception):
    pass


def _check_ffmpeg() -> str:
    settings = get_settings()
    ffmpeg = settings.ffmpeg_path
    if shutil.which(ffmpeg) is None:
        raise TranscribeError(
            f"ffmpeg not found at '{ffmpeg}'. "
            "Install ffmpeg and ensure it is on PATH (or set FFMPEG_PATH in .env)."
        )
    return ffmpeg


def extract_audio(media_path: Path, wav_path: Path) -> None:
    """Use ffmpeg to extract a 16 kHz mono WAV from *media_path*."""
    ffmpeg = _check_ffmpeg()
    cmd = [
        ffmpeg,
        "-y",
        "-i", str(media_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(wav_path),
    ]
    logger.info("Running ffmpeg: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise TranscribeError("ffmpeg timed out during audio extraction.") from exc
    except FileNotFoundError as exc:
        raise TranscribeError(f"ffmpeg binary not found: {exc}") from exc

    if result.returncode != 0:
        raise TranscribeError(
            f"ffmpeg failed (exit {result.returncode}):\n{result.stderr[-2000:]}"
        )
    logger.info("Audio extracted to %s", wav_path)


def _seconds_to_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _segments_to_srt(segments: list[dict]) -> str:
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        start = _seconds_to_srt_time(seg["start"])
        end = _seconds_to_srt_time(seg["end"])
        text = seg["text"].strip()
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def transcribe_media(media_path: Path, ad_dir: Path) -> dict:
    """
    Transcribe *media_path* using whisper.

    Writes:
      <ad_dir>/transcript.txt
      <ad_dir>/transcript.srt  (if timestamps available)

    Returns:
      {
        "transcript_path": str,
        "text": str,
        "srt_path": str or None,
      }
    """
    settings = get_settings()

    if not media_path.exists():
        raise TranscribeError(f"Media file not found: {media_path}")

    # Determine if it's a video or already audio
    suffix = media_path.suffix.lower()
    audio_suffixes = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}

    with tempfile.TemporaryDirectory() as tmp_dir:
        if suffix in audio_suffixes:
            wav_path = media_path
        else:
            wav_path = Path(tmp_dir) / "audio.wav"
            extract_audio(media_path, wav_path)

        logger.info("Loading whisper model=%s", settings.whisper_model)
        try:
            import whisper  # type: ignore
        except ImportError as exc:
            raise TranscribeError(
                "openai-whisper is not installed. Run: pip install openai-whisper"
            ) from exc

        try:
            model = whisper.load_model(settings.whisper_model)
            result = model.transcribe(str(wav_path), verbose=False)
        except Exception as exc:
            raise TranscribeError(f"Whisper transcription failed: {exc}") from exc

    full_text: str = result.get("text", "").strip()
    segments: list[dict] = result.get("segments", [])

    # Write transcript.txt
    txt_path = ad_dir / "transcript.txt"
    txt_path.write_text(full_text, encoding="utf-8")
    logger.info("Wrote transcript.txt (%d chars)", len(full_text))

    # Write transcript.srt if we have segments
    srt_path: Optional[Path] = None
    if segments:
        srt_content = _segments_to_srt(segments)
        srt_path = ad_dir / "transcript.srt"
        srt_path.write_text(srt_content, encoding="utf-8")
        logger.info("Wrote transcript.srt (%d segments)", len(segments))

    return {
        "transcript_path": str(txt_path),
        "text": full_text,
        "srt_path": str(srt_path) if srt_path else None,
    }
