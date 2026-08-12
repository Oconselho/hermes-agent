"""CRM funnel: intent capture, single introduction, dead-end re-entry.

Every case here traces to the 12/ago/2026 booking test, where a patient who
said "0quero agendar" was answered by the model pipeline, introduced to
twice, and finally parked in a handoff no later message could leave.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.whatsapp_appointments import (
    AppointmentStore,
    LeadStage,
    Route,
    WhatsAppAppointmentsHandler,
    classify_route,
)

from tests.gateway.appointment_helpers import FakeFeegow, MutableClock, event

BRT = ZoneInfo("America/Bahia")


def _leads(db_path):
    connection = sqlite3.connect(db_path)
    try:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT chat_key, stage FROM leads")
        }
    finally:
        connection.close()


def _lead_path(db_path, chat_key):
    connection = sqlite3.connect(db_path)
    try:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT to_stage FROM lead_events WHERE chat_key = ?"
                " ORDER BY created_at, rowid",
                (chat_key,),
            )
        ]
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Intent capture — the funnel's entry
# --------------------------------------------------------------------------


def test_digit_glued_to_verb_still_enters_the_funnel():
    """The exact message from the 12/ago booking test.

    ``0quero agendar`` never matched ``\\bquero`` because a digit is a word
    character, so the message escaped to the model pipeline and the patient
    got a generic reply instead of the menu.
    """

    assert classify_route(event("0quero agendar")) is Route.APPOINTMENT


@pytest.mark.parametrize(
    "text",
    [
        "0quero agendar",
        "1quero remarcar",
        "agendar",
        "agendamento",
        "teleconsulta",
        "telemedicina",
        "quero uma consulta",
        "queria marcar uma consulta",
        "gostaria de agendar",
        "tem horario disponivel?",
        "tem alguma vaga essa semana?",
        "quanto custa a consulta",
        "qual o valor da consulta?",
        "como faco para marcar consulta",
        "consulta com o dr victor",
        "primeira consulta",
        "agendar,consulta",
        "preciso remarcar minha consulta",
    ],
)
def test_lead_phrasings_are_recognized(text):
    assert classify_route(event(text)) is Route.APPOINTMENT


@pytest.mark.parametrize(
    "text",
    [
        # The qualification question the model asks offers these paths in
        # words ("agendar uma consulta, remarcar ou desmarcar, agendar seu
        # retorno"). Whatever the patient echoes back has to land in the
        # funnel, or qualifying just leaks the lead one turn later.
        "quero agendar uma consulta",
        "agendar",
        "remarcar",
        "quero desmarcar",
        "retorno",
        "meu retorno",
        "quero meu retorno",
        "agendar meu retorno",
        "marcar retorno",
    ],
)
def test_answers_to_the_qualification_question_reenter_the_funnel(text):
    assert classify_route(event(text)) is Route.APPOINTMENT


@pytest.mark.parametrize(
    "text",
    [
        # "retorno" is also how a partner closes a message. Matching it bare
        # would drop them into the booking menu.
        "aguardo retorno",
        "fico no aguardo do retorno",
        "obrigado pelo retorno",
        "aguardo seu retorno sobre a proposta",
    ],
)
def test_partner_sign_offs_are_not_booking_intent(text):
    assert classify_route(event(text)) is not Route.APPOINTMENT


@pytest.mark.parametrize(
    "text",
    [
        "oi, tudo bem?",
        "bom dia",
        "obrigado!",
        "1",
        "meu exame está anexado",
        "vocês aceitam plano de saúde?",
        "529.982.247-25",
    ],
)
def test_non_lead_messages_still_stay_out_of_the_funnel(text):
    """Broadening intent must not swallow the model pipeline's traffic."""

    assert classify_route(event(text)) is Route.OUT_OF_SCOPE


def test_institutional_exclusion_still_beats_a_glued_intent():
    """Ungluing must never smuggle a partner contact into the funnel."""

    assert (
        classify_route(event("Somos da clinica parceira, 0quero agendar"))
        is Route.EXCLUDED
    )


# --------------------------------------------------------------------------
# One introduction per conversation
# --------------------------------------------------------------------------


def test_menu_introduces_once_and_then_stops_repeating_itself(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    first = handler.handle(event("quero agendar", message_id="m-1"))
    assert "Sou a assistente do Dr. Victor Almeida" in first

    # Same chat comes back inside the greeting window with a new intent.
    clock.value += timedelta(hours=2)
    handler.handle(event("obrigado", message_id="m-2"))
    second = handler.handle(event("quero agendar", message_id="m-3"))
    assert "Sou a assistente do Dr. Victor Almeida" not in second
    assert "Como posso ajudar com o seu agendamento?" in second
    assert "**1** - Agendar uma consulta" in second


def test_model_greeting_suppresses_the_menu_introduction(tmp_path):
    """The exact 12/ago shape: model greets first, menu greets again."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    # Message 1 was answered by the model pipeline, which introduced itself.
    handler.handle(event("bom dia", message_id="g-0"))  # out of scope, no state
    AppointmentStore(db_path)  # the flow's db exists once anything ran
    handler.note_model_greeting(event("bom dia", message_id="g-0"))

    clock.value += timedelta(minutes=1)
    menu = handler.handle(event("quero agendar", message_id="g-1"))
    assert "Sou a assistente do Dr. Victor Almeida" not in menu
    assert "**1** - Agendar uma consulta" in menu


def test_model_greeting_is_ignored_for_excluded_contacts(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True},
        db_path=db_path,
        clock=lambda: datetime(2026, 8, 12, 9, tzinfo=BRT),
    )
    AppointmentStore(db_path)

    handler.note_model_greeting(
        event("Somos do laboratorio parceiro", chat_id="lab@lid")
    )
    assert _leads(db_path) == {}


def test_model_greeting_never_creates_the_database(tmp_path):
    """A disabled/never-used funnel must not be provisioned by a greeting."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True},
        db_path=db_path,
        clock=lambda: datetime(2026, 8, 12, 9, tzinfo=BRT),
    )

    handler.note_model_greeting(event("bom dia"))
    assert not db_path.exists()


def test_introduction_returns_after_the_greeting_window_expires(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "greeting_ttl_hours": 6}, db_path=db_path, clock=clock
    )

    assert "Sou a assistente" in handler.handle(
        event("quero agendar", message_id="m-1")
    )

    clock.value += timedelta(hours=7)
    assert "Sou a assistente" in handler.handle(
        event("quero agendar", message_id="m-2")
    )


# --------------------------------------------------------------------------
# A dead end is not permanent
# --------------------------------------------------------------------------


def test_handoff_reopens_for_a_new_intent_after_the_cooldown(tmp_path):
    """The 12/ago chat was locked into the reception line for 24h."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 9, 0, tzinfo=BRT))
    # write_enabled False forces the handoff branch deterministically.
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "write_enabled": False, "handoff_reentry_minutes": 30},
        db_path=db_path,
        clock=clock,
    )

    handler.handle(event("quero agendar", message_id="m-1"))
    handler.handle(event("1", message_id="m-2"))
    dead_end = handler.handle(event("1", message_id="m-3"))
    assert "recepção" in dead_end

    # Immediately after, the funnel stays closed: this is the same failed
    # conversation, and repeating the menu would loop the patient.
    clock.value += timedelta(minutes=5)
    assert "recepção" in handler.handle(event("quero agendar", message_id="m-4"))

    # An hour later, a fresh explicit intent reopens the funnel.
    clock.value += timedelta(hours=1)
    reopened = handler.handle(event("quero agendar uma consulta", message_id="m-5"))
    assert "**1** - Agendar uma consulta" in reopened


def test_handoff_without_new_intent_is_never_reopened(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "write_enabled": False, "handoff_reentry_minutes": 30},
        db_path=db_path,
        clock=clock,
    )
    handler.handle(event("quero agendar", message_id="m-1"))
    handler.handle(event("1", message_id="m-2"))
    assert "recepção" in handler.handle(event("1", message_id="m-3"))

    clock.value += timedelta(hours=5)
    assert "recepção" in handler.handle(event("e ai?", message_id="m-4"))


# --------------------------------------------------------------------------
# Funnel bookkeeping
# --------------------------------------------------------------------------


def test_funnel_records_the_stage_path_without_pii(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 9, 0, tzinfo=BRT))
    feegow = FakeFeegow(
        slots=[{"data": "2026-08-20", "horario": "14:00", "procedimento_id": 3}],
        procedures=[{"procedimento_id": 3, "nome": "Teleconsulta", "valor": 300}],
    )
    handler = WhatsAppAppointmentsHandler(
        {
            "enabled": True,
            "write_enabled": True,
            "payment": {
                "enabled": True,
                "beneficiary": "Clínica Exemplo",
                "instructions": "PIX 000",
            },
        },
        db_path=db_path,
        feegow_client=feegow,
        clock=clock,
    )

    chat_key = "5571999999999@s.whatsapp.net"
    handler.handle(event("quero agendar", message_id="f-1"))
    assert _leads(db_path)[chat_key] == LeadStage.QUALIFICANDO.value

    handler.handle(event("1", message_id="f-2"))
    assert _leads(db_path)[chat_key] == LeadStage.ESCOLHENDO_SERVICO.value

    handler.handle(event("3", message_id="f-3"))
    assert _leads(db_path)[chat_key] == LeadStage.ESCOLHENDO_HORARIO.value

    assert _lead_path(db_path, chat_key) == [
        LeadStage.QUALIFICANDO.value,
        LeadStage.ESCOLHENDO_SERVICO.value,
        LeadStage.ESCOLHENDO_HORARIO.value,
    ]

    # The funnel is reportable but PII-free: nothing beyond the opaque chat
    # key that flow_states already stores may appear in these tables.
    connection = sqlite3.connect(db_path)
    try:
        dump = " ".join(
            str(value)
            for table in ("leads", "lead_events")
            for row in connection.execute(f"SELECT * FROM {table}")
            for value in row
        )
    finally:
        connection.close()
    assert "52998224725" not in dump
    assert "Paciente" not in dump


def test_service_label_is_carried_into_the_lead(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    feegow = FakeFeegow(
        slots=[{"data": "2026-08-20", "horario": "14:00", "procedimento_id": 1}],
        procedures=[{"procedimento_id": 1, "nome": "Consulta", "valor": 600}],
    )
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "write_enabled": True},
        db_path=db_path,
        feegow_client=feegow,
        clock=lambda: datetime(2026, 8, 12, 9, 0, tzinfo=BRT),
    )
    handler.handle(event("quero agendar", message_id="s-1"))
    handler.handle(event("1", message_id="s-2"))
    handler.handle(event("1", message_id="s-3"))

    store = AppointmentStore(db_path)
    lead = store.load_lead("5571999999999@s.whatsapp.net")
    assert lead is not None
    assert lead.service_label == "Consulta presencial"
    assert lead.touches >= 3


def test_funnel_counts_and_stale_leads_feed_the_worklist(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    now = datetime(2026, 8, 12, 9, 0, tzinfo=BRT)

    store.record_lead("a@lid", LeadStage.ESCOLHENDO_HORARIO, now=now)
    store.record_lead("b@lid", LeadStage.AGENDADO, now=now)
    store.record_lead("c@lid", LeadStage.ESCOLHENDO_HORARIO, now=now)

    assert store.funnel_counts() == {
        LeadStage.ESCOLHENDO_HORARIO.value: 2,
        LeadStage.AGENDADO.value: 1,
    }

    stale = store.stale_leads(
        now=now + timedelta(days=1),
        older_than_seconds=3600,
        stages=[LeadStage.ESCOLHENDO_HORARIO.value],
    )
    assert sorted(lead.chat_key for lead in stale) == ["a@lid", "c@lid"]

    fresh = store.stale_leads(
        now=now + timedelta(minutes=5),
        older_than_seconds=3600,
        stages=[LeadStage.ESCOLHENDO_HORARIO.value],
    )
    assert fresh == []


def test_funnel_failure_never_costs_the_patient_a_reply(tmp_path, monkeypatch):
    """The funnel is observational: a broken write must not eat the answer."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=lambda: datetime(2026, 8, 12, 9, tzinfo=BRT)
    )

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("funnel is down")

    monkeypatch.setattr(AppointmentStore, "record_lead", explode)

    reply = handler.handle(event("quero agendar", message_id="x-1"))
    assert "**1** - Agendar uma consulta" in reply
