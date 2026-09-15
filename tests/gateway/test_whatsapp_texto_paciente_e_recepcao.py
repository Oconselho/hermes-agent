"""As quatro regras de texto que o Victor fixou em 15/set/2026.

Vieram da auditoria do atendimento do João Pedro Neiva (71 98362-0139), que
informou serviço, vaga, CPF e data de nascimento e leu "Por favor, fale com a
recepção" — com a recepção já avisada no mesmo segundo, sem que ele soubesse.

  1. recepção nunca lê "lead"; lê "Paciente: <nome>";
  2. dado que a Feegow tem não se pede ao paciente — prontuário e nome de
     cadastro vão no aviso;
  3. agendamento concluído confirma data e hora, oferece pagamento como
     OPÇÃO e cita a recepção como ALTERNATIVA;
  4. falha do fluxo não vira tarefa do paciente.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from gateway.platforms.whatsapp_appointments import (
    _RECEPTION,
    AppointmentStore,
    WhatsAppAppointmentsHandler,
    _identification_details,
)

from tests.gateway.appointment_helpers import MutableClock

BRT = ZoneInfo("America/Bahia")
RECEPCAO = "5571996691002@s.whatsapp.net"


def _handler(tmp_path, **extra):
    config = {"enabled": True, "reception_chat_id": RECEPCAO}
    config.update(extra)
    return WhatsAppAppointmentsHandler(
        config,
        db_path=tmp_path / "state" / "agenda.db",
        clock=MutableClock(datetime(2026, 9, 15, 11, 49, tzinfo=BRT)),
    )


# --- 1 e 2: o que a recepção lê -------------------------------------------


def test_o_aviso_a_recepcao_nunca_chama_paciente_de_lead(tmp_path):
    store = AppointmentStore(tmp_path / "state" / "agenda.db")
    aviso = store.enqueue_reception_handoff(
        "61500341371055@lid",
        RECEPCAO,
        now=datetime(2026, 9, 15, 12, 3, tzinfo=BRT),
        details={
            "Nome": "João Pedro Neiva",
            "Prontuário": "A-1015",
            "Telefone": "71 98362-0139",
        },
    )
    corpo = aviso["body"]

    assert "lead" not in corpo.lower(), f"a recepção ainda lê 'lead': {corpo!r}"
    assert "Paciente: João Pedro Neiva" in corpo
    assert "Prontuário: A-1015" in corpo
    assert "A recepção liga para o paciente" in corpo


def test_o_prontuario_e_o_nome_do_cadastro_entram_quando_a_feegow_os_tem():
    """Não se pede ao paciente o que a Feegow já sabe."""

    detalhes = _identification_details(
        {
            "contact_name": "João",          # o apelido do WhatsApp
            "patient_name": "João Pedro Neiva",  # o nome do cadastro
            "patient_record": "A-1015",
            "patient_id": 1015,
            "cpf": "08227254527",
        }
    )

    assert detalhes["Nome"] == "João Pedro Neiva"
    assert detalhes["Prontuário"] == "A-1015"


def test_sem_cadastro_na_feegow_o_aviso_ainda_sai_com_o_que_existe():
    """Paciente novo não tem prontuário — e isso não pode calar o aviso."""

    detalhes = _identification_details(
        {"contact_name": "João", "cpf": "08227254527"}
    )

    assert detalhes["Nome"] == "João"
    assert "Prontuário" not in detalhes


# --- 3: a confirmação de quem JÁ está agendado ----------------------------


def test_confirmacao_traz_data_hora_pagamento_opcional_e_recepcao(tmp_path):
    handler = _handler(
        tmp_path,
        payment={
            "enabled": True,
            "beneficiary": "V Franca de Almeida LTDA - CNPJ 40.279.059/0001-85",
            "instructions": "PIX de R$ 300 para a chave CNPJ 40279059000185.",
        },
    )

    texto = handler._confirmacao_de_agendamento(
        901,
        {
            "procedure_id": 1,
            "selected_slot": {"display_date": "24/09/2026", "time": "14:00"},
        },
    )

    assert "Consulta agendada para 24/09/2026 às 14:00" in texto
    assert "901" in texto
    # Preço da PRESENCIAL, não o da teleconsulta que mora no `instructions`.
    assert "R$ 600" in texto
    assert "R$ 300" not in texto
    assert "opcional" in texto.lower()
    assert "recepção atende" in texto
    # Nada aqui pode soar como etapa pendente.
    assert "confirmação final" not in texto.lower()


def test_sem_beneficiario_a_oferta_de_pagamento_some_inteira(tmp_path):
    """Melhor não oferecer do que oferecer sem dizer para quem pagar."""

    handler = _handler(tmp_path)

    texto = handler._confirmacao_de_agendamento(
        901,
        {
            "procedure_id": 1,
            "selected_slot": {"display_date": "24/09/2026", "time": "14:00"},
        },
    )

    assert "Consulta agendada para 24/09/2026 às 14:00" in texto
    assert "PIX" not in texto
    assert "recepção atende" in texto


# --- 4: a falha não vira tarefa do paciente -------------------------------


def test_falha_do_fluxo_nao_manda_o_paciente_procurar_ninguem():
    baixo = _RECEPTION.lower()

    assert "fale com a recepção" not in baixo
    assert "procure" not in baixo
    assert "entrar em contato" in baixo
    assert "não precisa fazer nada" in baixo
    assert "71 99669-1002" in _RECEPTION
