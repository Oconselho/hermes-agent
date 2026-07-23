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
