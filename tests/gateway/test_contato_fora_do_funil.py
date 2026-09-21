"""Quem não vem marcar consulta para de receber o menu de agendamento.

21/set/2026. Medido no banco da secretária e no log do serviço:

    96 menus reais (fora o chat de teste do Victor), em 34 contatos.
    **50 foram para quem não é paciente, familiar de paciente nem lead** —
    36 na abertura fria, 14 já com o fluxo aberto.

O caso que originou o arquivo: a técnica de enfermagem que atua com o Victor na
telemedicina escreve todo dia de atendimento ("a primeira paciente já chegou",
"o paciente das 15:30 também não veio") e levou **17 menus desde 14/ago** — a
segunda maior do banco. ``outbox_events`` do contato: zero. O aviso de que um
paciente faltou morreu dentro do menu.

Os textos abaixo são as frases dela, do log de 11, 18 e 21/set, sem nome de
paciente. ``test_os_textos_reais_ainda_reproduzem_o_defeito`` prova que eles
seguem caindo no menu quando o julgamento está desligado — fixture que não
reproduz o defeito não testa nada (lição de 15/set).

O que este arquivo protege:

1. **o caso medido** — texto com sinal dentro do menu tira o contato do funil;
2. **a abertura que não julga** — cumprimento não paga rede e não decide nada
   (o Jev responde 0,09 para "a primeira mensagem basta"); quem decide na
   abertura é o rótulo já gravado;
3. **quem pede agendamento entra assim mesmo** — colega de trabalho também
   marca consulta para si;
4. **a dúvida não mexe em nada** — confiança abaixo do limiar e
   ``indeterminado`` deixam o funil exatamente como está hoje;
5. **a rede de baixo** — Jev fora do ar, piso barrando ou módulo ausente
   devolvem o comportamento de sempre;
6. **o custo** — uma requisição por contato, não por mensagem.

Nenhum teste aqui toca a rede: a consulta é substituída por um duplo.
"""

from __future__ import annotations

import pytest

from gateway import jev_contato
from gateway.platforms.whatsapp_appointments import (
    AppointmentStore,
    FlowState,
    Route,
    WhatsAppAppointmentsHandler,
    classify_route,
)
from tests.gateway.appointment_helpers import event

CHAT = "132555626008614@lid"

# As frases reais, do log do serviço. Sem nome de paciente.
AVISO_DE_FALTA = "Dr. o paciente das 15:30 também não veio"
AVISO_DE_CHEGADA = "Estou aqui na sala A primeira paciente já chegou"
SAUDACAO = "Boa tarde Dr. Victor"
PEDIDO = "Quero agendar uma consulta"


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch, tmp_path):
    """Liga o julgamento e manda o registro para um HERMES_HOME descartável."""

    monkeypatch.delenv("HERMES_JEV_CONTATO", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))


def _handler(tmp_path, **config):
    db = tmp_path / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler({"enabled": True, **config}, db_path=db)
    return handler, AppointmentStore(db)


def _no_menu(handler, store, *, chat=CHAT):
    """O chat parado no menu, como os 12 que estão assim em produção."""

    store.record_response(
        "semente",
        chat,
        "Como posso ajudar?",
        FlowState.AWAITING_APPOINTMENT_ACTION.value,
        {},
        now=handler._now(),
    )


def _duplo(monkeypatch, rotulo, confianca=1.0):
    """Substitui só a chamada de rede; toda a decisão continua sendo a real."""

    chamadas = []

    def _falso(textos, timeout):  # noqa: ANN001
        chamadas.append((list(textos), timeout))
        if isinstance(rotulo, Exception):
            raise rotulo
        return rotulo, confianca

    monkeypatch.setattr(jev_contato, "_consulta", _falso)
    return chamadas


# --------------------------------------------------------------------------
# 0. O fixture reproduz o defeito
# --------------------------------------------------------------------------


def test_os_textos_reais_ainda_reproduzem_o_defeito(monkeypatch, tmp_path):
    """Com o julgamento desligado, as frases dela levam menu — como em produção."""

    monkeypatch.setenv("HERMES_JEV_CONTATO", "off")
    handler, store = _handler(tmp_path)
    _no_menu(handler, store)

    resposta = handler.handle(event(AVISO_DE_FALTA, chat_id=CHAT, message_id="m-1"))

    assert resposta is not None and "Agendar uma consulta" in resposta
    assert store.load_flow(CHAT) is not None

    # E a saudação é o que abre o funil para qualquer um.
    assert classify_route(event(SAUDACAO, chat_id=CHAT)) is Route.OPENER


# --------------------------------------------------------------------------
# 1. O caso medido
# --------------------------------------------------------------------------


def test_aviso_de_falta_tira_o_contato_do_funil(monkeypatch, tmp_path):
    handler, store = _handler(tmp_path)
    _no_menu(handler, store)
    chamadas = _duplo(monkeypatch, "colega_de_trabalho", 1.0)

    resposta = handler.handle(event(AVISO_DE_FALTA, chat_id=CHAT, message_id="m-1"))

    assert resposta is None, "o turno tinha de ir ao modelo, não virar menu"
    assert store.load_flow(CHAT) is None, "o contato continuou preso no funil"
    assert store.contact_kind(CHAT) == ("colega_de_trabalho", 1.0)
    assert len(chamadas) == 1 and chamadas[0][0] == [AVISO_DE_FALTA]


def test_a_abertura_seguinte_nao_leva_menu_e_nao_paga_rede(monkeypatch, tmp_path):
    """O dia seguinte: ela cumprimenta e o funil não abre."""

    handler, store = _handler(tmp_path)
    _no_menu(handler, store)
    _duplo(monkeypatch, "colega_de_trabalho", 1.0)
    handler.handle(event(AVISO_DE_FALTA, chat_id=CHAT, message_id="m-1"))

    chamadas = _duplo(monkeypatch, "colega_de_trabalho", 1.0)
    resposta = handler.handle(event(SAUDACAO, chat_id=CHAT, message_id="m-2"))

    assert resposta is None
    assert store.load_flow(CHAT) is None, "a abertura fria recriou o funil"
    assert chamadas == [], "a abertura não pode pagar rede: ela lê o rótulo"


def test_mensagem_seguinte_no_mesmo_dia_tambem_vai_ao_modelo(monkeypatch, tmp_path):
    handler, store = _handler(tmp_path)
    _no_menu(handler, store)
    _duplo(monkeypatch, "colega_de_trabalho", 1.0)
    handler.handle(event(AVISO_DE_FALTA, chat_id=CHAT, message_id="m-1"))

    chamadas = _duplo(monkeypatch, "colega_de_trabalho", 1.0)
    resposta = handler.handle(event(AVISO_DE_CHEGADA, chat_id=CHAT, message_id="m-2"))

    assert resposta is None
    assert chamadas == [], "sem fluxo e sem intenção, nem entra no funil"


# --------------------------------------------------------------------------
# 2. Quem pede agendamento entra assim mesmo
# --------------------------------------------------------------------------


def test_pedido_explicito_entra_mesmo_com_o_rotulo_fora(monkeypatch, tmp_path):
    """Colega de trabalho também marca a própria consulta."""

    handler, store = _handler(tmp_path)
    _no_menu(handler, store)
    _duplo(monkeypatch, "colega_de_trabalho", 1.0)
    handler.handle(event(AVISO_DE_FALTA, chat_id=CHAT, message_id="m-1"))
    assert store.contact_kind(CHAT)[0] == "colega_de_trabalho"

    assert classify_route(event(PEDIDO, chat_id=CHAT)) is Route.APPOINTMENT
    resposta = handler.handle(event(PEDIDO, chat_id=CHAT, message_id="m-2"))

    assert resposta is not None and "Agendar uma consulta" in resposta
    assert store.load_flow(CHAT) is not None


# --------------------------------------------------------------------------
# 3. A dúvida não mexe em nada
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rotulo, confianca",
    [
        ("colega_de_trabalho", 0.42),  # medido: "o senhor não encaminhou a receita"
        ("colega_de_trabalho", 0.30),  # medido: "Alt 155 Peso 85 Pa 11 X 7"
        ("indeterminado", 0.97),  # medido: "Bom dia Dr. Victor"
        ("paciente", 0.95),
        ("lead", 0.98),
        ("familiar_do_paciente", 1.0),
    ],
)
def test_duvida_e_paciente_seguem_no_funil(monkeypatch, tmp_path, rotulo, confianca):
    handler, store = _handler(tmp_path)
    _no_menu(handler, store)
    _duplo(monkeypatch, rotulo, confianca)

    resposta = handler.handle(event(AVISO_DE_FALTA, chat_id=CHAT, message_id="m-1"))

    assert resposta is not None and "Agendar uma consulta" in resposta
    assert store.load_flow(CHAT) is not None
    # Mas o rótulo fica gravado de qualquer jeito: é o histórico do contato.
    assert store.contact_kind(CHAT) == (rotulo, confianca)


# --------------------------------------------------------------------------
# 4. A rede de baixo
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "falha",
    [
        jev_contato._Indisponivel("rede: timeout"),
        jev_contato._Indisponivel("piso barrou: credencial nomeada"),
        RuntimeError("qualquer coisa"),
    ],
)
def test_falha_do_jev_devolve_o_comportamento_de_hoje(monkeypatch, tmp_path, falha):
    handler, store = _handler(tmp_path)
    _no_menu(handler, store)
    _duplo(monkeypatch, falha)

    resposta = handler.handle(event(AVISO_DE_FALTA, chat_id=CHAT, message_id="m-1"))

    assert resposta is not None and "Agendar uma consulta" in resposta
    assert store.load_flow(CHAT) is not None
    assert store.contact_kind(CHAT) is None, "falha não pode gravar rótulo"


def test_desligado_por_ambiente_nao_chama_o_jev(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_JEV_CONTATO", "off")
    handler, store = _handler(tmp_path)
    _no_menu(handler, store)
    chamadas = _duplo(monkeypatch, "colega_de_trabalho", 1.0)

    resposta = handler.handle(event(AVISO_DE_FALTA, chat_id=CHAT, message_id="m-1"))

    assert resposta is not None and "Agendar uma consulta" in resposta
    assert chamadas == []


def test_sem_o_modulo_o_funil_e_o_de_antes(monkeypatch, tmp_path):
    """Release sem ``gateway.jev_contato`` (ou import quebrado)."""

    import gateway.platforms.whatsapp_appointments as modulo

    monkeypatch.setattr(modulo, "_jev_contato", None)
    handler, store = _handler(tmp_path)
    _no_menu(handler, store)

    resposta = handler.handle(event(AVISO_DE_FALTA, chat_id=CHAT, message_id="m-1"))

    assert resposta is not None and "Agendar uma consulta" in resposta
    assert store.load_flow(CHAT) is not None


# --------------------------------------------------------------------------
# 5. O custo: uma requisição por contato, não por mensagem
# --------------------------------------------------------------------------


def test_contato_novo_cumprimentando_nao_paga_rede(monkeypatch, tmp_path):
    handler, store = _handler(tmp_path)
    chamadas = _duplo(monkeypatch, "colega_de_trabalho", 1.0)

    resposta = handler.handle(event(SAUDACAO, chat_id=CHAT, message_id="m-1"))

    assert resposta is not None and "Agendar uma consulta" in resposta
    assert chamadas == [], "a saudação de um contato sem rótulo não julga nada"


def test_paciente_ja_rotulado_nao_e_julgado_de_novo_na_abertura(monkeypatch, tmp_path):
    handler, store = _handler(tmp_path)
    store.record_contact_kind(CHAT, "paciente", 0.95, now=handler._now())
    chamadas = _duplo(monkeypatch, "paciente", 0.95)

    resposta = handler.handle(event(SAUDACAO, chat_id=CHAT, message_id="m-1"))

    assert resposta is not None and "Agendar uma consulta" in resposta
    assert chamadas == []


# --------------------------------------------------------------------------
# 6. O módulo, sozinho
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rotulo, confianca, esperado",
    [
        ("colega_de_trabalho", 1.0, True),
        ("colega_de_trabalho", 0.70, True),
        ("colega_de_trabalho", 0.699, False),
        ("empresa_ou_fornecedor", 0.94, True),
        ("convite_profissional", 0.75, True),
        ("spam", 0.99, True),
        ("fraude", 0.88, True),
        ("paciente", 1.0, False),
        ("lead", 1.0, False),
        ("familiar_do_paciente", 1.0, False),
        ("indeterminado", 1.0, False),
        ("", 1.0, False),
        ("colega_de_trabalho", None, False),
    ],
)
def test_so_saem_do_funil_as_classes_certas_com_confianca(rotulo, confianca, esperado):
    assert jev_contato.fora_do_funil(rotulo, confianca) is esperado


def test_as_nove_categorias_sao_rotulo_e_os_destinos_de_acao_sao_poucos():
    """As nove categorias descrevem; três destinos decidem (Jev 1,00, conf 1,00)."""

    assert jev_contato.DENTRO | jev_contato.FORA | {jev_contato.INDETERMINADO} == set(
        jev_contato.CATEGORIAS
    )
    assert not (jev_contato.DENTRO & jev_contato.FORA)


def test_resposta_com_rotulo_desconhecido_nao_decide_nada(monkeypatch):
    def _falso(textos, timeout):  # noqa: ANN001
        return "amigo_do_primo", 1.0

    monkeypatch.setattr(jev_contato, "_consulta", _falso)
    resultado, diag = jev_contato.classifica([AVISO_DE_FALTA])
    assert resultado is None
    assert "fallback" in diag["motivo"]


def test_texto_vazio_nao_vira_requisicao(monkeypatch):
    chamadas = []
    monkeypatch.setattr(
        jev_contato, "_consulta", lambda t, timeout: chamadas.append(t)
    )
    resultado, diag = jev_contato.classifica(["   ", ""])
    assert resultado is None and chamadas == []
    assert diag["motivo"] == "sem texto"


def test_o_registro_nao_guarda_uma_letra_da_conversa(monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        jev_contato, "_consulta", lambda t, timeout: ("colega_de_trabalho", 1.0)
    )
    _, diag = jev_contato.classifica([AVISO_DE_FALTA])
    jev_contato.registra(diag, chave_conversa=CHAT)

    linha = (tmp_path / "logs" / "jev-contato.jsonl").read_text(encoding="utf-8")
    assert "paciente das 15:30" not in linha
    registro = json.loads(linha)
    assert registro["rotulo"] == "colega_de_trabalho"
    assert registro["decide"] is True


# --------------------------------------------------------------------------
# 7. O banco de um release anterior
# --------------------------------------------------------------------------


def test_banco_antigo_ganha_as_colunas_sem_perder_nada(tmp_path):
    """Migração aditiva: o banco em produção tem 15 contatos gravados."""

    import sqlite3

    db = tmp_path / "appointments.sqlite3"
    conexao = sqlite3.connect(db)
    conexao.executescript(
        """
        CREATE TABLE contacts (
            chat_key TEXT PRIMARY KEY,
            external_id TEXT,
            is_quarantined INTEGER NOT NULL DEFAULT 0,
            quarantined_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO contacts (chat_key, created_at, updated_at)
        VALUES ('antigo@lid', '2026-08-04T12:16:09-03:00', '2026-08-04T12:16:09-03:00');
        """
    )
    conexao.commit()
    conexao.close()

    store = AppointmentStore(db)

    assert store.contact_kind("antigo@lid") is None
    from datetime import datetime, timezone

    agora = datetime.now(timezone.utc)
    store.record_contact_kind("antigo@lid", "empresa_ou_fornecedor", 0.94, now=agora)
    assert store.contact_kind("antigo@lid") == ("empresa_ou_fornecedor", 0.94)

    conexao = sqlite3.connect(db)
    guardados = conexao.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    conexao.close()
    assert guardados == 1, "a migração não pode duplicar contato"
