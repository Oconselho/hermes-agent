"""Google Gemini multimodal preprocessing for inbound messaging attachments.

The WhatsApp secretary uses this module as an attachment reader.  It sends the
bytes to Gemini and returns only text to the conversation model, so the main
secretary model does not need native media support and never receives raw
base64 payloads.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
_SUPPORTED_KINDS = frozenset({"audio", "image", "document"})


class GeminiMultimodalError(RuntimeError):
    """Raised when Gemini cannot read an inbound attachment."""


_MIME_BY_SUFFIX = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".json": "application/json",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}

_PROMPTS = {
    "audio": (
        "Transcreva este áudio em português brasileiro com máxima fidelidade. "
        "Preserve nomes, números, datas e termos técnicos; indique [inaudível] "
        "quando necessário. Retorne somente a transcrição, sem comentários."
    ),
    "image": (
        "Leia esta imagem de forma objetiva. Transcreva todo texto visível, "
        "incluindo números, datas, nomes e valores, e descreva brevemente os "
        "elementos relevantes. Não faça diagnóstico médico nem invente detalhes. "
        "Retorne somente a leitura da imagem."
    ),
    "document": (
        "Leia este documento ou arquivo de forma objetiva. Extraia o texto e os dados relevantes, "
        "preservando nomes, números, datas, valores e tabelas quando possível. "
        "Não faça diagnóstico médico nem invente conteúdo; marque trechos ilegíveis. "
        "Retorne somente o conteúdo lido ou um resumo fiel quando o formato exigir."
    ),
}


def mime_type_for_path(path: str | os.PathLike[str], supplied: str | None = None) -> str:
    """Return a stable MIME type for a cached attachment."""
    supplied = str(supplied or "").strip().lower()
    if supplied and supplied not in {"unknown", "application/octet-stream"}:
        return supplied
    suffix = Path(path).suffix.lower()
    if suffix in _MIME_BY_SUFFIX:
        return _MIME_BY_SUFFIX[suffix]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def mime_type_for_bytes(
    data: bytes,
    path: str | os.PathLike[str],
    supplied: str | None = None,
) -> str:
    """Prefer recognizable magic bytes over an untrusted bridge MIME type."""
    signatures = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"%PDF-", "application/pdf"),
        (b"OggS", "audio/ogg"),
        (b"ID3", "audio/mpeg"),
    )
    for signature, mime_type in signatures:
        if data.startswith(signature):
            return mime_type
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "audio/mp4" if data[8:12] in {b"M4A ", b"M4B ", b"M4P "} else "video/mp4"
    return mime_type_for_path(path, supplied)


def prompt_for_kind(kind: str) -> str:
    """Return the fixed, non-user-controlled instruction for an attachment kind."""
    try:
        return _PROMPTS[kind]
    except KeyError as exc:
        raise GeminiMultimodalError(f"unsupported attachment kind: {kind}") from exc


def _api_key_from_environment() -> str:
    return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()


def _response_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise GeminiMultimodalError("Gemini returned no candidate")
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise GeminiMultimodalError("Gemini returned no content parts")
    text = "\n".join(
        str(part.get("text", "")).strip()
        for part in parts
        if isinstance(part, dict) and part.get("text")
    ).strip()
    if not text:
        raise GeminiMultimodalError("Gemini returned empty content")
    return text


def analyze_file(
    path: str | os.PathLike[str],
    *,
    kind: str,
    api_key: str | None = None,
    model: str | None = None,
    mime_type: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """Read one local attachment with Gemini and return its textual result.

    The caller supplies the kind because a WhatsApp voice note and a regular
    audio file can have the same MIME type but different semantics.  No raw
    bytes or credentials are included in raised error messages.
    """
    if kind not in _SUPPORTED_KINDS:
        raise GeminiMultimodalError(f"unsupported attachment kind: {kind}")
    attachment = Path(path)
    try:
        size = attachment.stat().st_size
    except OSError as exc:
        raise GeminiMultimodalError("attachment is not readable") from exc
    if not attachment.is_file():
        raise GeminiMultimodalError("attachment is not a regular file")
    if size > max_bytes:
        raise GeminiMultimodalError(f"attachment too large ({size} bytes)")
    if max_bytes <= 0:
        raise GeminiMultimodalError("attachment size limit is invalid")

    key = (api_key or _api_key_from_environment()).strip()
    if not key:
        raise GeminiMultimodalError("Gemini API key is not configured")
    selected_model = (model or os.getenv("GEMINI_MULTIMODAL_MODEL") or DEFAULT_MODEL).strip()
    if not selected_model:
        raise GeminiMultimodalError("Gemini multimodal model is not configured")
    try:
        timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise GeminiMultimodalError("Gemini timeout is invalid") from exc
    if timeout <= 0:
        raise GeminiMultimodalError("Gemini timeout must be positive")

    try:
        data = attachment.read_bytes()
    except OSError as exc:
        raise GeminiMultimodalError("attachment could not be read") from exc

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt_for_kind(kind)},
                {
                    "inline_data": {
                        "mime_type": mime_type_for_bytes(data, attachment, mime_type),
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                },
            ]
        }],
        "generation_config": {"temperature": 0, "max_output_tokens": 4096},
    }
    endpoint = (
        f"{base_url.rstrip('/')}/models/"
        f"{urllib.parse.quote(selected_model, safe='')}:generateContent"
    )
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-goog-api-key": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Keep the provider response out of the patient-facing path.  The status
        # is enough for logs and avoids echoing a provider payload containing
        # request metadata or accidental credential-like strings.
        raise GeminiMultimodalError(f"Gemini request failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GeminiMultimodalError("Gemini request could not be completed") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeminiMultimodalError("Gemini returned invalid JSON") from exc

    if not isinstance(response_payload, dict):
        raise GeminiMultimodalError("Gemini returned an invalid response")
    return _response_text(response_payload)
