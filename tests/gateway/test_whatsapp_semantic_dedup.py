"""Regression tests for WhatsApp secretary semantic duplicate suppression."""

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
