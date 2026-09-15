"""A Feegow responde "não encontrei" com 409. Isso é vazio, não erro.

Medido contra a API de produção em 15/set/2026:

    CPF que existe      -> 200, um registro
    CPF que não existe  -> 409, {"success": false,
                                 "content": "Paciente não encontrado"}

``find_patient_by_cpf`` lê em modo estrito porque lista vazia é uma DECISÃO —
"cadastrar paciente novo". Como o vazio chegava como exceção, todo paciente
ainda não cadastrado morria no passo da data de nascimento e era mandado para
a recepção. João Pedro Neiva (71 98362-0139), 15/set 12:03 BRT.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.feegow_api import (
    FeegowAuthError,
    FeegowClient,
    FeegowConflictError,
)
from gateway.platforms.whatsapp_appointments import WhatsAppAppointmentsHandler

from tests.gateway.appointment_helpers import MutableClock, event

BRT = ZoneInfo("America/Bahia")


def _conflito(content):
    return FeegowConflictError(
        "Conflito informado pela API Feegow.",
        status_code=409,
        response_body={"success": False, "content": content},
    )


def test_409_de_paciente_nao_encontrado_e_busca_vazia(monkeypatch):
    client = FeegowClient(token="test-token")

    def request(method, endpoint, **kwargs):
        raise _conflito("Paciente não encontrado")

    monkeypatch.setattr(client, "_request", request)

    assert client.search_patients(cpf="00000000191", strict=True) == []
    assert client.find_patient_by_cpf("000.000.001-91") == []


def test_o_mesmo_vale_sem_acento(monkeypatch):
    """Acento é a única variação que o reconhecimento cobre — e de propósito.

    A grafia medida em produção é uma só: "Paciente não encontrado". Alargar
    para toda forma imaginável de dizer "não achei" ("nenhum registro", "sem
    resultados") seria adivinhar — e adivinhar errado aqui faz o funil ler um
    cadastro ilegível como "paciente não existe" e criar um segundo registro
    para quem a Feegow já tem. Se a API mudar a frase, o fluxo volta a falhar
    fechado na recepção, que é o lado seguro do erro.
    """

    client = FeegowClient(token="test-token")

    for corpo in ("Paciente nao encontrado", "PACIENTE NÃO ENCONTRADO"):
        def request(method, endpoint, _corpo=corpo, **kwargs):
            raise _conflito(_corpo)

        monkeypatch.setattr(client, "_request", request)
        assert client.search_patients(cpf="00000000191", strict=True) == []


def test_conflito_de_verdade_continua_subindo(monkeypatch):
    """Falhar fechado continua sendo o certo para conflito que É conflito.

    Se qualquer 409 virasse lista vazia, um cadastro ilegível seria lido como
    "paciente não existe" e o funil criaria um segundo registro para alguém que
    a Feegow já tem — exatamente o defeito de 13/ago/2026.
    """

    client = FeegowClient(token="test-token")

    def request(method, endpoint, **kwargs):
        raise _conflito("Registro duplicado para este CPF")

    monkeypatch.setattr(client, "_request", request)

    with pytest.raises(FeegowConflictError):
        client.search_patients(cpf="52998224725", strict=True)
    with pytest.raises(FeegowConflictError):
        client.find_patient_by_cpf("529.982.247-25")


def test_409_sem_corpo_legivel_continua_subindo(monkeypatch):
    client = FeegowClient(token="test-token")

    for corpo in (None, "texto solto", {"success": True, "content": "não encontrado"}):
        def request(method, endpoint, _corpo=corpo, **kwargs):
            raise FeegowConflictError(
                "Conflito informado pela API Feegow.",
                status_code=409,
                response_body=_corpo,
            )

        monkeypatch.setattr(client, "_request", request)
        with pytest.raises(FeegowConflictError):
            client.search_patients(cpf="52998224725", strict=True)


def test_outros_erros_nao_viram_vazio(monkeypatch):
    """Token sem permissão não é "paciente não existe"."""

    client = FeegowClient(token="test-token")

    def request(method, endpoint, **kwargs):
        raise FeegowAuthError(
            "Token inválido, expirado ou sem permissão.",
            status_code=403,
            response_body={"success": False, "content": "Paciente não encontrado"},
        )

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(FeegowAuthError):
        client.search_patients(cpf="52998224725", strict=True)


def test_busca_que_acha_continua_devolvendo_o_registro(monkeypatch):
    client = FeegowClient(token="test-token")

    def request(method, endpoint, **kwargs):
        return {
            "success": True,
            "content": [{"paciente_id": 1015, "cpf": "52998224725"}],
        }

    monkeypatch.setattr(client, "_request", request)
    assert client.find_patient_by_cpf("529.982.247-25") == [
        {"paciente_id": 1015, "cpf": "52998224725"}
    ]


def test_paciente_novo_avanca_em_vez_de_ir_para_a_recepcao(tmp_path, monkeypatch):
    """O caso do João Pedro, ponta a ponta, com o cliente DE VERDADE.

    Um FakeFeegow não serviria aqui: o defeito mora dentro do cliente, entre o
    409 da API e a lista que o funil lê. Então o cliente é o real, e só o
    transporte HTTP é fingido — a mesma fronteira que a API atravessa.

    15/set/2026 12:03 BRT, o que ele recebeu: "Não foi possível concluir este
    agendamento com segurança. Por favor, fale com a recepção…". O esperado é
    a pergunta seguinte do cadastro.
    """

    client = FeegowClient(token="test-token", write_enabled=True)

    def request(method, endpoint, **kwargs):
        if endpoint == "appoints/available-schedule":
            return {
                "success": True,
                "content": [
                    {"id": "s-1", "data": "24-09-2026", "horario": "14:00"}
                ],
            }
        if endpoint == "patient/search":
            raise _conflito("Paciente não encontrado")
        raise AssertionError(f"endpoint inesperado: {endpoint}")

    monkeypatch.setattr(client, "_request", request)

    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "write_enabled": True},
        db_path=tmp_path / "state" / "agenda.db",
        clock=MutableClock(datetime(2026, 9, 15, 11, 49, tzinfo=BRT)),
        feegow_client=client,
    )

    handler.handle(event("bom dia", message_id="j-1"))
    handler.handle(event("1", message_id="j-2"))            # agendar uma consulta
    handler.handle(event("1", message_id="j-3"))            # consulta presencial
    vagas = handler.handle(event("1", message_id="j-4"))    # primeira vaga
    assert "CPF" in (vagas or ""), f"esperava o passo do CPF, veio: {vagas!r}"
    handler.handle(event("08227254527", message_id="j-5"))  # CPF
    resposta = handler.handle(event("21/03/2004", message_id="j-6")) or ""

    assert "recepção" not in resposta, (
        f"paciente novo ainda cai na recepção: {resposta!r}"
    )
    assert "SIM" in resposta, f"esperava seguir o cadastro, veio: {resposta!r}"
