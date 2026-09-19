"""Optional Sarvam voice gateway.

The browser remains the zero-configuration fallback. When SARVAM_API_KEY is set,
short microphone recordings go to Saaras v3 for transcription and final agent text
goes to Bulbul v3 for Hindi audio. The key is only read by the backend.
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import SARVAM_LANGUAGES, settings


class VoiceError(RuntimeError):
    """A safe, user-facing error from the Sarvam voice service."""


def enabled() -> bool:
    return bool(settings.sarvam_api_key)


def status() -> dict[str, Any]:
    return {
        "provider": "sarvam" if enabled() else "browser",
        "configured": enabled(),
        "stt_model": settings.sarvam_stt_model,
        "tts_model": settings.sarvam_tts_model,
        "language": settings.sarvam_language,
        "speaker": settings.sarvam_speaker,
        "languages": [{"code": code, "name": name} for code, name in SARVAM_LANGUAGES.items()],
    }


def language_code(value: str | None) -> str:
    """Validate a UI-selected BCP-47 code and fall back to configured Hindi."""
    code = (value or settings.sarvam_language).strip()
    if code not in SARVAM_LANGUAGES:
        raise VoiceError(f"Unsupported language code: {code}")
    return code


def _api_url(path: str) -> str:
    return settings.sarvam_base_url.rstrip("/") + path


def _error_message(raw: bytes, fallback: str) -> str:
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or fallback)
        if err:
            return str(err)
        return str(payload.get("message") or fallback)
    except (TypeError, ValueError, UnicodeDecodeError):
        return fallback


def _post_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        _api_url(path),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-subscription-key": settings.sarvam_api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=settings.sarvam_timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise VoiceError(_error_message(exc.read(), f"Sarvam returned HTTP {exc.code}")) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise VoiceError(f"Could not reach Sarvam: {exc}") from exc
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise VoiceError("Sarvam returned an invalid response") from exc


def _multipart(fields: dict[str, str], filename: str, content_type: str, audio: bytes) -> tuple[bytes, str]:
    boundary = f"----vyapaar-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"),
            b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        audio,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), boundary


def transcribe(audio: bytes, filename: str = "voice.webm", content_type: str = "audio/webm",
               selected_language: str | None = None) -> str:
    """Transcribe one short browser recording with Saaras v3."""
    if not enabled():
        raise VoiceError("Sarvam voice is not configured")
    body, boundary = _multipart(
        {
            "model": settings.sarvam_stt_model,
            "language_code": language_code(selected_language),
            "mode": "codemix",
        },
        filename or "voice.webm",
        content_type or "audio/webm",
        audio,
    )
    request = Request(
        _api_url("/speech-to-text"),
        data=body,
        headers={
            "api-subscription-key": settings.sarvam_api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=settings.sarvam_timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise VoiceError(_error_message(exc.read(), f"Sarvam returned HTTP {exc.code}")) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise VoiceError(f"Could not reach Sarvam: {exc}") from exc
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise VoiceError("Sarvam returned an invalid transcription response") from exc
    text = str(payload.get("transcript") or "").strip()
    if not text:
        raise VoiceError("Sarvam could not hear any speech")
    return text


def synthesize(text: str, selected_language: str | None = None) -> bytes:
    """Generate a WAV response with Bulbul v3."""
    if not enabled():
        raise VoiceError("Sarvam voice is not configured")
    clean = text.strip()
    if not clean:
        raise VoiceError("Nothing to speak")
    if len(clean) > 2500:
        clean = clean[:2497].rstrip() + "…"
    payload = _post_json("/text-to-speech", {
        "text": clean,
        "language_code": language_code(selected_language),
        "model": settings.sarvam_tts_model,
        "speaker": settings.sarvam_speaker,
    })
    audios = payload.get("audios") or []
    if not audios:
        raise VoiceError("Sarvam returned no audio")
    try:
        return base64.b64decode(audios[0])
    except (TypeError, ValueError) as exc:
        raise VoiceError("Sarvam returned invalid audio") from exc
