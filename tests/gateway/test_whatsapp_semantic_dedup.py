"""Regression tests for WhatsApp secretary semantic duplicate suppression."""

import pytest

from gateway.run import _should_suppress_whatsapp_followup


def _history(*messages):
    return list(messages)


def _user(text, timestamp):
    return {"role": "user", "content": text, "timestamp": timestamp}


def _assistant(text, timestamp):
    return {"role": "assistant", "content": text, "timestamp": timestamp}


def test_exact_repeat_after_recent_secretary_reply_is_suppressed():
    history = _history(
        _user("Enviei o comprovante", 100.0),
        _assistant(
            "Recebi o documento. O Dr. Victor vai verificar sua mensagem em breve.",
            105.0,
        ),
    )

    assert _should_suppress_whatsapp_followup(
        "Enviei o comprovante", history, now=110.0
    ) is True


def test_attachment_continuation_after_recent_ack_is_suppressed():
    history = _history(
        _user("[document received]", 100.0),
        _assistant(
            "Muito obrigada pelo envio. O Dr. Victor vai analisar sua mensagem.",
            105.0,
        ),
    )

    assert _should_suppress_whatsapp_followup(
        "[document received]", history, now=111.0
    ) is True


def test_new_question_is_not_suppressed_even_when_topic_matches():
    history = _history(
        _user("Enviei o comprovante", 100.0),
        _assistant(
            "Recebi o documento. O Dr. Victor vai verificar sua mensagem em breve.",
            105.0,
        ),
    )

    assert _should_suppress_whatsapp_followup(
        "Qual o valor da consulta?", history, now=110.0
    ) is False


def test_new_correction_or_missing_item_is_not_suppressed():
    history = _history(
        _user("Enviei o comprovante", 100.0),
        _assistant(
            "Recebi o documento. O Dr. Victor vai verificar sua mensagem em breve.",
            105.0,
        ),
    )

    assert _should_suppress_whatsapp_followup(
        "Faltou o pedido médico, podem verificar?", history, now=110.0
    ) is False


@pytest.mark.parametrize(
    "text",
    [
        "Corrigindo: enviei o comprovante",
        "Correção: enviei o comprovante",
        "Desejo o comprovante",
        "Me ajuda com o comprovante",
    ],
)
def test_corrections_and_new_intents_are_not_suppressed(text):
    history = _history(
        _user("Enviei o comprovante", 100.0),
        _assistant(
            "Recebi o documento. O Dr. Victor vai verificar sua mensagem em breve.",
            105.0,
        ),
    )

    assert _should_suppress_whatsapp_followup(text, history, now=110.0) is False


def test_urgency_is_not_suppressed_after_recent_ack():
    history = _history(
        _user("Enviei o comprovante", 100.0),
        _assistant(
            "Recebi o documento. O Dr. Victor vai verificar sua mensagem em breve.",
            105.0,
        ),
    )

    assert _should_suppress_whatsapp_followup(
        "Urgência", history, now=110.0
    ) is False


def test_exact_new_question_is_not_suppressed_by_generic_ack():
    history = _history(
        _user("Qual o valor da consulta?", 100.0),
        _assistant("Recebi sua mensagem.", 105.0),
    )

    assert _should_suppress_whatsapp_followup(
        "Qual o valor da consulta?", history, now=110.0
    ) is False


def test_near_repeat_with_new_request_term_is_not_suppressed():
    history = _history(
        _user("Quero o comprovante", 100.0),
        _assistant("Recebi sua mensagem.", 105.0),
    )

    assert _should_suppress_whatsapp_followup(
        "Quero comprovante", history, now=110.0
    ) is False


def test_repeat_window_is_capped_at_120_seconds(monkeypatch):
    monkeypatch.setenv("HERMES_WHATSAPP_REPEAT_SUPPRESSION_SECONDS", "3600")
    history = _history(
        _user("Enviei o comprovante", 100.0),
        _assistant(
            "Recebi o documento. O Dr. Victor vai verificar sua mensagem em breve.",
            105.0,
        ),
    )

    assert _should_suppress_whatsapp_followup(
        "Enviei o comprovante", history, now=500.0
    ) is False


def test_unrelated_assistant_reply_does_not_suppress_repeat():
    history = _history(
        _user("Enviei o comprovante", 100.0),
        _assistant("Tenha uma boa tarde.", 105.0),
    )

    assert _should_suppress_whatsapp_followup(
        "Enviei o comprovante", history, now=110.0
    ) is False


def test_old_reply_is_not_suppressed():
    history = _history(
        _user("Enviei o comprovante", 100.0),
        _assistant(
            "Recebi o documento. O Dr. Victor vai verificar sua mensagem em breve.",
            105.0,
        ),
    )

    assert _should_suppress_whatsapp_followup(
        "Enviei outro comprovante", history, now=300.0
    ) is False


def test_short_new_request_is_not_suppressed_by_fallback():
    history = _history(
        _user("Enviei o comprovante", 100.0),
        _assistant(
            "Recebi o documento. O Dr. Victor vai verificar sua mensagem em breve.",
            105.0,
        ),
    )

    assert _should_suppress_whatsapp_followup(
        "Me manda o endereço", history, now=110.0
    ) is False


def test_near_duplicate_reply_is_rewritten_with_context_for_new_request():
    from gateway.run import _whatsapp_redundant_response_fallback

    history = _history(
        _user(
            "É, entendo que agrega sim à farmácia! Para oferecer já agregado esse serviço, temos que estar alinhados com os agendamentos",
            100.0,
        ),
        _assistant(
            "Boa tarde! Aqui é a assistente do Dr. Victor Almeida. Obrigada pelo contato. O Dr. Victor verificará sua mensagem e retornará assim que possível. Até mais!",
            105.0,
        ),
    )

    result = _whatsapp_redundant_response_fallback(
        "Boa tarde! Aqui é a secretária do Dr. Victor Almeida. Obrigada pelo contato. O Dr. Victor verificará sua mensagem e retornará em breve. Tenha um bom dia!",
        history,
        current_text="Outra coisa: integrar as tabelas médicas com preços populares à telemedicina tem viabilidade?",
        now=110.0,
    )

    assert result == (
        "Entendi que você está avaliando integrar serviços/tabelas da farmácia "
        "à telemedicina. Vou registrar esse ponto para o Dr. Victor avaliar "
        "a viabilidade e retornar."
    )


def test_distinct_contextual_reply_is_preserved():
    from gateway.run import _whatsapp_redundant_response_fallback

    history = _history(
        _user("Qual o valor da consulta?", 100.0),
        _assistant("Recebi sua mensagem e encaminhei para a recepção.", 105.0),
    )

    candidate = "A recepção poderá informar o valor e as formas de atendimento."
    assert _whatsapp_redundant_response_fallback(
        candidate,
        history,
        current_text="Qual é o valor da consulta?",
        now=110.0,
    ) == candidate


def test_contextual_fallback_does_not_reuse_stale_professional_topic():
    from gateway.run import _whatsapp_redundant_response_fallback

    history = _history(
        _user("A farmácia quer integrar tabelas à telemedicina", 100.0),
        _assistant(
            "Obrigada pelo contato. O Dr. Victor verificará sua mensagem e retornará em breve.",
            105.0,
        ),
    )

    candidate = "Obrigada pelo contato. O Dr. Victor verificará sua mensagem e retornará assim que possível."
    assert _whatsapp_redundant_response_fallback(
        candidate,
        history,
        current_text="Outra dúvida: qual é o horário de atendimento?",
        now=110.0,
    ) == "Entendi o novo ponto da sua mensagem. Vou registrá-lo para o Dr. Victor avaliar e retornar."


def test_secretary_prompt_allows_contextual_followups_without_forced_repetition():
    from pathlib import Path
    import gateway.run as run_module

    prompt_source = Path(run_module.__file__).read_text(encoding="utf-8").lower()
    assert "nunca responda perguntas. nunca dê informações além do template." not in prompt_source
    assert "leia as mensagens recentes antes de responder" in prompt_source
    assert "não repita automaticamente a saudação" in prompt_source
    assert "não reformule uma resposta já enviada apenas para variar" in prompt_source
