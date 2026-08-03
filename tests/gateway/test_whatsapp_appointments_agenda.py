"""Agenda policy matrix for in-person and teleconsultation slots.

In-person procedures 1 and 9 are restricted to Wednesday/Thursday between
14:00 and 18:00 BRT inclusive. Teleconsultation (procedure 3) uses the real
Feegow schedule with Saturday-morning priority. Nothing is ever fabricated,
at most three options are offered, and an empty result must hand off to
reception without any mutation.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.whatsapp_appointments import (
    WhatsAppAppointmentsHandler,
    filter_eligible_slots,
)
from tests.gateway.appointment_helpers import (
    BIRTH_DATE,
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

# 2026-08-03 Mon, 08-04 Tue, 08-05 Wed, 08-06 Thu, 08-07 Fri, 08-08 Sat, 08-09 Sun
WEDNESDAY = "2026-08-05"
THURSDAY = "2026-08-06"


def slot(slot_id, date_text, time_text, **extra):
    payload = {"id": slot_id, "data": date_text, "horario": time_text}
    payload.update(extra)
    return payload


@pytest.mark.parametrize("procedure_id", [1, 9])
@pytest.mark.parametrize("date_text", [WEDNESDAY, THURSDAY])
@pytest.mark.parametrize("time_text", ["14:00", "15:30", "18:00"])
def test_in_person_boundaries_are_inclusive_on_wednesday_and_thursday(
    procedure_id, date_text, time_text
):
    eligible = filter_eligible_slots(
        [slot("s-1", date_text, time_text)], procedure_id
    )

    assert [item["date"] for item in eligible] == [date_text]
    assert [item["time"] for item in eligible] == [time_text]


@pytest.mark.parametrize("procedure_id", [1, 9])
@pytest.mark.parametrize(
    "date_text,label",
    [
        ("2026-08-03", "monday"),
        ("2026-08-04", "tuesday"),
        ("2026-08-07", "friday"),
        ("2026-08-08", "saturday"),
        ("2026-08-09", "sunday"),
    ],
)
def test_in_person_rejects_every_day_other_than_wednesday_and_thursday(
    procedure_id, date_text, label
):
    assert filter_eligible_slots([slot("s-1", date_text, "15:00")], procedure_id) == []


@pytest.mark.parametrize("procedure_id", [1, 9])
@pytest.mark.parametrize(
    "time_text", ["07:00", "13:00", "13:59", "18:01", "19:00", "23:30"]
)
def test_in_person_rejects_times_outside_the_window(procedure_id, time_text):
    assert filter_eligible_slots([slot("s-1", WEDNESDAY, time_text)], procedure_id) == []


def test_mixed_batch_keeps_only_policy_eligible_slots_in_order():
    eligible = filter_eligible_slots(
        [
            slot("sat", "2026-08-08", "09:00"),
            slot("thu-late", THURSDAY, "18:30"),
            slot("wed-ok", WEDNESDAY, "16:00"),
            slot("tue", "2026-08-04", "14:00"),
            slot("thu-ok", THURSDAY, "14:00"),
            slot("fri", "2026-08-07", "15:00"),
        ],
        1,
    )

    assert [item["id"] for item in eligible] == ["wed-ok", "thu-ok"]


def test_at_most_three_options_are_ever_offered():
    eligible = filter_eligible_slots(
        [
            slot(f"s-{index}", WEDNESDAY, f"1{index}:00")
            for index in range(4, 9)  # 14:00..18:00
        ],
        1,
    )

    assert len(eligible) == 3
    assert [item["time"] for item in eligible] == ["14:00", "15:00", "16:00"]


def test_limit_above_three_is_still_capped_at_three():
    slots = [slot(f"s-{index}", WEDNESDAY, f"1{index}:00") for index in range(4, 9)]

    assert len(filter_eligible_slots(slots, 1, limit=10)) == 3


@pytest.mark.parametrize(
    "raw,expected_time,accepted",
    [
        # America/Bahia is UTC-3 year round.
        ("2026-08-05T17:00:00Z", "14:00", True),
        ("2026-08-05T21:00:00Z", "18:00", True),
        ("2026-08-05T16:59:00Z", None, False),
        ("2026-08-05T21:01:00Z", None, False),
        ("2026-08-05T13:00:00Z", None, False),
    ],
)
def test_utc_instants_are_converted_to_brt_before_the_policy_check(
    raw, expected_time, accepted
):
    eligible = filter_eligible_slots([{"id": "s-1", "datetime": raw}], 1)

    if accepted:
        assert [item["time"] for item in eligible] == [expected_time]
    else:
        assert eligible == []


@pytest.mark.parametrize(
    "raw,expected_date,expected_time",
    [
        # 01:00Z on Saturday is really Friday 22:00 in Bahia: the date has to
        # be converted with the time, or the patient is told the wrong day.
        ("2026-08-08T01:00:00Z", "2026-08-07", "22:00"),
        ("2026-08-08T02:30:00+00:00", "2026-08-07", "23:30"),
        # The reverse direction: 23:00 BRT on Saturday is Sunday 02:00 UTC.
        ("2026-08-09T02:00:00Z", "2026-08-08", "23:00"),
        ("2026-08-08T12:00:00Z", "2026-08-08", "09:00"),
    ],
)
def test_a_utc_instant_that_crosses_midnight_keeps_date_and_time_together(
    raw, expected_date, expected_time
):
    eligible = filter_eligible_slots([{"id": "s-1", "datetime": raw}], 3)

    assert [(item["date"], item["time"]) for item in eligible] == [
        (expected_date, expected_time)
    ]


def test_an_explicit_date_field_still_wins_over_the_instant():
    eligible = filter_eligible_slots(
        [{"id": "s-1", "data": WEDNESDAY, "horario": "14:00"}], 1
    )

    assert [(item["date"], item["time"]) for item in eligible] == [(WEDNESDAY, "14:00")]


def test_saturday_priority_uses_the_converted_local_day():
    """A Saturday-morning slot expressed in UTC must still sort first."""

    eligible = filter_eligible_slots(
        [
            {"id": "wed", "datetime": "2026-08-05T12:00:00Z"},
            {"id": "sat-am", "datetime": "2026-08-08T12:00:00Z"},  # 09:00 BRT Sat
        ],
        3,
    )

    assert [item["id"] for item in eligible] == ["sat-am", "wed"]


def test_explicit_offset_is_also_normalized_to_brt():
    eligible = filter_eligible_slots(
        [{"id": "s-1", "datetime": "2026-08-05T19:00:00+02:00"}], 1
    )

    assert [item["time"] for item in eligible] == ["14:00"]


def test_unavailable_flags_are_never_offered():
    for flag in ("available", "disponivel", "livre"):
        assert filter_eligible_slots(
            [slot("s-1", WEDNESDAY, "14:00", **{flag: False})], 1
        ) == []


def test_slots_for_another_procedure_are_discarded():
    eligible = filter_eligible_slots(
        [
            slot("other", WEDNESDAY, "14:00", procedimento_id=3),
            slot("mine", WEDNESDAY, "15:00", procedimento_id=1),
        ],
        1,
    )

    assert [item["id"] for item in eligible] == ["mine"]


def test_unparseable_and_empty_inputs_never_fabricate_a_slot():
    assert filter_eligible_slots(None, 1) == []
    assert filter_eligible_slots([], 1) == []
    assert filter_eligible_slots([{"id": "s-1"}], 1) == []
    assert filter_eligible_slots([slot("s-1", "not-a-date", "14:00")], 1) == []
    assert filter_eligible_slots([slot("s-1", WEDNESDAY, "not-a-time")], 1) == []
    assert filter_eligible_slots(["nonsense"], 1) == []


def test_unknown_procedure_without_modality_yields_nothing():
    assert filter_eligible_slots([slot("s-1", WEDNESDAY, "14:00")], 2) == []
    assert filter_eligible_slots([slot("s-1", WEDNESDAY, "14:00")], 2, modality="x") == []


def test_configured_return_procedure_follows_the_requested_modality():
    slots = [slot("s-1", WEDNESDAY, "14:00"), slot("s-2", "2026-08-08", "09:00")]

    presencial = filter_eligible_slots(slots, 2, modality="presencial")
    tele = filter_eligible_slots(slots, 2, modality="tele")

    assert [item["id"] for item in presencial] == ["s-1"]
    assert {item["id"] for item in tele} == {"s-1", "s-2"}


# --------------------------------------------------------------------------
# Teleconsultation: real schedule, Saturday-morning priority
# --------------------------------------------------------------------------


def test_teleconsultation_accepts_the_real_schedule_feegow_returns():
    eligible = filter_eligible_slots(
        [
            slot("mon", "2026-08-03", "09:00"),
            slot("fri", "2026-08-07", "20:00"),
        ],
        3,
    )

    assert {item["id"] for item in eligible} == {"mon", "fri"}


def test_teleconsultation_gives_saturday_morning_priority_without_inventing_slots():
    eligible = filter_eligible_slots(
        [
            slot("wed", WEDNESDAY, "14:00"),
            slot("sat-pm", "2026-08-08", "15:00"),
            slot("sat-am", "2026-08-08", "09:00"),
        ],
        3,
    )

    assert [item["id"] for item in eligible] == ["sat-am", "wed", "sat-pm"]


def test_teleconsultation_without_saturday_keeps_chronological_order():
    eligible = filter_eligible_slots(
        [
            slot("later", THURSDAY, "10:00"),
            slot("sooner", WEDNESDAY, "09:00"),
        ],
        3,
    )

    assert [item["id"] for item in eligible] == ["sooner", "later"]


def test_teleconsultation_saturday_priority_never_exceeds_three():
    eligible = filter_eligible_slots(
        [slot(f"sat-{index}", "2026-08-08", f"0{index}:00") for index in range(6, 9)]
        + [slot("wed", WEDNESDAY, "14:00")],
        3,
    )

    assert len(eligible) == 3
    assert all(item["date"] == "2026-08-08" for item in eligible)


# --------------------------------------------------------------------------
# Handler integration: no eligible slot means reception, never a mutation
# --------------------------------------------------------------------------


def _handler(feegow, tmp_path, procedure_price=600):
    return WhatsAppAppointmentsHandler(
        payment_config(),
        db_path=tmp_path / "appointments.sqlite3",
        proofs_dir=tmp_path / "proofs",
        feegow_client=feegow,
        clock=MutableClock(NOW),
    )


@pytest.mark.parametrize("service,label", [("1", "proc-1"), ("2", "proc-9")])
def test_saturday_only_agenda_hands_off_without_any_mutation(tmp_path, service, label):
    """The documented R$800 limitation: Saturday-only supply means reception."""

    feegow = FakeFeegow(
        slots=[slot("sat-1", "2026-08-08", "09:00"), slot("sat-2", "2026-08-15", "10:00")],
        patients=[known_patient()],
        procedures=[
            {"procedimento_id": 1, "nome": "Consulta", "valor": 600},
            {"procedimento_id": 9, "nome": "Consulta com retorno", "valor": 800},
        ],
    )
    handler = _handler(feegow, tmp_path)

    handler.handle(event("Quero agendar uma consulta", message_id=f"{label}-1"))
    handler.handle(event("1", message_id=f"{label}-2"))
    response = handler.handle(event(service, message_id=f"{label}-3"))

    assert "recepção" in response.lower()
    assert feegow.created_appointments == []
    assert feegow.created_patients == []


def test_available_slots_queries_a_bounded_brt_window(tmp_path):
    feegow = FakeFeegow(slots=[slot("wed", WEDNESDAY, "14:00")])
    handler = WhatsAppAppointmentsHandler(
        payment_config(slot_search_days=15),
        db_path=tmp_path / "appointments.sqlite3",
        feegow_client=feegow,
        clock=MutableClock(NOW),
    )

    eligible = handler.available_slots(1)

    assert [item["id"] for item in eligible] == ["wed"]
    (_name, filters), = [call for call in feegow.calls if call[0] == "list_available_slots"]
    assert filters["start_date"] == "01-08-2026"
    assert filters["end_date"] == "16-08-2026"
    assert filters["procedure_id"] == 1


def test_available_slots_falls_back_to_the_appointment_reader(tmp_path):
    class SlotlessFeegow(FakeFeegow):
        list_available_slots = None

    feegow = SlotlessFeegow(appointments=[])
    feegow.appointments = [slot("wed", WEDNESDAY, "14:00", procedimento_id=1)]
    handler = WhatsAppAppointmentsHandler(
        payment_config(),
        db_path=tmp_path / "appointments.sqlite3",
        feegow_client=feegow,
        clock=MutableClock(NOW),
    )

    eligible = handler.available_slots(1)

    assert [item["id"] for item in eligible] == ["wed"]
    assert [name for name, _ in feegow.calls] == ["search_appointments"]


def test_no_slot_reader_at_all_is_a_hard_failure_not_a_fabrication(tmp_path):
    handler = WhatsAppAppointmentsHandler(
        payment_config(),
        db_path=tmp_path / "appointments.sqlite3",
        feegow_client=object(),
        clock=MutableClock(NOW),
    )

    with pytest.raises(RuntimeError):
        handler.available_slots(1)


def test_slot_menu_offers_exactly_the_filtered_options(tmp_path):
    feegow = FakeFeegow(
        slots=[
            slot("wed-14", WEDNESDAY, "14:00"),
            slot("sat", "2026-08-08", "09:00"),
            slot("thu-18", THURSDAY, "18:00"),
        ],
        patients=[known_patient()],
        procedures=[{"procedimento_id": 1, "nome": "Consulta", "valor": 600}],
    )
    handler = _handler(feegow, tmp_path)

    handler.handle(event("Quero agendar uma consulta", message_id="menu-1"))
    handler.handle(event("1", message_id="menu-2"))
    options = handler.handle(event("1", message_id="menu-3"))

    assert "05/08/2026 às 14:00" in options
    assert "06/08/2026 às 18:00" in options
    assert "08/08/2026" not in options
    assert "Brasília" in options


def test_out_of_range_slot_choice_does_not_start_a_mutation(tmp_path):
    feegow = FakeFeegow(
        slots=[slot("wed-14", WEDNESDAY, "14:00")],
        patients=[known_patient()],
        procedures=[{"procedimento_id": 1, "nome": "Consulta", "valor": 600}],
    )
    handler = _handler(feegow, tmp_path)
    handler.handle(event("Quero agendar uma consulta", message_id="oob-1"))
    handler.handle(event("1", message_id="oob-2"))
    handler.handle(event("1", message_id="oob-3"))

    for reply, message_id in (("0", "oob-4"), ("9", "oob-5"), ("abc", "oob-6")):
        response = handler.handle(event(reply, message_id=message_id))
        assert "número" in response.lower()

    assert feegow.created_appointments == []


def test_zero_eligible_slots_never_reaches_the_identity_step(tmp_path):
    feegow = FakeFeegow(
        slots=[],
        patients=[known_patient()],
        procedures=[{"procedimento_id": 1, "nome": "Consulta", "valor": 600}],
    )
    handler = _handler(feegow, tmp_path)
    handler.handle(event("Quero agendar uma consulta", message_id="none-1"))
    handler.handle(event("1", message_id="none-2"))
    response = handler.handle(event("1", message_id="none-3"))

    assert "recepção" in response.lower()
    # The flow never asked for a CPF, so no identity data was collected.
    follow_up = handler.handle(event(CPF_FORMATTED, message_id="none-4"))
    assert "recepção" in follow_up.lower()
    assert BIRTH_DATE not in follow_up
    assert [name for name, _ in feegow.calls] == ["list_available_slots"]


def test_a_write_disabled_deployment_never_reads_the_agenda(tmp_path):
    feegow = NoCallFeegow()
    handler = WhatsAppAppointmentsHandler(
        payment_config(write_enabled=False),
        db_path=tmp_path / "appointments.sqlite3",
        feegow_client=feegow,
        clock=MutableClock(NOW),
    )
    handler.handle(event("Quero agendar uma consulta", message_id="ro-1"))
    handler.handle(event("1", message_id="ro-2"))
    response = handler.handle(event("1", message_id="ro-3"))

    assert "recepção" in response.lower()
    assert feegow.calls == []
