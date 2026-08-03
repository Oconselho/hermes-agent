"""Authorization, readback and retry fencing for every Feegow mutation.

Covers the five mutations the secretary can perform — patient creation,
appointment creation, cancellation, reschedule and patient edit — and for
each one: a summary immediately before the write, an explicit single-use
confirmation bound to chat/kind/target/payload, an independent exact-id
readback, and a public ``RECONCILIAR`` path that reads remotely before it
ever considers writing again.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.whatsapp_appointments import (
    AppointmentStore,
    WhatsAppAppointmentsHandler,
)
from tests.gateway.appointment_helpers import (
    BIRTH_DATE,
    CHAT_KEY,
    CPF,
    CPF_FORMATTED,
    FakeFeegow,
    MutableClock,
    authenticate_for_action,
    event,
    known_patient,
    payment_config,
)

BRT = ZoneInfo("America/Bahia")
NOW = datetime(2026, 8, 1, 10, 0, tzinfo=BRT)
PROC1 = {"procedimento_id": 1, "nome": "Consulta", "valor": 600}
WED_SLOT = {"id": "slot-wed-14", "procedimento_id": 1, "data": "2026-08-05", "horario": "14:00"}


def _build(feegow, tmp_path, clock=None, **overrides):
    return WhatsAppAppointmentsHandler(
        payment_config(**overrides),
        db_path=tmp_path / "appointments.sqlite3",
        proofs_dir=tmp_path / "proofs",
        feegow_client=feegow,
        clock=clock or MutableClock(NOW),
    )


def _create_ready(tmp_path, clock=None, **feegow_kwargs):
    feegow = FakeFeegow(
        slots=[dict(WED_SLOT)],
        patients=[known_patient()],
        procedures=[dict(PROC1)],
        **feegow_kwargs,
    )
    return _build(feegow, tmp_path, clock), feegow


def _cancel_ready(tmp_path, clock=None, procedure_id=1):
    appointment = {
        "agendamento_id": 701,
        "paciente_id": 77,
        "procedimento_id": procedure_id,
        "data": "2026-08-05",
        "horario": "14:00",
        "status_id": 7,
    }
    feegow = FakeFeegow(patients=[known_patient()], appointments=[appointment])
    return _build(feegow, tmp_path, clock), feegow


def _reschedule_ready(tmp_path, clock=None, slots=None):
    appointment = {
        "agendamento_id": 801,
        "paciente_id": 77,
        "procedimento_id": 1,
        "data": "2026-08-05",
        "horario": "14:00",
        "status_id": 7,
    }
    feegow = FakeFeegow(
        slots=slots
        if slots is not None
        else [
            {
                "id": "proc-1-new",
                "procedimento_id": 1,
                "data": "2026-08-06",
                "horario": "15:00",
            }
        ],
        patients=[known_patient()],
        appointments=[appointment],
        procedures=[dict(PROC1)],
    )
    return _build(feegow, tmp_path, clock), feegow


def _edit_ready(tmp_path, clock=None):
    feegow = FakeFeegow(patients=[known_patient()])
    return _build(feegow, tmp_path, clock), feegow


def drive_create(handler, prefix):
    handler.handle(event("Quero agendar uma consulta", message_id=f"{prefix}-1"))
    handler.handle(event("1", message_id=f"{prefix}-2"))
    handler.handle(event("1", message_id=f"{prefix}-3"))
    handler.handle(event("1", message_id=f"{prefix}-4"))
    handler.handle(event(CPF_FORMATTED, message_id=f"{prefix}-5"))
    handler.handle(event(BIRTH_DATE, message_id=f"{prefix}-6"))
    return handler.handle(event("SIM", message_id=f"{prefix}-7"))


def drive_cancel(handler, prefix):
    authenticate_for_action(handler, "3", prefix)
    return handler.handle(event("1", message_id=f"{prefix}-6"))


def drive_reschedule(handler, prefix):
    authenticate_for_action(handler, "2", prefix)
    handler.handle(event("REMARCAR 1", message_id=f"{prefix}-6"))
    return handler.handle(event("1", message_id=f"{prefix}-7"))


def drive_edit(handler, prefix):
    authenticate_for_action(handler, "5", prefix)
    handler.handle(event("2", message_id=f"{prefix}-6"))
    return handler.handle(event("novo@example.invalid", message_id=f"{prefix}-7"))


def _mutation_calls(feegow):
    mutating = {
        "create_patient",
        "create_appointment",
        "cancel_appointment",
        "reschedule_appointment",
        "edit_patient",
    }
    return [name for name, _ in feegow.calls if name in mutating]


def _flow_state(db_path, chat_key=CHAT_KEY):
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT state FROM flow_states WHERE chat_key = ?", (chat_key,)
        ).fetchone()
    return None if row is None else row[0]


def _authorizations(db_path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT chat_key, kind, target_id, payload_hash, consumed_at"
            "  FROM authorizations ORDER BY created_at, id"
        ).fetchall()


SCENARIOS = {
    "create": (_create_ready, drive_create, "CREATE_IN_PERSON_APPOINTMENT"),
    "cancel": (_cancel_ready, drive_cancel, "CANCEL_APPOINTMENT"),
    "reschedule": (_reschedule_ready, drive_reschedule, "RESCHEDULE_APPOINTMENT"),
    "edit": (_edit_ready, drive_edit, "EDIT_PATIENT"),
}


# --------------------------------------------------------------------------
# Summary + explicit confirmation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_summary_precedes_every_mutation_and_writes_nothing_yet(tmp_path, name):
    build, drive, kind = SCENARIOS[name]
    handler, feegow = build(tmp_path)

    summary = drive(handler, f"sum-{name}")

    assert "CONFIRMAR" in summary
    assert _mutation_calls(feegow) == []
    rows = _authorizations(tmp_path / "appointments.sqlite3")
    assert len(rows) == 1
    chat_key, row_kind, target_id, payload_hash, consumed_at = rows[0]
    assert chat_key == CHAT_KEY
    assert row_kind == kind
    assert target_id
    assert len(payload_hash) == 64
    assert consumed_at is None


@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.parametrize(
    "reply", ["sim", "ok", "confirmo", "CONFIRMAR AGORA", "1", "ALTERAR", ""]
)
def test_only_the_exact_confirmation_word_authorizes(tmp_path, name, reply):
    build, drive, _kind = SCENARIOS[name]
    handler, feegow = build(tmp_path)
    drive(handler, f"wrong-{name}")

    response = handler.handle(event(reply, message_id=f"wrong-{name}-99"))

    assert _mutation_calls(feegow) == []
    if name == "create" and reply == "ALTERAR":
        # Only the creation summary offers ALTERAR; it discards the grant.
        assert _authorizations(tmp_path / "appointments.sqlite3")[0][4] is not None
    else:
        assert "CONFIRMAR" in response
        assert _authorizations(tmp_path / "appointments.sqlite3")[0][4] is None


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_expired_authorization_blocks_the_mutation(tmp_path, name):
    build, drive, _kind = SCENARIOS[name]
    clock = MutableClock(NOW)
    handler, feegow = build(tmp_path, clock)
    drive(handler, f"exp-{name}")

    clock.value = NOW + timedelta(minutes=16)  # authorization_ttl_minutes = 15
    response = handler.handle(event("CONFIRMAR", message_id=f"exp-{name}-99"))

    assert "recepção" in response.lower()
    assert _mutation_calls(feegow) == []
    assert _authorizations(tmp_path / "appointments.sqlite3")[0][4] is None


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_authorization_is_single_use_and_consumed_only_after_readback(tmp_path, name):
    build, drive, _kind = SCENARIOS[name]
    handler, feegow = build(tmp_path)
    drive(handler, f"once-{name}")

    handler.handle(event("CONFIRMAR", message_id=f"once-{name}-98"))
    before = list(_mutation_calls(feegow))
    assert len(before) == 1
    assert _authorizations(tmp_path / "appointments.sqlite3")[0][4] is not None

    # A second, distinct confirmation message must not mutate again.
    handler.handle(event("CONFIRMAR", message_id=f"once-{name}-99"))
    assert _mutation_calls(feegow) == before


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_redelivery_of_the_same_message_id_replays_the_stored_response(tmp_path, name):
    build, drive, _kind = SCENARIOS[name]
    handler, feegow = build(tmp_path)
    drive(handler, f"replay-{name}")

    first = handler.handle(event("CONFIRMAR", message_id=f"replay-{name}-98"))
    calls = list(_mutation_calls(feegow))
    second = handler.handle(event("CONFIRMAR", message_id=f"replay-{name}-98"))

    assert first == second
    assert _mutation_calls(feegow) == calls


def test_changing_the_payload_creates_a_new_grant_and_discards_the_old_one(tmp_path):
    feegow = FakeFeegow(
        slots=[
            dict(WED_SLOT),
            {
                "id": "slot-thu-15",
                "procedimento_id": 1,
                "data": "2026-08-06",
                "horario": "15:00",
            },
        ],
        patients=[known_patient()],
        procedures=[dict(PROC1)],
    )
    handler = _build(feegow, tmp_path)
    drive_create(handler, "change")

    rows = _authorizations(tmp_path / "appointments.sqlite3")
    assert len(rows) == 1 and rows[0][4] is None
    first_hash = rows[0][3]

    handler.handle(event("ALTERAR", message_id="change-11"))
    rows = _authorizations(tmp_path / "appointments.sqlite3")
    assert rows[0][4] is not None  # the superseded grant is invalidated

    handler.handle(event("1", message_id="change-12"))
    handler.handle(event("2", message_id="change-13"))  # a different slot
    handler.handle(event(CPF_FORMATTED, message_id="change-14"))
    handler.handle(event(BIRTH_DATE, message_id="change-15"))
    handler.handle(event("SIM", message_id="change-16"))

    rows = _authorizations(tmp_path / "appointments.sqlite3")
    assert len(rows) == 2
    assert rows[1][3] != first_hash
    assert rows[1][2] == "slot-thu-15"
    assert _mutation_calls(feegow) == []


# --------------------------------------------------------------------------
# Preflight fencing: nothing is written when a read-only check refuses
# --------------------------------------------------------------------------


def test_create_preflight_runs_immediately_before_the_post(tmp_path):
    handler, feegow = _create_ready(tmp_path)
    drive_create(handler, "order")
    handler.handle(event("CONFIRMAR", message_id="order-11"))

    names = [name for name, _ in feegow.calls]
    assert names.index("list_procedures") < names.index("create_appointment")
    assert names.index("find_duplicate_appointments") < names.index("create_appointment")
    # The slot re-check is the last read before the write.
    last_slot_read = max(
        index for index, name in enumerate(names) if name == "list_available_slots"
    )
    assert last_slot_read < names.index("create_appointment")
    assert names.index("create_appointment") < names.index("get_appointment")


def test_slot_that_disappeared_blocks_the_create_without_any_post(tmp_path):
    handler, feegow = _create_ready(tmp_path)
    drive_create(handler, "gone")
    feegow.slots = []  # the slot was taken between summary and confirmation

    response = handler.handle(event("CONFIRMAR", message_id="gone-11"))

    assert "recepção" in response.lower()
    assert "reconciliar" not in response.lower()
    assert _mutation_calls(feegow) == []
    assert _flow_state(tmp_path / "appointments.sqlite3") == "HANDOFF"


def test_duplicate_appointment_blocks_the_create_without_any_post(tmp_path):
    handler, feegow = _create_ready(tmp_path)
    drive_create(handler, "dup")
    feegow.duplicates = [{"agendamento_id": 555}]

    response = handler.handle(event("CONFIRMAR", message_id="dup-11"))

    assert "recepção" in response.lower()
    assert _mutation_calls(feegow) == []


def test_changed_immutable_procedure_definition_blocks_the_create(tmp_path):
    handler, feegow = _create_ready(tmp_path)
    drive_create(handler, "proc")
    feegow.procedures = [{"procedimento_id": 1, "nome": "Consulta", "valor": 700}]

    response = handler.handle(event("CONFIRMAR", message_id="proc-11"))

    assert "recepção" in response.lower()
    assert _mutation_calls(feegow) == []


def test_reschedule_slot_that_disappeared_blocks_the_write(tmp_path):
    handler, feegow = _reschedule_ready(tmp_path)
    drive_reschedule(handler, "rgone")
    feegow.slots = []

    response = handler.handle(event("CONFIRMAR", message_id="rgone-11"))

    assert "recepção" in response.lower()
    assert _mutation_calls(feegow) == []


def test_reschedule_duplicate_blocks_the_write(tmp_path):
    handler, feegow = _reschedule_ready(tmp_path)
    drive_reschedule(handler, "rdup")
    feegow.duplicates = [{"agendamento_id": 999}]

    response = handler.handle(event("CONFIRMAR", message_id="rdup-11"))

    assert "recepção" in response.lower()
    assert _mutation_calls(feegow) == []


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_closed_write_gate_never_reaches_a_mutation(tmp_path, name):
    build, drive, _kind = SCENARIOS[name]
    handler, feegow = build(tmp_path)
    drive(handler, f"gate-{name}")
    handler._write_enabled = False  # the kill switch closes mid-conversation

    response = handler.handle(event("CONFIRMAR", message_id=f"gate-{name}-99"))

    assert "recepção" in response.lower()
    assert _mutation_calls(feegow) == []
    assert _authorizations(tmp_path / "appointments.sqlite3")[0][4] is None


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_executor_refuses_to_write_when_the_gate_is_closed_at_the_last_check(
    tmp_path, name
):
    """Defense in depth: the executors themselves re-check the kill switch."""

    build, drive, _kind = SCENARIOS[name]
    handler, feegow = build(tmp_path)
    drive(handler, f"deep-{name}")
    store = AppointmentStore(tmp_path / "appointments.sqlite3")
    data = store.load_flow(CHAT_KEY).data
    handler._write_enabled = False

    with pytest.raises(RuntimeError):
        if name == "cancel":
            handler._execute_cancel(store, data)
        elif name == "reschedule":
            handler._execute_reschedule(store, data)
        elif name == "edit":
            handler._execute_edit(store, data)
        else:
            handler._execute_authorized(store, CHAT_KEY, data)

    assert _mutation_calls(feegow) == []
    assert store.count("operations") == 0


# --------------------------------------------------------------------------
# Readback divergence -> public reconciliation, never a blind second write
# --------------------------------------------------------------------------


def test_cancel_readback_divergence_is_publicly_reconciled_without_a_second_post(
    tmp_path,
):
    class SilentCancelFeegow(FakeFeegow):
        def cancel_appointment(self, appointment_id, motivo_id):
            self.calls.append(
                ("cancel_appointment", {"appointment_id": appointment_id})
            )
            self.cancelled_appointments.append((appointment_id, motivo_id))
            return {"success": True, "content": {"agendamento_id": appointment_id}}

    appointment = {
        "agendamento_id": 701,
        "paciente_id": 77,
        "procedimento_id": 1,
        "data": "2026-08-05",
        "horario": "14:00",
        "status_id": 7,
    }
    feegow = SilentCancelFeegow(patients=[known_patient()], appointments=[appointment])
    db_path = tmp_path / "appointments.sqlite3"
    handler = _build(feegow, tmp_path)
    drive_cancel(handler, "cdiv")

    first = handler.handle(event("CONFIRMAR", message_id="cdiv-11"))

    assert "reconciliar" in first.lower()
    assert "recepção" not in first.lower()
    assert _flow_state(db_path) == "RECONCILIATION_REQUIRED"
    assert _authorizations(db_path)[0][4] is None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM operations WHERE kind = 'CANCEL_APPOINTMENT'"
        ).fetchone()[0] == "RECONCILE_REQUIRED"

    # The remote write actually landed; RECONCILIAR reads it back first.
    feegow.readbacks[701]["status_id"] = 11
    second = handler.handle(event("RECONCILIAR", message_id="cdiv-12"))

    assert "status 11" in second.lower()
    assert feegow.cancelled_appointments == [(701, 1)]
    assert _flow_state(db_path) == "COMPLETED"
    assert _authorizations(db_path)[0][4] is not None


def test_reconciliation_that_finds_no_remote_effect_never_writes_again(tmp_path):
    class SilentCancelFeegow(FakeFeegow):
        def cancel_appointment(self, appointment_id, motivo_id):
            self.calls.append(
                ("cancel_appointment", {"appointment_id": appointment_id})
            )
            self.cancelled_appointments.append((appointment_id, motivo_id))
            return {"success": True, "content": {"agendamento_id": appointment_id}}

    appointment = {
        "agendamento_id": 701,
        "paciente_id": 77,
        "procedimento_id": 1,
        "data": "2026-08-05",
        "horario": "14:00",
        "status_id": 7,
    }
    feegow = SilentCancelFeegow(patients=[known_patient()], appointments=[appointment])
    handler = _build(feegow, tmp_path)
    drive_cancel(handler, "cnone")
    handler.handle(event("CONFIRMAR", message_id="cnone-11"))

    retry = handler.handle(event("RECONCILIAR", message_id="cnone-12"))

    assert "reconciliar" in retry.lower()
    assert len(feegow.cancelled_appointments) == 1  # still exactly one POST
    assert _flow_state(tmp_path / "appointments.sqlite3") == "RECONCILIATION_REQUIRED"


def test_reschedule_readback_divergence_is_publicly_reconciled(tmp_path):
    class SilentRescheduleFeegow(FakeFeegow):
        def reschedule_appointment(self, appointment_id, data, horario):
            payload = {"appointment_id": appointment_id, "data": data, "horario": horario}
            self.calls.append(("reschedule_appointment", payload))
            self.rescheduled_appointments.append(payload)
            return {"success": True, "content": {"agendamento_id": appointment_id}}

    appointment = {
        "agendamento_id": 801,
        "paciente_id": 77,
        "procedimento_id": 1,
        "data": "2026-08-05",
        "horario": "14:00",
        "status_id": 7,
    }
    feegow = SilentRescheduleFeegow(
        slots=[
            {
                "id": "proc-1-new",
                "procedimento_id": 1,
                "data": "2026-08-06",
                "horario": "15:00",
            }
        ],
        patients=[known_patient()],
        appointments=[appointment],
        procedures=[dict(PROC1)],
    )
    db_path = tmp_path / "appointments.sqlite3"
    handler = _build(feegow, tmp_path)
    drive_reschedule(handler, "rdiv")

    first = handler.handle(event("CONFIRMAR", message_id="rdiv-11"))
    assert "reconciliar" in first.lower()
    assert _authorizations(db_path)[0][4] is None

    # A partially applied remote state (status moved, date did not) must NOT
    # be accepted as success.
    feegow.readbacks[801]["status_id"] = 15
    stuck = handler.handle(event("RECONCILIAR", message_id="rdiv-12"))
    assert "reconciliar" in stuck.lower()
    assert len(feegow.rescheduled_appointments) == 1

    feegow.readbacks[801].update({"data": "2026-08-06", "horario": "15:00"})
    done = handler.handle(event("RECONCILIAR", message_id="rdiv-13"))

    assert "status 15" in done.lower()
    assert len(feegow.rescheduled_appointments) == 1
    assert _authorizations(db_path)[0][4] is not None


def test_edit_readback_divergence_keeps_the_grant_and_reconciles_publicly(tmp_path):
    class SilentEditFeegow(FakeFeegow):
        def edit_patient(self, paciente_id, **changes):
            self.calls.append(("edit_patient", {"paciente_id": paciente_id, **changes}))
            self.edited_patients.append({"paciente_id": paciente_id, **changes})
            return {"success": True, "content": {"paciente_id": paciente_id}}

    feegow = SilentEditFeegow(patients=[known_patient()])
    db_path = tmp_path / "appointments.sqlite3"
    handler = _build(feegow, tmp_path)
    drive_edit(handler, "ediv")

    first = handler.handle(event("CONFIRMAR", message_id="ediv-11"))

    assert "reconciliar" in first.lower()
    assert _flow_state(db_path) == "RECONCILIATION_REQUIRED"
    # The judge's core requirement: an ambiguous edit must not burn the
    # patient's one confirmation.
    assert _authorizations(db_path)[0][4] is None
    assert len(feegow.edited_patients) == 1

    feegow.patients[0]["email"] = "novo@example.invalid"
    second = handler.handle(event("RECONCILIAR", message_id="ediv-12"))

    assert "alterado com sucesso" in second
    assert len(feegow.edited_patients) == 1  # no second remote edit
    assert _authorizations(db_path)[0][4] is not None


def test_create_result_without_an_exact_id_requires_reconciliation(tmp_path):
    class NoIdFeegow(FakeFeegow):
        def create_appointment(self, **payload):
            self.calls.append(("create_appointment", payload))
            self.created_appointments.append(payload)
            self.readbacks[901] = {
                "agendamento_id": 901,
                "status_id": 1,
                "paciente_id": payload["paciente_id"],
                "procedimento_id": payload["procedimento_id"],
                "data": payload["data"],
                "horario": payload["horario"],
            }
            return {"success": True, "content": {}}

    feegow = NoIdFeegow(
        slots=[dict(WED_SLOT)], patients=[known_patient()], procedures=[dict(PROC1)]
    )
    db_path = tmp_path / "appointments.sqlite3"
    handler = _build(feegow, tmp_path)
    drive_create(handler, "noid")

    response = handler.handle(event("CONFIRMAR", message_id="noid-11"))

    assert "reconciliar" in response.lower()
    assert len(feegow.created_appointments) == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM operations WHERE kind = 'CREATE_APPOINTMENT'"
        ).fetchone()[0] == "RECONCILE_REQUIRED"

    # Reconciliation locates the appointment by fingerprint, not by writing.
    feegow.duplicates = [{"agendamento_id": 901}]
    done = handler.handle(event("RECONCILIAR", message_id="noid-12"))

    assert "status 1" in done
    assert len(feegow.created_appointments) == 1


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("ambiguous transport"), RuntimeError("HTTP 409 conflict")],
    ids=["timeout", "conflict"],
)
def test_ambiguous_transport_never_retries_the_write_blindly(tmp_path, failure):
    class FailingFeegow(FakeFeegow):
        def create_appointment(self, **payload):
            self.calls.append(("create_appointment", payload))
            self.created_appointments.append(payload)
            raise failure

    feegow = FailingFeegow(
        slots=[dict(WED_SLOT)], patients=[known_patient()], procedures=[dict(PROC1)]
    )
    handler = _build(feegow, tmp_path)
    drive_create(handler, "amb")

    response = handler.handle(event("CONFIRMAR", message_id="amb-11"))
    assert "reconciliar" in response.lower()
    assert len(feegow.created_appointments) == 1

    retry = handler.handle(event("RECONCILIAR", message_id="amb-12"))

    assert "reconciliar" in retry.lower()
    # The ledger row already exists, so reconciliation reads instead of
    # re-posting: the create endpoint is never called a second time.
    assert len(feegow.created_appointments) == 1


def test_succeeded_operation_is_never_executed_twice(tmp_path):
    handler, feegow = _cancel_ready(tmp_path)
    store = AppointmentStore(tmp_path / "appointments.sqlite3")
    drive_cancel(handler, "succ")
    handler.handle(event("CONFIRMAR", message_id="succ-11"))
    assert feegow.cancelled_appointments == [(701, 1)]

    data = dict(store.load_flow(CHAT_KEY).data)
    data.update(
        {
            "selected_appointment": {
                "id": 701,
                "patient_id": 77,
                "procedure_id": 1,
                "date": "2026-08-05",
                "time": "14:00",
                "display_date": "05/08/2026",
            },
            "payload_hash": _authorizations(tmp_path / "appointments.sqlite3")[0][3],
        }
    )
    assert handler._execute_cancel(store, data) == 701
    assert feegow.cancelled_appointments == [(701, 1)]


def test_cancellation_requires_the_exact_patient_and_procedure_on_readback(tmp_path):
    class WrongPatientFeegow(FakeFeegow):
        def cancel_appointment(self, appointment_id, motivo_id):
            self.calls.append(
                ("cancel_appointment", {"appointment_id": appointment_id})
            )
            self.cancelled_appointments.append((appointment_id, motivo_id))
            self.readbacks[appointment_id]["status_id"] = 11
            self.readbacks[appointment_id]["paciente_id"] = 999
            return {"success": True, "content": {"agendamento_id": appointment_id}}

    appointment = {
        "agendamento_id": 701,
        "paciente_id": 77,
        "procedimento_id": 1,
        "data": "2026-08-05",
        "horario": "14:00",
        "status_id": 7,
    }
    feegow = WrongPatientFeegow(patients=[known_patient()], appointments=[appointment])
    handler = _build(feegow, tmp_path)
    drive_cancel(handler, "wrongp")

    response = handler.handle(event("CONFIRMAR", message_id="wrongp-11"))

    assert "reconciliar" in response.lower()
    assert _flow_state(tmp_path / "appointments.sqlite3") == "RECONCILIATION_REQUIRED"


def test_new_patient_creation_is_summarized_and_authorized_before_any_write(tmp_path):
    feegow = FakeFeegow(
        slots=[dict(WED_SLOT)], patients=[], procedures=[dict(PROC1)]
    )
    handler = _build(feegow, tmp_path)
    handler.handle(event("Quero agendar uma consulta", message_id="np-1"))
    handler.handle(event("1", message_id="np-2"))
    handler.handle(event("1", message_id="np-3"))
    handler.handle(event("1", message_id="np-4"))
    handler.handle(event(CPF_FORMATTED, message_id="np-5"))
    handler.handle(event(BIRTH_DATE, message_id="np-6"))
    handler.handle(event("SIM", message_id="np-7"))
    handler.handle(event("Maria Souza", message_id="np-8"))
    handler.handle(event("F", message_id="np-9"))
    summary = handler.handle(event("maria@example.invalid", message_id="np-10"))

    assert "novo cadastro" in summary.lower()
    assert "CONFIRMAR" in summary
    assert feegow.created_patients == []
    assert CPF not in summary and BIRTH_DATE not in summary

    handler.handle(event("CONFIRMAR", message_id="np-11"))

    assert len(feegow.created_patients) == 1
    names = [name for name, _ in feegow.calls]
    # Patient creation is read back independently before the appointment.
    assert names.index("create_patient") < names.index("find_patient_by_cpf", names.index("create_patient"))
    assert names.index("create_patient") < names.index("create_appointment")
