"""Ownership matrix: deterministic flow vs legacy pipeline vs quarantine.

Every row proves one of two contracts:

* ``handle()`` returns ``None`` -> the legacy model pipeline runs exactly
  once and completely unchanged, with zero deterministic response, zero
  Feegow call, zero outbox row and zero watcher effect; or
* the contact is quarantined -> pre-existing contaminated state is
  neutralized and neither ``process_due`` nor ``claim_outbox`` can ever
  process it afterwards.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.whatsapp_appointments import (
    AppointmentStore,
    Route,
    WhatsAppAppointmentsHandler,
    classify_route,
)
from tests.gateway.appointment_helpers import (
    CHAT_KEY,
    CPF_FORMATTED,
    FakeFeegow,
    MutableClock,
    NoCallFeegow,
    event,
    payment_config,
)

BRT = ZoneInfo("America/Bahia")

# (label, event) — every row must stay out of the deterministic flow.
EXCLUDED_ROWS = [
    ("group", event("Quero marcar consulta", chat_id="1203630@g.us", chat_type="group")),
    ("broadcast", event("Quero agendar uma consulta", chat_id="list@broadcast")),
    ("status", event("Quero agendar uma consulta", chat_id="status", chat_type="status")),
    (
        "status-broadcast",
        event("CPF 52998224725", chat_id="status@broadcast", chat_type="broadcast"),
    ),
    (
        "organization-name",
        event("Quero agendar uma consulta", user_name="Empresa Saúde LTDA"),
    ),
    (
        "partner-name",
        event("Quero remarcar a consulta", user_name="Parceiro Rapidoc"),
    ),
    (
        "platform-name",
        event("Preciso desmarcar consulta", user_name="Plataforma Telemedicina"),
    ),
    (
        "vendor-name",
        event("Quero marcar consulta", user_name="Fornecedor Hospitalar"),
    ),
    (
        "institutional-text",
        event("Somos da clínica parceira e queremos agendar uma consulta"),
    ),
    (
        "lab-text",
        event("Somos do laboratorio, precisamos remarcar a consulta do convênio"),
    ),
    (
        "intent-plus-cpf",
        event(
            f"Contato institucional: quero agendar uma consulta, CPF {CPF_FORMATTED}"
        ),
    ),
    (
        "intent-plus-media",
        event(
            "Empresa parceira: quero agendar uma consulta, segue o comprovante",
            media_urls=("/nonexistent/proof.jpg",),
        ),
    ),
]

# Rows that are individual patients but carry no explicit appointment intent.
OUT_OF_SCOPE_ROWS = [
    ("cpf-only", event(CPF_FORMATTED)),
    ("cpf-digits-only", event("52998224725")),
    ("generic-greeting", event("Bom dia, tudo bem?")),
    ("generic-mention", event("Minha consulta foi ótima, obrigado!")),
    (
        "media-without-reservation",
        event("", media_urls=("/nonexistent/proof.jpg",)),
    ),
]


def _handler(tmp_path, feegow, **config_overrides):
    return WhatsAppAppointmentsHandler(
        payment_config(**config_overrides),
        db_path=tmp_path / "state" / "appointments.sqlite3",
        proofs_dir=tmp_path / "proofs",
        feegow_client=feegow,
    )


@pytest.mark.parametrize(
    "label,incoming", EXCLUDED_ROWS, ids=[row[0] for row in EXCLUDED_ROWS]
)
def test_excluded_rows_return_none_and_create_no_state(tmp_path, label, incoming):
    feegow = NoCallFeegow()
    handler = _handler(tmp_path, feegow)

    assert classify_route(incoming) is Route.EXCLUDED
    assert handler.handle(incoming) is None
    # Re-delivery of the same excluded event stays ``None`` as well, so the
    # legacy pipeline sees it exactly once per delivery and never a
    # deterministic reply.
    assert handler.handle(incoming) is None

    assert feegow.calls == []
    assert not (tmp_path / "state" / "appointments.sqlite3").exists()
    assert not (tmp_path / "proofs").exists()


@pytest.mark.parametrize(
    "label,incoming", OUT_OF_SCOPE_ROWS, ids=[row[0] for row in OUT_OF_SCOPE_ROWS]
)
def test_out_of_scope_rows_return_none_and_create_no_state(tmp_path, label, incoming):
    feegow = NoCallFeegow()
    handler = _handler(tmp_path, feegow)

    assert classify_route(incoming) is Route.OUT_OF_SCOPE
    assert handler.handle(incoming) is None

    assert feegow.calls == []
    assert not (tmp_path / "state" / "appointments.sqlite3").exists()


def test_out_of_scope_row_with_existing_database_still_returns_none(tmp_path):
    """An unrelated contact's database must not pull a stranger into the flow."""

    feegow = NoCallFeegow()
    handler = _handler(tmp_path, feegow)
    db_path = tmp_path / "state" / "appointments.sqlite3"
    AppointmentStore(db_path)  # another chat already provisioned the database

    for _label, incoming in OUT_OF_SCOPE_ROWS:
        assert handler.handle(incoming) is None

    store = AppointmentStore(db_path)
    assert store.count("flow_states") == 0
    assert store.count("inbox_events") == 0
    assert store.count("outbox_events") == 0
    assert feegow.calls == []


def test_explicit_patient_intent_is_the_only_row_that_takes_ownership(tmp_path):
    feegow = NoCallFeegow()
    handler = _handler(tmp_path, feegow)
    incoming = event("Quero agendar uma consulta", message_id="own-1")

    assert classify_route(incoming) is Route.APPOINTMENT
    response = handler.handle(incoming)

    assert response is not None
    assert "agendamento" in response.lower()
    # Still zero remote calls: taking ownership only opens the menu.
    assert feegow.calls == []


def _contaminate(handler, db_path, chat_key):
    """Give an institutional chat a full set of pre-existing local state."""

    now = datetime(2026, 8, 3, 9, 0, tzinfo=BRT)
    store = AppointmentStore(db_path)
    store.record_response(
        "inst-old-1", chat_key, "resposta antiga", "AWAITING_SLOT", {"procedure_id": 1},
        now=now,
    )
    store.create_authorization(
        chat_key,
        kind="CREATE_IN_PERSON_APPOINTMENT",
        target_id="slot-1",
        payload_hash="hash-1",
        expires_at=now + timedelta(minutes=15),
        now=now,
    )
    store.create_reservation(
        4242,
        chat_key,
        deadline_at=now - timedelta(hours=1),
        reminder_at=now - timedelta(hours=2),
    )
    # A second reservation with no proof at all: this one is genuinely due
    # for expiry cancellation until quarantine neutralizes it.
    store.create_reservation(
        4243,
        chat_key,
        deadline_at=now - timedelta(hours=3),
        reminder_at=now - timedelta(hours=4),
    )
    store.create_return_ledger_entry(
        7777, chat_key, base_date=now - timedelta(days=1), now=now
    )
    proof_file = db_path.parent / "leaked-proof.bin"
    proof_file.write_bytes(b"conteudo do comprovante")
    store.accept_payment_proof(
        message_id="inst-proof-1",
        appointment_id=4242,
        chat_key=chat_key,
        received_at=now - timedelta(hours=2),
        sha256="a" * 64,
        private_path=str(proof_file),
    )
    store.enqueue_reception_receipt(4242, chat_key, now=now)
    assert store.count("outbox_events") == 1
    return store, proof_file


def test_institutional_contact_with_contaminated_state_is_quarantined(tmp_path):
    chat_key = "5571988887777@s.whatsapp.net"
    db_path = tmp_path / "state" / "appointments.sqlite3"
    feegow = NoCallFeegow()
    handler = WhatsAppAppointmentsHandler(
        payment_config(reception_chat_id=chat_key),
        db_path=db_path,
        proofs_dir=tmp_path / "proofs",
        feegow_client=feegow,
        clock=MutableClock(datetime(2026, 8, 3, 12, 0, tzinfo=BRT)),
    )
    store, proof_file = _contaminate(handler, db_path, chat_key)

    incoming = event(
        f"Somos da clínica parceira, queremos agendar uma consulta. CPF {CPF_FORMATTED}",
        message_id="inst-now-1",
        chat_id=chat_key,
        user_name="Empresa Parceira",
        media_urls=("/nonexistent/proof.jpg",),
    )
    assert handler.handle(incoming) is None

    assert handler.is_contact_quarantined(chat_key) is True
    assert store.count("flow_states") == 0
    assert store.count("inbox_events") == 0
    assert store.count("outbox_events") == 0
    assert store.count("payment_proofs") == 0
    assert not proof_file.exists()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM reservations WHERE chat_key = ?", (chat_key,)
        ).fetchone()[0] == "QUARANTINED"
        assert connection.execute(
            "SELECT state FROM returns_ledger WHERE chat_key = ?", (chat_key,)
        ).fetchone()[0] == "QUARANTINED"
        assert connection.execute(
            "SELECT consumed_at FROM authorizations WHERE chat_key = ?", (chat_key,)
        ).fetchone()[0] is not None
    assert feegow.calls == []


def test_quarantined_contact_cannot_be_processed_by_watcher_or_outbox(tmp_path):
    chat_key = "5571988887777@s.whatsapp.net"
    db_path = tmp_path / "state" / "appointments.sqlite3"
    feegow = NoCallFeegow()
    handler = WhatsAppAppointmentsHandler(
        payment_config(reception_chat_id=chat_key),
        db_path=db_path,
        proofs_dir=tmp_path / "proofs",
        feegow_client=feegow,
        clock=MutableClock(datetime(2026, 8, 3, 12, 0, tzinfo=BRT)),
    )
    store, _proof_file = _contaminate(handler, db_path, chat_key)
    # Control: the reservation is genuinely past its deadline, so without
    # quarantine this row would be picked up for expiry cancellation.
    assert [row["appointment_id"] for row in store.due_reservations()] == ["4243"]

    handler.handle(
        event(
            "Contato institucional: precisamos remarcar a consulta",
            message_id="inst-quarantine-1",
            chat_id=chat_key,
            user_name="Fornecedor Hospitalar",
        )
    )

    assert store.due_reservations() == []
    assert handler.process_due(worker_id="w-1") == []
    assert handler.claim_outbox(worker_id="w-1") == []
    assert feegow.calls == []


def test_quarantined_chat_is_skipped_by_outbox_drain(tmp_path):
    """A row enqueued before quarantine is never delivered afterwards."""

    chat_key = "5571988887777@s.whatsapp.net"
    db_path = tmp_path / "state" / "appointments.sqlite3"
    now = datetime(2026, 8, 3, 12, 0, tzinfo=BRT)
    handler = WhatsAppAppointmentsHandler(
        payment_config(),
        db_path=db_path,
        proofs_dir=tmp_path / "proofs",
        feegow_client=NoCallFeegow(),
        clock=MutableClock(now),
    )
    store = AppointmentStore(db_path)
    store.enqueue_reception_receipt(4242, chat_key, now=now)
    claimed = handler.claim_outbox(worker_id="w-1")
    assert len(claimed) == 1

    store.quarantine_contact(chat_key, now=now)

    sent = []

    class RecordingAdapter:
        async def send(self, target, body):
            sent.append((target, body))
            return SimpleNamespace(success=True)

    from gateway.platforms.whatsapp_appointments import drain_appointment_outbox

    # Re-claim after the lease expires; the drain must skip the quarantined chat.
    handler_late = WhatsAppAppointmentsHandler(
        payment_config(),
        db_path=db_path,
        proofs_dir=tmp_path / "proofs",
        feegow_client=NoCallFeegow(),
        clock=MutableClock(now + timedelta(hours=1)),
    )
    delivered = asyncio.run(
        drain_appointment_outbox(handler_late, RecordingAdapter(), worker_id="w-2")
    )

    assert delivered == 0
    assert sent == []


def test_active_patient_flow_is_not_disturbed_by_another_chats_quarantine(tmp_path):
    """Quarantine is scoped to one chat_key and never leaks across contacts."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    feegow = FakeFeegow()
    handler = _handler(tmp_path, feegow)
    handler._db_path = db_path

    patient = handler.handle(event("Quero agendar uma consulta", message_id="mix-1"))
    assert patient is not None

    assert (
        handler.handle(
            event(
                "Somos da clínica parceira",
                message_id="mix-2",
                chat_id="5571900000000@s.whatsapp.net",
                user_name="Clinica Parceira",
            )
        )
        is None
    )

    assert handler.is_contact_quarantined("5571900000000@s.whatsapp.net") is True
    assert handler.is_contact_quarantined(CHAT_KEY) is False
    store = AppointmentStore(db_path)
    assert store.count("flow_states") == 1
