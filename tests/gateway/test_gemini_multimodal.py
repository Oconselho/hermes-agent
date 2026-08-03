"""Tests for the WhatsApp Gemini multimodal attachment pipeline."""

import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


def _fake_response(payload: dict):
    raw = json.dumps(payload).encode("utf-8")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return raw

    return Response()


def test_analyze_file_sends_audio_as_inline_data_to_selected_model(tmp_path):
    from gateway.gemini_multimodal import analyze_file

    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"ogg-fixture")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["api_key_header"] = request.get_header("X-goog-api-key")
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _fake_response(
            {"candidates": [{"content": {"parts": [{"text": "transcrição do áudio"}]}}]}
        )

    with patch("gateway.gemini_multimodal.urllib.request.urlopen", fake_urlopen):
        result = analyze_file(
            audio_path,
            kind="audio",
            api_key="test-key",
            model="gemini-3.1-flash-lite",
        )

    assert result == "transcrição do áudio"
    assert "/models/gemini-3.1-flash-lite:generateContent" in captured["url"]
    assert "key=" not in captured["url"]
    assert captured["api_key_header"] == "test-key"
    part = captured["body"]["contents"][0]["parts"][1]["inline_data"]
    assert part["mime_type"] == "audio/ogg"
    assert base64.b64decode(part["data"]) == b"ogg-fixture"
    assert "transcreva" in captured["body"]["contents"][0]["parts"][0]["text"].lower()


def test_analyze_file_uses_image_and_document_mime_types(tmp_path):
    from gateway.gemini_multimodal import analyze_file

    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    document_path = tmp_path / "report.pdf"
    document_path.write_bytes(b"pdf-fixture")
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(json.loads(request.data.decode("utf-8")))
        return _fake_response(
            {"candidates": [{"content": {"parts": [{"text": "leitura"}]}}]}
        )

    with patch("gateway.gemini_multimodal.urllib.request.urlopen", fake_urlopen):
        analyze_file(image_path, kind="image", api_key="test-key", mime_type="image/jpeg")
        analyze_file(
            document_path,
            kind="document",
            api_key="test-key",
            mime_type="application/octet-stream",
        )

    assert requests[0]["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "image/png"
    assert requests[1]["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "application/pdf"
    assert "imagem" in requests[0]["contents"][0]["parts"][0]["text"].lower()
    assert "documento" in requests[1]["contents"][0]["parts"][0]["text"].lower()


def test_analyze_file_rejects_oversized_attachment_before_network(tmp_path):
    from gateway.gemini_multimodal import GeminiMultimodalError, analyze_file

    path = tmp_path / "too-large.bin"
    path.write_bytes(b"12345")

    with patch("gateway.gemini_multimodal.urllib.request.urlopen") as urlopen:
        with pytest.raises(GeminiMultimodalError, match="too large"):
            analyze_file(path, kind="document", api_key="test-key", max_bytes=4)

    urlopen.assert_not_called()


@pytest.mark.asyncio
async def test_whatsapp_preparation_routes_audio_images_and_documents_to_gemini(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "main-secretary-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False

    paths = [tmp_path / "voice.ogg", tmp_path / "photo.jpg", tmp_path / "report.pdf"]
    for path in paths:
        path.write_bytes(b"fixture")
    event = MessageEvent(
        text="Leia os anexos",
        message_type=MessageType.VOICE,
        source=SessionSource(platform=Platform.WHATSAPP, chat_id="5511999999999", chat_type="dm"),
        media_urls=[str(path) for path in paths],
        media_types=["audio/ogg", "image/jpeg", "application/pdf"],
    )
    captured = {}

    async def fake_enrich(message_text, media_items):
        captured["message_text"] = message_text
        captured["media_items"] = media_items
        return "[Gemini multimodal]\ntranscrição e leitura", ["transcrição"]

    monkeypatch.setattr(runner, "_enrich_message_with_gemini_multimodal", fake_enrich, raising=False)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"multimodal": {"enabled": True, "model": "gemini-3.1-flash-lite"}},
    )

    result = await runner._prepare_inbound_message_text(event=event, source=event.source, history=[])

    assert "Gemini multimodal" in result
    assert [item["kind"] for item in captured["media_items"]] == ["audio", "image", "document"]
    assert [item["path"] for item in captured["media_items"]] == [str(path) for path in paths]
    assert not getattr(runner, "_pending_native_image_paths_by_session", {})


@pytest.mark.asyncio
async def test_non_whatsapp_attachments_keep_existing_pipeline(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=False)
    runner.adapters = {}
    runner._model = "main-secretary-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"fixture")
    event = MessageEvent(
        text="",
        message_type=MessageType.PHOTO,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm"),
        media_urls=[str(path)],
        media_types=["image/jpeg"],
    )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Gemini WhatsApp pipeline must not run on Telegram")

    monkeypatch.setattr(runner, "_enrich_message_with_gemini_multimodal", fail_if_called, raising=False)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"multimodal": {"enabled": True}})
    monkeypatch.setattr("gateway.run.GatewayRunner._decide_image_input_mode", lambda _self, **_kwargs: "text")

    async def fake_vision(*_args):
        return "vision text"

    monkeypatch.setattr("gateway.run.GatewayRunner._enrich_message_with_vision", fake_vision)

    result = await runner._prepare_inbound_message_text(event=event, source=event.source, history=[])

    assert result == "vision text"
