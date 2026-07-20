"""Regression tests for the WhatsApp secretary guardrails.

Covers the ignored-number denylist and the outbound leak guardrails
(sanitizer silence rules + transport fallback suppression).
"""

import asyncio
from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import (
    _sanitize_gateway_final_response,
    _whatsapp_blocklist_match,
    _whatsapp_blocklist_status,
)


def test_blocklist_matches_phone_jid_and_lid_resolution():
    mapping = [("5511998877665", "123456789012345")]

    assert _whatsapp_blocklist_match(
        ["55 11 99887-7665@s.whatsapp.net"],
        ["5511998877665"],
        mapping,
    )
    assert _whatsapp_blocklist_match(
        ["123456789012345@lid"],
        ["5511998877665"],
        mapping,
    )
    assert not _whatsapp_blocklist_match(
        ["5511998877666@s.whatsapp.net"],
        ["5511998877665"],
        mapping,
    )


def test_blocklist_status_reads_active_profile_and_reverse_mapping(tmp_path, monkeypatch):
    whatsapp_dir = tmp_path / "whatsapp"
    session_dir = whatsapp_dir / "session"
    session_dir.mkdir(parents=True)
    (whatsapp_dir / "ignored_numbers.txt").write_text(
        "5511998877665\n", encoding="utf-8"
    )
    (session_dir / "lid-mapping-123456789012345_reverse.json").write_text(
        '"5511998877665"', encoding="utf-8"
    )
    monkeypatch.setattr("gateway.run.get_hermes_home", lambda: tmp_path)

    source = SimpleNamespace(
        user_id="123456789012345@lid",
        user_id_alt=None,
        chat_id="123456789012345@lid",
    )
    blocked, reason = _whatsapp_blocklist_status(source)

    assert blocked
    assert reason == "matched"


def test_blocklist_status_allows_sender_not_in_list(tmp_path, monkeypatch):
    whatsapp_dir = tmp_path / "whatsapp"
    whatsapp_dir.mkdir(parents=True)
    (whatsapp_dir / "ignored_numbers.txt").write_text(
        "5511998877665\n", encoding="utf-8"
    )
    monkeypatch.setattr("gateway.run.get_hermes_home", lambda: tmp_path)

    source = SimpleNamespace(
        user_id="5511998877666@s.whatsapp.net",
        user_id_alt=None,
        chat_id="5511998877666@s.whatsapp.net",
    )
    blocked, reason = _whatsapp_blocklist_status(source)

    assert not blocked
    assert reason == "not_matched"


# ── Sanitizer: effectively-empty replies must be silenced (20/jul/2026) ──
#
# Incident 20/jul/2026: the model returned "\u200b\u200b" (zero-width spaces).
# The sanitizer let it through; the WhatsApp transport stripped the
# invisible chars, the bridge rejected the empty message
# ("chatId and message are required"), and the upstream plain-text
# fallback re-sent the content prefixed with the technical marker
# "(Response formatting failed, plain text:)" to the patient.


def test_whatsapp_sanitize_silences_invisible_only_response():
    """Zero-width / invisible-only content must be silenced at the
    sanitizer — there is nothing legitimate to deliver."""
    assert _sanitize_gateway_final_response("whatsapp", "\u200b\u200b") is None
    assert _sanitize_gateway_final_response("whatsapp", "\u200b\u2063\ufeff") is None
    assert _sanitize_gateway_final_response("whatsapp", " \u200b\u00a0 ") is None


def test_whatsapp_sanitize_blocks_plain_text_fallback_marker():
    """The upstream transport marker must never reach a contact, even if
    it somehow appears in the agent's own text."""
    leaked = "(Response formatting failed, plain text:)\n\nOlá"
    assert _sanitize_gateway_final_response("whatsapp", leaked) is None


def test_whatsapp_sanitize_keeps_normal_reply():
    answer = "Olá. Assistente do Dr. Victor Almeida. Em que posso ajudar?"
    assert _sanitize_gateway_final_response("whatsapp", answer) == answer


def test_whatsapp_sanitize_emoji_only_becomes_safe_default():
    """Pre-existing 7-layer behavior: emojis are stripped; an emoji-only
    reply degrades to the safe default message (never a bare emoji)."""
    result = _sanitize_gateway_final_response("whatsapp", "👍")
    assert result == "Recebi sua mensagem. O Dr. Victor verificará assim que possível."


# ── Transport: plain-text fallback suppressed on WhatsApp ─────────────


class _DummyAdapter(BasePlatformAdapter):
    """Minimal adapter whose send() always fails with a fixed error."""

    def __init__(self, platform, fail_error):
        super().__init__(SimpleNamespace(), platform)
        self._fail_error = fail_error
        self.sent_contents = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def get_chat_info(self, chat_id):
        return {"name": "dummy", "type": "dm"}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent_contents.append(content)
        return SendResult(success=False, error=self._fail_error, retryable=False)


def test_send_with_retry_suppresses_plain_text_fallback_on_whatsapp():
    """On WhatsApp, a failed send must NOT trigger the marked plain-text
    fallback — the technical marker must never leave the server."""
    adapter = _DummyAdapter(Platform.WHATSAPP, '{"error":"chatId and message are required"}')
    result = asyncio.run(adapter._send_with_retry(chat_id="123@lid", content="\u200b\u200b"))
    assert not result.success
    # Only the original send was attempted — no fallback with the marker.
    assert adapter.sent_contents == ["\u200b\u200b"]
    assert all("Response formatting failed" not in c for c in adapter.sent_contents)


def test_send_with_retry_still_falls_back_on_other_platforms():
    """Control case: non-WhatsApp platforms keep the upstream fallback."""
    adapter = _DummyAdapter(Platform.TELEGRAM, "some formatting error")
    result = asyncio.run(adapter._send_with_retry(chat_id="42", content="**broken"))
    assert not result.success
    assert len(adapter.sent_contents) == 2
    assert adapter.sent_contents[1].startswith("(Response formatting failed, plain text:)")
