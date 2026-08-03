"""Fail-closed contract for current Feegow read/write operations."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests

from gateway.platforms.feegow_api import (
    FeegowAmbiguousResultError,
    FeegowClient,
    FeegowConflictError,
    FeegowValidationError,
    FeegowWriteDisabledError,
)


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        (
            "create_appointment",
            dict(
                paciente_id=10,
                profissional_id=20,
                unidade_id=30,
                especialidade_id=40,
                procedimento_id=1,
                data="05-08-2026",
                horario="14:00",
            ),
        ),
        ("edit_patient", dict(paciente_id=10, telefone="71999999999")),
        ("update_appointment_status", dict(appointment_id=50, status_id=1)),
        ("cancel_appointment", dict(appointment_id=50, motivo_id=3)),
        (
            "reschedule_appointment",
            dict(appointment_id=50, data="06-08-2026", horario="15:00"),
        ),
    ],
)
def test_all_mutators_are_blocked_without_explicit_write_enable(method, kwargs):
    client = FeegowClient(token="test-token")
    client.session.request = Mock()

    with pytest.raises(FeegowWriteDisabledError):
        getattr(client, method)(**kwargs)

    client.session.request.assert_not_called()


def test_status_7_is_unconditionally_forbidden_even_with_writes_enabled():
    client = FeegowClient(token="test-token", write_enabled=True)
    client.session.request = Mock()

    with pytest.raises(FeegowValidationError, match="status 7"):
        client.update_appointment_status(appointment_id=50, status_id=7)

    client.session.request.assert_not_called()


def test_create_appointment_uses_current_endpoint_and_safe_initial_status(monkeypatch):
    client = FeegowClient(token="test-token", write_enabled=True)
    seen = {}

    def fake_request(method, endpoint, **kwargs):
        seen.update(method=method, endpoint=endpoint, **kwargs)
        return {"success": True, "content": {"agendamento_id": 99}}

    monkeypatch.setattr(client, "_request", fake_request)
    result = client.create_appointment(
        paciente_id=10,
        profissional_id=20,
        unidade_id=30,
        especialidade_id=40,
        procedimento_id=1,
        data="05-08-2026",
        horario="14:00",
    )

    assert result["content"]["agendamento_id"] == 99
    assert seen["method"] == "POST"
    assert seen["endpoint"] == "appoints/new-appoint"
    assert seen["json_data"]["status_id"] == 1
    assert seen["json_data"]["local_id"] == 30
    assert "unidade_id" not in seen["json_data"]
    assert seen["max_attempts"] == 1
    assert seen["ambiguous_on_transport"] is True


def test_mutating_timeout_is_not_retried_and_is_reported_ambiguous():
    client = FeegowClient(token="test-token", write_enabled=True)
    client.session.request = Mock(side_effect=requests.Timeout())

    with pytest.raises(FeegowAmbiguousResultError):
        client.cancel_appointment(appointment_id=50, motivo_id=3)

    assert client.session.request.call_count == 1


def test_http_409_has_distinct_conflict_type():
    client = FeegowClient(token="test-token")
    response = Mock(status_code=409, text="conflict")
    response.json.return_value = {"message": "already exists"}
    client.session.request = Mock(return_value=response)

    with pytest.raises(FeegowConflictError):
        client._request("GET", "appoints/search")
