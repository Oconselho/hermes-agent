"""Julgamento tipado de intenção de agendamento — só no resíduo onde o léxico falha.

POR QUE ESTE MÓDULO EXISTE (19/set/2026)
========================================
`_whatsapp_has_scheduling_intent` decide se a mensagem entra no funil de
agendamento. Medido em 120 mensagens reais de entrada (11–19/set, cópia do
`state.db` em ``mode=ro``), contra a versão que está em produção:

    disparou 8 vezes em 120 — o Jev sustenta 1
    perdeu 2 pedidos de verdade, um deles um áudio

E o padrão dos erros tem nome: **27% das mensagens (32 de 120) são transcrição de
anexo** feita por um modelo de visão/áudio. A heurística casa "agendar",
"consulta", "horário" dentro do laudo do Gemini — dois falsos positivos eram o
*mesmo convite de evento em imagem*. Medido também que não adianta remover o
cabeçalho do anexo (continua 8 disparos): o gatilho está no conteúdo transcrito.
E remover o bloco inteiro derruba os disparos para 4, mas devolve a cegueira para
pedido falado em áudio, que foi o defeito de 26/ago.

Ou seja: **o anexo é exatamente onde o léxico erra dos dois lados.** É esse o
resíduo.

DESENHO (as duas escolhas passaram pelo Jev, com o número junto)
----------------------------------------------------------------
- **Onde roda:** `residuo` 0,91 (confiança 0,89) contra `todas` 0,01. A heurística
  continua decidindo de graça o caso fácil; o Jev só é chamado quando há anexo
  transcrito ou quando a heurística disparou (para confirmar).
  Isso põe ~29% das mensagens na rede, não 100%.
- **Faixa de dúvida:** `nao_entra_no_funil` 0,82 (confiança 0,77). Entre 0,30 e
  0,70 o Jev está dizendo que está em cima do muro — e em cima do muro o sistema
  cala em vez de sequestrar a conversa. Fica registrado para revisão.

REDE DE BAIXO
-------------
Qualquer coisa que dê errado — jevlib ausente, chave ausente, piso barrando,
timeout, HTTP, JSON estranho — cai de volta no veredito léxico, que é o
comportamento de hoje. Este módulo **nunca** levanta para quem o chama.
A ausência de fallback era o defeito apontado na revisão de 19/set.

Desligar sem subir código: ``HERMES_JEV_ROUTING=off`` no ambiente do serviço.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Limiares. 0,50 não é lei: é empate. Quem entra no funil precisa de maioria
# folgada; a faixa do meio é declarada como dúvida, não resolvida no silêncio.
LIMIAR_SIM = 0.70
LIMIAR_MURO = 0.30

TIMEOUT_PADRAO = 4.0
URL = "https://api.typesafe.ai/v1/systemone"
MODELO = "jev-latest"

MARCA_ANEXO = "[Leitura do anexo"

PERGUNTA = {
    "type": "noul",
    "instructions": (
        "A mensagem pede para marcar, remarcar ou agendar consulta ou "
        "atendimento com o Dr. Victor — ou pergunta como ser atendido?"
    ),
    "criteria": {
        "true": (
            "pedido ou pergunta explícita de agendamento, remarcação ou "
            'atendimento (ex.: "o dr atende quando?", "quero passar com ele", '
            '"como faço pra ser atendido?")'
        ),
        "false": (
            "menciona consulta, atendimento ou retorno apenas como histórico, "
            "contexto clínico, convite, divulgação ou assunto administrativo "
            "de parceiro — sem pedir para agendar"
        ),
    },
}

# A segunda pergunta não é enfeite, e não custa outra requisição: vai na mesma.
#
# 19/set, medido contra os avisos que realmente saíram (`outbox_events`): em
# 17/set 11:00 BRT uma plataforma parceira escreveu *"Dr Victor, por acaso você
# conseguiria disponibilizar mais algumas horas ainda em setembro?"*. É pedido de
# agenda, e o Jev responde 0,78 — acima do limiar. Só que quem pede não é
# paciente: mandar isso à recepção reabre 14/ago/2026, quando um parceiro chegou
# lá como pedido de agendamento. A trava do léxico para esse caso é uma lista de
# nomes de empresa; a daqui é quem escreveu, julgado no texto.
PERGUNTA_QUEM = {
    "type": "choice",
    "instructions": "Quem está enviando esta mensagem?",
    "criteria": {
        "paciente": "a própria pessoa que é (ou quer ser) paciente do Dr. Victor",
        "familiar_ou_conhecido": (
            "escreve em nome ou no lugar de outra pessoa (filho, cônjuge, "
            "cuidador, amigo)"
        ),
        "profissional_de_saude": (
            "médico, enfermeiro, secretária ou funcionário de outro serviço de saúde"
        ),
        "empresa_ou_parceiro": (
            "empresa, plataforma, laboratório, convênio, operadora, cobrança, "
            "indústria — inclusive quando pede horário na agenda dele"
        ),
        "outro": "não é possível determinar",
    },
}

# Remetente que, mesmo pedindo agenda, não é lead da recepção.
QUEM_FORA_DO_FUNIL = frozenset({"empresa_ou_parceiro"})


class _Indisponivel(Exception):
    """O Jev não pode ser usado agora. Nunca escapa deste módulo."""


def _jevlib():
    """O núcleo comum (anonimização + piso + chave). Importado por caminho.

    Mora em ``~/os/bin/jevlib.py`` porque o piso é do OS, não deste release —
    e o piso tem de ser o mesmo para toda chamada que sai desta máquina.
    """
    caminho = Path(os.environ.get("HERMES_JEVLIB", "/home/ubuntu/os/bin/jevlib.py"))
    if not caminho.is_file():
        raise _Indisponivel("jevlib ausente em %s" % caminho)
    import importlib.util

    spec = importlib.util.spec_from_file_location("_jevlib_roteamento", caminho)
    if spec is None or spec.loader is None:
        raise _Indisponivel("jevlib ilegível")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tem_anexo(texto: Any) -> bool:
    """A mensagem carrega transcrição de anexo feita por outro modelo?"""
    return MARCA_ANEXO in str(texto or "")


def no_residuo(texto: Any, lexico: bool) -> bool:
    """Onde o léxico é comprovadamente fraco — e só aí vale pagar rede."""
    return bool(lexico) or tem_anexo(texto)


def _consulta(texto: str, timeout: float) -> tuple[float, str]:
    """Devolve ``(probabilidade_de_pedido, quem_envia)``. Uma requisição só."""
    lib = _jevlib()
    payload = {
        "state": {"mensagem": texto},
        "model": MODELO,
        "questions": {
            "pedido_agendamento": PERGUNTA,
            "quem_envia": PERGUNTA_QUEM,
        },
    }
    payload = lib.anonimiza(payload)
    rotulo = lib.checa_fronteira(payload)
    if rotulo:
        raise _Indisponivel("piso barrou: %s" % rotulo)
    try:
        chave = lib.le_chave()
    except Exception as exc:  # noqa: BLE001
        raise _Indisponivel("chave: %s" % exc) from exc

    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + chave,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resposta:
            corpo = json.load(resposta)
    except urllib.error.HTTPError as exc:
        raise _Indisponivel("HTTP %s" % exc.code) from exc
    except urllib.error.URLError as exc:
        raise _Indisponivel("rede: %s" % (exc.reason,)) from exc
    except Exception as exc:  # noqa: BLE001  (timeout, socket, json)
        raise _Indisponivel("falha: %s" % type(exc).__name__) from exc

    try:
        respostas = corpo["answers"]
        valor = float(respostas["pedido_agendamento"]["noul"])
    except Exception as exc:  # noqa: BLE001
        raise _Indisponivel("resposta sem noul") from exc
    # A segunda resposta é opcional por construção: se vier estranha, o pedido
    # ainda vale e a trava de remetente simplesmente não se aplica.
    try:
        quem = str(respostas["quem_envia"]["choice"])
    except Exception:  # noqa: BLE001
        quem = ""
    return valor, quem


def scheduling_intent(texto: Any, *, lexico: bool, timeout: float | None = None):
    """Decide intenção de agendamento. Devolve ``(decisao, diagnostico)``.

    ``lexico`` é o veredito de ``_whatsapp_has_scheduling_intent`` — e é ele que
    vale sempre que o Jev não puder ser consultado. Este módulo não levanta.
    """
    diag: dict[str, Any] = {"lexico": bool(lexico), "fonte": "lexico"}

    if os.environ.get("HERMES_JEV_ROUTING", "on").lower() in ("off", "0", "false"):
        diag["motivo"] = "desligado"
        return bool(lexico), diag

    texto_s = str(texto or "")
    diag["anexo"] = tem_anexo(texto_s)
    if not no_residuo(texto_s, lexico):
        diag["motivo"] = "fora do residuo"
        return False, diag

    if timeout is None:
        try:
            timeout = float(os.environ.get("HERMES_JEV_TIMEOUT", TIMEOUT_PADRAO))
        except ValueError:
            timeout = TIMEOUT_PADRAO

    inicio = time.monotonic()
    try:
        p, quem = _consulta(texto_s, timeout)
    except _Indisponivel as exc:
        diag["motivo"] = "fallback: %s" % exc
        diag["ms"] = int((time.monotonic() - inicio) * 1000)
        return bool(lexico), diag
    except Exception as exc:  # noqa: BLE001  — rede de baixo de verdade
        diag["motivo"] = "fallback inesperado: %s" % type(exc).__name__
        diag["ms"] = int((time.monotonic() - inicio) * 1000)
        return bool(lexico), diag

    diag.update(
        fonte="jev",
        p=round(p, 3),
        quem=quem,
        ms=int((time.monotonic() - inicio) * 1000),
    )
    if p >= LIMIAR_SIM:
        if quem in QUEM_FORA_DO_FUNIL:
            # Pede agenda, mas não é lead de recepção. Medido em 17/set 11:00 BRT.
            diag["motivo"] = "pedido de agenda, mas o remetente é %s" % quem
            return False, diag
        diag["motivo"] = "acima do limiar"
        return True, diag
    if p >= LIMIAR_MURO:
        # Em cima do muro é resultado, não empate a ser desfeito em silêncio.
        diag["motivo"] = "em cima do muro — não entra no funil"
        diag["muro"] = True
        return False, diag
    diag["motivo"] = "abaixo do limiar"
    return False, diag


def registra(diag: dict[str, Any], *, chave_conversa: str = "") -> None:
    """Uma linha por decisão, sem uma letra do texto da pessoa.

    Serve para responder depois *"quantas vezes o Jev discordou do léxico?"* sem
    guardar conversa de paciente em arquivo nenhum.
    """
    try:
        destino = (
            Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
            / "logs"
            / "jev-roteamento.jsonl"
        )
        destino.parent.mkdir(parents=True, exist_ok=True)
        linha = dict(diag)
        linha["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if chave_conversa:
            linha["conversa"] = chave_conversa[:24]
        with destino.open("a", encoding="utf-8") as saida:
            saida.write(json.dumps(linha, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001  — registro nunca derruba conversa
        pass
