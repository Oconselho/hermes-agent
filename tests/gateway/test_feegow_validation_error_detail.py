"""A 422 from Feegow must say which field it rejected.

Feegow reports validation failures under the field's own key and sends no
``message`` key at all. Reading only ``message`` logged a bare
"Parâmetros inválidos ou faltando:" with nothing after the colon — which is
how a missing mandatory parameter stayed invisible while every booking in
production failed.

The bodies below are the verbatim 422 responses from
``appoints/available-schedule`` on the clinic's own account.
"""

from __future__ import annotations

import pytest

from gateway.platforms.feegow_api import (
    FeegowClient,
    FeegowValidationError,
)


class _Response:
    def __init__(self, payload, status_code=422):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise ValueError("not json")
        return self._payload


class _Session:
    def __init__(self, response):
        self._response = response
        self.headers = {}

    def request(self, **kwargs):
        return self._response


def _client(payload):
    client = FeegowClient.__new__(FeegowClient)
    client.base_url = "https://api.feegow.invalid/v1/api"
    client.timeout = 5
    client.session = _Session(_Response(payload))
    return client


# Verbatim, captured 2026-08-11.
MISSING_TIPO = {"tipo": ["O campo tipo é obrigatório."]}
INVALID_TIPO = {
    "tipo": [
        "O campo tipo não pode ser superior a 1 caracteres.",
        "O campo tipo selecionado é inválido.",
    ]
}


def test_missing_mandatory_field_is_named_in_the_error():
    with pytest.raises(FeegowValidationError) as excinfo:
        _client(MISSING_TIPO)._request("GET", "appoints/available-schedule")

    text = str(excinfo.value)
    assert "tipo" in text
    assert "obrigatório" in text
    # The bug: the message used to end at the colon.
    assert not text.rstrip().endswith(":")


def test_every_reason_for_a_field_is_kept():
    with pytest.raises(FeegowValidationError) as excinfo:
        _client(INVALID_TIPO)._request("GET", "appoints/available-schedule")

    text = str(excinfo.value)
    assert "1 caracteres" in text
    assert "inválido" in text


def test_an_explicit_message_key_still_wins():
    body = {"message": "Mensagem da API", "tipo": ["irrelevante"]}
    with pytest.raises(FeegowValidationError) as excinfo:
        _client(body)._request("GET", "appoints/available-schedule")

    assert "Mensagem da API" in str(excinfo.value)


def test_the_error_names_fields_and_not_the_values_we_sent():
    """No PII: a 422 on a booking call must not echo patient data.

    The detail is built from the RESPONSE body, never from the request
    params, so identifiers we sent cannot appear even when Feegow rejects
    the very field that carried them.
    """
    body = {"cpf": ["O campo cpf é inválido."]}
    with pytest.raises(FeegowValidationError) as excinfo:
        _client(body)._request(
            "GET",
            "patient/search",
            params={"cpf": "12345678901", "telefone": "71999999999"},
        )

    text = str(excinfo.value)
    assert "cpf" in text
    assert "12345678901" not in text
    assert "71999999999" not in text


def test_detail_is_bounded():
    body = {f"campo_{index}": ["erro"] * 20 for index in range(40)}
    with pytest.raises(FeegowValidationError) as excinfo:
        _client(body)._request("GET", "appoints/available-schedule")

    # At most 5 fields are named, so a pathological body cannot flood the log.
    text = str(excinfo.value)
    assert text.count("campo_") <= 5


@pytest.mark.parametrize("body", ["texto solto", [], 42])
def test_non_dict_bodies_do_not_crash_the_error_path(body):
    with pytest.raises(FeegowValidationError):
        _client(body)._request("GET", "appoints/available-schedule")
