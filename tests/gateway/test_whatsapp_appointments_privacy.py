"""Privacy and operational hygiene: no PII anywhere durable or observable.

Sentinel values (CPF, birth date, phone, e-mail, patient name, proof bytes,
original file name) are asserted absent from log records, exception text,
``audit_log.metadata_json`` and ``outbox_events.body`` on both the success
and the failure path. File modes and the retention policy are pinned here
too, since they are the other half of the same requirement.
"""

from __future__ import annotations

import logging
import sqlite3
import stat
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

PATIENT_NAME = "Mariana Sentinela Testesilva"
PATIENT_EMAIL = "mariana.sentinela@example.invalid"
PATIENT_PHONE = "71999999999"
PROOF_BYTES = b"SENTINELA-BYTES-DO-COMPROVANTE-NAO-DEVE-VAZAR"
PROOF_FILENAME = "comprovante-mariana-cpf-52998224725.jpg"

SENTINELS = (
    CPF,
    CPF_FORMATTED,
    BIRTH_DATE,
    PATIENT_EMAIL,
    PATIENT_NAME,
    PROOF_FILENAME,
    PROOF_BYTES.decode(),
)


def assert_no_pii(text: str, *, extra=()):
    haystack = str(text)
    for sentinel in tuple(SENTINELS) + tuple(extra):
        assert sentinel not in haystack, f"PII leak: {sentinel!r} in {haystack!r}"


def _durable_text(db_path):
    """Every operator-visible durable string except the flow working set."""

    with sqlite3.connect(db_path) as connection:
        audit = connection.execute(
            "SELECT event, metadata_json FROM audit_log"
        ).fetchall()
        outbox = connection.execute("SELECT body FROM outbox_events").fetchall()
        operations = connection.execute(
            "SELECT kind, target_id, error_class, remote_id FROM operations"
        ).fetchall()
    return " | ".join(
        str(value)
        for rows in (audit, outbox, operations)
        for row in rows
        for value in row
    )


def _build(feegow, tmp_path, clock=None, **overrides):
    return WhatsAppAppointmentsHandler(
        payment_config(**overrides),
        db_path=tmp_path / "state" / "appointments.sqlite3",
        proofs_dir=tmp_path / "proofs",
        feegow_client=feegow,
        clock=clock or MutableClock(NOW),
    )


def _new_patient_create(handler, prefix):
    handler.handle(event("Quero agendar uma consulta", message_id=f"{prefix}-1"))
    handler.handle(event("1", message_id=f"{prefix}-2"))
    handler.handle(event("1", message_id=f"{prefix}-3"))
    handler.handle(event("1", message_id=f"{prefix}-4"))
    handler.handle(event(CPF_FORMATTED, message_id=f"{prefix}-5"))
    handler.handle(event(BIRTH_DATE, message_id=f"{prefix}-6"))
    handler.handle(event("SIM", message_id=f"{prefix}-7"))
    handler.handle(event(PATIENT_NAME, message_id=f"{prefix}-8"))
    handler.handle(event("F", message_id=f"{prefix}-9"))
    return handler.handle(event(PATIENT_EMAIL, message_id=f"{prefix}-10"))


WED_SLOT = {"id": "slot-wed-14", "procedimento_id": 1, "data": "2026-08-05", "horario": "14:00"}
PROC1 = {"procedimento_id": 1, "nome": "Consulta", "valor": 600}


def test_successful_patient_and_appointment_creation_leaks_nothing(tmp_path, caplog):
    feegow = FakeFeegow(slots=[dict(WED_SLOT)], patients=[], procedures=[dict(PROC1)])
    handler = _build(feegow, tmp_path)
    db_path = tmp_path / "state" / "appointments.sqlite3"

    with caplog.at_level(logging.DEBUG):
        summary = _new_patient_create(handler, "ok")
        result = handler.handle(event("CONFIRMAR", message_id="ok-11"))

    assert "status 1" in result
    assert feegow.created_patients and feegow.created_appointments
    assert_no_pii(caplog.text)
    assert_no_pii(summary)
    assert_no_pii(result)
    assert_no_pii(_durable_text(db_path))


def test_failed_patient_readback_leaks_nothing_in_logs_or_audit(tmp_path, caplog):
    class MismatchingFeegow(FakeFeegow):
        def create_patient(self, **payload):
            self.calls.append(("create_patient", payload))
            self.created_patients.append(payload)
            self.patients = [{"paciente_id": 501, "cpf": payload["cpf"], "nome": "Outra"}]
            return {"success": True, "content": {"paciente_id": 501}}

    feegow = MismatchingFeegow(
        slots=[dict(WED_SLOT)], patients=[], procedures=[dict(PROC1)]
    )
    handler = _build(feegow, tmp_path)
    db_path = tmp_path / "state" / "appointments.sqlite3"

    with caplog.at_level(logging.DEBUG):
        _new_patient_create(handler, "bad")
        response = handler.handle(event("CONFIRMAR", message_id="bad-11"))

    assert "reconciliar" in response.lower()
    assert_no_pii(caplog.text)
    assert_no_pii(response)
    assert_no_pii(_durable_text(db_path))
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM operations WHERE kind = 'CREATE_PATIENT'"
        ).fetchone()[0] == "RECONCILE_REQUIRED"


def test_mutation_exceptions_never_carry_patient_data(tmp_path):
    """The exception text itself is an observable surface."""

    class ExplodingFeegow(FakeFeegow):
        def create_patient(self, **payload):
            self.calls.append(("create_patient", payload))
            raise RuntimeError("upstream failed")

    feegow = ExplodingFeegow(
        slots=[dict(WED_SLOT)], patients=[], procedures=[dict(PROC1)]
    )
    handler = _build(feegow, tmp_path)
    store = AppointmentStore(tmp_path / "state" / "appointments.sqlite3")
    _new_patient_create(handler, "exc")
    data = dict(store.load_flow(CHAT_KEY).data)

    with pytest.raises(RuntimeError) as excinfo:
        handler._execute_authorized(store, CHAT_KEY, data)

    assert_no_pii(str(excinfo.value))
    assert_no_pii(repr(excinfo.value))


def test_audit_metadata_only_accepts_the_operational_allow_list(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    store = AppointmentStore(db_path)

    store.audit(
        "operation_succeeded",
        operation_id="op-1",
        now=NOW,
        metadata={
            "kind": "CREATE_APPOINTMENT",
            "procedure_id": 1,
            "state": "SUCCEEDED",
            "status_id": 1,
            "cpf": CPF,
            "nome": PATIENT_NAME,
            "email": PATIENT_EMAIL,
            "telefone": PATIENT_PHONE,
            "data_nascimento": BIRTH_DATE,
            "private_path": "/tmp/" + PROOF_FILENAME,
        },
    )

    with sqlite3.connect(db_path) as connection:
        metadata = connection.execute(
            "SELECT metadata_json FROM audit_log"
        ).fetchone()[0]

    assert_no_pii(metadata, extra=(PATIENT_PHONE,))
    assert "CREATE_APPOINTMENT" in metadata
    assert "SUCCEEDED" in metadata


# --------------------------------------------------------------------------
# Payment proof handling: private copy, opaque names, opaque outbox bodies
# --------------------------------------------------------------------------


def _reservation_ready(tmp_path, clock, *, reception_chat_id=""):
    feegow = FakeFeegow(patients=[known_patient()])
    handler = _build(
        feegow, tmp_path, clock, reception_chat_id=reception_chat_id
    )
    db_path = tmp_path / "state" / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    store.record_response(
        "seed-1",
        CHAT_KEY,
        "reserva criada",
        FlowState.AGUARDANDO_COMPROVANTE.value,
        {"appointment_id": 901, "procedure_id": 3},
        now=clock.value,
    )
    store.create_reservation(
        901,
        CHAT_KEY,
        deadline_at=clock.value + timedelta(hours=4),
        reminder_at=clock.value + timedelta(hours=3),
    )
    return handler, store, db_path


def _proof_file(tmp_path):
    source = tmp_path / "incoming"
    source.mkdir(exist_ok=True)
    path = source / PROOF_FILENAME
    path.write_bytes(PROOF_BYTES)
    return path


def test_payment_proof_is_stored_privately_without_name_bytes_or_path_leaks(
    tmp_path, caplog
):
    clock = MutableClock(NOW)
    reception = "5571900000000@s.whatsapp.net"
    handler, store, db_path = _reservation_ready(
        tmp_path, clock, reception_chat_id=reception
    )
    proof = _proof_file(tmp_path)

    with caplog.at_level(logging.DEBUG):
        response = handler.handle(
            event(
                "",
                message_id="proof-1",
                media_urls=(str(proof),),
                media_types=("image/jpeg",),
                timestamp=clock.value,
            )
        )

    assert "Comprovante recebido" in response
    assert_no_pii(response)
    assert_no_pii(caplog.text)
    assert_no_pii(_durable_text(db_path), extra=(str(proof),))

    with sqlite3.connect(db_path) as connection:
        private_path, sha256 = connection.execute(
            "SELECT private_path, sha256 FROM payment_proofs"
        ).fetchone()
    stored = tmp_path / "proofs" / sha256
    assert str(stored) == private_path
    assert stored.read_bytes() == PROOF_BYTES
    # The private copy is named by digest only, never by the sender's name.
    assert PROOF_FILENAME not in private_path

    outbox_bodies = [row["body"] for row in handler.claim_outbox(worker_id="w-1")]
    assert outbox_bodies and all("901" in body for body in outbox_bodies)
    for body in outbox_bodies:
        assert_no_pii(body, extra=(str(proof), PATIENT_PHONE))


def test_proof_directory_and_files_are_private(tmp_path):
    clock = MutableClock(NOW)
    handler, _store, db_path = _reservation_ready(tmp_path, clock)
    proof = _proof_file(tmp_path)

    handler.handle(
        event(
            "",
            message_id="perm-1",
            media_urls=(str(proof),),
            media_types=("image/jpeg",),
            timestamp=clock.value,
        )
    )

    proofs_dir = tmp_path / "proofs"
    assert stat.S_IMODE(proofs_dir.stat().st_mode) == 0o700
    stored = list(proofs_dir.iterdir())
    assert len(stored) == 1
    assert stat.S_IMODE(stored[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700


def test_duplicate_proof_is_idempotent_and_writes_one_copy(tmp_path):
    clock = MutableClock(NOW)
    handler, _store, db_path = _reservation_ready(tmp_path, clock)
    proof = _proof_file(tmp_path)

    first = handler.handle(
        event(
            "",
            message_id="dup-1",
            media_urls=(str(proof),),
            media_types=("image/jpeg",),
            timestamp=clock.value,
        )
    )
    second = handler.handle(
        event(
            "",
            message_id="dup-2",
            media_urls=(str(proof),),
            media_types=("image/jpeg",),
            timestamp=clock.value,
        )
    )

    assert "Comprovante recebido" in first
    assert "Comprovante recebido" in second
    assert len(list((tmp_path / "proofs").iterdir())) == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payment_proofs").fetchone()[0] == 1


def test_database_is_created_private_even_on_a_permissive_umask(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    AppointmentStore(db_path)

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


def test_inactive_flows_are_purged_after_thirty_days(tmp_path):
    clock = MutableClock(NOW)
    handler = _build(FakeFeegow(), tmp_path, clock)
    db_path = tmp_path / "state" / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    store.record_response(
        "old-1", "5571900000001@s.whatsapp.net", "antigo", "AWAITING_CPF",
        {"cpf": CPF}, now=NOW - timedelta(days=31),
    )
    store.record_response(
        "recent-1", "5571900000002@s.whatsapp.net", "recente", "AWAITING_CPF",
        {"cpf": CPF}, now=NOW - timedelta(days=29),
    )

    result = handler.run_retention_cleanup(worker_id="ret-1")

    assert result["flows_purged"] == 1
    with sqlite3.connect(db_path) as connection:
        remaining = [
            row[0] for row in connection.execute("SELECT chat_key FROM flow_states")
        ]
    assert remaining == ["5571900000002@s.whatsapp.net"]


def test_proofs_are_purged_after_one_hundred_eighty_days(tmp_path):
    clock = MutableClock(NOW)
    handler = _build(FakeFeegow(), tmp_path, clock)
    db_path = tmp_path / "state" / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    old_file = tmp_path / "proofs" / "old"
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_bytes(PROOF_BYTES)
    store.create_reservation(
        902, CHAT_KEY,
        deadline_at=NOW - timedelta(days=180),
        reminder_at=NOW - timedelta(days=181),
    )
    store.accept_payment_proof(
        message_id="old-proof",
        appointment_id=902,
        chat_key=CHAT_KEY,
        received_at=NOW - timedelta(days=181),
        sha256="b" * 64,
        private_path=str(old_file),
    )

    result = handler.run_retention_cleanup(worker_id="ret-2")

    assert result["proofs_purged"] == 1
    assert not old_file.exists()
    assert store.count("payment_proofs") == 0


def test_a_reception_exception_preserves_its_proof_past_retention(tmp_path):
    clock = MutableClock(NOW)
    handler = _build(FakeFeegow(), tmp_path, clock)
    db_path = tmp_path / "state" / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    held = tmp_path / "proofs" / "held"
    held.parent.mkdir(parents=True, exist_ok=True)
    held.write_bytes(PROOF_BYTES)
    store.create_reservation(
        903, CHAT_KEY,
        deadline_at=NOW - timedelta(days=180),
        reminder_at=NOW - timedelta(days=181),
    )
    store.accept_payment_proof(
        message_id="held-proof",
        appointment_id=903,
        chat_key=CHAT_KEY,
        received_at=NOW - timedelta(days=181),
        sha256="c" * 64,
        private_path=str(held),
    )
    store.set_reservation_state(
        903, FlowState.EXCECAO_RECEPCAO.value, require_no_proof=False
    )

    result = handler.run_retention_cleanup(worker_id="ret-3")

    assert result["proofs_purged"] == 0
    assert held.exists()
    assert store.count("payment_proofs") == 1


def test_retention_never_creates_a_database_for_an_unused_deployment(tmp_path):
    handler = _build(FakeFeegow(), tmp_path, MutableClock(NOW))

    assert handler.run_retention_cleanup(worker_id="ret-4") == {
        "flows_purged": 0,
        "proofs_purged": 0,
    }
    assert not (tmp_path / "state" / "appointments.sqlite3").exists()


def test_a_disabled_deployment_runs_no_retention_at_all(tmp_path):
    clock = MutableClock(NOW)
    enabled = _build(FakeFeegow(), tmp_path, clock)
    db_path = tmp_path / "state" / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    store.record_response(
        "old-1", CHAT_KEY, "antigo", "AWAITING_CPF", {},
        now=NOW - timedelta(days=400),
    )
    disabled = _build(FakeFeegow(), tmp_path, clock, enabled=False)

    assert disabled.run_retention_cleanup(worker_id="ret-5") == {
        "flows_purged": 0,
        "proofs_purged": 0,
    }
    assert store.count("flow_states") == 1
    assert enabled.run_retention_cleanup(worker_id="ret-6")["flows_purged"] == 1


def test_a_held_retention_lease_blocks_a_second_worker(tmp_path):
    clock = MutableClock(NOW)
    handler = _build(FakeFeegow(), tmp_path, clock)
    db_path = tmp_path / "state" / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    store.record_response(
        "old-1", CHAT_KEY, "antigo", "AWAITING_CPF", {},
        now=NOW - timedelta(days=400),
    )
    assert store.acquire_watcher_lease(
        "appointment-retention-cleanup", "other-worker", now=NOW, lease_seconds=3600
    )

    assert handler.run_retention_cleanup(worker_id="ret-7") == {
        "flows_purged": 0,
        "proofs_purged": 0,
    }
    assert store.count("flow_states") == 1

    store.release_watcher_lease("appointment-retention-cleanup", "other-worker")
    assert handler.run_retention_cleanup(worker_id="ret-8")["flows_purged"] == 1


def test_concurrent_retention_workers_purge_each_row_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    clock = MutableClock(NOW)
    db_path = tmp_path / "state" / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    for index in range(20):
        store.record_response(
            f"old-{index}",
            f"557190000{index:04d}@s.whatsapp.net",
            "antigo",
            "AWAITING_CPF",
            {},
            now=NOW - timedelta(days=400),
        )
    handlers = [_build(FakeFeegow(), tmp_path, clock) for _ in range(4)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda pair: pair[1].run_retention_cleanup(worker_id=f"w-{pair[0]}"),
                enumerate(handlers),
            )
        )

    assert sum(result["flows_purged"] for result in results) == 20
    assert store.count("flow_states") == 0
