"""Onde o dono já atendeu à mão — o sinal mais forte de que o chat não é do funil.

POR QUE (21/set/2026)
=====================
O bridge grava um carimbo toda vez que o Dr. Victor responde um chat pelo
celular, e por 30 minutos descarta o que chega ali. Passados os 30 minutos, o
contato volta a ser tratado como qualquer um — e a prima dele, que ele atende
pessoalmente há semanas, levou o menu de agendamento às 16:47 de 21/set, 31
segundos antes de ele mesmo responder.

Intervenção manual é uma declaração: *este chat é meu*. Ela vale mais que
qualquer classificação de texto, e vale por muito mais que 30 minutos. O bridge
passou a gravá-la num arquivo de retenção longa
(``~/.hermes/whatsapp/owner-interventions.json``, 120 dias) e este módulo é o
lado que lê.

O QUE ISTO NÃO FAZ
------------------
Não cala a secretária. Quem chega por este caminho vai ao **modelo**, que
conversa e é o único caminho pelo qual as rotas de aviso chegam ao Victor. E
não vale para pedido explícito de agendamento: quem escreve "quero marcar
consulta" entra no funil mesmo que ele já tenha atendido aquele chat à mão —
colega de trabalho também marca a própria consulta.

Desligar: ``HERMES_OWNER_INTERVENTION_DAYS=0``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ARQUIVO_PADRAO = "~/.hermes/whatsapp/owner-interventions.json"
DIAS_PADRAO = 30


def _caminho(path: Any = None) -> Path:
    bruto = path or os.environ.get("HERMES_OWNER_INTERVENTION_FILE") or ARQUIVO_PADRAO
    return Path(os.path.expanduser(str(bruto)))


def janela_em_dias() -> int:
    """Quantos dias uma intervenção continua valendo. ``0`` desliga."""

    try:
        return max(0, int(os.environ.get("HERMES_OWNER_INTERVENTION_DAYS", DIAS_PADRAO)))
    except (TypeError, ValueError):
        return DIAS_PADRAO


def ultima_intervencao(chat_key: str, *, path: Any = None) -> float | None:
    """Epoch em segundos da última vez que o dono escreveu neste chat.

    Ausência de arquivo, JSON quebrado, chave ausente ou valor estranho
    devolvem ``None`` — não saber é o estado normal, não um erro.
    """

    arquivo = _caminho(path)
    try:
        if not arquivo.is_file():
            return None
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — arquivo é de outro processo; nunca derruba
        logger.debug("owner interventions unreadable", exc_info=True)
        return None
    if not isinstance(dados, dict):
        return None
    bruto = dados.get(str(chat_key))
    try:
        # O bridge grava em milissegundos (Date.now()).
        return float(bruto) / 1000.0
    except (TypeError, ValueError):
        return None


def interveio_recentemente(
    chat_key: str, *, within_days: int | None = None, path: Any = None, now: float | None = None
) -> bool:
    """O dono atendeu este chat à mão dentro da janela?"""

    dias = janela_em_dias() if within_days is None else max(0, int(within_days))
    if dias == 0:
        return False
    quando = ultima_intervencao(chat_key, path=path)
    if quando is None:
        return False
    agora = time.time() if now is None else float(now)
    idade = agora - quando
    # Carimbo no futuro é relógio torto, não intervenção: ignorar em vez de
    # confiar em aritmética negativa.
    if idade < -3600:
        return False
    return idade <= dias * 86400
