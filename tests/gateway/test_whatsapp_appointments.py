"""Deterministic WhatsApp appointment routing and durable-state tests."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.whatsapp_appointments import (
    AppointmentStore,
    Route,
    WhatsAppAppointmentsHandler,
    classify_route,
    drain_appointment_outbox,
    run_appointment_watcher,
)


from tests.gateway.appointment_helpers import (
    FakeFeegow,
    MutableClock,
    NoCallFeegow,
    authenticate_for_action,
    event,
    payment_config,
)


def tele_feegow(slot):
    return FakeFeegow(
        slots=[slot],
        patients=[
            {
                "paciente_id": 77,
                "cpf": "52998224725",
                "data_nascimento": "01/02/1990",
                "celular": "71999999999",
            }
        ],
        procedures=[
            {"procedimento_id": 3, "nome": "Teleconsulta", "valor": 300}
        ],
    )


def advance_teleconsultation(handler, prefix="tele"):
    assert "Agendar" in handler.handle(
        event("Quero agendar uma consulta", message_id=f"{prefix}-1")
    )
    service_menu = handler.handle(event("1", message_id=f"{prefix}-2"))
    assert "Teleconsulta" in service_menu
    assert "R$" not in service_menu
    assert "dispon" in handler.handle(event("3", message_id=f"{prefix}-3")).lower()
    assert "CPF" in handler.handle(event("1", message_id=f"{prefix}-4"))
    assert "nascimento" in handler.handle(
        event("529.982.247-25", message_id=f"{prefix}-5")
    ).lower()
    assert "telefone" in handler.handle(
        event("01/02/1990", message_id=f"{prefix}-6")
    ).lower()
    return handler.handle(event("SIM", message_id=f"{prefix}-7"))




@pytest.mark.parametrize(
    "incoming",
    [
        event("Somos da Clínica Parceira e queremos agendar. CPF 529.982.247-25"),
        event("Fornecedor da plataforma Rapidoc: agenda urgente", user_name="Rapidoc suporte"),
        event("Quero marcar consulta", chat_id="120363000000@g.us", chat_type="group"),
        event("CPF 52998224725", chat_id="status@broadcast"),
        event("Veja o comprovante", chat_id="newsletter@broadcast", media_urls=("/tmp/prova.jpg",)),
    ],
)
def test_excluded_sender_has_absolute_priority_and_zero_side_effects(tmp_path, incoming):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    feegow = NoCallFeegow()
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, feegow_client=feegow
    )

    assert handler.handle(incoming) is None
    assert not db_path.exists()
    assert feegow.calls == []


def test_route_priority_is_excluded_before_cpf_media_or_appointment_intent():
    incoming = event(
        "Laboratório parceiro: preciso consultar agenda; CPF 52998224725",
        media_urls=("/tmp/doc.pdf",),
    )
    assert classify_route(incoming) is Route.EXCLUDED


@pytest.mark.parametrize(
    "text",
    [
        "529.982.247-25",
        "segue meu CPF 52998224725",
        "enviei um documento",
        "meu exame está anexado",
        "oi, tudo bem?",
        "vocês aceitam plano de saúde?",
    ],
)
def test_isolated_cpf_media_and_generic_messages_do_not_enter_flow(tmp_path, text):
    db_path = tmp_path / "appointments.sqlite3"
    incoming = event(text, media_urls=("/tmp/a.jpg",) if "documento" in text else ())

    assert classify_route(incoming) is Route.OUT_OF_SCOPE
    assert WhatsAppAppointmentsHandler({"enabled": True}, db_path=db_path).handle(incoming) is None
    assert not db_path.exists()


def test_explicit_appointment_intent_creates_only_idempotent_minimal_state(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler({"enabled": True}, db_path=db_path)
    incoming = event("Quero agendar uma consulta", message_id="wamid-123")

    first = handler.handle(incoming)
    second = handler.handle(incoming)

    assert "1" in first and "2" in first and "3" in first
    assert second == first
    assert db_path.exists()
    assert db_path.stat().st_mode & 0o777 == 0o600

    store = AppointmentStore(db_path)
    assert store.count("inbox_events") == 1
    assert store.count("flow_states") == 1
    assert store.count("operations") == 0
    assert store.count("outbox_events") == 0
    assert store.count("payment_proofs") == 0


def test_sqlite_schema_has_required_tables_and_wal(tmp_path):
    db_path = tmp_path / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    required = {
        "inbox_events",
        "contacts",
        "flow_states",
        "reservations",
        "operations",
        "payment_proofs",
        "reminders",
        "outbox_events",
        "audit_log",
        "watcher_leases",
        "authorizations",
    }

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert required <= tables
    assert journal.lower() == "wal"
    assert store.count("audit_log") == 0


def test_store_rejects_unknown_table_names(tmp_path):
    store = AppointmentStore(tmp_path / "appointments.sqlite3")
    with pytest.raises(ValueError):
        store.count("sqlite_master; DROP TABLE flow_states")


def test_existing_patient_completes_proc1_with_preflight_status1_and_readback(tmp_path):
    slot = {"id": "slot-wed-14", "data": "2026-08-05", "horario": "14:00"}
    feegow = FakeFeegow(
        slots=[slot],
        patients=[
            {
                "paciente_id": 77,
                "cpf": "52998224725",
                "data_nascimento": "01/02/1990",
                "celular": "71999999999",
            }
        ],
        procedures=[{"procedimento_id": 1, "nome": "Consulta", "valor": 600}],
    )
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "write_enabled": True},
        db_path=tmp_path / "appointments.sqlite3",
        feegow_client=feegow,
        clock=lambda: datetime(2026, 8, 1, 10, tzinfo=ZoneInfo("America/Bahia")),
    )

    greeting = handler.handle(event("Quero agendar uma consulta", message_id="p1-1"))
    assert "1 - Agendar" in greeting
    assert "assistente do Dr. Victor Almeida" in greeting
    assert "assistente virtual" not in greeting.lower()
    service_menu = handler.handle(event("1", message_id="p1-2"))
    assert "R$" not in service_menu
    assert "05/08/2026" in handler.handle(event("1", message_id="p1-3"))
    assert "CPF" in handler.handle(event("1", message_id="p1-4"))
    assert "nascimento" in handler.handle(event("529.982.247-25", message_id="p1-5")).lower()
    assert "telefone" in handler.handle(event("01/02/1990", message_id="p1-6")).lower()
    summary = handler.handle(event("SIM", message_id="p1-7"))
    assert "R$ 600" in summary and "CONFIRMAR" in summary

    result = handler.handle(event("CONFIRMAR", message_id="p1-8"))

    assert "status 1" in result.lower()
    assert len(feegow.created_patients) == 0
    assert len(feegow.created_appointments) == 1
    assert feegow.created_appointments[0]["procedimento_id"] == 1
    assert feegow.created_appointments[0]["status_id"] == 1
    assert any(name == "get_appointment" for name, _ in feegow.calls)


def test_action_2_authenticates_and_lists_only_three_unambiguous_future_appointments(
    tmp_path,
):
    appointments = [
        {
            "agendamento_id": 701 + index,
            "paciente_id": 77,
            "procedimento_id": 1,
            "data": appointment_date,
            "horario": "14:00",
            "status_id": 1,
        }
        for index, appointment_date in enumerate(
            ("2026-08-05", "2026-08-06", "2026-08-12", "2026-08-13")
        )
    ]
    feegow = FakeFeegow(
        patients=[
            {
                "paciente_id": 77,
                "cpf": "52998224725",
                "data_nascimento": "01/02/1990",
                "celular": "71999999999",
            }
        ],
        appointments=appointments,
    )
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "write_enabled": True},
        db_path=tmp_path / "appointments.sqlite3",
        feegow_client=feegow,
        clock=lambda: datetime(2026, 8, 1, 10, tzinfo=ZoneInfo("America/Bahia")),
    )

    handler.handle(event("Quero consultar meu agendamento", message_id="list-1"))
    assert "CPF" in handler.handle(event("2", message_id="list-2"))
    assert "nascimento" in handler.handle(
        event("529.982.247-25", message_id="list-3")
    ).lower()
    assert "telefone" in handler.handle(event("01/02/1990", message_id="list-4")).lower()

    result = handler.handle(event("SIM", message_id="list-5"))

    assert "05/08/2026" in result
    assert "06/08/2026" in result
    assert "12/08/2026" in result
    assert "13/08/2026" not in result
    assert result.count("Agendamento ") == 3
    assert [name for name, _ in feegow.calls].count("search_appointments") == 1


def test_action_3_cancels_once_with_reason_1_and_requires_status_11_readback(tmp_path):
    appointment = {
        "agendamento_id": 701,
        "paciente_id": 77,
        "procedimento_id": 1,
        "data": "2026-08-05",
        "horario": "14:00",
        "status_id": 7,
    }
    feegow = FakeFeegow(
        patients=[
            {
                "paciente_id": 77,
                "cpf": "52998224725",
                "data_nascimento": "01/02/1990",
                "celular": "71999999999",
            }
        ],
        appointments=[appointment],
    )
    handler = WhatsAppAppointmentsHandler(
        {
            "enabled": True,
            "write_enabled": True,
            "cancel_reason_id": 1,
        },
        db_path=tmp_path / "appointments.sqlite3",
        feegow_client=feegow,
        clock=lambda: datetime(2026, 8, 1, 10, tzinfo=ZoneInfo("America/Bahia")),
    )
    authenticate_for_action(handler, "3", "cancel")

    summary = handler.handle(event("1", message_id="cancel-6"))

    assert "Resumo" in summary
    assert "05/08/2026" in summary
    assert "CONFIRMAR" in summary
    result = handler.handle(event("CONFIRMAR", message_id="cancel-7"))
    replay = handler.handle(event("CONFIRMAR", message_id="cancel-8"))

    assert "status 11" in result.lower()
    assert "status 11" in replay.lower()
    assert feegow.cancelled_appointments == [(701, 1)]
    call_names = [name for name, _ in feegow.calls]
    assert call_names.count("cancel_appointment") == 1
    assert call_names.index("cancel_appointment") < call_names.index("get_appointment")


def test_action_2_reschedules_same_procedure_after_fresh_slot_and_duplicate_checks(
    tmp_path,
):
    appointment = {
        "agendamento_id": 801,
        "paciente_id": 77,
        "procedimento_id": 9,
        "data": "2026-08-05",
        "horario": "14:00",
        "status_id": 7,
    }
    feegow = FakeFeegow(
        slots=[
            {
                "id": "proc-9-new",
                "procedimento_id": 9,
                "data": "2026-08-06",
                "horario": "15:00",
            }
        ],
        patients=[
            {
                "paciente_id": 77,
                "cpf": "52998224725",
                "data_nascimento": "01/02/1990",
                "celular": "71999999999",
            }
        ],
        appointments=[appointment],
    )
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "write_enabled": True},
        db_path=tmp_path / "appointments.sqlite3",
        feegow_client=feegow,
        clock=lambda: datetime(2026, 8, 1, 10, tzinfo=ZoneInfo("America/Bahia")),
    )
    authenticate_for_action(handler, "2", "move")

    slots = handler.handle(event("REMARCAR 1", message_id="move-6"))
    assert "06/08/2026" in slots
    summary = handler.handle(event("1", message_id="move-7"))
    assert "Resumo" in summary and "CONFIRMAR" in summary
    result = handler.handle(event("CONFIRMAR", message_id="move-8"))
    replay = handler.handle(event("CONFIRMAR", message_id="move-9"))

    assert "status 15" in result.lower()
    assert "status 15" in replay.lower()
    assert feegow.rescheduled_appointments == [
        {"appointment_id": 801, "data": "2026-08-06", "horario": "15:00"}
    ]
    slot_calls = [
        payload for name, payload in feegow.calls if name == "list_available_slots"
    ]
    assert len(slot_calls) == 2
    assert all(call["procedure_id"] == 9 for call in slot_calls)
    call_names = [name for name, _ in feegow.calls]
    assert call_names.index("find_duplicate_appointments") < call_names.index(
        "reschedule_appointment"
    )
    assert call_names.index("reschedule_appointment") < call_names.index(
        "get_appointment"
    )


@pytest.mark.parametrize(
    "payment",
    [
        {},
        {"enabled": False, "beneficiary": "Clínica", "instructions": "PIX"},
        {"enabled": True, "beneficiary": "   ", "instructions": "PIX"},
        {"enabled": True, "beneficiary": "Clínica", "instructions": ""},
    ],
)
def test_partial_payment_configuration_hands_off_before_any_tele_side_effect(
    tmp_path, payment
):
    db_path = tmp_path / "appointments.sqlite3"
    feegow = NoCallFeegow()
    handler = WhatsAppAppointmentsHandler(
        payment_config(payment=payment),
        db_path=db_path,
        feegow_client=feegow,
    )

    handler.handle(event("Quero agendar uma consulta", message_id="cfg-1"))
    assert "Teleconsulta" in handler.handle(event("1", message_id="cfg-2"))
    response = handler.handle(event("3", message_id="cfg-3"))

    assert "recepção" in response.lower()
    assert feegow.calls == []
    store = AppointmentStore(db_path)
    for table in (
        "reservations",
        "operations",
        "payment_proofs",
        "reminders",
        "outbox_events",
        "watcher_leases",
    ):
        assert store.count(table) == 0


def test_partial_payment_configuration_disables_watcher_without_creating_state(tmp_path):
    db_path = tmp_path / "appointments.sqlite3"
    feegow = NoCallFeegow()
    handler = WhatsAppAppointmentsHandler(
        payment_config(payment={"enabled": True, "beneficiary": "Clínica"}),
        db_path=db_path,
        feegow_client=feegow,
    )

    assert handler.process_due(worker_id="partial") == []
    assert not db_path.exists()
    assert feegow.calls == []


@pytest.mark.parametrize(
    ("slot_at", "expected_hours"),
    [
        (datetime(2026, 8, 4, 14, tzinfo=ZoneInfo("America/Bahia")), 12),
        (datetime(2026, 8, 2, 14, tzinfo=ZoneInfo("America/Bahia")), 4),
        (datetime(2026, 8, 1, 14, tzinfo=ZoneInfo("America/Bahia")), 1),
    ],
)
def test_teleconsultation_creates_proc3_status1_with_absolute_payment_deadline(
    tmp_path, slot_at, expected_hours
):
    now = datetime(2026, 8, 1, 10, tzinfo=ZoneInfo("America/Bahia"))
    slot = {
        "id": f"tele-{expected_hours}",
        "procedimento_id": 3,
        "data": slot_at.date().isoformat(),
        "horario": slot_at.strftime("%H:%M"),
    }
    feegow = tele_feegow(slot)
    db_path = tmp_path / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        payment_config(), db_path=db_path, feegow_client=feegow, clock=lambda: now
    )

    summary = advance_teleconsultation(handler, prefix=f"deadline-{expected_hours}")
    assert "Clínica Exemplo" in summary
    assert "PIX oficial" in summary
    assert "expir" in summary.lower()
    assert "cancel" in summary.lower()
    result = handler.handle(
        event("CONFIRMAR", message_id=f"deadline-{expected_hours}-8")
    )

    assert "status 1" in result.lower()
    assert "comprovante" in result.lower()
    assert len(feegow.created_appointments) == 1
    created = feegow.created_appointments[0]
    assert created["procedimento_id"] == 3
    assert created["valor"] == 300
    assert created["status_id"] == 1
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT state, deadline_at, reminder_at FROM reservations"
        ).fetchone()
    assert row[0] == "AGUARDANDO_COMPROVANTE"
    deadline = datetime.fromisoformat(row[1])
    reminder = datetime.fromisoformat(row[2])
    assert deadline == now + timedelta(hours=expected_hours)
    expected_notice = {12: 2, 4: 1, 1: 0.25}[expected_hours]
    assert reminder == deadline - timedelta(hours=expected_notice)
    # The post-create response must restate the absolute BRT deadline plus
    # the official beneficiary/instructions, not just "within the deadline".
    assert deadline.strftime("%d/%m/%Y %H:%M") in result
    assert "horário de Brasília" in result
    assert "Clínica Exemplo" in result
    assert "PIX oficial" in result


def test_teleconsultation_at_three_hours_hands_off_without_reservation_or_mutation(
    tmp_path,
):
    now = datetime(2026, 8, 1, 10, tzinfo=ZoneInfo("America/Bahia"))
    slot = {
        "id": "tele-too-soon",
        "procedimento_id": 3,
        "data": "2026-08-01",
        "horario": "13:00",
    }
    feegow = tele_feegow(slot)
    db_path = tmp_path / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        payment_config(), db_path=db_path, feegow_client=feegow, clock=lambda: now
    )

    handler.handle(event("Quero agendar uma consulta", message_id="soon-1"))
    handler.handle(event("1", message_id="soon-2"))
    handler.handle(event("3", message_id="soon-3"))
    response = handler.handle(event("1", message_id="soon-4"))

    assert "recepção" in response.lower()
    assert feegow.created_appointments == []
    store = AppointmentStore(db_path)
    assert store.count("reservations") == 0
    assert store.count("operations") == 0


def _create_tele_reservation(tmp_path, *, prefix="proof"):
    now = datetime(2026, 8, 1, 10, tzinfo=ZoneInfo("America/Bahia"))
    clock = MutableClock(now)
    slot = {
        "id": f"{prefix}-slot",
        "procedimento_id": 3,
        "data": "2026-08-01",
        "horario": "14:00",
    }
    feegow = tele_feegow(slot)
    db_path = tmp_path / "appointments.sqlite3"
    proofs_dir = tmp_path / "private-proofs"
    handler = WhatsAppAppointmentsHandler(
        payment_config(),
        db_path=db_path,
        proofs_dir=proofs_dir,
        feegow_client=feegow,
        clock=clock,
    )
    advance_teleconsultation(handler, prefix=prefix)
    handler.handle(event("CONFIRMAR", message_id=f"{prefix}-8"))
    with sqlite3.connect(db_path) as connection:
        appointment_id, deadline_text = connection.execute(
            "SELECT appointment_id, deadline_at FROM reservations"
        ).fetchone()
    return (
        handler,
        feegow,
        db_path,
        proofs_dir,
        clock,
        int(appointment_id),
        datetime.fromisoformat(deadline_text),
    )


def test_payment_proof_at_last_second_is_private_sha_idempotent_and_stops_expiry(
    tmp_path,
):
    handler, feegow, db_path, proofs_dir, clock, _, deadline = _create_tele_reservation(
        tmp_path
    )
    source = tmp_path / "upload.jpg"
    source.write_bytes(b"offline-proof-bytes")

    response = handler.handle(
        event(
            "Segue o comprovante",
            message_id="proof-media-1",
            media_urls=[str(source)],
            timestamp=deadline,
        )
    )
    replay_by_hash = handler.handle(
        event(
            "Reenvio",
            message_id="proof-media-2",
            media_urls=[str(source)],
            timestamp=deadline,
        )
    )

    assert "recebido" in response.lower()
    assert "recebido" in replay_by_hash.lower()
    assert proofs_dir.stat().st_mode & 0o777 == 0o700
    stored_files = list(proofs_dir.iterdir())
    assert len(stored_files) == 1
    assert stored_files[0].stat().st_mode & 0o777 == 0o600
    assert stored_files[0].read_bytes() == b"offline-proof-bytes"
    with sqlite3.connect(db_path) as connection:
        reservation = connection.execute(
            "SELECT state, proof_received_at FROM reservations"
        ).fetchone()
        proof_count = connection.execute("SELECT COUNT(*) FROM payment_proofs").fetchone()[0]
    assert reservation == ("COMPROVANTE_RECEBIDO", deadline.isoformat())
    assert proof_count == 1

    clock.value = deadline + timedelta(seconds=61)
    handler.process_due(worker_id="after-proof")
    assert feegow.cancelled_appointments == []


def test_late_payment_proof_is_not_copied_or_persisted(tmp_path):
    handler, _, db_path, proofs_dir, _, _, deadline = _create_tele_reservation(
        tmp_path, prefix="late"
    )
    source = tmp_path / "late.pdf"
    source.write_bytes(b"late-pdf")

    response = handler.handle(
        event(
            "Comprovante",
            message_id="late-media",
            media_urls=[str(source)],
            media_types=["application/pdf"],
            timestamp=deadline + timedelta(microseconds=1),
        )
    )

    assert "recepção" in response.lower()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payment_proofs").fetchone()[0] == 0
        assert connection.execute(
            "SELECT proof_received_at FROM reservations"
        ).fetchone()[0] is None
    assert not proofs_dir.exists() or list(proofs_dir.iterdir()) == []


def test_process_due_uses_lease_unique_reminder_and_cancel_readback_before_outbox(
    tmp_path,
):
    handler, feegow, db_path, _, clock, appointment_id, deadline = _create_tele_reservation(
        tmp_path, prefix="watch"
    )

    clock.value = deadline - timedelta(minutes=15)
    first = handler.process_due(worker_id="worker-a")
    second = handler.process_due(worker_id="worker-b")
    assert len(first) == 1
    assert second == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 1

    clock.value = deadline + timedelta(seconds=61)
    cancelled = handler.process_due(worker_id="worker-a")
    replay = handler.process_due(worker_id="worker-b")

    assert len(cancelled) == 1
    assert replay == []
    assert feegow.cancelled_appointments == [(appointment_id, 1)]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM reservations").fetchone()[0] == "RESERVA_CANCELADA"
        outbox = connection.execute(
            "SELECT body FROM outbox_events ORDER BY created_at, id"
        ).fetchall()
    assert len(outbox) == 2
    # Wording is restricted to "no longer reserved" — never a promise about
    # a future slot being available again.
    assert "não está mais reservada" in outbox[-1][0]
    assert "dispon" not in outbox[-1][0].lower()
    call_names = [name for name, _ in feegow.calls]
    cancel_index = max(i for i, name in enumerate(call_names) if name == "cancel_appointment")
    readback_index = max(i for i, name in enumerate(call_names) if name == "get_appointment")
    assert cancel_index < readback_index


def test_process_due_only_observes_status7_and_never_cancels_or_writes_it(tmp_path):
    handler, feegow, db_path, _, clock, appointment_id, deadline = _create_tele_reservation(
        tmp_path, prefix="status7"
    )
    feegow.readbacks[appointment_id]["status_id"] = 7
    clock.value = deadline + timedelta(seconds=61)

    assert handler.process_due(worker_id="observer") == []

    assert feegow.cancelled_appointments == []
    assert all(name != "update_appointment_status" for name, _ in feegow.calls)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM reservations").fetchone()[0] == "CONFIRMADO_STATUS_7"
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 0


def test_expiration_timeout_is_reconciled_by_readback_without_second_cancel(
    tmp_path, monkeypatch
):
    handler, feegow, db_path, _, clock, appointment_id, deadline = _create_tele_reservation(
        tmp_path, prefix="expire-reconcile"
    )
    original_cancel = feegow.cancel_appointment

    def ambiguous_cancel(*args, **kwargs):
        original_cancel(*args, **kwargs)
        raise TimeoutError("ambiguous timeout after remote commit")

    monkeypatch.setattr(feegow, "cancel_appointment", ambiguous_cancel)
    clock.value = deadline + timedelta(seconds=61)

    assert handler.process_due(worker_id="worker-first") == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM operations WHERE kind = 'EXPIRE_TELECONSULTATION'"
        ).fetchone()[0] == "RECONCILE_REQUIRED"

    reconciled = handler.process_due(worker_id="worker-reconcile")

    assert len(reconciled) == 1
    assert feegow.cancelled_appointments == [(appointment_id, 1)]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM operations WHERE kind = 'EXPIRE_TELECONSULTATION'"
        ).fetchone()[0] == "SUCCEEDED"
        assert connection.execute("SELECT state FROM reservations").fetchone()[0] == "RESERVA_CANCELADA"
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 1


def test_expiration_ambiguity_with_status1_never_repeats_cancel(tmp_path, monkeypatch):
    handler, feegow, db_path, _, clock, appointment_id, deadline = _create_tele_reservation(
        tmp_path, prefix="expire-unresolved"
    )

    def timeout_without_observable_commit(appointment_id, motivo_id):
        feegow.calls.append(
            (
                "cancel_appointment",
                {"appointment_id": appointment_id, "motivo_id": motivo_id},
            )
        )
        feegow.cancelled_appointments.append((appointment_id, motivo_id))
        raise TimeoutError("ambiguous timeout with status still pending")

    monkeypatch.setattr(feegow, "cancel_appointment", timeout_without_observable_commit)
    clock.value = deadline + timedelta(seconds=61)

    assert handler.process_due(worker_id="worker-first") == []
    assert handler.process_due(worker_id="worker-reconcile") == []

    assert feegow.cancelled_appointments == [(appointment_id, 1)]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM operations WHERE kind = 'EXPIRE_TELECONSULTATION'"
        ).fetchone()[0] == "RECONCILE_REQUIRED"
        assert connection.execute("SELECT state FROM reservations").fetchone()[0] == "CANCELAMENTO_FEEGOW_PENDENTE"
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 0


def _insert_outbox_event(db_path: Path, *, outbox_id: str, created_at: datetime) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO outbox_events
                (id, idempotency_key, chat_key, body, state, created_at, sent_at)
            VALUES (?, ?, 'patient-chat', 'message body', 'PENDING', ?, NULL)
            """,
            (outbox_id, f"key-{outbox_id}", created_at.isoformat()),
        )


def test_concurrent_outbox_claimers_never_receive_the_same_event(tmp_path):
    db_path = tmp_path / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    now = datetime(2026, 8, 2, 12, 0, tzinfo=ZoneInfo("America/Bahia"))
    _insert_outbox_event(db_path, outbox_id="event-concurrent", created_at=now)
    barrier = threading.Barrier(3)

    def claim(owner: str):
        barrier.wait()
        return store.claim_outbox_batch(
            owner=owner,
            now=now,
            lease_seconds=30,
            limit=1,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(claim, "worker-a")
        second = executor.submit(claim, "worker-b")
        barrier.wait()
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert sorted(len(result) for result in results) == [0, 1]
    with sqlite3.connect(db_path) as connection:
        state, owner = connection.execute(
            "SELECT state, owner FROM outbox_events WHERE id = 'event-concurrent'"
        ).fetchone()
    assert state == "CLAIMED"
    assert owner in {"worker-a", "worker-b"}


def test_stale_outbox_owner_cannot_ack_after_lease_reclaim(tmp_path):
    db_path = tmp_path / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    claimed_at = datetime(2026, 8, 2, 12, 0, tzinfo=ZoneInfo("America/Bahia"))
    _insert_outbox_event(db_path, outbox_id="event-ack", created_at=claimed_at)

    first_claim = store.claim_outbox_batch(
        owner="worker-a", now=claimed_at, lease_seconds=5, limit=1
    )
    assert len(first_claim) == 1
    reclaimed_at = claimed_at + timedelta(seconds=6)
    current_claim = store.claim_outbox_batch(
        owner="worker-b", now=reclaimed_at, lease_seconds=30, limit=1
    )
    assert len(current_claim) == 1

    assert (
        store.ack_outbox_sent(
            "event-ack",
            owner="worker-a",
            claim_token=first_claim[0]["claim_token"],
            now=reclaimed_at + timedelta(seconds=1),
        )
        is False
    )
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT state, owner, sent_at FROM outbox_events WHERE id = 'event-ack'"
        ).fetchone()
    assert row == ("CLAIMED", "worker-b", None)

    assert (
        store.ack_outbox_sent(
            "event-ack",
            owner="worker-b",
            claim_token=current_claim[0]["claim_token"],
            now=reclaimed_at + timedelta(seconds=2),
        )
        is True
    )
    with sqlite3.connect(db_path) as connection:
        state, owner, sent_at = connection.execute(
            "SELECT state, owner, sent_at FROM outbox_events WHERE id = 'event-ack'"
        ).fetchone()
    assert state == "SENT"
    assert owner is None
    assert sent_at == (reclaimed_at + timedelta(seconds=2)).isoformat()


def test_stale_outbox_owner_cannot_release_after_lease_reclaim(tmp_path):
    db_path = tmp_path / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    claimed_at = datetime(2026, 8, 2, 12, 0, tzinfo=ZoneInfo("America/Bahia"))
    _insert_outbox_event(db_path, outbox_id="event-release", created_at=claimed_at)

    first_claim = store.claim_outbox_batch(
        owner="worker-a", now=claimed_at, lease_seconds=5, limit=1
    )
    assert len(first_claim) == 1
    reclaimed_at = claimed_at + timedelta(seconds=6)
    current_claim = store.claim_outbox_batch(
        owner="worker-b", now=reclaimed_at, lease_seconds=30, limit=1
    )
    assert len(current_claim) == 1

    assert (
        store.release_outbox_failure(
            "event-release",
            owner="worker-a",
            claim_token=first_claim[0]["claim_token"],
            now=reclaimed_at + timedelta(seconds=1),
            backoff_seconds=10,
        )
        is False
    )
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT state, owner, attempts, next_attempt_at
              FROM outbox_events WHERE id = 'event-release'
            """
        ).fetchone()
    assert row == ("CLAIMED", "worker-b", 0, None)

    released_at = reclaimed_at + timedelta(seconds=2)
    assert (
        store.release_outbox_failure(
            "event-release",
            owner="worker-b",
            claim_token=current_claim[0]["claim_token"],
            now=released_at,
            backoff_seconds=10,
        )
        is True
    )
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT state, owner, attempts, next_attempt_at
              FROM outbox_events WHERE id = 'event-release'
            """
        ).fetchone()
    assert row == (
        "PENDING",
        None,
        1,
        (released_at + timedelta(seconds=10)).isoformat(),
    )


def test_same_owner_reclaim_fences_late_ack_by_claim_token(tmp_path):
    db_path = tmp_path / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    claimed_at = datetime(2026, 8, 2, 12, 0, tzinfo=ZoneInfo("America/Bahia"))
    _insert_outbox_event(db_path, outbox_id="event-same-owner-ack", created_at=claimed_at)

    first = store.claim_outbox_batch(
        owner="worker-a", now=claimed_at, lease_seconds=5, limit=1
    )[0]
    reclaimed_at = claimed_at + timedelta(seconds=6)
    current = store.claim_outbox_batch(
        owner="worker-a", now=reclaimed_at, lease_seconds=30, limit=1
    )[0]

    assert first["claim_token"] != current["claim_token"]
    assert store.ack_outbox_sent(
        "event-same-owner-ack",
        owner="worker-a",
        claim_token=first["claim_token"],
        now=reclaimed_at + timedelta(seconds=1),
    ) is False
    assert store.ack_outbox_sent(
        "event-same-owner-ack",
        owner="worker-a",
        claim_token=current["claim_token"],
        now=reclaimed_at + timedelta(seconds=2),
    ) is True


def test_same_owner_reclaim_fences_late_release_by_claim_token(tmp_path):
    db_path = tmp_path / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    claimed_at = datetime(2026, 8, 2, 12, 0, tzinfo=ZoneInfo("America/Bahia"))
    _insert_outbox_event(db_path, outbox_id="event-same-owner-release", created_at=claimed_at)

    first = store.claim_outbox_batch(
        owner="worker-a", now=claimed_at, lease_seconds=5, limit=1
    )[0]
    reclaimed_at = claimed_at + timedelta(seconds=6)
    current = store.claim_outbox_batch(
        owner="worker-a", now=reclaimed_at, lease_seconds=30, limit=1
    )[0]

    assert first["claim_token"] != current["claim_token"]
    assert store.release_outbox_failure(
        "event-same-owner-release",
        owner="worker-a",
        claim_token=first["claim_token"],
        now=reclaimed_at + timedelta(seconds=1),
        backoff_seconds=10,
    ) is False
    assert store.release_outbox_failure(
        "event-same-owner-release",
        owner="worker-a",
        claim_token=current["claim_token"],
        now=reclaimed_at + timedelta(seconds=2),
        backoff_seconds=10,
    ) is True


@pytest.mark.asyncio
async def test_late_successful_sender_cannot_ack_event_reclaimed_by_another_drain(tmp_path):
    db_path = tmp_path / "appointments.sqlite3"
    claimed_at = datetime(2026, 8, 2, 12, 0, tzinfo=ZoneInfo("America/Bahia"))
    clock = MutableClock(claimed_at)
    handler = WhatsAppAppointmentsHandler(
        payment_config(outbox_lease_seconds=10),
        db_path=db_path,
        clock=clock,
    )
    AppointmentStore(db_path)
    _insert_outbox_event(db_path, outbox_id="event-drain-ack", created_at=claimed_at)

    class BlockingSuccessAdapter:
        def __init__(self):
            self.started = asyncio.Event()
            self.resume = asyncio.Event()
            self.sent = []

        async def send(self, chat_key, body):
            self.sent.append((chat_key, body))
            self.started.set()
            await self.resume.wait()
            return SimpleNamespace(success=True)

    class ImmediateSuccessAdapter:
        def __init__(self):
            self.sent = []

        async def send(self, chat_key, body):
            self.sent.append((chat_key, body))
            return SimpleNamespace(success=True)

    stale_adapter = BlockingSuccessAdapter()
    stale_drain = asyncio.create_task(
        drain_appointment_outbox(handler, stale_adapter, worker_id="worker-a")
    )
    await asyncio.wait_for(stale_adapter.started.wait(), timeout=5)

    clock.value = claimed_at + timedelta(seconds=11)
    current_adapter = ImmediateSuccessAdapter()
    assert (
        await drain_appointment_outbox(handler, current_adapter, worker_id="worker-b")
        == 1
    )

    stale_adapter.resume.set()
    assert await asyncio.wait_for(stale_drain, timeout=5) == 0
    assert len(stale_adapter.sent) == 1
    assert len(current_adapter.sent) == 1
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT state, owner, attempts, sent_at
              FROM outbox_events WHERE id = 'event-drain-ack'
            """
        ).fetchone()
    assert row == ("SENT", None, 0, clock.value.isoformat())


@pytest.mark.asyncio
async def test_failed_sender_releases_claim_with_same_drain_worker_id(tmp_path):
    db_path = tmp_path / "appointments.sqlite3"
    now = datetime(2026, 8, 2, 12, 0, tzinfo=ZoneInfo("America/Bahia"))
    clock = MutableClock(now)
    handler = WhatsAppAppointmentsHandler(
        payment_config(outbox_backoff_seconds=10),
        db_path=db_path,
        clock=clock,
    )
    AppointmentStore(db_path)
    _insert_outbox_event(db_path, outbox_id="event-drain-release", created_at=now)

    class FailedAdapter:
        async def send(self, chat_key, body):
            return SimpleNamespace(success=False)

    assert await drain_appointment_outbox(handler, FailedAdapter(), worker_id="worker-a") == 0
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT state, owner, attempts, next_attempt_at, sent_at
              FROM outbox_events WHERE id = 'event-drain-release'
            """
        ).fetchone()
    assert row == (
        "PENDING",
        None,
        1,
        (now + timedelta(seconds=10)).isoformat(),
        None,
    )


@pytest.mark.parametrize(
    "config",
    [
        payment_config(write_enabled=False),
        payment_config(payment={"enabled": False, "beneficiary": "Clínica", "instructions": "PIX"}),
    ],
)
@pytest.mark.asyncio
async def test_kill_switches_block_watcher_start_and_outbox_claim(tmp_path, config):
    db_path = tmp_path / "appointments.sqlite3"
    now = datetime(2026, 8, 2, 12, tzinfo=ZoneInfo("America/Bahia"))
    handler = WhatsAppAppointmentsHandler(config, db_path=db_path, clock=lambda: now)
    AppointmentStore(db_path)
    _insert_outbox_event(db_path, outbox_id="blocked-event", created_at=now)

    class Adapter:
        def __init__(self):
            self.sent = []

        async def send(self, chat_key, body):
            self.sent.append((chat_key, body))
            return SimpleNamespace(success=True)

    adapter = Adapter()
    assert handler.watcher_enabled is False
    assert handler.claim_outbox(worker_id="blocked") == []
    assert await drain_appointment_outbox(handler, adapter, worker_id="blocked") == 0
    await asyncio.wait_for(
        run_appointment_watcher(handler, lambda: adapter, asyncio.Event(), interval=60),
        timeout=0.2,
    )
    assert adapter.sent == []


def _new_patient_flow(handler, prefix="new"):
    handler.handle(event("Quero agendar uma consulta", message_id=f"{prefix}-1"))
    handler.handle(event("1", message_id=f"{prefix}-2"))
    handler.handle(event("1", message_id=f"{prefix}-3"))
    handler.handle(event("1", message_id=f"{prefix}-4"))
    handler.handle(event("529.982.247-25", message_id=f"{prefix}-5"))
    handler.handle(event("01/02/1990", message_id=f"{prefix}-6"))
    handler.handle(event("SIM", message_id=f"{prefix}-7"))
    handler.handle(event("Pessoa Teste", message_id=f"{prefix}-8"))
    handler.handle(event("F", message_id=f"{prefix}-9"))
    return handler.handle(event("pessoa.teste@example.invalid", message_id=f"{prefix}-10"))


def test_new_patient_requires_independent_exact_readback_before_appointment(tmp_path):
    slot = {"id": "slot-wed-14", "data": "2026-08-05", "horario": "14:00"}
    feegow = FakeFeegow(
        slots=[slot],
        procedures=[{"procedimento_id": 1, "nome": "Consulta", "valor": 600}],
    )
    handler = WhatsAppAppointmentsHandler(
        payment_config(), db_path=tmp_path / "appointments.sqlite3", feegow_client=feegow
    )

    summary = _new_patient_flow(handler)
    # The authorization summary must describe the appointment without
    # restating persisted patient PII.
    assert "Pessoa Teste" not in summary
    assert "novo cadastro de paciente" in summary.lower()
    response = handler.handle(event("CONFIRMAR", message_id="new-11"))

    assert "status 1" in response
    assert len(feegow.created_patients) == 1
    assert len(feegow.created_appointments) == 1
    assert sum(name == "find_patient_by_cpf" for name, _ in feegow.calls) >= 2


def test_new_patient_teleconsultation_summary_discloses_registration(tmp_path):
    feegow = FakeFeegow(
        slots=[{"id": "slot-tele-14", "data": "2026-08-05", "horario": "14:00"}],
        procedures=[{"procedimento_id": 3, "nome": "Teleconsulta", "valor": 300}],
    )
    handler = WhatsAppAppointmentsHandler(
        payment_config(), db_path=tmp_path / "appointments.sqlite3", feegow_client=feegow
    )

    handler.handle(event("Quero agendar uma consulta", message_id="tele-new-1"))
    handler.handle(event("1", message_id="tele-new-2"))
    handler.handle(event("3", message_id="tele-new-3"))
    handler.handle(event("1", message_id="tele-new-4"))
    handler.handle(event("529.982.247-25", message_id="tele-new-5"))
    handler.handle(event("01/02/1990", message_id="tele-new-6"))
    handler.handle(event("SIM", message_id="tele-new-7"))
    handler.handle(event("Pessoa Tele", message_id="tele-new-8"))
    handler.handle(event("F", message_id="tele-new-9"))
    summary = handler.handle(
        event("pessoa.tele@example.invalid", message_id="tele-new-10")
    )

    assert "novo cadastro de paciente" in summary.lower()
    assert "Pessoa Tele" not in summary


def test_public_flow_exposes_reconciliation_after_ambiguous_authorized_operation(
    tmp_path, monkeypatch
):
    slot = {"id": "slot-wed-14", "data": "2026-08-05", "horario": "14:00"}
    feegow = FakeFeegow(
        slots=[slot],
        procedures=[{"procedimento_id": 1, "nome": "Consulta", "valor": 600}],
    )
    db_path = tmp_path / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        payment_config(), db_path=db_path, feegow_client=feegow
    )
    _new_patient_flow(handler, prefix="public-reconcile")
    calls = []

    def ambiguous_once(store, chat_key, data):
        calls.append(data)
        if len(calls) == 1:
            raise TimeoutError("ambiguous transport after remote commit")
        return 901

    monkeypatch.setattr(handler, "_execute_authorized", ambiguous_once)
    first = handler.handle(event("CONFIRMAR", message_id="public-reconcile-11"))

    assert "reconciliar" in first.lower()
    assert "recepção" not in first.lower()
    with sqlite3.connect(db_path) as connection:
        state = connection.execute(
            "SELECT state FROM flow_states WHERE chat_key = ?",
            ("5571999999999@s.whatsapp.net",),
        ).fetchone()[0]
    assert state == "RECONCILIATION_REQUIRED"

    second = handler.handle(event("RECONCILIAR", message_id="public-reconcile-12"))

    assert "status 1" in second
    assert len(calls) == 2
    with sqlite3.connect(db_path) as connection:
        consumed_at = connection.execute(
            "SELECT consumed_at FROM authorizations"
        ).fetchone()[0]
    assert consumed_at is not None


def test_confirmation_is_not_consumed_when_remote_create_is_ambiguous(
    tmp_path, monkeypatch
):
    slot = {"id": "slot-wed-14", "data": "2026-08-05", "horario": "14:00"}
    feegow = FakeFeegow(
        slots=[slot],
        procedures=[{"procedimento_id": 1, "nome": "Consulta", "valor": 600}],
    )
    db_path = tmp_path / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        payment_config(), db_path=db_path, feegow_client=feegow
    )
    _new_patient_flow(handler, prefix="auth-ambiguous")

    def ambiguous(*args, **kwargs):
        raise TimeoutError("ambiguous transport after remote commit")

    monkeypatch.setattr(handler, "_execute_authorized", ambiguous)
    response = handler.handle(event("CONFIRMAR", message_id="auth-ambiguous-11"))

    assert "reconciliar" in response.lower()
    with sqlite3.connect(db_path) as connection:
        consumed_at = connection.execute(
            "SELECT consumed_at FROM authorizations"
        ).fetchone()[0]
    assert consumed_at is None


def test_new_patient_readback_mismatch_stops_before_appointment_and_requires_reconcile(tmp_path):
    class MismatchingPatientFeegow(FakeFeegow):
        def create_patient(self, **payload):
            self.calls.append(("create_patient", payload))
            self.created_patients.append(payload)
            self.patients = [{"paciente_id": 501, "cpf": payload["cpf"], "nome": "Outra Pessoa"}]
            return {"success": True, "content": {"paciente_id": 501}}

    slot = {"id": "slot-wed-14", "data": "2026-08-05", "horario": "14:00"}
    feegow = MismatchingPatientFeegow(
        slots=[slot],
        procedures=[{"procedimento_id": 1, "nome": "Consulta", "valor": 600}],
    )
    db_path = tmp_path / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(payment_config(), db_path=db_path, feegow_client=feegow)
    _new_patient_flow(handler, prefix="mismatch")

    result = handler.handle(event("CONFIRMAR", message_id="mismatch-11"))
    assert "reconciliar" in result.lower()

    assert feegow.created_appointments == []
    with sqlite3.connect(db_path) as connection:
        state = connection.execute(
            "SELECT state FROM operations WHERE kind = 'CREATE_PATIENT'"
        ).fetchone()[0]
    assert state == "RECONCILE_REQUIRED"


def test_edit_patient_full_flow_has_exact_readback(tmp_path):
    patient = {
        "paciente_id": 77,
        "cpf": "52998224725",
        "data_nascimento": "01/02/1990",
        "celular": "71999999999",
        "email": "old@example.invalid",
    }
    feegow = FakeFeegow(patients=[patient])
    handler = WhatsAppAppointmentsHandler(
        payment_config(), db_path=tmp_path / "appointments.sqlite3", feegow_client=feegow
    )

    response = authenticate_for_action(handler, "5", "edit")
    assert "atualizar" in response.lower()
    handler.handle(event("2", message_id="edit-6"))
    handler.handle(event("new@example.invalid", message_id="edit-7"))
    response = handler.handle(event("CONFIRMAR", message_id="edit-8"))

    assert "alterado com sucesso" in response
    assert feegow.edited_patients == [
        {"paciente_id": 77, "email": "new@example.invalid"}
    ]


def test_ambiguous_create_is_reconciled_by_remote_fingerprint_without_second_write(tmp_path):
    class AmbiguousCreateFeegow(FakeFeegow):
        def create_appointment(self, **payload):
            self.calls.append(("create_appointment", payload))
            self.created_appointments.append(payload)
            appointment_id = 901
            remote = {
                "agendamento_id": appointment_id,
                "status_id": 1,
                "paciente_id": payload["paciente_id"],
                "procedimento_id": payload["procedimento_id"],
                "profissional_id": payload["profissional_id"],
                "data": payload["data"],
                "horario": payload["horario"],
            }
            self.readbacks[appointment_id] = remote
            self.duplicates = [remote]
            raise TimeoutError("ambiguous transport")

    slot = {"id": "slot-wed-14", "data": "2026-08-05", "horario": "14:00"}
    patient = {
        "paciente_id": 77,
        "cpf": "52998224725",
        "data_nascimento": "01/02/1990",
        "celular": "71999999999",
    }
    feegow = AmbiguousCreateFeegow(
        slots=[slot],
        patients=[patient],
        procedures=[{"procedimento_id": 1, "nome": "Consulta", "valor": 600}],
    )
    db_path = tmp_path / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(payment_config(), db_path=db_path, feegow_client=feegow)
    data = {
        "cpf": patient["cpf"],
        "patient_id": 77,
        "new_patient": False,
        "procedure_id": 1,
        "price": 600,
        "selected_slot": {
            "id": slot["id"],
            "date": slot["data"],
            "time": slot["horario"],
        },
        "payload_hash": "fingerprint-hash",
    }
    store = AppointmentStore(db_path)

    with pytest.raises(TimeoutError, match="ambiguous"):
        handler._execute_authorized(store, "5571999999999@s.whatsapp.net", data)
    # A successful ambiguous POST normally consumes the slot. Reconciliation
    # must therefore run before any fresh availability preflight on retry.
    feegow.slots = []
    assert handler._execute_authorized(store, "5571999999999@s.whatsapp.net", data) == 901
    assert len(feegow.created_appointments) == 1
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT state, remote_id FROM operations WHERE kind = 'CREATE_APPOINTMENT'"
        ).fetchone()
    assert row == ("SUCCEEDED", "901")


def test_reschedule_readback_requires_new_date_and_time():
    selected = {
        "id": 123,
        "patient_id": 77,
        "procedure_id": 1,
        "date": "05-08-2026",
        "time": "14:00",
    }
    remote = {
        "agendamento_id": 123,
        "paciente_id": 77,
        "procedimento_id": 1,
        "status_id": 15,
        "data": "05-08-2026",
        "horario": "14:00",
    }
    new_slot = {"data": "06-08-2026", "horario": "15:00"}

    with pytest.raises(RuntimeError, match="date readback mismatch"):
        WhatsAppAppointmentsHandler._verify_mutation_readback(
            remote,
            appointment_id=123,
            expected_status=15,
            selected=selected,
            expected_slot=new_slot,
        )


def test_institutional_message_quarantines_preexisting_local_state_and_outbox(tmp_path):
    db_path = tmp_path / "appointments.sqlite3"
    now = datetime(2026, 8, 2, 12, tzinfo=ZoneInfo("America/Bahia"))
    chat_key = "5571999999999@s.whatsapp.net"
    handler = WhatsAppAppointmentsHandler(payment_config(), db_path=db_path, clock=lambda: now)
    handler.handle(event("Quero agendar uma consulta", message_id="contaminated-1"))
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO reservations (appointment_id, chat_key, state, deadline_at, reminder_at) VALUES ('901', ?, 'PENDING_PAYMENT', ?, ?)",
            (chat_key, now.isoformat(), now.isoformat()),
        )
        connection.execute(
            "INSERT INTO reminders (id, appointment_id, due_at, state, created_at) VALUES ('rem-901', '901', ?, 'PENDING', ?)",
            (now.isoformat(), now.isoformat()),
        )
        connection.execute(
            "INSERT INTO outbox_events (id, idempotency_key, chat_key, body, state, created_at) VALUES ('out-901', 'key-901', ?, 'não enviar', 'PENDING', ?)",
            (chat_key, now.isoformat()),
        )
        connection.execute(
            "INSERT INTO returns_ledger (base_appointment_id, chat_key, base_date, state, created_at, updated_at) VALUES ('base-901', ?, ?, 'OPEN', ?, ?)",
            (chat_key, now.isoformat(), now.isoformat(), now.isoformat()),
        )
        connection.commit()

    assert handler.handle(
        event("Somos da clínica parceira Rapidoc", message_id="contaminated-2")
    ) is None

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM flow_states WHERE chat_key = ?", (chat_key,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT state FROM reservations WHERE appointment_id = '901'"
        ).fetchone()[0] == "QUARANTINED"
        assert connection.execute(
            "SELECT state FROM reminders WHERE id = 'rem-901'"
        ).fetchone()[0] == "QUARANTINED"
        assert connection.execute(
            "SELECT state FROM returns_ledger WHERE base_appointment_id = 'base-901'"
        ).fetchone()[0] == "QUARANTINED"
        assert connection.execute(
            "SELECT COUNT(*) FROM outbox_events WHERE chat_key = ?", (chat_key,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT is_quarantined FROM contacts WHERE chat_key = ?", (chat_key,)
        ).fetchone()[0] == 1
    assert handler.process_due(worker_id="quarantine") == []
    assert handler.claim_outbox(worker_id="quarantine") == []


def test_free_return_full_flow_consumes_controlled_ledger_at_zero_price(tmp_path):
    now = datetime(2026, 8, 2, 10, tzinfo=ZoneInfo("America/Bahia"))
    slot = {"id": "return-wed", "procedimento_id": 2, "data": "2026-08-05", "horario": "14:00"}
    patient = {
        "paciente_id": 77,
        "cpf": "52998224725",
        "data_nascimento": "01/02/1990",
        "celular": "71999999999",
    }
    base = {
        "agendamento_id": 700,
        "paciente_id": 77,
        "procedimento_id": 9,
        "status_id": 3,
    }
    feegow = FakeFeegow(
        slots=[slot],
        patients=[patient],
        appointments=[base],
        procedures=[{"procedimento_id": 2, "nome": "Retorno", "valor": 0}],
    )
    db_path = tmp_path / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        payment_config(return_policy={"enabled": True, "procedure_id": 2}),
        db_path=db_path,
        feegow_client=feegow,
        clock=lambda: now,
    )
    store = AppointmentStore(db_path)
    store.create_return_ledger_entry(
        700,
        "5571999999999@s.whatsapp.net",
        base_date=now - timedelta(days=30),
        now=now,
    )

    assert "retorno" in authenticate_for_action(handler, "4", "return").lower()
    handler.handle(event("1", message_id="return-6"))
    summary = handler.handle(event("1", message_id="return-7"))
    assert "R$ 0" in summary
    response = handler.handle(event("CONFIRMAR", message_id="return-8"))

    assert "status 1" in response
    assert feegow.created_appointments[0]["procedimento_id"] == 2
    assert feegow.created_appointments[0]["valor"] == 0
    with sqlite3.connect(db_path) as connection:
        state, return_id = connection.execute(
            "SELECT state, return_appointment_id FROM returns_ledger WHERE base_appointment_id = '700'"
        ).fetchone()
    assert state == "CONSUMED"
    assert return_id == "901"


def test_free_return_ledger_allows_only_one_concurrent_remote_write(tmp_path):
    class ConcurrentReturnFeegow(FakeFeegow):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.write_barrier = threading.Barrier(2)

        def create_appointment(self, **payload):
            try:
                self.write_barrier.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                pass
            return super().create_appointment(**payload)

    now = datetime(2026, 8, 2, 10, tzinfo=ZoneInfo("America/Bahia"))
    slots = [
        {"id": "return-wed", "procedimento_id": 2, "data": "2026-08-05", "horario": "14:00"},
        {"id": "return-thu", "procedimento_id": 2, "data": "2026-08-06", "horario": "15:00"},
    ]
    feegow = ConcurrentReturnFeegow(
        slots=slots,
        procedures=[{"procedimento_id": 2, "nome": "Retorno", "valor": 0}],
    )
    db_path = tmp_path / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        payment_config(return_procedure_id=2),
        db_path=db_path,
        feegow_client=feegow,
        clock=lambda: now,
    )
    store = AppointmentStore(db_path)
    chat_key = "5571999999999@s.whatsapp.net"
    store.create_return_ledger_entry(
        700,
        chat_key,
        base_date=now - timedelta(days=30),
        now=now,
    )

    def execute(slot):
        data = {
            "appointment_action": "RETURN",
            "base_appointment_id": 700,
            "return_modality": "presencial",
            "cpf": "52998224725",
            "patient_id": 77,
            "new_patient": False,
            "procedure_id": 2,
            "price": 0,
            "selected_slot": {
                "id": slot["id"],
                "date": slot["data"],
                "time": slot["horario"],
            },
            "payload_hash": f"return-{slot['id']}",
        }
        try:
            return handler._execute_authorized(store, chat_key, data)
        except RuntimeError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, slots))

    assert sum(isinstance(result, int) for result in results) == 1
    assert sum(isinstance(result, RuntimeError) for result in results) == 1
    assert len(feegow.created_appointments) == 1
    with sqlite3.connect(db_path) as connection:
        state, return_id = connection.execute(
            "SELECT state, return_appointment_id FROM returns_ledger WHERE base_appointment_id = '700'"
        ).fetchone()
    assert state == "CONSUMED"
    assert return_id == str(next(result for result in results if isinstance(result, int)))


def test_sensitive_values_never_enter_logs_during_failed_new_patient_readback(tmp_path, caplog):
    class FailingReadbackFeegow(FakeFeegow):
        def create_patient(self, **payload):
            self.calls.append(("create_patient", payload))
            self.created_patients.append(payload)
            raise TimeoutError("transport")

    feegow = FailingReadbackFeegow(
        slots=[{"id": "slot", "data": "2026-08-05", "horario": "14:00"}],
        procedures=[{"procedimento_id": 1, "nome": "Consulta", "valor": 600}],
    )
    handler = WhatsAppAppointmentsHandler(
        payment_config(), db_path=tmp_path / "appointments.sqlite3", feegow_client=feegow
    )
    _new_patient_flow(handler, prefix="pii")

    result = handler.handle(event("CONFIRMAR", message_id="pii-11"))
    assert "reconciliar" in result.lower()

    for sentinel in (
        "52998224725",
        "Pessoa Teste",
        "pessoa.teste@example.invalid",
        "71999999999",
        "01/02/1990",
    ):
        assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_watcher_cancellation_is_awaitable_and_cancels_inflight_send(tmp_path):
    db_path = tmp_path / "appointments.sqlite3"
    now = datetime(2026, 8, 2, 12, tzinfo=ZoneInfo("America/Bahia"))
    handler = WhatsAppAppointmentsHandler(payment_config(), db_path=db_path, clock=lambda: now)
    AppointmentStore(db_path)
    _insert_outbox_event(db_path, outbox_id="shutdown-event", created_at=now)

    class BlockingAdapter:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def send(self, chat_key, body):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    adapter = BlockingAdapter()
    task = asyncio.create_task(
        run_appointment_watcher(handler, lambda: adapter, asyncio.Event(), interval=60)
    )
    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.cancelled.is_set()
