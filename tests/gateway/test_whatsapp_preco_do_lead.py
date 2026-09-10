"""A pergunta de preço que o próprio menu convida tem de ser respondida.

Incidente de 10/set/2026, 17:22 BRT. O lead 71 9925-0705 (LID
``193630144847917@lid``) leu o menu de serviço, que termina em *Para saber os
valores, pergunte "quanto custa"*, e perguntou exatamente isso. O que
aconteceu, medido no ``agent.log`` e no ``state.db``:

1. a guarda de pergunta tirou o turno do funil — que tinha a resposta pronta,
   da tabela que cobra — e mandou ao modelo;
2. o modelo acertou: consultou ``servicos_e_precos`` e escreveu 273 caracteres
   com os três valores certos;
3. a guarda de preço do ``run.py``, escrita quando o modelo não tinha tabela
   nenhuma, trocou tudo por 60 caracteres sem preço;
4. e essa troca não avisou ninguém: ``outbox_events`` ficou vazia e o lead
   parou em ``ESCOLHENDO_SERVICO``.

Um teste por camada, e cada um falha no código de antes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from gateway.platforms.whatsapp_appointments import (
    FlowState,
    _SERVICES,
    pergunta_interrompe_o_passo,
)
from gateway.run import (
    _sanitize_gateway_final_response,
    _whatsapp_money_amount,
    _whatsapp_quotes_unlisted_money,
)


# A resposta real, como o modelo a escreveu — 273 caracteres.
RESPOSTA_REAL = (
    "Sr(a). Prata, os valores são:\n\n"
    "Consulta presencial: R$ 600,00, com pagamento na clínica no dia.\n"
    "Consulta presencial + 1 consulta sequencial: R$ 800,00, com pagamento "
    "na clínica no dia.\n"
    "Teleconsulta: R$ 300,00, com pagamento antecipado via PIX.\n\n"
    "Qual serviço deseja agendar?"
)

CONTENCAO = "Obrigado. O Dr. Victor verificará sua mensagem pessoalmente."


# ---------------------------------------------------------------------------
# Camada 1 — o funil fica com a pergunta que ele sabe responder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pergunta",
    [
        "quanto custa",
        "Quanto custa?",
        "quanto é a consulta",
        "qual é o valor",
        "quais são os valores",
        "preço",
    ],
)
@pytest.mark.parametrize(
    "estado",
    [
        FlowState.AWAITING_SERVICE.value,
        FlowState.AWAITING_APPOINTMENT_ACTION.value,
    ],
)
def test_pergunta_de_preco_fica_com_o_funil(pergunta, estado):
    """O passo que tem ramo de preço responde sozinho, sem passar pelo modelo."""
    assert pergunta_interrompe_o_passo(pergunta, estado) is False


def test_pergunta_de_preco_em_passo_sem_ramo_continua_indo_ao_modelo():
    """A exceção é estreita: só onde o ``_advance`` sabe responder.

    No passo do CPF não existe ramo de preço, e engolir a pergunta ali seria
    voltar ao defeito que a guarda de pergunta foi escrita para consertar.
    """
    assert pergunta_interrompe_o_passo("quanto custa", FlowState.AWAITING_CPF.value)


def test_outras_perguntas_no_mesmo_passo_continuam_indo_ao_modelo():
    """Só a pergunta de preço muda de dono, não toda pergunta."""
    assert pergunta_interrompe_o_passo(
        "vocês atendem plano de saúde?", FlowState.AWAITING_SERVICE.value
    )


# ---------------------------------------------------------------------------
# Camada 2 — a guarda confere o dinheiro em vez de censurá-lo
# ---------------------------------------------------------------------------


def test_a_resposta_certa_do_modelo_chega_inteira():
    """O caso do lead: três preços reais, lidos da tabela, entregues."""
    saida = _sanitize_gateway_final_response("whatsapp", RESPOSTA_REAL)

    assert "R$ 600,00" in saida
    assert "R$ 800,00" in saida
    assert "R$ 300,00" in saida
    assert saida != CONTENCAO


def test_valor_inventado_continua_bloqueado():
    """O que a guarda existe para impedir continua impedido."""
    inventado = "A consulta com o Dr. Victor custa R$ 450,00, pode ser?"

    assert _sanitize_gateway_final_response("whatsapp", inventado) == CONTENCAO


def test_desconto_sobre_preco_real_tambem_e_bloqueado():
    """Um número certo ao lado de um errado não salva a mensagem."""
    misto = "A consulta é R$ 600, mas posso fazer por R$ 500 hoje."

    assert _sanitize_gateway_final_response("whatsapp", misto) == CONTENCAO


def test_texto_do_funil_nunca_passa_pela_conferencia():
    """``trusted_source`` continua atravessando: o funil cobra pela tabela."""
    saida = _sanitize_gateway_final_response(
        "whatsapp", "Teleconsulta — R$ 300", trusted_source=True
    )

    assert saida == "Teleconsulta — R$ 300"


def test_sem_dinheiro_no_texto_a_guarda_nao_opina():
    texto = "Qual serviço deseja agendar?"

    assert _whatsapp_quotes_unlisted_money(texto) is False


def test_a_conferencia_usa_a_tabela_que_cobra():
    """Preço novo na tabela passa a ser dizível sem editar guarda nenhuma."""
    for servico in _SERVICES.values():
        assert not _whatsapp_quotes_unlisted_money(
            f"A {servico['label']} custa R$ {servico['price']},00."
        )


@pytest.mark.parametrize(
    "bruto,esperado",
    [
        ("600", Decimal("600")),
        ("600,00", Decimal("600")),
        ("600.00", Decimal("600")),
        ("600,00,", Decimal("600")),
        ("1.200,00", Decimal("1200")),
        ("1,200.00", Decimal("1200")),
        ("600,5", None),
        ("", None),
    ],
)
def test_leitura_de_valor_em_reais(bruto, esperado):
    """"600.00" é seiscentos, não seiscentos mil — e o ilegível é ``None``."""
    lido = _whatsapp_money_amount(bruto)
    if esperado is None:
        assert lido is None or lido != Decimal("600")
    else:
        assert lido == esperado


def test_tabela_ilegivel_bloqueia(monkeypatch):
    """Fail-closed: sem referência, todo número é desconhecido."""
    monkeypatch.setattr(
        "gateway.run._whatsapp_price_table_reais", lambda: None
    )

    assert _whatsapp_quotes_unlisted_money("custa R$ 600,00") is True


# ---------------------------------------------------------------------------
# Camada 3 — a contenção avisa alguém
# ---------------------------------------------------------------------------


def test_a_contencao_diz_qual_guarda_disparou():
    """Sem isto a troca é invisível por dentro, e ninguém pode ser avisado."""
    audit: dict = {}
    saida = _sanitize_gateway_final_response(
        "whatsapp", "Fica R$ 450,00 a consulta.", audit=audit
    )

    assert saida == CONTENCAO
    assert audit["guard"] == "dinheiro_fora_da_tabela"


def test_resposta_limpa_nao_marca_guarda_nenhuma():
    audit: dict = {}
    _sanitize_gateway_final_response("whatsapp", RESPOSTA_REAL, audit=audit)

    assert audit == {}


def test_agendamento_inventado_tambem_se_identifica():
    audit: dict = {}
    _sanitize_gateway_final_response(
        "whatsapp",
        "Sua consulta está confirmada para segunda às 14:00.",
        audit=audit,
    )

    assert audit["guard"] == "agendamento_inventado"
