"""Regression tests for the WhatsApp secretary guardrails.

Covers the ignored-number denylist and the outbound leak guardrails
(sanitizer silence rules + transport fallback suppression).
"""

import asyncio
from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import (
    GatewayRunner,
    _sanitize_gateway_final_response,
    _should_suppress_whatsapp_followup,
    _whatsapp_blocklist_match,
    _whatsapp_blocklist_status,
    _whatsapp_contact_context,
    _whatsapp_contact_is_organization,
    _whatsapp_declared_person_name,
    _whatsapp_finalize_secretary_response,
    _whatsapp_greeting_response,
    _whatsapp_has_scheduling_intent,
    _whatsapp_is_social_greeting,
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


def test_blocklisted_message_returns_none_to_the_platform_adapter(monkeypatch):
    """A denylisted WhatsApp contact is silently ignored, never returned as
    an agent-result dict for BasePlatformAdapter to treat as response text."""
    runner = object.__new__(GatewayRunner)
    source = SimpleNamespace(
        platform=Platform.WHATSAPP,
        user_name=None,
        user_id="5511998877665@s.whatsapp.net",
        chat_id="5511998877665@s.whatsapp.net",
    )
    event = SimpleNamespace(text="Oi")
    monkeypatch.setattr(
        "gateway.run._whatsapp_blocklist_status", lambda _source: (True, "matched")
    )

    result = asyncio.run(
        runner._handle_message_with_agent(event, source, "blocked-whatsapp", 1)
    )

    assert result is None


def test_rapidoc_partner_context_keeps_sender_separate_from_patient():
    source = SimpleNamespace(
        chat_name="Rapidoc Telemedicina",
        user_name="Rapidoc Telemedicina",
    )
    message = "*Grazi*\nA paciente foi atendida pela plataforma e precisa de orientação sobre a receita."

    context = _whatsapp_contact_context(source, message)

    assert _whatsapp_contact_is_organization(source)
    assert _whatsapp_declared_person_name(message) == "Grazi"
    assert context["role"] == "empresa/plataforma parceira de telemedicina"
    assert context["declared_name"] == "Grazi"
    assert context["organization"] == "true"


def test_medical_context_with_consulta_is_not_scheduling_by_itself():
    message = "A paciente teve uma nova consulta e precisa avaliar a receita emitida."
    assert not _whatsapp_has_scheduling_intent(message)


def test_explicit_booking_request_is_scheduling():
    assert _whatsapp_has_scheduling_intent("Gostaria de marcar uma consulta e saber os horários disponíveis.")


def test_social_greeting_is_classified_without_operational_request():
    assert _whatsapp_is_social_greeting("Oi velho Tudo bem?")
    assert _whatsapp_is_social_greeting("Tudo ótimo")
    assert not _whatsapp_is_social_greeting("Bom dia, quero agendar uma consulta.")


def test_initial_social_greeting_gets_safe_secretary_reply():
    result = _whatsapp_greeting_response("Oi")
    assert result == (
        "Olá, sou a assistente do Dr. Victor. Ele está ocupado no momento. "
        "Posso anotar seu recado?"
    )


def test_status_update_without_request_is_suppressed_after_recent_reply():
    history = [
        {"role": "user", "content": "Oi", "timestamp": 90},
        {
            "role": "assistant",
            "content": "A secretaria recebeu sua mensagem. O Dr. Victor avaliará.",
            "timestamp": 100,
        },
    ]
    assert _should_suppress_whatsapp_followup("Ainda estou em Feira", history, now=110)
    assert _should_suppress_whatsapp_followup("Não vou conseguir chegar pra 17:00", history, now=111)


def test_secretary_response_cannot_claim_human_identity_or_use_human_signoff():
    result = _whatsapp_finalize_secretary_response(
        "Tudo ótimo! O Dr. Victor está sabendo. Um abraço!",
        [],
        current_text="Oi velho Tudo bem?",
    )
    lowered = result.lower()
    assert "assistente do dr. victor almeida" in lowered
    assert "tudo ótimo" not in lowered
    assert "abraço" not in lowered


def test_first_substantive_reply_identifies_automated_secretary():
    result = _whatsapp_finalize_secretary_response(
        "Boa tarde. Para agendamento, fale com a recepção.",
        [],
        current_text="Boa tarde, quero agendar uma consulta.",
    )
    assert result.lower().startswith("boa tarde. aqui é a assistente do dr. victor almeida")


def test_only_time_based_greeting_is_allowed_outbound():
    result = _whatsapp_finalize_secretary_response(
        "Olá! A secretaria recebeu sua mensagem. Tenha um bom dia.",
        [],
        current_text="Preciso de uma informação.",
    )
    lowered = result.lower()
    assert not lowered.startswith("olá")
    assert "tenha um bom dia" not in lowered
    assert "assistente do dr. victor almeida" in lowered


#
# Incident 20/jul/2026: the model returned "\\u200b\\u200b" (zero-width spaces).

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
