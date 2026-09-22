"""Timestamped speech evidence providers for Footage Index Shot Cards."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class SpeechProviderError(RuntimeError):
    """Raised when an explicitly requested speech provider cannot complete."""


class SpeechProvider(Protocol):
    name: str
    model: str
    version: str

    def transcribe(self, source: Path) -> dict[str, Any]:
        """Return language metadata plus timestamped segments and words."""


def validate_transcript(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise SpeechProviderError("speech transcript must contain a segments array")
    normalized_segments: list[dict[str, Any]] = []
    for index, segment in enumerate(payload["segments"]):
        if not isinstance(segment, dict):
            raise SpeechProviderError(f"speech segment {index} must be an object")
        try:
            start = float(segment["start_seconds"])
            end = float(segment["end_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SpeechProviderError(f"speech segment {index} has invalid timestamps") from exc
        text = segment.get("text")
        if start < 0 or end <= start or not isinstance(text, str) or not text.strip():
            raise SpeechProviderError(f"speech segment {index} has an invalid range or empty text")
        words: list[dict[str, Any]] = []
        for word_index, word in enumerate(segment.get("words") or []):
            if not isinstance(word, dict):
                raise SpeechProviderError(f"speech word {index}/{word_index} must be an object")
            try:
                word_start = float(word["start_seconds"])
                word_end = float(word["end_seconds"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SpeechProviderError(f"speech word {index}/{word_index} has invalid timestamps") from exc
            word_text = word.get("text")
            if word_start < 0 or word_end <= word_start or not isinstance(word_text, str) or not word_text:
                raise SpeechProviderError(f"speech word {index}/{word_index} is invalid")
            normalized_word: dict[str, Any] = {
                "start_seconds": round(word_start, 6),
                "end_seconds": round(word_end, 6),
                "text": word_text,
            }
            probability = word.get("probability")
            if isinstance(probability, (int, float)) and not isinstance(probability, bool):
                normalized_word["probability"] = round(min(max(float(probability), 0.0), 1.0), 4)
            words.append(normalized_word)
        normalized_segment: dict[str, Any] = {
            "start_seconds": round(start, 6),
            "end_seconds": round(end, 6),
            "text": text.strip(),
            "words": words,
        }
        normalized_segments.append(normalized_segment)
    language = payload.get("language")
    probability = payload.get("language_probability")
    return {
        "language": language if isinstance(language, str) else "",
        "language_probability": (
            round(min(max(float(probability), 0.0), 1.0), 4)
            if isinstance(probability, (int, float)) and not isinstance(probability, bool)
            else None
        ),
        "segments": normalized_segments,
    }


def speech_text_for_range(transcript: dict[str, Any], start_seconds: float, end_seconds: float) -> str:
    """Assign words to exactly one contiguous Shot using their midpoint."""
    pieces: list[str] = []
    for segment in transcript.get("segments", []):
        words = segment.get("words") or []
        if words:
            for word in words:
                midpoint = (word["start_seconds"] + word["end_seconds"]) / 2.0
                if start_seconds <= midpoint < end_seconds:
                    pieces.append(word["text"])
            continue
        midpoint = (segment["start_seconds"] + segment["end_seconds"]) / 2.0
        if start_seconds <= midpoint < end_seconds:
            pieces.append(segment["text"])
    return "".join(pieces).strip()


class FasterWhisperSpeechProvider:
    """Local-only faster-whisper adapter with VAD and word timestamps."""

    name = "faster-whisper"
    version = "1"

    def __init__(
        self,
        model: str = "medium",
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        local_files_only: bool = True,
    ) -> None:
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.local_files_only = local_files_only
        self._model: Any = None

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise SpeechProviderError("faster-whisper is not installed") from exc
        try:
            self._model = WhisperModel(
                self.model,
                device=self.device,
                compute_type=self.compute_type,
                local_files_only=self.local_files_only,
            )
        except Exception as exc:
            raise SpeechProviderError(
                f"cannot load local faster-whisper model {self.model}: {type(exc).__name__}: {exc}"
            ) from exc
        return self._model

    def transcribe(self, source: Path) -> dict[str, Any]:
        model = self._load_model()
        try:
            segments, info = model.transcribe(
                str(source),
                beam_size=5,
                condition_on_previous_text=False,
                word_timestamps=True,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )
            normalized: list[dict[str, Any]] = []
            for segment in segments:
                text = str(segment.text or "").strip()
                if not text or float(getattr(segment, "no_speech_prob", 0.0) or 0.0) > 0.6:
                    continue
                words: list[dict[str, Any]] = []
                for word in getattr(segment, "words", None) or []:
                    if word.start is None or word.end is None or not word.word:
                        continue
                    words.append(
                        {
                            "start_seconds": float(word.start),
                            "end_seconds": float(word.end),
                            "text": str(word.word),
                            "probability": float(word.probability),
                        }
                    )
                normalized.append(
                    {
                        "start_seconds": float(segment.start),
                        "end_seconds": float(segment.end),
                        "text": text,
                        "words": words,
                    }
                )
        except Exception as exc:
            raise SpeechProviderError(
                f"faster-whisper transcription failed for {source.name}: {type(exc).__name__}: {exc}"
            ) from exc
        return validate_transcript(
            {
                "language": str(getattr(info, "language", "") or ""),
                "language_probability": getattr(info, "language_probability", None),
                "segments": normalized,
            }
        )


def create_speech_provider(
    provider: str,
    model: str,
    *,
    device: str,
    compute_type: str,
) -> SpeechProvider | None:
    selected = provider.casefold()
    if selected == "none":
        return None
    if selected != "faster-whisper":
        raise SpeechProviderError(f"unsupported speech provider: {provider}")
    return FasterWhisperSpeechProvider(
        model,
        device=device,
        compute_type=compute_type,
        local_files_only=True,
    )
