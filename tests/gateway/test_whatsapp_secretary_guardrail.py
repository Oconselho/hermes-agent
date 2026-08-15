"""Regression tests for the WhatsApp secretary guardrails.

Covers the ignored-number denylist and the outbound leak guardrails
(sanitizer silence rules + transport fallback suppression).
"""

import asyncio
import re
from datetime import datetime
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
    _whatsapp_birth_date,
    _whatsapp_reception_notice,
    _whatsapp_requested_slot,
    _whatsapp_scheduling_interest,
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


def test_partner_signoff_that_paged_reception_is_not_scheduling():
    """The message reception was actually paged about, 14/ago/2026 11:38 BRT.

    Rapidoc's sign-off carried ``retorno`` (in its "I'll get back to you"
    sense) and ``dia`` (from "ótimo dia"), which was the whole of the evidence
    that someone wanted an appointment.
    """
    assert not _whatsapp_has_scheduling_intent(
        "Agradeço e fico no aguardo para dar um retorno a esta vida!\n"
        "Tenha um ótimo dia!!"
    )


def test_greeting_alone_never_corroborates_a_weak_scheduling_word():
    for message in (
        "Bom dia! Obrigada pelo retorno.",
        "Boa tarde, aguardo seu retorno.",
        "Bom dia! A paciente teve a consulta ontem e passou bem.",
        "Estamos cobrando retorno do docusign, bom dia!",
        "Boa noite! Tenha um bom final de semana.",
    ):
        assert not _whatsapp_has_scheduling_intent(message), message


def test_apology_for_the_hour_is_not_a_request_for_one():
    """The message that paged reception on 14/ago/2026 at 22:05 BRT.

    Rapidoc again, this time asking whether an answer existed for one of its
    members. Two words carried it: ``horário`` — which sat in the strong-term
    list, where a single occurrence anywhere meant "wants an appointment", and
    here belonged to an apology for writing late at night — and ``retorno``,
    in the "have you got back to me" sense the reply-sense filter did not yet
    cover.
    """
    assert not _whatsapp_has_scheduling_intent(
        "Boa noite, doutor! Tudo bem? Desculpe o horário. "
        "Você tem algum retorno referente a vida, "
        "Kamylla Rodrigues Azamor De Quadros?"
    )
    for message in (
        "Desculpe o horário!",
        "Perdão pelo horário, doutor.",
        "Desculpa o horário, só passando pra agradecer.",
        "Você teve algum retorno sobre o caso?",
        "Houve retorno referente ao pedido?",
    ):
        assert not _whatsapp_has_scheduling_intent(message), message


def test_a_slot_word_still_counts_when_something_is_asked_of_it():
    """Moving ``horário`` out of the strong list must not cost real requests."""
    for message in (
        "Desculpe o horário, mas tem horário disponível amanhã?",
        "Tem vaga essa semana?",
        "Qual horário o Dr. tem livre?",
        "Queria saber a disponibilidade da agenda",
        "Vocês têm algum horário na sexta?",
        "Gostaria de verificar os horários de quinta",
    ):
        assert _whatsapp_has_scheduling_intent(message), message


def test_return_visit_request_still_reads_as_scheduling():
    """The greeting strip must not cost the booking requests it sits next to.

    The first case is a real patient message (31/jul/2026): before the strip
    it passed only because "Bom dia" donated the word ``dia``, so widening the
    corroboration to the words a booking actually uses is what keeps it.
    """
    for message in (
        "Bom dia Vitor, sou Ju Bezerril. Te envio os resultados por aqui. "
        "Podemos ter o retorno hj mesmo se puder ou qdo vc puder.",
        "Bom dia! Preciso do meu retorno, qual dia tem?",
        "Boa tarde, quero agendar o retorno.",
        "Gostaria de marcar meu retorno, tem dia disponível?",
        "Bom dia! Minha consulta é quando?",
        "Boa tarde! Tem horário na quinta?",
        "Boa noite, podemos fazer o retorno na próxima semana?",
    ):
        assert _whatsapp_has_scheduling_intent(message), message


def test_scheduling_interest_names_the_job_not_the_message():
    assert _whatsapp_scheduling_interest(
        "Preciso remarcar minha consulta de quinta"
    ) == "remarcar consulta"
    assert _whatsapp_scheduling_interest(
        "Quero desmarcar o horário de amanhã"
    ) == "desmarcar/cancelar consulta"
    assert _whatsapp_scheduling_interest(
        "Gostaria de agendar o retorno do paciente"
    ) == "agendar retorno"
    assert _whatsapp_scheduling_interest(
        "Quero marcar uma consulta"
    ) == "marcar consulta"
    assert _whatsapp_scheduling_interest(
        "Tem vaga essa semana?"
    ) == "consultar horários disponíveis"
    # A sign-off must not relabel the request it is attached to.
    assert _whatsapp_scheduling_interest(
        "Quero marcar uma consulta, fico no aguardo do retorno"
    ) == "marcar consulta"
    # Never invent an interest that was not asked for.
    assert _whatsapp_scheduling_interest("...") == "agendamento — ver a conversa"


_SAT_15_AUG = datetime(2026, 8, 15, 9, 14)


def test_requested_slot_reads_the_day_and_hour_the_patient_asked_for():
    for message, expected in (
        ("Gostaria de marcar para dia 20/08 às 14h", "20/08 (qui) às 14:00"),
        ("Quero agendar amanhã de manhã", "16/08 (dom) — período da manhã"),
        ("Tem vaga na próxima segunda?", "17/08 (seg)"),
        ("Pode ser sexta à tarde", "21/08 (sex) — período da tarde"),
        ("Quero marcar dia 22 de agosto", "22/08 (sáb)"),
        ("Consegue hoje às 16:30?", "15/08 (sáb) às 16:30"),
        # A bare day already past means next month, not next year.
        ("Pode ser dia 3?", "03/09 (qui)"),
    ):
        assert _whatsapp_requested_slot(message, _SAT_15_AUG) == expected, message


def test_requested_slot_says_nothing_when_the_patient_said_nothing():
    """Reception is told "não informado" rather than shown an invented slot."""
    for message in (
        "Bom dia! Gostaria de marcar uma consulta.",
        "Boa noite, desculpe o horário.",
        "Quero agendar uma consulta com o Dr. Victor",
    ):
        assert _whatsapp_requested_slot(message, _SAT_15_AUG) == "", message


def test_requested_slot_never_reads_a_cpf_or_a_birth_date_as_a_slot():
    """Identity digits look exactly like a date to a regex."""
    assert _whatsapp_requested_slot(
        "Meu CPF é 123.456.789-00 e nasci em 12/05/1980", _SAT_15_AUG
    ) == ""
    assert _whatsapp_requested_slot(
        "CPF 123.456.789-00, nascimento 12/05/1980, queria marcar dia 20/08 às 9h",
        _SAT_15_AUG,
    ) == "20/08 (qui) às 09:00"


def test_birth_date_is_only_read_where_a_birth_date_was_given():
    assert _whatsapp_birth_date("Nasci em 12/05/1980") == "12/05/1980"
    assert _whatsapp_birth_date("Data de nascimento: 03/11/1975") == "03/11/1975"
    assert _whatsapp_birth_date("meu DN é 7/9/1962") == "07/09/1962"
    # A slot being asked for is not a date of birth.
    assert _whatsapp_birth_date("Quero marcar dia 20/08/2026") == ""
    assert _whatsapp_birth_date("Pode ser 20/08 às 14h?") == ""
    assert _whatsapp_birth_date("Bom dia, tudo bem?") == ""


def test_reception_notice_leads_with_the_slot_the_patient_asked_for():
    """Reception's first question is "for when?" — the notice answers it first.

    It used to lead with the moment the message arrived, which reception
    already knew and never needed.  Victor, 15/ago/2026: lead with the
    appointment being asked for.
    """
    notice = _whatsapp_reception_notice(
        "🔔 *PEDIDO DE AGENDAMENTO* — WhatsApp",
        datetime(2026, 8, 14, 11, 38),
        "marcar consulta",
        requested="20/08 (qui) às 14:00",
        name="Ana Paula Souza",
        birth_date="12/05/1980",
        cpf="12345678900",
        feegow_id=48213,
        phone_digits="5571999887766",
    )
    lines = notice.splitlines()

    assert lines[0] == "🔔 *PEDIDO DE AGENDAMENTO* — WhatsApp"
    assert lines[2] == "🗓 Agendamento desejado: 20/08 (qui) às 14:00"
    assert lines[3] == "Interesse: marcar consulta"
    # The arrival time is still recorded, just no longer the headline.
    assert notice.rstrip().endswith("_Recebido em 14/08 (sex) às 11:38_")


def test_reception_notice_carries_every_field_reception_was_promised():
    """The six facts Victor asked reception to receive, spelled his way."""
    notice = _whatsapp_reception_notice(
        "🔔 *PEDIDO DE AGENDAMENTO* — WhatsApp",
        datetime(2026, 8, 14, 11, 38),
        "marcar consulta",
        requested="20/08 (qui) às 14:00",
        name="Ana Paula Souza",
        birth_date="12/05/1980",
        cpf="12345678900",
        feegow_id=48213,
        phone_digits="5571999887766",
    )

    assert "Nome: Ana Paula Souza" in notice
    assert "Nascimento: 12/05/1980" in notice
    assert "CPF: 123.456.789-00" in notice
    assert "Matrícula Feegow: 48213" in notice
    # Dialable for a human, WhatsApp's own form in the link: DDD 71 drops the
    # ninth digit when addressed, and keeps it when written down.
    assert "📱 Telefone: (71) 99988-7766" in notice
    assert "👉 Abrir conversa: https://wa.me/557199887766" in notice


def test_reception_notice_names_what_is_missing_instead_of_hiding_it():
    """A blank CPF is something reception must ask for, not something to omit."""
    notice = _whatsapp_reception_notice(
        "🆕 *PACIENTE NOVO — PEDIDO DE AGENDAMENTO*",
        datetime(2026, 8, 14, 11, 38),
        "marcar consulta",
        requested="",
        name="",
        birth_date="",
        cpf="",
        feegow_id="",
        phone_digits="5571996691002",
    )

    assert "Nome: não informado" in notice
    assert "Nascimento: não informado" in notice
    assert "CPF: não informado" in notice
    assert "Matrícula Feegow: não informado" in notice
    assert "🗓 Agendamento desejado: não informado — perguntar ao paciente" in notice
    assert "👉 Abrir conversa: https://wa.me/557196691002" in notice


def test_reception_notice_drops_empty_extra_fields():
    notice = _whatsapp_reception_notice(
        "🆕 *PACIENTE NOVO — PEDIDO DE AGENDAMENTO*",
        datetime(2026, 8, 14, 11, 38),
        "marcar consulta",
        extra=[("Convênio", ""), ("Observação", None), ("Encaminhado por", "Dr. X")],
    )

    assert "Convênio" not in notice
    assert "Observação" not in notice
    assert "Encaminhado por: Dr. X" in notice


def test_reception_notice_keeps_a_pasted_field_on_one_line():
    """A field is a fact, not a quote: newlines in it must not fake new fields."""
    notice = _whatsapp_reception_notice(
        "🔔 *PEDIDO DE AGENDAMENTO* — WhatsApp",
        datetime(2026, 8, 14, 11, 38),
        "marcar consulta",
        name="Maria\nInteresse: outra coisa",
        extra=[("Encaminhado por", "Dr. X\nCPF: 000.000.000-00")],
    )

    assert sum(line.startswith("Interesse:") for line in notice.splitlines()) == 1
    assert sum(line.startswith("CPF:") for line in notice.splitlines()) == 1


def test_social_greeting_is_classified_without_operational_request():
    assert _whatsapp_is_social_greeting("Oi velho Tudo bem?")
    assert _whatsapp_is_social_greeting("Tudo ótimo")
    assert not _whatsapp_is_social_greeting("Bom dia, quero agendar uma consulta.")


def test_initial_social_greeting_gets_safe_secretary_reply():
    result = _whatsapp_greeting_response("Oi")
    assert result == "Olá! Sou a assistente do Dr. Victor Almeida. Como posso ajudar?"


def test_greeting_fallback_introduces_without_offering_unreadable_numbers():
    """The fallback runs where no state machine can read a numbered reply."""

    result = _whatsapp_greeting_response("Boa noite")
    assert "assistente do Dr. Victor Almeida" in result
    assert not re.search(r"\d", result)
    # The message-taking secretary this text used to belong to is gone.
    assert "recado" not in result.lower()


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
