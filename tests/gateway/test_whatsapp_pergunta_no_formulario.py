"""O funil pergunta; quem responde com outra pergunta não pode levar erro.

Medido no banco de produção em 04/set/2026, sobre todo o histórico de
``inbox_events``: das 46 mensagens de 03/set, **43 receberam texto fixo do
funil**. O modelo falou em 7% dos turnos. A base de conhecimento ligada em
03/set (``references/qa.md``, 32 KB) só podia ser usada nesses 7%.

O caso que originou este arquivo, do teste ao vivo do Victor:

    22:16:41  secretária: "Informe o CPF do paciente."
    22:17:49  Victor:     "Tem durante a.semana?"
    22:17:49  secretária: "CPF inválido. Confira os 11 dígitos e envie
                           novamente."

O agendamento sobreviveu — isso o conserto de 03/set já garantia —, mas a
resposta ACUSA o paciente de um erro que ele não cometeu, e a pergunta fica
sem resposta.

Consertar passo a passo era a armadilha: são 17 estados com a mesma forma
(``se não é o formato → devolve o erro``). Aqui há UMA regra, na entrada de
``_advance``, e o que este arquivo testa é sobretudo o que ela **não** pode
fazer: interceptar um valor legítimo. Um CPF engolido quebraria o agendamento
inteiro, que é um estrago muito maior do que o que se está consertando.
"""

from __future__ import annotations

import pytest

from gateway.platforms.whatsapp_appointments import (
    AppointmentStore,
    FlowState,
    WhatsAppAppointmentsHandler,
    pergunta_interrompe_o_passo,
)
from tests.gateway.appointment_helpers import event

VAGA = {
    "id": "a" * 64,
    "date": "2026-09-12",
    "time": "09:00",
    "display_date": "12/09/2026",
}

# (texto, estado) que o paciente PODE escrever legitimamente naquele passo.
# Nenhum deles pode ser interceptado.
VALORES_LEGITIMOS = [
    ("1", FlowState.AWAITING_SLOT),
    ("3", FlowState.AWAITING_APPOINTMENT_ACTION),
    ("2", FlowState.AWAITING_SERVICE),
    ("12", FlowState.AWAITING_APPOINTMENT_SELECTION),
    ("111.444.777-35", FlowState.AWAITING_CPF),
    ("11144477735", FlowState.AWAITING_CPF),
    ("111 444 777 35", FlowState.AWAITING_CPF),
    ("12/05/1980", FlowState.AWAITING_BIRTH_DATE),
    ("12-05-1980", FlowState.AWAITING_BIRTH_DATE),
    ("sim", FlowState.AWAITING_PHONE_CONFIRMATION),
    ("Sim", FlowState.AWAITING_AUTHORIZATION),
    ("nao", FlowState.AWAITING_CANCEL_AUTHORIZATION),
    ("confirmo", FlowState.AWAITING_AUTHORIZATION),
    ("maria@gmail.com", FlowState.AWAITING_NEW_PATIENT_EMAIL),
    ("F", FlowState.AWAITING_NEW_PATIENT_SEX),
    ("feminino", FlowState.AWAITING_NEW_PATIENT_SEX),
    ("71 99999-8888", FlowState.AWAITING_PHONE_CONFIRMATION),
    # Nomes com palavra interrogativa dentro. São a razão de o passo de texto
    # livre só aceitar interrogação explícita como sinal.
    ("Maria da Silva", FlowState.AWAITING_NEW_PATIENT_NAME),
    ("Ana Quando Souza", FlowState.AWAITING_NEW_PATIENT_NAME),
    ("Rua Como Assim, 40", FlowState.AWAITING_EDIT_VALUE),
]

PERGUNTAS_EM_PASSOS = [
    ("Tem durante a.semana?", FlowState.AWAITING_CPF),
    ("tem durante a semana", FlowState.AWAITING_CPF),
    ("quando o doutor atende", FlowState.AWAITING_CPF),
    ("Qual o valor da consulta?", FlowState.AWAITING_SLOT),
    ("tem horario de manha", FlowState.AWAITING_SLOT),
    ("quanto custa", FlowState.AWAITING_SERVICE),
    ("voces atendem plano de saude", FlowState.AWAITING_APPOINTMENT_ACTION),
    ("onde fica o consultorio", FlowState.AWAITING_BIRTH_DATE),
    ("como funciona a teleconsulta", FlowState.AWAITING_PHONE_CONFIRMATION),
    ("aceita pix?", FlowState.AWAITING_AUTHORIZATION),
    ("Meu nome é Maria?", FlowState.AWAITING_NEW_PATIENT_NAME),
]


def _parado_no_cpf(tmp_path):
    """Um chat exatamente onde o Victor estava às 22:16:41 BRT."""

    db = tmp_path / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler({"enabled": True}, db_path=db)
    store = AppointmentStore(db)
    store.record_response(
        "semente",
        "vitor@lid",
        "Informe o CPF do paciente.",
        FlowState.AWAITING_CPF.value,
        {
            "procedure_id": 3,
            "price": 300,
            "service_label": "Teleconsulta",
            "selected_slot": VAGA,
            "contact_name": "Victor",
        },
        now=handler._now(),
    )
    return handler, store, db


# --------------------------------------------------------------------------
# O que a regra NÃO pode fazer — o lado caro do erro
# --------------------------------------------------------------------------


@pytest.mark.parametrize("texto,estado", VALORES_LEGITIMOS)
def test_valor_legitimo_nunca_e_interceptado(texto, estado):
    assert pergunta_interrompe_o_passo(texto, estado.value) is False, (
        f"{texto!r} é um valor válido em {estado.value} e foi engolido pela "
        "guarda — isso quebraria o agendamento"
    )


def test_cpf_com_interrogacao_colada_ainda_e_cpf():
    """A ordem dos dois testes é o que torna a regra segura."""

    assert pergunta_interrompe_o_passo(
        "111.444.777-35?", FlowState.AWAITING_CPF.value
    ) is False


def test_a_guarda_nao_reengaja_onde_um_humano_ja_assumiu():
    """Em HANDOFF a recepção já foi avisada; conversar cria atendimento duplo."""

    for estado in (FlowState.HANDOFF, FlowState.RECONCILIATION_REQUIRED):
        assert pergunta_interrompe_o_passo(
            "quanto custa a consulta?", estado.value
        ) is False, f"{estado.value} não pode voltar a conversar"


# --------------------------------------------------------------------------
# O que a regra existe para fazer
# --------------------------------------------------------------------------


@pytest.mark.parametrize("texto,estado", PERGUNTAS_EM_PASSOS)
def test_pergunta_no_passo_vai_para_o_modelo(texto, estado):
    assert pergunta_interrompe_o_passo(texto, estado.value) is True


def test_a_acusacao_de_cpf_invalido_nao_e_mais_dita(tmp_path):
    """A regressão de 03/set 22:17:49, ponta a ponta."""

    handler, store, _ = _parado_no_cpf(tmp_path)

    resposta = handler.handle(
        event("Tem durante a.semana?", chat_id="vitor@lid", message_id="m-cpf")
    )

    assert resposta is None, f"o funil respondeu sozinho: {resposta!r}"
    fluxo = store.load_flow("vitor@lid")
    assert fluxo is not None, "o agendamento foi apagado"
    assert fluxo.state == FlowState.AWAITING_CPF.value
    assert fluxo.data["selected_slot"]["time"] == "09:00", "a vaga foi perdida"


def test_o_cpf_ainda_agenda_depois_da_pergunta(tmp_path):
    """Perguntar E DEPOIS responder: o passo continua de onde parou."""

    handler, store, _ = _parado_no_cpf(tmp_path)
    assert handler.handle(
        event("Tem durante a.semana?", chat_id="vitor@lid", message_id="m-1")
    ) is None

    resposta = handler.handle(
        event("111.444.777-35", chat_id="vitor@lid", message_id="m-2")
    )

    assert resposta is not None, "o CPF válido foi engolido pela guarda"
    assert "nascimento" in resposta.lower(), (
        f"esperava avançar para a data de nascimento, veio: {resposta!r}"
    )
    fluxo = store.load_flow("vitor@lid")
    assert fluxo.state == FlowState.AWAITING_BIRTH_DATE.value


# --------------------------------------------------------------------------
# Interrogação não é sinal suficiente sozinha
# --------------------------------------------------------------------------


def test_o_cutucao_continua_com_o_funil():
    """"Alguém ai?" tem "?" e não é dúvida — é quem ainda não leu nada.

    Pego por regressão ao escrever esta guarda: a versão que só olhava o "?"
    tirava do funil o ramo que trata o cutucão, e esse ramo carrega o
    incidente de 12/ago/2026 (duas listas completas de opções em sete
    segundos). Perder um caso já resolvido para "consertar" outro é troca
    ruim.
    """

    for cutucao in ("Alguém ai?", "Oi?", "Boa tarde?", "olá?"):
        assert pergunta_interrompe_o_passo(
            cutucao, FlowState.AWAITING_APPOINTMENT_ACTION.value
        ) is False, f"{cutucao!r} é abertura, não pergunta"


def test_o_aceite_do_convite_nao_e_lido_como_pergunta():
    """No convite, "manhã" e "pode ser" são RESPOSTA, e avançam o agendamento.

    ``_ACEITE_DE_OFERTA_RE`` é lido tanto pela guarda quanto pelo ramo
    ``AWAITING_APPOINTMENT_OFFER_REPLY``, de propósito: duas cópias
    divergiriam, e divergir aqui custa agendamento.
    """

    oferta = FlowState.AWAITING_APPOINTMENT_OFFER_REPLY.value
    for aceite in (
        "Gostaria sim. No final das manhãs de terça-feira ou quinta-feira.",
        "pode ser de manha?",
        "quinta a tarde",
    ):
        assert pergunta_interrompe_o_passo(aceite, oferta) is False, (
            f"{aceite!r} é aceite do convite — o funil precisa dele para avançar"
        )


def test_a_chave_desliga_a_guarda_sem_deploy(tmp_path):
    """A volta atrás precisa ser um VALOR, não uma troca de arquivo.

    Desenho do Victor ao decidir a virada em 03/set ("IA conduz, funil vira
    ferramenta"): a etapa que entrega turnos de conversa ao modelo tem de ser
    reversível por configuração. Padrão ligado, porque é o comportamento
    pedido; desligar é uma linha.
    """

    db = tmp_path / "appointments.sqlite3"
    desligado = WhatsAppAppointmentsHandler(
        {"enabled": True, "pergunta_vai_ao_modelo": False}, db_path=db
    )
    assert desligado._pergunta_vai_ao_modelo is False
    AppointmentStore(db).record_response(
        "semente",
        "vitor@lid",
        "Informe o CPF do paciente.",
        FlowState.AWAITING_CPF.value,
        {"selected_slot": VAGA},
        now=desligado._now(),
    )

    resposta = desligado.handle(
        event("Tem durante a.semana?", chat_id="vitor@lid", message_id="m-off")
    )

    assert resposta is not None, "desligada, a guarda não pode devolver o turno"
    assert "CPF" in resposta

    ligado = WhatsAppAppointmentsHandler({"enabled": True}, db_path=db)
    assert ligado._pergunta_vai_ao_modelo is True, "o padrão é ligado"


def test_cumprimentar_no_meio_do_passo_nao_vira_acusacao(tmp_path):
    """Medido na tela do Victor em 04/set 16:51 e 16:52 BRT.

    O fluxo dele estava parado em ``AWAITING_CPF`` desde a véspera (22:16).
    Ele escreveu "Ola", e um minuto depois "Oi". As duas vezes leu:

        "CPF inválido. Confira os 11 dígitos e envie novamente."

    Cumprimentar e ser acusado de errar um número que ninguém digitou é pior
    do que a pergunta sem resposta — é o funil culpando o paciente pelo
    próprio desenho.

    A primeira versão desta guarda NÃO pegava este caso: ela exigia pergunta,
    e "Ola" não é pergunta; e além disso excluía toda abertura, para proteger
    o ramo do cutucão. A exclusão estava certa e larga demais — vale só onde o
    funil TEM ramo de abertura, que é o menu.
    """

    handler, store, _ = _parado_no_cpf(tmp_path)

    for i, saudacao in enumerate(("Ola", "Oi", "Bom dia", "boa noite")):
        resposta = handler.handle(
            event(saudacao, chat_id="vitor@lid", message_id=f"m-ola-{i}")
        )
        assert resposta is None, (
            f"{saudacao!r} no passo do CPF respondeu {resposta!r}"
        )

    fluxo = store.load_flow("vitor@lid")
    assert fluxo is not None, "o agendamento foi apagado"
    assert fluxo.state == FlowState.AWAITING_CPF.value
    assert fluxo.data["selected_slot"]["time"] == "09:00", "a vaga foi perdida"


def test_a_abertura_no_menu_continua_com_o_funil():
    """O ramo do cutucão carrega o incidente de 12/ago e não pode ser perdido.

    É o contrapeso exato do teste acima: a mesma saudação tem dono diferente
    conforme o passo — no menu o funil sabe tratá-la sem reimprimir a lista;
    nos passos de coleta não existe ramo nenhum.
    """

    menu = FlowState.AWAITING_APPOINTMENT_ACTION.value
    for saudacao in ("Alguém ai?", "Oi", "Boa tarde", "olá?"):
        assert pergunta_interrompe_o_passo(saudacao, menu) is False, (
            f"{saudacao!r} no menu tem ramo próprio no funil"
        )
