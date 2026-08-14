"""Reception has to learn about booking attempts that gave up.

Reception was only ever told about appointments that COMPLETED. A patient
whose booking hit a dead end was told "talk to reception" while reception was
told nothing — and with the agenda read broken, that was every single attempt:
``outbox_events`` held zero rows while patients were being turned away.

This is the fail-safe for exactly the case where the rest of the flow cannot
be trusted, so it must fire on a handoff whatever the cause: no vacancy, a
gate that is off, or an outage.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.whatsapp_appointments import WhatsAppAppointmentsHandler
from tests.gateway.appointment_helpers import (
    BIRTH_DATE,
    CPF,
    CPF_FORMATTED,
    FakeFeegow,
    MutableClock,
    event,
    known_patient,
    payment_config,
)

BRT = ZoneInfo("America/Bahia")
NOW = datetime(2026, 8, 1, 10, 0, tzinfo=BRT)
# As written in config.yaml — a Bahia number spelled with the ninth
# digit, the way a human writes it on a card.
RECEPTION = "5571996691002@s.whatsapp.net"
# The shape WhatsApp actually delivers to for DDD 71. Sends to the
# spelling above are accepted and reach nobody.
RECEPTION_DELIVERED = "557196691002@s.whatsapp.net"


def _handler(tmp_path, *, reception_chat_id=RECEPTION, slots=()):
    """A handler whose agenda is empty by default — the real failure mode."""
    feegow = FakeFeegow(
        slots=list(slots),
        patients=[known_patient()],
        procedures=[
            {"procedimento_id": 1, "nome": "Consulta", "valor": 600},
            {"procedimento_id": 3, "nome": "Teleconsulta", "valor": 300},
        ],
    )
    db_path = tmp_path / "appointments.sqlite3"
    clock = MutableClock(NOW)
    handler = WhatsAppAppointmentsHandler(
        payment_config(reception_chat_id=reception_chat_id),
        db_path=db_path,
        feegow_client=feegow,
        clock=clock,
    )
    return handler, db_path, clock


def _outbox(db_path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT chat_key, body FROM outbox_events"
        ).fetchall()


def _attempt_booking(handler, prefix="ho", service="3"):
    """Walk to service selection, where an empty agenda hands off."""
    handler.handle(event("Quero agendar uma consulta", message_id=f"{prefix}-1"))
    handler.handle(event("1", message_id=f"{prefix}-2"))
    return handler.handle(event(service, message_id=f"{prefix}-3"))


class TestHandoffNotifiesReception:
    @pytest.mark.parametrize("service", ["1", "3"])
    def test_dead_end_queues_exactly_one_notice(self, tmp_path, service):
        """Teleconsultation and in-person alike."""
        handler, db_path, _ = _handler(tmp_path)
        response = _attempt_booking(handler, service=service)

        assert "recepção" in response.lower()
        rows = _outbox(db_path)
        assert len(rows) == 1
        chat_key, body = rows[0]
        assert chat_key == RECEPTION_DELIVERED
        assert "agendamento automático" in body.lower()
        # The point of the notice: a human is asked to pick up the phone.
        assert "ligar para o paciente" in body.lower()

    def test_notice_identifies_who_reception_should_call(self, tmp_path):
        """Anonymous, this notice was unactionable — see the module docstring."""
        handler, db_path, _ = _handler(tmp_path)
        _attempt_booking(handler)

        body = _outbox(db_path)[0][1]
        assert "71 99999-9999" in body
        assert "Paciente" in body

    def test_notice_carries_no_record_data(self, tmp_path):
        """Name and phone are what reception needs to call; the record is not.

        CPF, birth date, e-mail and the registered name stay in Feegow, and
        the patient's own words stay in the chat — each under its own access
        control. Only the WhatsApp identity reception is about to dial travels
        in the notice.
        """
        handler, db_path, _ = _handler(tmp_path)
        _attempt_booking(handler)

        body = _outbox(db_path)[0][1]
        for sentinel in (CPF, CPF_FORMATTED, BIRTH_DATE, "Paciente Exemplo",
                         "old@example.invalid"):
            assert sentinel not in body, f"PII leak: {sentinel!r}"

    def test_retrying_the_same_day_does_not_flood_reception(self, tmp_path):
        """Once a chat is in HANDOFF every later message re-enters _handoff
        (whatsapp_appointments.py:2848), so without per-day idempotency an
        insistent patient would queue a notice per message."""
        handler, db_path, _ = _handler(tmp_path)
        _attempt_booking(handler, prefix="a")
        _attempt_booking(handler, prefix="b")
        handler.handle(event("por favor", message_id="c-1"))
        handler.handle(event("alguem ai?", message_id="c-2"))

        assert len(_outbox(db_path)) == 1

    def test_a_new_day_is_a_new_attempt_worth_reporting(self, tmp_path):
        handler, db_path, clock = _handler(tmp_path)
        _attempt_booking(handler, prefix="day1")
        clock.value = NOW + timedelta(days=1)
        _attempt_booking(handler, prefix="day2")

        assert len(_outbox(db_path)) == 2

    def test_nothing_is_queued_when_reception_is_not_configured(self, tmp_path):
        handler, db_path, _ = _handler(tmp_path, reception_chat_id="")
        response = _attempt_booking(handler)

        assert "recepção" in response.lower()
        assert _outbox(db_path) == []

    def test_a_failing_notice_does_not_change_the_patient_reply(
        self, tmp_path, monkeypatch
    ):
        """The notice is a side effect; the patient must still be answered."""
        handler, _, _ = _handler(tmp_path)

        def _boom(*args, **kwargs):
            raise sqlite3.OperationalError("outbox unavailable")

        monkeypatch.setattr(
            "gateway.platforms.whatsapp_appointments.AppointmentStore."
            "enqueue_reception_handoff",
            _boom,
        )
        response = _attempt_booking(handler)
        assert "recepção" in response.lower()

    def test_the_failure_log_does_not_leak_the_patient_chat(self, tmp_path, monkeypatch, caplog):
        """RNF2 covers logs, not just the outbox.

        The chat key is derived from the patient's phone number, so a warning
        that interpolated it — or an exception rendering the bound SQL
        parameters — would put patient-identifying data in the gateway log.
        """
        handler, _, _ = _handler(tmp_path)
        chat = "5571988887777@s.whatsapp.net"

        def _boom(*args, **kwargs):
            raise sqlite3.OperationalError("outbox unavailable")

        monkeypatch.setattr(
            "gateway.platforms.whatsapp_appointments.AppointmentStore."
            "enqueue_reception_handoff",
            _boom,
        )
        with caplog.at_level("WARNING"):
            _attempt_booking(handler)

        logged = "\n".join(record.getMessage() for record in caplog.records)
        traces = "\n".join(
            record.exc_text or "" for record in caplog.records
        )
        assert "could not be queued" in logged
        for sentinel in ("5571988887777", chat, CPF, "71999999999"):
            assert sentinel not in logged, f"PII in log message: {sentinel!r}"
            assert sentinel not in traces, f"PII in traceback: {sentinel!r}"

    def test_a_successful_booking_does_not_queue_a_handoff_notice(self, tmp_path):
        """Only dead ends. A working booking has its own notice."""
        handler, db_path, _ = _handler(
            tmp_path, slots=[{"id": "s-1", "data": "2026-08-12", "horario": "14:00"}]
        )
        response = _attempt_booking(handler, service="1")

        assert "recepção" not in response.lower()
        assert _outbox(db_path) == []


class TestReceptionAddress:
    """The address notices are sent to, and the addresses reception writes from.

    Until 14/ago/2026 ``gateway.run`` hardcoded the delivering form while
    ``config.yaml`` carried the human spelling, so the model pipeline paged an
    address that worked and this class paged one that did not.
    """

    def test_notices_go_to_the_shape_whatsapp_delivers_to(self, tmp_path):
        handler, _, _ = _handler(tmp_path, reception_chat_id=RECEPTION)

        assert handler._reception_chat_id == RECEPTION_DELIVERED

    def test_reception_is_recognised_under_either_spelling(self, tmp_path):
        handler, _, _ = _handler(tmp_path, reception_chat_id=RECEPTION)

        assert handler._is_reception_chat(RECEPTION)
        assert handler._is_reception_chat(RECEPTION_DELIVERED)
        assert not handler._is_reception_chat("5571988326547@s.whatsapp.net")

    def test_an_unset_address_stays_unset(self, tmp_path):
        handler, _, _ = _handler(tmp_path, reception_chat_id="")

        assert handler._reception_chat_id == ""
        assert not handler._is_reception_chat(RECEPTION)
