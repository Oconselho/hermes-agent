"""Reception has to learn about bookings it will have to confirm.

In-person appointments carry no payment hold — the patient pays on site — so
nothing later in the flow ever mentions them again. Reception would only find
out by watching Feegow. This queues a notice on the same outbox the
payment-proof notice already uses, under the same no-PII contract.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.whatsapp_appointments import (
    AppointmentStore,
    WhatsAppAppointmentsHandler,
)
from tests.gateway.appointment_helpers import (
    BIRTH_DATE,
    CPF,
    CPF_FORMATTED,
    FakeFeegow,
    MutableClock,
    event,
    existing_patient_flow,
    known_patient,
    payment_config,
)

BRT = ZoneInfo("America/Bahia")
NOW = datetime(2026, 8, 1, 10, 0, tzinfo=BRT)
RECEPTION = "5571996691002@s.whatsapp.net"


def _handler(tmp_path, *, reception_chat_id="", service="1"):
    feegow = FakeFeegow(
        slots=[{"id": "slot-1", "data": "2026-08-12", "horario": "14:00"}],
        patients=[known_patient()],
        procedures=[
            {"procedimento_id": 1, "nome": "Consulta", "valor": 600},
            {"procedimento_id": 3, "nome": "Teleconsulta", "valor": 300},
        ],
    )
    db_path = tmp_path / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        payment_config(reception_chat_id=reception_chat_id),
        db_path=db_path,
        feegow_client=feegow,
        clock=MutableClock(NOW),
    )
    return handler, db_path


def _outbox(db_path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT chat_key, body FROM outbox_events"
        ).fetchall()


def _book_in_person(handler, prefix="bk"):
    existing_patient_flow(handler, prefix, service="1")
    return handler.handle(event("CONFIRMAR", message_id=f"{prefix}-8"))


class TestInPersonBookingNotifiesReception:
    def test_notice_is_queued_for_reception(self, tmp_path):
        handler, db_path = _handler(tmp_path, reception_chat_id=RECEPTION)
        response = _book_in_person(handler)
        assert "criado" in response.lower()

        rows = _outbox(db_path)
        assert len(rows) == 1
        chat_key, body = rows[0]
        assert chat_key == RECEPTION
        assert "agendamento" in body.lower()
        assert "confirmar" in body.lower()

    def test_notice_carries_no_patient_data(self, tmp_path):
        """Same contract as the payment-proof notice: id only."""
        handler, db_path = _handler(tmp_path, reception_chat_id=RECEPTION)
        _book_in_person(handler)

        body = _outbox(db_path)[0][1]
        for sentinel in (CPF, CPF_FORMATTED, BIRTH_DATE, "Paciente Exemplo",
                         "71999999999", "old@example.invalid"):
            assert sentinel not in body, f"PII leak: {sentinel!r}"

    def test_nothing_is_queued_when_reception_is_not_configured(self, tmp_path):
        """Unset reception_chat_id must stay silent, not crash the booking."""
        handler, db_path = _handler(tmp_path, reception_chat_id="")
        response = _book_in_person(handler)

        assert "criado" in response.lower()
        assert _outbox(db_path) == []

    def test_patient_still_gets_the_confirmation(self, tmp_path):
        """The notice is a side effect; it must not alter the patient reply."""
        handler, _ = _handler(tmp_path, reception_chat_id=RECEPTION)
        response = _book_in_person(handler)
        assert "A confirmação final será feita pela recepção." in response

    def test_a_failing_notice_does_not_lose_the_booking(self, tmp_path, monkeypatch):
        handler, db_path = _handler(tmp_path, reception_chat_id=RECEPTION)

        def _boom(*args, **kwargs):
            raise sqlite3.OperationalError("outbox unavailable")

        monkeypatch.setattr(
            AppointmentStore, "enqueue_reception_booking", _boom
        )
        response = _book_in_person(handler)
        # The appointment was created remotely; the patient must be told.
        assert "criado" in response.lower()


class TestNoticeIsIdempotent:
    def test_same_appointment_queues_once(self, tmp_path):
        store = AppointmentStore(tmp_path / "a.sqlite3")
        store.enqueue_reception_booking(4242, RECEPTION, now=NOW)
        store.enqueue_reception_booking(4242, RECEPTION, now=NOW)

        with sqlite3.connect(tmp_path / "a.sqlite3") as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM outbox_events"
            ).fetchone()[0]
        assert count == 1

    def test_booking_and_receipt_notices_do_not_collide(self, tmp_path):
        """Different events for one appointment must both survive."""
        store = AppointmentStore(tmp_path / "a.sqlite3")
        store.enqueue_reception_booking(4242, RECEPTION, now=NOW)
        store.enqueue_reception_receipt(4242, RECEPTION, now=NOW)

        with sqlite3.connect(tmp_path / "a.sqlite3") as connection:
            bodies = [
                row[0] for row in connection.execute(
                    "SELECT body FROM outbox_events"
                )
            ]
        assert len(bodies) == 2
        assert any("Novo agendamento" in b for b in bodies)
        assert any("Comprovante" in b for b in bodies)
