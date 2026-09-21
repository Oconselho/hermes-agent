"""Que tipo de contato é quem escreve — julgado uma vez e gravado no contato.

POR QUE ESTE MÓDULO EXISTE (21/set/2026)
========================================
O funil abre por **cumprimento**. ``classify_route`` lê "Boa tarde Dr. Victor"
como ``Route.OPENER``, cria estado e responde o menu de agendamento inteiro sem
chamar o modelo; com o estado vivo, todo texto que não é 1–6 nem pergunta volta
ao menu. Quem não é paciente nunca sai de lá sozinho.

Medido em 21/set no banco da secretária e no log do serviço:

    96 menus reais (fora o chat de teste do Victor), em 34 contatos.
    **50 foram para quem não é paciente, familiar de paciente nem lead** —
    36 na abertura fria, 14 já com o fluxo aberto.
    12 conversas seguem paradas em AWAITING_APPOINTMENT_ACTION; a mais
    antiga desde 24/ago.

O caso que originou isto: a técnica de enfermagem que atua com o Victor na
telemedicina escreve todo dia de atendimento ("a primeira paciente já chegou",
"o paciente das 15:30 também não veio") e levou **17 menus desde 14/ago**.
``outbox_events`` do contato: zero. O aviso de que um paciente faltou morreu
dentro do menu, e ninguém foi avisado.

O QUE O JEV RESPONDEU, SOBRE CONVERSA REAL
------------------------------------------
Classificados os 34 contatos a partir das mensagens deles:

- a decisão que importa — *entra no funil?* — acerta **6/6** nos casos que
  conhecemos por fora (a técnica sai ``colega_de_trabalho`` **1,00**; o
  paciente Georges, ``paciente`` 0,95);
- a **sub-classe** erra 2/6 quando há pouco texto (um fornecedor a 0,31, uma
  plataforma a 0,45) — e nos dois a confiança baixa já denuncia;
- com **uma** mensagem só, 4 de 6 saem ``indeterminado``. O Jev responde
  **0,09** para *"a primeira mensagem basta para decidir a rota"*.

Daí o desenho, com os números que o sustentam:

- classificar **na abertura** (0,80, confiança 0,74), mas **a abertura não
  julga**: ela lê o rótulo que já está gravado. Cumprimento não tem sinal.
- o rótulo é **persistido no contato** (0,78) e reaproveitado;
- **confiança baixa não recebe menu** (0,79) — aqui isso vira: confiança baixa
  não muda nada, e o comportamento de hoje segue valendo;
- a rota é decidida por **poucos destinos de ação** (1,00, confiança 1,00);
  as nove categorias ficam como rótulo, para registro e revisão humana.

ONDE O JULGAMENTO ACONTECE
--------------------------
No único ponto em que existe texto com sinal e o funil ia responder o menu de
novo: dentro de ``AWAITING_APPOINTMENT_ACTION``, quando o contato escreveu algo
que não é opção de menu. É onde a técnica caiu 14 vezes. A abertura seguinte
não paga rede: lê o rótulo.

REDE DE BAIXO
-------------
Qualquer coisa que dê errado — jevlib ausente, chave ausente, piso barrando,
timeout, HTTP, JSON estranho — devolve ``None`` e o funil segue exatamente como
hoje. Este módulo **nunca** levanta para quem o chama.

Desligar sem subir código: ``HERMES_JEV_CONTATO=off`` no ambiente do serviço.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Sequence

# O carregador do piso mora no módulo irmão de propósito: `jevlib` é a fonte
# única de anonimização e fronteira para tudo que sai desta máquina, e ter dois
# carregadores seria ter duas chances de divergir. O POST, esse sim, é local:
# `jevlib.pede` usa timeout de 60 s, que é aceitável numa linha de comando e
# inaceitável no caminho da resposta a um paciente.
from gateway.jev_intent import _Indisponivel, _jevlib

URL = "https://api.typesafe.ai/v1/systemone"
MODELO = "jev-latest"
TIMEOUT_PADRAO = 3.0

# Confiança mínima para o rótulo valer como decisão. Abaixo disto o rótulo é
# gravado assim mesmo — serve de registro e de história do contato — mas não
# tira ninguém do funil. Foi o que separou o acerto do erro na medição: os dois
# erros de sub-classe saíram a 0,31 e 0,45.
LIMIAR_CONFIANCA = 0.70

# Duas classes pedem MAIS que as outras, e a razão é o custo de errar (21/set,
# achado na suíte): um paciente que compartilha uma reportagem sai `spam` com
# **0,73** — acima do limiar comum. Marcar um paciente de spam é perder um
# agendamento, e o rótulo é persistido; deixar um spam de verdade seguir no
# funil não custa nada a ninguém. Assimetria de dano, limiar assimétrico.
LIMIAR_POR_CLASSE = {
    "spam": 0.90,
    "fraude": 0.90,
}

# Abaixo disto não há sinal para julgar: sobra cumprimento, link ou uma palavra
# solta. Não paga rede e não decide nada — a mensagem seguinte decide.
MINIMO_DE_PALAVRAS = 3

# Os três destinos de AÇÃO. As nove categorias abaixo existem para descrever o
# contato; o que o código decide com elas é só isto.
DENTRO = frozenset({"paciente", "familiar_do_paciente", "lead"})
FORA = frozenset(
    {
        "colega_de_trabalho",
        "empresa_ou_fornecedor",
        "convite_profissional",
        "amigo_ou_familia_do_medico",
        "spam",
        "fraude",
    }
)
INDETERMINADO = "indeterminado"

CATEGORIAS = {
    "paciente": (
        "a própria pessoa que é ou quer ser paciente do consultório, falando "
        "do próprio atendimento"
    ),
    "familiar_do_paciente": (
        "fala em nome de outra pessoa que é paciente (filho, cônjuge, mãe, "
        "cuidador) para resolver o atendimento dela"
    ),
    "lead": (
        "ainda não é paciente: pergunta preço, convênio, endereço ou como "
        "marcar, para si mesmo"
    ),
    "colega_de_trabalho": (
        "trabalha COM o médico na rotina assistencial — enfermeira, técnica, "
        "secretária de outro serviço, telemedicina, outro médico da equipe. "
        "Relata pacientes que chegaram, atrasos, sala, exames, escala"
    ),
    "amigo_ou_familia_do_medico": (
        "relação pessoal com o médico, assunto que não é atendimento"
    ),
    "empresa_ou_fornecedor": (
        "empresa, laboratório, plataforma, convênio, operadora, financeiro, "
        "cobrança, vendas"
    ),
    "convite_profissional": (
        "convite para palestra, aula, evento, entrevista, parceria ou projeto"
    ),
    "spam": "divulgação em massa, corrente, propaganda não solicitada",
    "fraude": (
        "tentativa de golpe, falsidade de identidade, pedido de dinheiro sob "
        "pretexto"
    ),
    INDETERMINADO: "não há texto suficiente para decidir",
}

PERGUNTA = {
    "type": "choice",
    "instructions": (
        "Estas são mensagens enviadas ao WhatsApp de um consultório médico de "
        "endocrinologia (Dr. Victor). Que tipo de contato é o REMETENTE, do "
        "ponto de vista do consultório? Julgue quem ESCREVE, não de quem se "
        "fala: quem relata o estado de um paciente em terceira pessoa não é o "
        "paciente."
    ),
    "criteria": CATEGORIAS,
}


def desligado() -> bool:
    return os.environ.get("HERMES_JEV_CONTATO", "on").lower() in ("off", "0", "false")


def limiar_de(rotulo: Any) -> float:
    return LIMIAR_POR_CLASSE.get(str(rotulo or ""), LIMIAR_CONFIANCA)


def fora_do_funil(rotulo: Any, confianca: Any) -> bool:
    """O rótulo, com a confiança que tem, basta para NÃO abrir o funil?"""

    try:
        valor = float(confianca)
    except (TypeError, ValueError):
        return False
    nome = str(rotulo or "")
    return nome in FORA and valor >= limiar_de(nome)


_ENDERECO_RE = re.compile(r"(https?://\S+|www\.\S+)", re.I)


def sem_enderecos(texto: Any) -> str:
    """O texto sem endereços colados, e só isso.

    De propósito NÃO é o ``_intent_text`` do funil: aquele também tira acento,
    pontuação e caixa, e o julgamento de *quem escreve* se faz melhor sobre a
    frase como a pessoa a escreveu. O que precisa sair é o endereço — foi ele
    que virou ``spam`` 0,73 e apagou o fluxo de um paciente com reportagem.
    """

    return _ENDERECO_RE.sub(" ", str(texto or "")).strip()


def tem_sinal(texto: Any) -> bool:
    """Há texto suficiente para julgar quem escreve?

    ``"bom dia"`` e um link sozinho não dizem nada sobre o remetente — e um
    link chegou a sair ``spam`` 0,73, o que apagaria o fluxo de um paciente
    que compartilhou uma reportagem. Quem chama deve passar o texto JÁ sem
    endereço (o funil tem ``_intent_text`` para isso).
    """

    return len(str(texto or "").split()) >= MINIMO_DE_PALAVRAS


def _timeout_padrao() -> float:
    try:
        return float(os.environ.get("HERMES_JEV_CONTATO_TIMEOUT", TIMEOUT_PADRAO))
    except ValueError:
        return TIMEOUT_PADRAO


def _consulta(textos: Sequence[str], timeout: float) -> tuple[str, float]:
    """Devolve ``(rotulo, confianca)``. Uma requisição, uma pergunta."""

    lib = _jevlib()
    payload = {
        "state": {"mensagens": list(textos)},
        "model": MODELO,
        "questions": {"tipo_de_contato": PERGUNTA},
    }
    payload = lib.anonimiza(payload)
    rotulo_piso = lib.checa_fronteira(payload)
    if rotulo_piso:
        raise _Indisponivel("piso barrou: %s" % rotulo_piso)
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
        resposta_tipo = corpo["answers"]["tipo_de_contato"]
        escolha = str(resposta_tipo["choice"])
    except Exception as exc:  # noqa: BLE001
        raise _Indisponivel("resposta sem choice") from exc
    try:
        confianca = float(resposta_tipo.get("confidence", 0.0))
    except (TypeError, ValueError):
        confianca = 0.0
    return escolha, confianca


def classifica(
    textos: Sequence[str], *, timeout: float | None = None
) -> tuple[tuple[str, float] | None, dict[str, Any]]:
    """Julga o contato. Devolve ``(resultado, diagnostico)``; nunca levanta.

    ``resultado`` é ``None`` sempre que o julgamento não pôde ser feito — e aí
    quem chamou segue com o comportamento de hoje.
    """

    diag: dict[str, Any] = {"fonte": "jev"}
    if desligado():
        diag["motivo"] = "desligado"
        return None, diag

    uteis = [str(t).strip() for t in textos if str(t or "").strip()]
    if not uteis:
        diag["motivo"] = "sem texto"
        return None, diag
    if not any(tem_sinal(t) for t in uteis):
        diag["motivo"] = "texto sem sinal"
        return None, diag
    diag["n_textos"] = len(uteis)

    if timeout is None:
        timeout = _timeout_padrao()

    inicio = time.monotonic()
    try:
        rotulo, confianca = _consulta(uteis, timeout)
    except _Indisponivel as exc:
        diag["motivo"] = "fallback: %s" % exc
        diag["ms"] = int((time.monotonic() - inicio) * 1000)
        return None, diag
    except Exception as exc:  # noqa: BLE001 — rede de baixo de verdade
        diag["motivo"] = "fallback inesperado: %s" % type(exc).__name__
        diag["ms"] = int((time.monotonic() - inicio) * 1000)
        return None, diag

    if rotulo not in CATEGORIAS:
        # Rótulo fora do conjunto fechado é resposta que não entendemos, e
        # decidir rota com ela seria pior que não decidir nada. A checagem mora
        # aqui, e não no POST, porque quem decide é este caminho — vale para
        # qualquer fonte de resposta, inclusive um duplo de teste.
        diag["motivo"] = "fallback: rótulo desconhecido"
        diag["ms"] = int((time.monotonic() - inicio) * 1000)
        return None, diag

    diag.update(
        rotulo=rotulo,
        conf=round(confianca, 3),
        ms=int((time.monotonic() - inicio) * 1000),
        decide=fora_do_funil(rotulo, confianca),
    )
    if rotulo == INDETERMINADO:
        diag["motivo"] = "indeterminado — texto não decide"
    elif rotulo in DENTRO:
        diag["motivo"] = "contato do funil"
    elif confianca < LIMIAR_CONFIANCA:
        # Em cima do muro é resultado, não empate a ser desfeito em silêncio.
        diag["motivo"] = "confiança abaixo do limiar — rótulo só registrado"
        diag["muro"] = True
    else:
        diag["motivo"] = "fora do funil"
    return (rotulo, confianca), diag


def registra(diag: dict[str, Any], *, chave_conversa: str = "") -> None:
    """Uma linha por decisão, sem uma letra do texto da pessoa."""

    try:
        from pathlib import Path

        destino = (
            Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
            / "logs"
            / "jev-contato.jsonl"
        )
        destino.parent.mkdir(parents=True, exist_ok=True)
        linha = dict(diag)
        linha["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if chave_conversa:
            linha["conversa"] = chave_conversa[:24]
        with destino.open("a", encoding="utf-8") as saida:
            saida.write(json.dumps(linha, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — registro nunca derruba conversa
        pass
