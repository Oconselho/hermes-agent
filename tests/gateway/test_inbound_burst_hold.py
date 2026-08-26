"""Uma rajada do mesmo contato vira UM turno, não um turno por mensagem.

Incidente 26/ago/2026. A Val mandou o pedido em texto e quatro anexos em onze
minutos e recebeu cinco respostas. O Victor: "para cada mensagem que o contato
envia a secretária responde outra. É importante entender o contexto e responder
a mensagem que precisa."

Não havia agrupamento nenhum na estrada dela. O amortecedor que existe
(``_queue_text_debounce``) é outro bicho: roda só com o agente OCUPADO, exige
``busy_input_mode: queue`` — a secretária está em ``interrupt`` —, só aceita
``MessageType.TEXT`` (quatro das cinco mensagens da Val eram anexo) e mede
0,35s com teto de 1s. Não teria juntado nada nem ligado.

Janela e teto escolhidos pelo Victor em 26/ago/2026: 45s e 90s.
"""

import asyncio

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SessionSource,
)


class _Adapter(BasePlatformAdapter):
    """Adaptador mínimo: só registra que turnos ele abriu."""

    def __init__(self, **extra):
        super().__init__(PlatformConfig(enabled=True, extra=extra), Platform.WHATSAPP)
        self.turnos: list[MessageEvent] = []

    # ── o que este teste observa ──────────────────────────────────────────
    def _start_session_processing(self, event, session_key):  # type: ignore[override]
        self.turnos.append(event)

    # ── superfície abstrata, irrelevante aqui ─────────────────────────────
    async def connect(self): ...
    async def disconnect(self): ...
    async def get_chat_info(self, chat_id): return {}
    async def send(self, chat_id, content, **kwargs): ...
    @property
    def name(self): return "whatsapp"


def _evento(texto, *, quem="557187749408", tipo=MessageType.TEXT, anexo=None):
    ev = MessageEvent(
        text=texto,
        message_type=tipo,
        source=SessionSource(
            platform=Platform.WHATSAPP,
            chat_id="170707921182854@lid",
            chat_type="dm",
            user_id=quem,
        ),
        message_id=f"msg-{texto[:8]}",
    )
    if anexo:
        ev.media_urls.append(anexo)
        ev.media_types.append("document")
    return ev


@pytest.fixture
def val():
    """A sequência real da Val: pedido em texto + três PDFs + uma imagem."""
    return [
        _evento("Bom dia, Dr Vítor! Tem como adaptar esse relatório de Luna"),
        _evento("[document received]", tipo=MessageType.DOCUMENT, anexo="/tmp/relatorio_luna.pdf"),
        _evento("Laudo /filha", tipo=MessageType.DOCUMENT, anexo="/tmp/ZnUrma.pdf"),
        _evento("", tipo=MessageType.DOCUMENT, anexo="/tmp/XXh7V3.pdf"),
        _evento("E desde quando começou o tratamento aos dez anos"),
    ]


@pytest.mark.asyncio
async def test_desligado_por_padrao_cada_mensagem_abre_seu_turno(val):
    """Nenhuma superfície ganha atraso sem alguém ter escrito o número."""
    a = _Adapter()
    assert a._inbound_burst_seconds == 0
    for ev in val:
        assert a._is_inbound_burst_candidate(ev) is False


@pytest.mark.asyncio
async def test_a_rajada_da_val_vira_um_turno_com_todos_os_anexos(val):
    """As cinco mensagens, chegando juntas, abrem UM turno só."""
    a = _Adapter(inbound_burst_seconds=45, inbound_burst_max_seconds=90)
    for ev in val:
        await a._hold_inbound_burst("s", ev)

    assert a.turnos == [], "nada pode abrir antes de a janela fechar"
    assert a._inbound_burst["s"].count == 5

    await a._flush_inbound_burst_now("s")

    assert len(a.turnos) == 1, "cinco mensagens, um turno"
    turno = a.turnos[0]
    assert turno.media_urls == [
        "/tmp/relatorio_luna.pdf", "/tmp/ZnUrma.pdf", "/tmp/XXh7V3.pdf",
    ], "nenhum anexo pode se perder na fusão"
    assert "adaptar esse relatório" in turno.text
    assert "dez anos" in turno.text, "a última fala tem que chegar ao modelo"


@pytest.mark.asyncio
async def test_o_teto_fecha_a_rajada_de_quem_nao_para_de_escrever(val):
    """Sem teto, quem escreve a cada 40s nunca seria respondido."""
    a = _Adapter(inbound_burst_seconds=45, inbound_burst_max_seconds=90)
    await a._hold_inbound_burst("s", val[0])
    estado = a._inbound_burst["s"]
    estado.first_ts -= 89.0  # quase no teto
    estado.last_ts -= 0.0
    await a._hold_inbound_burst("s", val[1])
    # Reagendou: o teto manda, não a janela de 45s.
    assert a._inbound_burst["s"].task is not None
    a._inbound_burst["s"].task.cancel()


@pytest.mark.asyncio
async def test_comando_nunca_espera(val):
    """/status existe para responder quando o resto não responde."""
    a = _Adapter(inbound_burst_seconds=45, inbound_burst_max_seconds=90)
    assert a._is_inbound_burst_candidate(_evento("/status")) is False
    assert a._is_inbound_burst_candidate(val[0]) is True


@pytest.mark.asyncio
async def test_rajada_que_encontra_a_sessao_ocupada_vira_follow_up(val):
    """Se o turno abriu no meio, o bloco vai para a fila — nunca para o lixo."""
    a = _Adapter(inbound_burst_seconds=45, inbound_burst_max_seconds=90)
    await a._hold_inbound_burst("s", val[0])
    await a._hold_inbound_burst("s", val[1])

    a._active_sessions["s"] = asyncio.Event()  # outra estrada abriu o turno
    await a._flush_inbound_burst_now("s")

    assert a.turnos == [], "não pode abrir um segundo turno concorrente"
    assert "s" in a._pending_messages, "e não pode perder a mensagem"
    assert a._pending_messages["s"].media_urls == ["/tmp/relatorio_luna.pdf"]


@pytest.mark.asyncio
async def test_fala_de_outra_pessoa_nao_entra_na_rajada_alheia(val):
    """Sessão compartilhada: a fala de um não é anexada à do outro."""
    a = _Adapter(inbound_burst_seconds=45, inbound_burst_max_seconds=90)
    await a._hold_inbound_burst("s", val[0])
    await a._hold_inbound_burst("s", _evento("outro assunto", quem="5511999999999"))

    # A rajada da Val foi fechada e virou turno; a nova pessoa começa a sua.
    assert len(a.turnos) == 1
    assert "adaptar esse relatório" in a.turnos[0].text
    assert "outro assunto" not in a.turnos[0].text
    assert a._inbound_burst["s"].event.text == "outro assunto"
    if a._inbound_burst["s"].task is not None:
        a._inbound_burst["s"].task.cancel()


@pytest.mark.asyncio
async def test_shutdown_avisa_alto_quem_ficou_sem_resposta(val, caplog):
    """O bridge não reenvia. Se algo se perde, o operador tem que saber quem.

    ``cancel_background_tasks`` é o teardown que o gateway chama no restart.
    """
    a = _Adapter(inbound_burst_seconds=45, inbound_burst_max_seconds=90)
    await a._hold_inbound_burst("s", val[0])
    a._inbound_burst["s"].task.cancel()

    with caplog.at_level("WARNING"):
        await a.cancel_background_tasks()

    assert any(
        "NOT replayed" in r.message or "NOT replayed" in r.getMessage()
        for r in caplog.records
    ), "perder mensagem em silêncio é o que esta guarda existe para não fazer"
    assert a._inbound_burst == {}
