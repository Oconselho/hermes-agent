"""The stay-silent marker must be recognised however the model spells it.

The prompt asks for a bare ``[SILENCIOSO]``. The suppressor compared that
string literally, so when gpt-5.6-luna wrote ``[ SILENCIOSO ]`` the marker was
treated as ordinary text and delivered. Four patients received it between 09
and 11/ago/2026 — three as
``Aqui é a assistente do Dr. Victor Almeida. [ SILENCIOSO ]`` (the finalizer
prepends identity on a fresh conversation) and one bare, once identity had
already been disclosed earlier in the session.

The variants below are the real one from production plus the spellings a model
plausibly reaches for. The second half of the file is the other half of the
contract: text that merely *mentions* the word is real text and must survive.
"""

from __future__ import annotations

import pytest

from gateway.run import (
    _sanitize_gateway_final_response,
    _whatsapp_finalize_secretary_response,
    _whatsapp_is_silence_marker,
)


SILENT_VARIANTS = [
    "[SILENCIOSO]",          # exactly as the prompt asks
    "[ SILENCIOSO ]",        # the one that actually reached patients
    "[  SILENCIOSO  ]",
    "[silencioso]",
    "[ silencioso ]",
    "{silencioso}",
    "{ SILENCIOSO }",
    "(silencioso)",
    "<SILENCIOSO>",
    "SILENCIOSO",
    "silencioso",
    "Silencioso",
    "silenciosa",
    "**[SILENCIOSO]**",      # markdown emphasis
    "_silencioso_",
    "[SILENCIOSO].",
    "[ SILENCIOSO ]!",
    "  [ SILENCIOSO ]  ",
    "\n[SILENCIOSO]\n",
    "[SILENCIOSO]​",    # zero-width joiner smuggling
    " [ SILENCIOSO ] ",
    "[ SILENCIÓSO ]",        # stray accent
]


@pytest.mark.parametrize("variant", SILENT_VARIANTS)
def test_marker_variants_are_recognised(variant):
    assert _whatsapp_is_silence_marker(variant) is True


@pytest.mark.parametrize("variant", SILENT_VARIANTS)
def test_marker_variants_never_reach_a_patient(variant):
    assert _sanitize_gateway_final_response("whatsapp", variant) is None


REAL_TEXT = [
    "Bom dia! Aqui é a assistente do Dr. Victor Almeida.",
    "O consultório fica em silêncio após as 18h.",
    "O exame precisa ser feito em ambiente silencioso.",
    "Prefere um horário mais silencioso, no início da manhã?",
    "silencioso e outra coisa",
    "Aqui é a assistente do Dr. Victor. [SILENCIOSO] não é para você ver.",
]


@pytest.mark.parametrize("text", REAL_TEXT)
def test_real_text_mentioning_the_word_is_not_swallowed(text):
    """Silence is only right when the marker IS the whole message."""
    assert _whatsapp_is_silence_marker(text) is False


@pytest.mark.parametrize("value", ["", None, "   ", "\n"])
def test_empty_values_are_not_treated_as_the_marker(value):
    assert _whatsapp_is_silence_marker(value) is False


def test_the_exact_string_that_reached_patients_is_suppressed():
    """Regression pin: the 57-char message three patients actually received."""
    leaked = "Aqui é a assistente do Dr. Victor Almeida. [ SILENCIOSO ]"
    # The finalizer built that by prefixing identity onto a bare marker. The
    # bare marker is what the sanitizer sees first, and it must stop there.
    assert _sanitize_gateway_final_response("whatsapp", "[ SILENCIOSO ]") is None
    # And if such a line ever arrives already assembled it is ordinary text —
    # suppressing it silently would hide a real reply. It must NOT match.
    assert _whatsapp_is_silence_marker(leaked) is False


@pytest.mark.parametrize("marker", ["[SILENCIOSO]", "[ SILENCIOSO ]"])
def test_the_identity_prefix_is_never_reached_for_a_marker(marker):
    """Three of the four leaks were identity + marker. Fixing the bare marker
    fixes those too, and this pins why.

    ``_whatsapp_finalize_secretary_response`` — the only thing that prepends
    "Aqui é a assistente do Dr. Victor Almeida." — has exactly one call site
    (run.py:13068) and runs strictly AFTER the sanitizer, guarded by
    ``if response is None: return None``. So the assembled 57-char line was
    never something the model produced; it was built here from a marker the
    sanitizer had already let through. Once the sanitizer stops the marker,
    the finalizer is unreachable for it and the identity-prefixed form cannot
    be constructed at all.
    """
    sanitized = _sanitize_gateway_final_response("whatsapp", marker)
    assert sanitized is None

    # Reproduce the real pipeline order: finalize only runs on a non-None
    # sanitizer result. With None, it is never called.
    finalize_calls = []

    def _pipeline(model_output, history):
        cleaned = _sanitize_gateway_final_response("whatsapp", model_output)
        if cleaned is None:
            return None
        finalize_calls.append(cleaned)
        return _whatsapp_finalize_secretary_response(cleaned, history)

    assert _pipeline(marker, []) is None
    assert finalize_calls == [], "the marker must never reach the identity step"

    # Sanity: real text does reach it and does get the identity line.
    delivered = _pipeline("O agendamento foi recebido.", [])
    assert finalize_calls == ["O agendamento foi recebido."]
    assert "assistente do Dr. Victor Almeida" in delivered


def test_non_whatsapp_platforms_are_untouched():
    """Only the WhatsApp secretary uses this marker."""
    assert _sanitize_gateway_final_response("telegram", "[ SILENCIOSO ]") is not None
