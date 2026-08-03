"""Free-return entitlement, cancellation window and no-show matrix.

A free return is only offered when our own ledger links the base
appointment, the base was actually attended (status 3), the 60-day window
is still open and the entitlement was never consumed. Every negative row
must hand off to reception with zero Feegow mutation, zero outbox row and
an untouched ledger.
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
    CHAT_KEY,
    FakeFeegow,
    MutableClock,
    authenticate_for_action,
    event,
    known_patient,
    payment_config,
)

BRT = ZoneInfo("America/Bahia")
NOW = datetime(2026, 8, 5, 10, 0, tzinfo=BRT)  # a Wednesday
BASE_ID = 700
RETURN_PROC = 2


def _handler(tmp_path, feegow, clock, **overrides):
    return WhatsAppAppointmentsHandler(
        payment_config(return_procedure_id=RETURN_PROC, **overrides),
        db_path=tmp_path / "appointments.sqlite3",
        proofs_dir=tmp_path / "proofs",
        feegow_client=feegow,
        clock=clock,
    )


def _feegow(base_status=3, slots=None):
    base = {
        "agendamento_id": BASE_ID,
        "paciente_id": 77,
        "procedimento_id": 9,
        "data": "2026-07-01",
        "horario": "14:00",
        "status_id": base_status,
    }
    return FakeFeegow(
        slots=slots
        if slots is not None
        else [
            {
                "id": "ret-wed",
                "procedimento_id": RETURN_PROC,
                "data": "2026-08-12",
                "horario": "14:00",
            }
        ],
        patients=[known_patient()],
        appointments=[base],
        procedures=[{"procedimento_id": RETURN_PROC, "nome": "Retorno", "valor": 0}],
    )


def _seed_ledger(db_path, *, base_date, now, state="OPEN"):
    store = AppointmentStore(db_path)
    store.create_return_ledger_entry(BASE_ID, CHAT_KEY, base_date=base_date, now=now)
    if state != "OPEN":
        store.close_return_ledger(BASE_ID, state=state, now=now)
    return store


def _ledger_state(db_path):
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT state, return_appointment_id, modality FROM returns_ledger"
            " WHERE base_appointment_id = ?",
            (str(BASE_ID),),
        ).fetchone()
    return None if row is None else row


def _mutations(feegow):
    return [
        name
        for name, _ in feegow.calls
        if name in {"create_appointment", "cancel_appointment", "reschedule_appointment"}
    ]


# --------------------------------------------------------------------------
# Positive path
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "choice,modality,slot_date",
    [("1", "presencial", "2026-08-12"), ("2", "tele", "2026-08-08")],
)
def test_attended_base_inside_the_window_grants_one_free_return(
    tmp_path, choice, modality, slot_date
):
    clock = MutableClock(NOW)
    feegow = _feegow(
        slots=[
            {
                "id": f"ret-{modality}",
                "procedimento_id": RETURN_PROC,
                "data": slot_date,
                "horario": "09:00" if modality == "tele" else "14:00",
            }
        ]
    )
    db_path = tmp_path / "appointments.sqlite3"
    handler = _handler(tmp_path, feegow, clock)
    _seed_ledger(db_path, base_date=NOW - timedelta(days=30), now=NOW)

    authenticate_for_action(handler, "4", f"ret-{modality}")
    prompt = handler.handle(event(choice, message_id=f"ret-{modality}-6"))
    assert "dispon" in prompt.lower()
    summary = handler.handle(event("1", message_id=f"ret-{modality}-7"))
    assert "CONFIRMAR" in summary
    assert "R$ 0" in summary
    result = handler.handle(event("CONFIRMAR", message_id=f"ret-{modality}-8"))

    assert "status 1" in result
    assert len(feegow.created_appointments) == 1
    assert feegow.created_appointments[0]["valor"] == 0
    assert feegow.created_appointments[0]["procedimento_id"] == RETURN_PROC
    state, return_id, stored_modality = _ledger_state(db_path)
    assert state == "CONSUMED"
    assert return_id == "901"
    assert stored_modality == modality


def test_teleconsultation_return_may_use_an_exceptional_saturday(tmp_path):
    clock = MutableClock(NOW)
    feegow = _feegow(
        slots=[
            {
                "id": "sat-am",
                "procedimento_id": RETURN_PROC,
                "data": "2026-08-08",  # Saturday
                "horario": "09:00",
            }
        ]
    )
    handler = _handler(tmp_path, feegow, clock)
    _seed_ledger(tmp_path / "appointments.sqlite3", base_date=NOW - timedelta(days=5), now=NOW)

    authenticate_for_action(handler, "4", "sat")
    options = handler.handle(event("2", message_id="sat-6"))

    assert "08/08/2026" in options


def test_in_person_return_never_offers_saturday(tmp_path):
    clock = MutableClock(NOW)
    feegow = _feegow(
        slots=[
            {
                "id": "sat-am",
                "procedimento_id": RETURN_PROC,
                "data": "2026-08-08",
                "horario": "09:00",
            }
        ]
    )
    handler = _handler(tmp_path, feegow, clock)
    _seed_ledger(tmp_path / "appointments.sqlite3", base_date=NOW - timedelta(days=5), now=NOW)

    authenticate_for_action(handler, "4", "nosat")
    response = handler.handle(event("1", message_id="nosat-6"))

    assert "recepção" in response.lower()
    assert _mutations(feegow) == []


def test_exactly_sixty_days_is_still_inside_the_window(tmp_path):
    clock = MutableClock(NOW)
    feegow = _feegow()
    handler = _handler(tmp_path, feegow, clock)
    _seed_ledger(
        tmp_path / "appointments.sqlite3",
        base_date=NOW - timedelta(days=60),
        now=NOW - timedelta(days=60),
    )

    authenticate_for_action(handler, "4", "edge")
    response = handler.handle(event("1", message_id="edge-6"))

    assert "dispon" in response.lower()


# --------------------------------------------------------------------------
# Negative matrix: reception, zero mutation, zero outbox, ledger untouched
# --------------------------------------------------------------------------


def test_base_not_attended_leaves_the_ledger_open_for_reception(tmp_path):
    clock = MutableClock(NOW)
    feegow = _feegow(base_status=1)
    db_path = tmp_path / "appointments.sqlite3"
    handler = _handler(tmp_path, feegow, clock)
    store = _seed_ledger(db_path, base_date=NOW - timedelta(days=5), now=NOW)

    authenticate_for_action(handler, "4", "notatt")

    assert _ledger_state(db_path)[0] == "OPEN"
    assert _mutations(feegow) == []
    assert store.count("outbox_events") == 0


def test_a_configured_no_show_status_voids_the_entitlement(tmp_path):
    clock = MutableClock(NOW)
    feegow = _feegow(base_status=13)
    db_path = tmp_path / "appointments.sqlite3"
    handler = _handler(tmp_path, feegow, clock, return_no_show_status_ids=[12, 13])
    store = _seed_ledger(db_path, base_date=NOW - timedelta(days=5), now=NOW)

    response = authenticate_for_action(handler, "4", "noshow")

    assert "recepção" in response.lower()
    assert _ledger_state(db_path)[0] == "VOID"
    assert _mutations(feegow) == []
    assert store.count("outbox_events") == 0


def test_an_unconfigured_status_is_never_inferred_as_a_no_show(tmp_path):
    clock = MutableClock(NOW)
    feegow = _feegow(base_status=13)
    db_path = tmp_path / "appointments.sqlite3"
    handler = _handler(tmp_path, feegow, clock)  # no no-show ids configured
    _seed_ledger(db_path, base_date=NOW - timedelta(days=5), now=NOW)

    response = authenticate_for_action(handler, "4", "unknown")

    assert "recepção" in response.lower()
    assert _ledger_state(db_path)[0] == "OPEN"


def test_a_legacy_return_without_a_ledger_entry_goes_to_reception(tmp_path):
    clock = MutableClock(NOW)
    feegow = _feegow()
    handler = _handler(tmp_path, feegow, clock)
    AppointmentStore(tmp_path / "appointments.sqlite3")  # no ledger row at all

    response = authenticate_for_action(handler, "4", "legacy")

    assert "recepção" in response.lower()
    assert _mutations(feegow) == []


def test_an_expired_window_closes_the_ledger_and_hands_off(tmp_path):
    clock = MutableClock(NOW)
    feegow = _feegow()
    db_path = tmp_path / "appointments.sqlite3"
    handler = _handler(tmp_path, feegow, clock)
    _seed_ledger(
        db_path,
        base_date=NOW - timedelta(days=61),
        now=NOW - timedelta(days=61),
    )

    response = authenticate_for_action(handler, "4", "expired")

    assert "recepção" in response.lower()
    assert _ledger_state(db_path)[0] == "EXPIRED"
    assert _mutations(feegow) == []


@pytest.mark.parametrize("state", ["CONSUMED", "RESERVED", "VOID", "EXPIRED"])
def test_a_non_open_ledger_never_grants_a_second_return(tmp_path, state):
    clock = MutableClock(NOW)
    feegow = _feegow()
    db_path = tmp_path / "appointments.sqlite3"
    handler = _handler(tmp_path, feegow, clock)
    _seed_ledger(db_path, base_date=NOW - timedelta(days=5), now=NOW, state=state)

    response = authenticate_for_action(handler, "4", f"used-{state}")

    assert "recepção" in response.lower()
    assert _ledger_state(db_path)[0] == state
    assert _mutations(feegow) == []


def test_an_ambiguous_base_readback_never_consumes_the_entitlement(tmp_path):
    class AmbiguousFeegow(FakeFeegow):
        def get_appointment(self, appointment_id):
            self.calls.append(("get_appointment", {"appointment_id": appointment_id}))
            return [dict(self.readbacks[appointment_id]), {"agendamento_id": 999}]

    clock = MutableClock(NOW)
    feegow = AmbiguousFeegow(
        patients=[known_patient()],
        appointments=[
            {
                "agendamento_id": BASE_ID,
                "paciente_id": 77,
                "procedimento_id": 9,
                "data": "2026-07-01",
                "horario": "14:00",
                "status_id": 3,
            }
        ],
    )
    db_path = tmp_path / "appointments.sqlite3"
    handler = _handler(tmp_path, feegow, clock)
    _seed_ledger(db_path, base_date=NOW - timedelta(days=5), now=NOW)

    response = authenticate_for_action(handler, "4", "amb")

    assert "recepção" in response.lower()
    assert _ledger_state(db_path)[0] == "OPEN"


def test_disabling_the_return_procedure_closes_the_feature(tmp_path):
    clock = MutableClock(NOW)
    feegow = _feegow()
    db_path = tmp_path / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        payment_config(return_procedure_id=1),  # collides with a real procedure
        db_path=db_path,
        feegow_client=feegow,
        clock=clock,
    )
    _seed_ledger(db_path, base_date=NOW - timedelta(days=5), now=NOW)

    response = authenticate_for_action(handler, "4", "off")

    assert "recepção" in response.lower()
    assert _ledger_state(db_path)[0] == "OPEN"
    assert _mutations(feegow) == []


# --------------------------------------------------------------------------
# Cancellation window and ledger reopening
# --------------------------------------------------------------------------


def _cancellable(tmp_path, clock, starts_at, appointment_id=701, procedure_id=1):
    appointment = {
        "agendamento_id": appointment_id,
        "paciente_id": 77,
        "procedimento_id": procedure_id,
        "data": starts_at.date().isoformat(),
        "horario": starts_at.strftime("%H:%M"),
        "status_id": 7,
    }
    feegow = FakeFeegow(patients=[known_patient()], appointments=[appointment])
    return _handler(tmp_path, feegow, clock), feegow


def test_cancellation_inside_twenty_four_hours_goes_to_reception(tmp_path):
    clock = MutableClock(NOW)
    handler, feegow = _cancellable(tmp_path, clock, NOW + timedelta(hours=23, minutes=59))

    authenticate_for_action(handler, "3", "late")
    response = handler.handle(event("1", message_id="late-6"))

    assert "recepção" in response.lower()
    assert feegow.cancelled_appointments == []


def test_cancellation_at_exactly_twenty_four_hours_is_allowed(tmp_path):
    clock = MutableClock(NOW)
    handler, feegow = _cancellable(tmp_path, clock, NOW + timedelta(hours=24))

    authenticate_for_action(handler, "3", "edge24")
    summary = handler.handle(event("1", message_id="edge24-6"))
    assert "CONFIRMAR" in summary
    result = handler.handle(event("CONFIRMAR", message_id="edge24-7"))

    assert "status 11" in result.lower()
    assert feegow.cancelled_appointments == [(701, 1)]


def test_cancelling_a_linked_return_reopens_its_base_entitlement(tmp_path):
    clock = MutableClock(NOW)
    db_path = tmp_path / "appointments.sqlite3"
    handler, feegow = _cancellable(
        tmp_path, clock, NOW + timedelta(days=3), appointment_id=901,
        procedure_id=RETURN_PROC,
    )
    store = _seed_ledger(db_path, base_date=NOW - timedelta(days=10), now=NOW)
    assert store.claim_return_ledger(
        BASE_ID, claim_token="op-1", modality="presencial", now=NOW
    )
    assert store.consume_return_ledger(
        BASE_ID, 901, claim_token="op-1", modality="presencial", now=NOW
    )

    authenticate_for_action(handler, "3", "linked")
    handler.handle(event("1", message_id="linked-6"))
    result = handler.handle(event("CONFIRMAR", message_id="linked-7"))

    assert "status 11" in result.lower()
    state, return_id, modality = _ledger_state(db_path)
    assert state == "OPEN"
    assert return_id is None and modality is None


def test_cancelling_a_return_outside_the_window_expires_instead_of_reopening(tmp_path):
    clock = MutableClock(NOW)
    db_path = tmp_path / "appointments.sqlite3"
    handler, feegow = _cancellable(
        tmp_path, clock, NOW + timedelta(days=3), appointment_id=901,
        procedure_id=RETURN_PROC,
    )
    store = _seed_ledger(
        db_path, base_date=NOW - timedelta(days=61), now=NOW - timedelta(days=61)
    )
    assert store.claim_return_ledger(
        BASE_ID, claim_token="op-1", modality="presencial", now=NOW
    )
    assert store.consume_return_ledger(
        BASE_ID, 901, claim_token="op-1", modality="presencial", now=NOW
    )

    authenticate_for_action(handler, "3", "expcancel")
    handler.handle(event("1", message_id="expcancel-6"))
    handler.handle(event("CONFIRMAR", message_id="expcancel-7"))

    assert _ledger_state(db_path)[0] == "EXPIRED"


def test_cancelling_an_unrelated_appointment_never_touches_the_ledger(tmp_path):
    clock = MutableClock(NOW)
    db_path = tmp_path / "appointments.sqlite3"
    handler, feegow = _cancellable(tmp_path, clock, NOW + timedelta(days=3))
    _seed_ledger(db_path, base_date=NOW - timedelta(days=5), now=NOW)

    authenticate_for_action(handler, "3", "other")
    handler.handle(event("1", message_id="other-6"))
    handler.handle(event("CONFIRMAR", message_id="other-7"))

    assert _ledger_state(db_path)[0] == "OPEN"


def test_rescheduling_an_unlinked_legacy_return_id_fails_closed(tmp_path):
    """The configurable return id alone cannot select a modality policy."""

    clock = MutableClock(NOW)
    handler, feegow = _cancellable(
        tmp_path, clock, NOW + timedelta(days=3), appointment_id=901,
        procedure_id=RETURN_PROC,
    )
    feegow.slots = [
        {
            "id": "ret-wed",
            "procedimento_id": RETURN_PROC,
            "data": "2026-08-12",
            "horario": "14:00",
        }
    ]

    authenticate_for_action(handler, "2", "legacymove")
    response = handler.handle(event("REMARCAR 1", message_id="legacymove-6"))

    assert "recepção" in response.lower()
    assert feegow.rescheduled_appointments == []


def test_only_one_concurrent_writer_can_claim_the_same_entitlement(tmp_path):
    db_path = tmp_path / "appointments.sqlite3"
    store = _seed_ledger(db_path, base_date=NOW - timedelta(days=5), now=NOW)

    first = store.claim_return_ledger(
        BASE_ID, claim_token="op-a", modality="presencial", now=NOW
    )
    second = store.claim_return_ledger(
        BASE_ID, claim_token="op-b", modality="tele", now=NOW
    )
    replay = store.claim_return_ledger(
        BASE_ID, claim_token="op-a", modality="presencial", now=NOW
    )

    assert first is True
    assert second is False  # a different operation cannot steal the claim
    assert replay is True  # the same operation may retry its own claim
    assert not store.consume_return_ledger(
        BASE_ID, 902, claim_token="op-b", modality="tele", now=NOW
    )
    assert store.consume_return_ledger(
        BASE_ID, 901, claim_token="op-a", modality="presencial", now=NOW
    )
    assert _ledger_state(db_path)[0] == "CONSUMED"
