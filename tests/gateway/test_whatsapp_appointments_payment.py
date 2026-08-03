"""Teleconsultation payment windows, reminders, expiry and reconciliation.

The payment policy is exact: >48h lead time gets a 12h window with a 2h
reminder, >24h gets 4h/1h, >3h gets 1h/15min, and <=3h creates no
reservation at all. Expiry is an absolute BRT instant, an on-time proof
always beats it, and a cancellation is only ever emitted after an exact-id
readback.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.whatsapp_appointments import (
    AppointmentStore,
    FlowState,
    WhatsAppAppointmentsHandler,
)
from tests.gateway.appointment_helpers import (
    BIRTH_DATE,
    CHAT_KEY,
    CPF_FORMATTED,
    FakeFeegow,
    MutableClock,
    NoCallFeegow,
    event,
    known_patient,
    payment_config,
)

BRT = ZoneInfo("America/Bahia")
NOW = datetime(2026, 8, 1, 10, 0, tzinfo=BRT)
APPOINTMENT_ID = 901


def _build(feegow, tmp_path, clock, **overrides):
    return WhatsAppAppointmentsHandler(
        payment_config(**overrides),
        db_path=tmp_path / "state" / "appointments.sqlite3",
        proofs_dir=tmp_path / "proofs",
        feegow_client=feegow,
        clock=clock,
    )


# --------------------------------------------------------------------------
# Exact window/reminder boundaries
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lead,window_hours,reminder_minutes",
    [
        (timedelta(days=7), 12, 120),
        (timedelta(hours=48, minutes=1), 12, 120),
        (timedelta(hours=48), 4, 60),  # exactly 48h -> the 4h tier
        (timedelta(hours=24, minutes=1), 4, 60),
        (timedelta(hours=24), 1, 15),  # exactly 24h -> the 1h tier
        (timedelta(hours=3, minutes=1), 1, 15),
    ],
)
def test_payment_window_boundaries_are_exact(
    tmp_path, lead, window_hours, reminder_minutes
):
    handler = _build(FakeFeegow(), tmp_path, MutableClock(NOW))
    starts_at = NOW + lead

    terms = handler._tele_payment_terms(
        {"date": starts_at.date().isoformat(), "time": starts_at.strftime("%H:%M")}
    )

    assert terms == (
        timedelta(hours=window_hours),
        timedelta(minutes=reminder_minutes),
    )


@pytest.mark.parametrize(
    "lead", [timedelta(hours=3), timedelta(hours=2), timedelta(minutes=30)]
)
def test_three_hours_or_less_creates_no_reservation(tmp_path, lead):
    handler = _build(FakeFeegow(), tmp_path, MutableClock(NOW))
    starts_at = NOW + lead

    assert (
        handler._tele_payment_terms(
            {"date": starts_at.date().isoformat(), "time": starts_at.strftime("%H:%M")}
        )
        is None
    )


def test_a_malformed_slot_never_yields_payment_terms(tmp_path):
    handler = _build(FakeFeegow(), tmp_path, MutableClock(NOW))

    assert handler._tele_payment_terms({}) is None
    assert handler._tele_payment_terms({"date": "nope", "time": "14:00"}) is None


def _tele_flow(handler, prefix):
    handler.handle(event("Quero agendar uma consulta", message_id=f"{prefix}-1"))
    handler.handle(event("1", message_id=f"{prefix}-2"))
    handler.handle(event("3", message_id=f"{prefix}-3"))
    handler.handle(event("1", message_id=f"{prefix}-4"))
    handler.handle(event(CPF_FORMATTED, message_id=f"{prefix}-5"))
    handler.handle(event(BIRTH_DATE, message_id=f"{prefix}-6"))
    return handler.handle(event("SIM", message_id=f"{prefix}-7"))


def _tele_feegow(starts_at):
    return FakeFeegow(
        slots=[
            {
                "id": "tele-1",
                "procedimento_id": 3,
                "data": starts_at.date().isoformat(),
                "horario": starts_at.strftime("%H:%M"),
            }
        ],
        patients=[known_patient()],
        procedures=[{"procedimento_id": 3, "nome": "Teleconsulta", "valor": 300}],
    )


def test_absolute_brt_deadline_is_disclosed_after_the_status_one_readback(tmp_path):
    clock = MutableClock(NOW)
    starts_at = NOW + timedelta(days=7)
    feegow = _tele_feegow(starts_at)
    db_path = tmp_path / "state" / "appointments.sqlite3"
    handler = _build(feegow, tmp_path, clock)

    summary = _tele_flow(handler, "abs")
    assert "CONFIRMAR" in summary
    result = handler.handle(event("CONFIRMAR", message_id="abs-8"))

    expected_deadline = (NOW + timedelta(hours=12)).strftime("%d/%m/%Y %H:%M")
    assert expected_deadline in result
    assert "horário de Brasília" in result
    assert feegow.created_appointments[0]["status_id"] == 1
    names = [name for name, _ in feegow.calls]
    assert names.index("create_appointment") < names.index("get_appointment")

    with sqlite3.connect(db_path) as connection:
        deadline_at, reminder_at, state = connection.execute(
            "SELECT deadline_at, reminder_at, state FROM reservations"
        ).fetchone()
    assert datetime.fromisoformat(deadline_at) == NOW + timedelta(hours=12)
    assert datetime.fromisoformat(reminder_at) == NOW + timedelta(hours=10)
    assert state == "AGUARDANDO_COMPROVANTE"


def test_a_teleconsultation_within_three_hours_never_creates_a_reservation(tmp_path):
    clock = MutableClock(NOW)
    feegow = _tele_feegow(NOW + timedelta(hours=2))
    handler = _build(feegow, tmp_path, clock)

    response = _tele_flow(handler, "short")

    assert "recepção" in response.lower()
    assert feegow.created_appointments == []
    store = AppointmentStore(tmp_path / "state" / "appointments.sqlite3")
    assert store.count("reservations") == 0


@pytest.mark.parametrize(
    "payment",
    [
        {"enabled": False, "beneficiary": "X", "instructions": "Y"},
        {"enabled": True, "beneficiary": "", "instructions": "Y"},
        {"enabled": True, "beneficiary": "X", "instructions": ""},
        {},
    ],
    ids=["disabled", "no-beneficiary", "no-instructions", "empty"],
)
def test_partial_payment_configuration_produces_zero_effects(tmp_path, payment):
    clock = MutableClock(NOW)
    feegow = NoCallFeegow()
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "write_enabled": True, "payment": payment},
        db_path=tmp_path / "state" / "appointments.sqlite3",
        proofs_dir=tmp_path / "proofs",
        feegow_client=feegow,
        clock=clock,
    )

    handler.handle(event("Quero agendar uma consulta", message_id="cfg-1"))
    handler.handle(event("1", message_id="cfg-2"))
    response = handler.handle(event("3", message_id="cfg-3"))

    assert "recepção" in response.lower()
    assert feegow.calls == []
    assert handler.watcher_enabled is False
    assert handler.process_due(worker_id="w-1") == []
    assert handler.claim_outbox(worker_id="w-1") == []
    store = AppointmentStore(tmp_path / "state" / "appointments.sqlite3")
    assert store.count("reservations") == 0
    assert store.count("payment_proofs") == 0
    assert store.count("reminders") == 0
    assert store.count("outbox_events") == 0
    assert not (tmp_path / "proofs").exists()


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------


def _seed_reservation(tmp_path, clock, *, deadline, reminder, state=None):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    store.record_response(
        "seed-1",
        CHAT_KEY,
        "reserva",
        FlowState.AGUARDANDO_COMPROVANTE.value,
        {"appointment_id": APPOINTMENT_ID},
        now=clock.value,
    )
    store.create_reservation(
        APPOINTMENT_ID, CHAT_KEY, deadline_at=deadline, reminder_at=reminder
    )
    if state:
        store.set_reservation_state(APPOINTMENT_ID, state)
    return store, db_path


def _readback_feegow(status_id=1):
    feegow = FakeFeegow()
    feegow.readbacks[APPOINTMENT_ID] = {
        "agendamento_id": APPOINTMENT_ID,
        "status_id": status_id,
        "paciente_id": 77,
        "procedimento_id": 3,
        "data": "2026-08-10",
        "horario": "14:00",
    }
    return feegow


@pytest.mark.parametrize(
    "notice", [timedelta(hours=2), timedelta(hours=1), timedelta(minutes=15)]
)
def test_exactly_one_reminder_is_emitted_at_its_notice(tmp_path, notice):
    clock = MutableClock(NOW)
    feegow = _readback_feegow()
    handler = _build(feegow, tmp_path, clock)
    deadline = NOW + timedelta(hours=12)
    store, _db = _seed_reservation(
        tmp_path, clock, deadline=deadline, reminder=deadline - notice
    )

    # Before the notice: nothing.
    assert handler.process_due(worker_id="w-1") == []
    assert store.count("reminders") == 0

    clock.value = deadline - notice
    first = handler.process_due(worker_id="w-1")
    clock.value = deadline - notice + timedelta(minutes=1)
    second = handler.process_due(worker_id="w-1")

    assert len(first) == 1
    assert "comprovante" in first[0]["body"].lower()
    assert second == []
    assert store.count("reminders") == 1
    assert store.count("outbox_events") == 1


def test_no_reminder_is_emitted_once_a_proof_arrived(tmp_path):
    clock = MutableClock(NOW)
    feegow = _readback_feegow()
    handler = _build(feegow, tmp_path, clock)
    deadline = NOW + timedelta(hours=12)
    store, _db = _seed_reservation(
        tmp_path, clock, deadline=deadline, reminder=deadline - timedelta(hours=2)
    )
    store.accept_payment_proof(
        message_id="p-1",
        appointment_id=APPOINTMENT_ID,
        chat_key=CHAT_KEY,
        received_at=NOW,
        sha256="d" * 64,
        private_path=str(tmp_path / "proofs" / "d"),
    )

    clock.value = deadline - timedelta(hours=2)

    assert handler.process_due(worker_id="w-1") == []
    assert store.count("reminders") == 0


# --------------------------------------------------------------------------
# Expiry: status handling, grace, reconciliation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_id,expected_state,cancels,outbox",
    [
        (7, "CONFIRMADO_STATUS_7", 0, 0),
        (11, "RESERVA_CANCELADA", 0, 1),
        (3, "EXCECAO_RECEPCAO", 0, 0),
        (99, "EXCECAO_RECEPCAO", 0, 0),
        (1, "RESERVA_CANCELADA", 1, 1),
    ],
    ids=["confirmed", "already-cancelled", "finished", "unexpected", "still-open"],
)
def test_expiry_reacts_to_the_exact_remote_status(
    tmp_path, status_id, expected_state, cancels, outbox
):
    clock = MutableClock(NOW)
    feegow = _readback_feegow(status_id)
    handler = _build(feegow, tmp_path, clock)
    deadline = NOW + timedelta(hours=1)
    store, db_path = _seed_reservation(
        tmp_path, clock, deadline=deadline, reminder=deadline - timedelta(minutes=15)
    )

    clock.value = deadline + timedelta(minutes=5)
    handler.process_due(worker_id="w-1")

    with sqlite3.connect(db_path) as connection:
        state = connection.execute(
            "SELECT state FROM reservations WHERE appointment_id = ?",
            (str(APPOINTMENT_ID),),
        ).fetchone()[0]
    assert state == expected_state
    assert len(feegow.cancelled_appointments) == cancels
    # A status-7 reservation is only ever observed, never written.
    assert all(
        name != "update_appointment_status" for name, _ in feegow.calls
    )
    assert store.count("outbox_events") == outbox


def test_the_ingestion_grace_period_defers_the_remote_cancellation(tmp_path):
    clock = MutableClock(NOW)
    feegow = _readback_feegow()
    handler = _build(feegow, tmp_path, clock, payment={
        "enabled": True,
        "beneficiary": "Clínica Exemplo",
        "instructions": "PIX oficial",
        "ingestion_grace_seconds": 600,
    })
    deadline = NOW + timedelta(hours=1)
    store, _db = _seed_reservation(
        tmp_path, clock, deadline=deadline, reminder=deadline - timedelta(minutes=15)
    )

    # Inside the grace window: no cancellation yet.
    clock.value = deadline + timedelta(seconds=300)
    handler.process_due(worker_id="w-1")
    assert feegow.cancelled_appointments == []

    # A proof that lands at the very last second still wins.
    assert (
        store.accept_payment_proof(
            message_id="grace-1",
            appointment_id=APPOINTMENT_ID,
            chat_key=CHAT_KEY,
            received_at=deadline,
            sha256="e" * 64,
            private_path=str(tmp_path / "proofs" / "e"),
        )
        == "accepted"
    )

    clock.value = deadline + timedelta(seconds=900)
    handler.process_due(worker_id="w-1")

    assert feegow.cancelled_appointments == []
    assert store.count("outbox_events") == 0


def test_a_proof_after_the_deadline_is_rejected_and_never_stored(tmp_path):
    clock = MutableClock(NOW)
    deadline = NOW + timedelta(hours=1)
    store, _db = _seed_reservation(
        tmp_path, clock, deadline=deadline, reminder=deadline - timedelta(minutes=15)
    )

    assert (
        store.accept_payment_proof(
            message_id="late-1",
            appointment_id=APPOINTMENT_ID,
            chat_key=CHAT_KEY,
            received_at=deadline + timedelta(seconds=1),
            sha256="f" * 64,
            private_path=str(tmp_path / "proofs" / "f"),
        )
        == "late"
    )
    assert store.count("payment_proofs") == 0


def test_a_pdf_proof_is_associated_by_infrastructure_timestamp(tmp_path):
    clock = MutableClock(NOW)
    feegow = _readback_feegow()
    handler = _build(feegow, tmp_path, clock)
    deadline = NOW + timedelta(hours=4)
    store, db_path = _seed_reservation(
        tmp_path, clock, deadline=deadline, reminder=deadline - timedelta(hours=1)
    )
    pdf = tmp_path / "incoming.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 comprovante")

    # The message is handled long after it arrived; the infrastructure
    # timestamp is what decides, not the processing clock.
    clock.value = deadline + timedelta(minutes=30)
    response = handler.handle(
        event(
            "",
            message_id="pdf-1",
            media_urls=(str(pdf),),
            media_types=("application/pdf",),
            timestamp=deadline - timedelta(minutes=5),
        )
    )

    assert "Comprovante recebido" in response
    with sqlite3.connect(db_path) as connection:
        appointment_id, received_at = connection.execute(
            "SELECT appointment_id, received_at FROM payment_proofs"
        ).fetchone()
    assert appointment_id == str(APPOINTMENT_ID)
    assert datetime.fromisoformat(received_at) == deadline - timedelta(minutes=5)
    assert store.count("payment_proofs") == 1


def test_an_unsupported_media_type_is_not_accepted_as_a_proof(tmp_path):
    clock = MutableClock(NOW)
    feegow = _readback_feegow()
    handler = _build(feegow, tmp_path, clock)
    deadline = NOW + timedelta(hours=4)
    store, _db = _seed_reservation(
        tmp_path, clock, deadline=deadline, reminder=deadline - timedelta(hours=1)
    )
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(b"not a receipt")

    response = handler.handle(
        event(
            "",
            message_id="audio-1",
            media_urls=(str(audio),),
            media_types=("audio/ogg",),
            timestamp=NOW,
        )
    )

    assert "recepção" in response.lower()
    assert store.count("payment_proofs") == 0
    assert not any((tmp_path / "proofs").glob("*")) if (tmp_path / "proofs").exists() else True


def test_the_outbox_row_is_only_written_after_the_cancellation_readback(tmp_path):
    class OrderingFeegow(FakeFeegow):
        def __init__(self, probe):
            super().__init__()
            self.probe = probe
            self.readbacks[APPOINTMENT_ID] = {
                "agendamento_id": APPOINTMENT_ID,
                "status_id": 1,
                "paciente_id": 77,
                "procedimento_id": 3,
                "data": "2026-08-10",
                "horario": "14:00",
            }

        def cancel_appointment(self, appointment_id, motivo_id):
            self.calls.append(("cancel_appointment", {"appointment_id": appointment_id}))
            self.cancelled_appointments.append((appointment_id, motivo_id))
            # At POST time no outbox row may exist yet.
            self.probe.append(("post", _outbox_count(db_path)))
            self.readbacks[appointment_id]["status_id"] = 11
            return {"success": True, "content": {"agendamento_id": appointment_id}}

    def _outbox_count(path):
        with sqlite3.connect(path) as connection:
            return connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0]

    clock = MutableClock(NOW)
    db_path = tmp_path / "state" / "appointments.sqlite3"
    probe: list = []
    feegow = OrderingFeegow(probe)
    handler = _build(feegow, tmp_path, clock)
    deadline = NOW + timedelta(hours=1)
    store, _db = _seed_reservation(
        tmp_path, clock, deadline=deadline, reminder=deadline - timedelta(minutes=15)
    )

    clock.value = deadline + timedelta(minutes=5)
    emitted = handler.process_due(worker_id="w-1")

    assert probe == [("post", 0)]
    assert len(emitted) == 1
    assert store.count("outbox_events") == 1


def test_a_crash_between_post_and_commit_is_reconciled_without_a_second_cancel(
    tmp_path,
):
    class CrashingFeegow(FakeFeegow):
        def __init__(self):
            super().__init__()
            self.readbacks[APPOINTMENT_ID] = {
                "agendamento_id": APPOINTMENT_ID,
                "status_id": 1,
                "paciente_id": 77,
                "procedimento_id": 3,
                "data": "2026-08-10",
                "horario": "14:00",
            }

        def cancel_appointment(self, appointment_id, motivo_id):
            self.calls.append(("cancel_appointment", {"appointment_id": appointment_id}))
            self.cancelled_appointments.append((appointment_id, motivo_id))
            self.readbacks[appointment_id]["status_id"] = 11
            raise TimeoutError("connection dropped after the remote commit")

    clock = MutableClock(NOW)
    feegow = CrashingFeegow()
    handler = _build(feegow, tmp_path, clock)
    db_path = tmp_path / "state" / "appointments.sqlite3"
    deadline = NOW + timedelta(hours=1)
    store, _db = _seed_reservation(
        tmp_path, clock, deadline=deadline, reminder=deadline - timedelta(minutes=15)
    )

    clock.value = deadline + timedelta(minutes=5)
    assert handler.process_due(worker_id="w-1") == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM operations WHERE kind = 'EXPIRE_TELECONSULTATION'"
        ).fetchone()[0] == "RECONCILE_REQUIRED"
    assert store.count("outbox_events") == 0

    # The next tick reads the exact id back and finishes without a new POST.
    clock.value = deadline + timedelta(minutes=10)
    emitted = handler.process_due(worker_id="w-1")

    assert len(feegow.cancelled_appointments) == 1
    assert len(emitted) == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM operations WHERE kind = 'EXPIRE_TELECONSULTATION'"
        ).fetchone()[0] == "SUCCEEDED"
        assert connection.execute(
            "SELECT state FROM reservations WHERE appointment_id = ?",
            (str(APPOINTMENT_ID),),
        ).fetchone()[0] == "RESERVA_CANCELADA"


def test_closing_the_kill_switch_mid_tick_stops_the_expiry_cancellation(tmp_path):
    """The gate is re-checked immediately before the watcher's remote write."""

    clock = MutableClock(NOW)
    feegow = _readback_feegow()
    handler = _build(feegow, tmp_path, clock)
    deadline = NOW + timedelta(hours=1)
    store, db_path = _seed_reservation(
        tmp_path, clock, deadline=deadline, reminder=deadline - timedelta(minutes=15)
    )
    clock.value = deadline + timedelta(minutes=5)

    original = handler._require_write_enabled

    def close_then_check():
        handler._write_enabled = False
        original()

    handler._require_write_enabled = close_then_check
    emitted = handler.process_due(worker_id="w-1")

    assert emitted == []
    assert feegow.cancelled_appointments == []
    assert store.count("outbox_events") == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM operations WHERE kind = 'EXPIRE_TELECONSULTATION'"
        ).fetchone()[0] == "RECONCILE_REQUIRED"


def test_a_held_watcher_lease_stops_a_second_process(tmp_path):
    clock = MutableClock(NOW)
    feegow = _readback_feegow()
    handler = _build(feegow, tmp_path, clock)
    deadline = NOW + timedelta(hours=1)
    store, _db = _seed_reservation(
        tmp_path, clock, deadline=deadline, reminder=deadline - timedelta(minutes=15)
    )
    clock.value = deadline + timedelta(minutes=5)
    assert store.acquire_watcher_lease(
        "teleconsultation-payment-watcher", "other", now=clock.value, lease_seconds=3600
    )

    assert handler.process_due(worker_id="w-1") == []
    assert feegow.cancelled_appointments == []

    store.release_watcher_lease("teleconsultation-payment-watcher", "other")
    assert len(handler.process_due(worker_id="w-1")) == 1


def test_repeated_ticks_never_duplicate_the_cancellation_or_the_outbox(tmp_path):
    clock = MutableClock(NOW)
    feegow = _readback_feegow()
    handler = _build(feegow, tmp_path, clock)
    deadline = NOW + timedelta(hours=1)
    store, _db = _seed_reservation(
        tmp_path, clock, deadline=deadline, reminder=deadline - timedelta(minutes=15)
    )

    clock.value = deadline + timedelta(minutes=5)
    for index in range(5):
        clock.value = deadline + timedelta(minutes=5 + index)
        handler.process_due(worker_id=f"w-{index}")

    assert len(feegow.cancelled_appointments) == 1
    assert store.count("outbox_events") == 1
