"""Slot reading against the shape Feegow actually returns.

The pre-existing fixtures for this flow used a flat, invented row —
``{"id": "slot-wed-14", "procedimento_id": 1, "data": ..., "horario": ...}`` —
that the API never produces. The parser was green against a contract that did
not exist, while in production every booking died on an empty agenda.

``REAL_PAYLOAD`` below is the verbatim body returned by
``GET /v1/api/appoints/available-schedule`` for the clinic's own account on
2026-08-11 (procedure 1, professional 1, specialty 1, local 1, tipo=P),
captured read-only.
"""

from __future__ import annotations

import pytest

from gateway.platforms.feegow_api import FeegowClient
from gateway.platforms.whatsapp_appointments import filter_eligible_slots


REAL_PAYLOAD = {
    "success": True,
    "content": {
        "profissional_id": {
            "1": {
                "local_id": {
                    "1": {
                        "2026-08-12": [
                            "14:00:00", "14:30:00", "15:00:00", "15:30:00",
                            "16:00:00", "16:30:00", "17:00:00",
                        ],
                        "2026-08-13": [
                            "14:00:00", "14:30:00", "15:00:00", "15:30:00",
                        ],
                        "2026-08-15": [
                            "09:00:00", "09:30:00", "10:00:00", "10:30:00",
                            "11:00:00",
                        ],
                        "2026-08-19": ["14:30:00", "15:00:00", "15:30:00"],
                        "2026-08-20": [
                            "14:00:00", "14:30:00", "15:00:00", "15:30:00",
                            "16:00:00", "16:30:00", "17:00:00",
                        ],
                        "2026-08-26": [],
                    }
                },
                "age_restriction": {"age_from": 1, "age_to": 120},
            }
        }
    },
    "total": 1,
}

# 2026-08-12 and 2026-08-19 are Wednesdays, 2026-08-13 and 2026-08-20 are
# Thursdays, and 2026-08-15 is a Saturday.


def _normalize(payload, procedure_id=1, professional_id=1, local_id=1):
    return FeegowClient._normalize_available_schedule(
        payload,
        procedure_id=procedure_id,
        professional_id=professional_id,
        local_id=local_id,
    )


def test_real_payload_flattens_into_one_row_per_free_time():
    slots = _normalize(REAL_PAYLOAD)

    # 7 + 4 + 5 + 3 + 7 free times; the empty 2026-08-26 contributes nothing.
    assert len(slots) == 26
    assert slots[0] == {
        "data": "2026-08-12",
        "horario": "14:00",
        "procedimento_id": 1,
        "profissional_id": 1,
        "local_id": 1,
    }
    assert all(len(slot["horario"]) == 5 for slot in slots)
    assert {slot["data"] for slot in slots} == {
        "2026-08-12", "2026-08-13", "2026-08-15", "2026-08-19", "2026-08-20",
    }


def test_age_restriction_sibling_is_not_read_as_a_day():
    slots = _normalize(REAL_PAYLOAD)
    assert all(slot["data"].startswith("2026-08-") for slot in slots)


def test_only_the_requested_professional_and_local_are_offered():
    payload = {
        "success": True,
        "content": {
            "profissional_id": {
                "1": {"local_id": {"1": {"2026-08-12": ["14:00:00"]}}},
                "2": {"local_id": {"1": {"2026-08-12": ["09:00:00"]}}},
            }
        },
    }
    slots = _normalize(payload, professional_id=1)
    assert [slot["horario"] for slot in slots] == ["14:00"]

    other_local = {
        "success": True,
        "content": {
            "profissional_id": {
                "1": {"local_id": {"9": {"2026-08-12": ["14:00:00"]}}}
            }
        },
    }
    assert _normalize(other_local, local_id=1) == []


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        [],
        {},
        {"success": False, "content": {}},
        {"error": True},
        {"success": True, "content": {}},
        {"success": True, "content": {"profissional_id": []}},
        {"success": True, "content": {"profissional_id": {"1": {}}}},
        {"success": True, "content": {"profissional_id": {"1": {"local_id": "x"}}}},
    ],
)
def test_unusable_payloads_fail_closed_to_no_vacancy(payload):
    assert _normalize(payload) == []


def test_bad_day_and_time_entries_are_skipped_not_fatal():
    payload = {
        "success": True,
        "content": {
            "profissional_id": {
                "1": {
                    "local_id": {
                        "1": {
                            "nao-e-data": ["14:00:00"],
                            "2026-08-12": ["14:00:00", "hora errada", None, "15:00"],
                            "2026-08-13": "nao e lista",
                        }
                    }
                }
            }
        },
    }
    slots = _normalize(payload)
    assert [(slot["data"], slot["horario"]) for slot in slots] == [
        ("2026-08-12", "14:00"),
        ("2026-08-12", "15:00"),
    ]


def test_flat_list_shape_still_passes_through():
    """The legacy flat shape used by existing fixtures must keep working."""
    flat = [{"id": "slot-wed-14", "procedimento_id": 1, "data": "2026-08-12", "horario": "14:00"}]
    assert _normalize(flat) == flat
    assert _normalize({"success": True, "content": flat}) == flat


def test_filter_eligible_slots_reads_the_normalized_real_payload():
    """The regression that mattered: 200 OK used to still yield zero slots."""
    in_person = filter_eligible_slots(_normalize(REAL_PAYLOAD, procedure_id=1), 1)
    assert in_person, "in-person booking must offer real vacancies"
    # Policy: Wednesdays/Thursdays, 14:00-18:00 BRT.
    assert [(slot["date"], slot["time"]) for slot in in_person] == [
        ("2026-08-12", "14:00"),
        ("2026-08-12", "14:30"),
        ("2026-08-12", "15:00"),
    ]

    tele = filter_eligible_slots(_normalize(REAL_PAYLOAD, procedure_id=3), 3)
    assert tele, "teleconsultation booking must offer real vacancies"
    # Policy: real agenda, Saturday morning first.
    assert [(slot["date"], slot["time"]) for slot in tele] == [
        ("2026-08-15", "09:00"),
        ("2026-08-15", "09:30"),
        ("2026-08-15", "10:00"),
    ]


def test_every_offered_slot_carries_a_stable_id():
    slots = filter_eligible_slots(_normalize(REAL_PAYLOAD, procedure_id=3), 3)
    again = filter_eligible_slots(_normalize(REAL_PAYLOAD, procedure_id=3), 3)
    assert [slot["id"] for slot in slots] == [slot["id"] for slot in again]
    assert all(slot["id"] for slot in slots)


def test_tipo_is_sent_on_the_availability_request():
    """Without ``tipo`` Feegow answers 422 and the agenda silently empties."""
    captured: dict[str, object] = {}

    class _Recorder(FeegowClient):
        def __init__(self):  # bypass session/token setup
            pass

        def _request(self, method, endpoint, params=None, **kwargs):
            captured["method"] = method
            captured["endpoint"] = endpoint
            captured["params"] = params
            return REAL_PAYLOAD

    slots = _Recorder().list_available_slots(
        procedure_id=3,
        professional_id=1,
        specialty_id=1,
        local_id=1,
        start_date="11-08-2026",
        end_date="26-08-2026",
    )

    assert captured["method"] == "GET"
    assert captured["endpoint"] == "appoints/available-schedule"
    assert captured["params"]["tipo"] == "P"
    assert captured["params"]["procedimento_id"] == 3
    assert slots and slots[0]["procedimento_id"] == 3
