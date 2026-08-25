"""The stay-silent marker must be recognised however the model spells it.

The prompt asks for a bare ``[SILENCIOSO]``. The suppressor compared that
string literally, so when gpt-5.6-luna wrote ``[ SILENCIOSO ]`` the marker was
treated as ordinary text and delivered. Four patients received it between 09
and 11/ago/2026 — three as
``Aqui é a assistente do Dr. Victor Almeida. [ SILENCIOSO ]`` (the finalizer
prepends identity on a fresh conversation) and one bare, once identity had
already been disclosed earlier in the session.

Then the same model spaced out the letters — ``[ S I L E N C I O S O ]`` — and
the regex written for the first leak, which described the decoration *around*
the word, had nothing to say about a separator INSIDE it. Five of those
between 09 and 25/ago/2026; four reached contacts. So the recogniser stopped
enumerating shapes and started erasing decoration: keep letters and digits,
require the remainder to BE the word. ``SPACED_OUT_VARIANTS`` below is that
class, and it is deliberately wider than what production has shown.

The variants are the real ones from production plus the spellings a model
plausibly reaches for. The second half of the file is the other half of the
contract: text that merely *mentions* the word is real text and must survive.
"""

from __future__ import annotations

import pytest

from gateway.response_filters import (
    is_intentional_silence_agent_result,
    is_internal_control_artifact,
    reads_as_human_message,
    whatsapp_reply_disposition,
)
from gateway.run import (
    _normalize_whatsapp_silence_decision,
    _queued_followup_resend_text,
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


# The second spelling that reached contacts: the letters themselves pulled
# apart. The first entry is byte-for-byte what Nilvo Luiz Cassol received on
# 25/ago/2026 at 13:07 BRT; the rest are the same idea with other separators,
# because the point of the fix is that the separator does not matter.
SPACED_OUT_VARIANTS = [
    "[ S I L E N C I O S O ]",   # 25/ago/2026 13:07 BRT — 23 chars, delivered
    "S I L E N C I O S O",
    "[S I L E N C I O S O]",
    "[ s i l e n c i o s o ]",
    "S.I.L.E.N.C.I.O.S.O",
    "S-I-L-E-N-C-I-O-S-O",
    "S_I_L_E_N_C_I_O_S_O",
    "[ S  I  L  E  N  C  I  O  S  O ]",
    "Sil enc ioso",
    "[ S I L E N C I O S A ]",
    "**S I L E N C I O S O**",
    "[ S I L E N C I O S O ].",
    "[\u00a0S\u00a0I\u00a0L\u00a0E\u00a0N\u00a0C\u00a0I\u00a0O\u00a0S\u00a0O\u00a0]",  # non-breaking spaces
]

SILENT_VARIANTS += SPACED_OUT_VARIANTS


@pytest.mark.parametrize("variant", SILENT_VARIANTS)
def test_marker_variants_are_recognised(variant):
    assert _whatsapp_is_silence_marker(variant) is True


@pytest.mark.parametrize("variant", SILENT_VARIANTS)
def test_marker_variants_never_reach_a_patient(variant):
    assert _sanitize_gateway_final_response("whatsapp", variant) is None


REAL_TEXT = [
    "Bom dia! Aqui é a assistente do Dr. Victor Almeida.",
    # Erasing decoration must not start swallowing prose: each of these keeps
    # at least one letter the marker does not have, so the equality fails.
    "silencioso2",
    "S I L E N C I O S O agora",
    "Não silencioso",
    "silenciosos",
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


# ---------------------------------------------------------------------------
# The resend before a queued follow-up
# ---------------------------------------------------------------------------
#
# Second leak of the same marker, by a different road. When a message arrives
# while the agent is still working, the gateway resends "the first response"
# before processing the queued one. That branch reads the agent-result dict,
# which holds the model's RAW text — the delivery path sanitizes only its own
# local copy. And ``_suppress_silence_marker`` leaves the delivery flags False
# on purpose ("nothing was delivered"), so intentional silence ALWAYS looks
# like an unconfirmed delivery there and got resent verbatim.
#
# Six times between 03 and 18/ago/2026: 03/ago 16:33 BRT (twice), 13/ago 12:22,
# and 18/ago 14:02 / 14:05 / 14:08 — the last three to Rapidoc Telemedicina,
# a partner clinic, while it sent a burst of exam images.


@pytest.mark.parametrize("variant", SILENT_VARIANTS)
def test_a_marker_is_never_resent_before_a_queued_followup(variant):
    assert _queued_followup_resend_text("whatsapp", variant) == ""


@pytest.mark.parametrize("text", REAL_TEXT)
def test_real_text_is_still_resent(text):
    """Withholding a real reply would lose it: the resend is its only delivery.

    Not asserted as byte-identical on purpose — the sanitizer also normalises
    tone ("Bom dia!" becomes "Bom dia."), which the resend used to skip along
    with everything else. What matters here is that real text survives.
    """
    resent = _queued_followup_resend_text("whatsapp", text)
    assert resent, "a real reply must still be delivered"
    # Text that merely mentions the word keeps it: swallowing these would be
    # the opposite failure — a real answer lost to an over-eager guard.
    assert resent == _sanitize_gateway_final_response("whatsapp", text)


@pytest.mark.parametrize("value", ["", None, "   ", "\n"])
def test_nothing_to_resend_is_empty_not_an_error(value):
    assert _queued_followup_resend_text("whatsapp", value) == ""


def test_the_exact_burst_that_reached_the_partner_clinic():
    """Regression pin: what Rapidoc received three times on 18/ago/2026."""
    assert _queued_followup_resend_text("whatsapp", "[SILENCIOSO]") == ""


def test_the_resend_applies_the_same_sanitizer_as_the_delivery_path():
    """The point is not one marker — it is that this branch stopped bypassing
    the guards. Any text the delivery path would refuse must be refused here
    too, so the two can never drift apart again.
    """
    for probe in SILENT_VARIANTS + REAL_TEXT:
        delivered = _sanitize_gateway_final_response("whatsapp", probe)
        assert _queued_followup_resend_text("whatsapp", probe) == (delivered or "")


# ---------------------------------------------------------------------------
# The third road: the letters pulled apart
# ---------------------------------------------------------------------------
#
# 09/ago 10:45, 17/ago 13:15, 20/ago 08:35, 22/ago 22:20 and 25/ago 13:07 BRT
# (all times BRT). Four were delivered — Samuel Bach/Rapidoc, Lígia Souza,
# 5511995631610 and Nilvo Luiz Cassol. Three of those four happened AFTER the
# 18/ago fix, which is what makes this its own road rather than a relapse.


@pytest.mark.parametrize("variant", SPACED_OUT_VARIANTS)
def test_spaced_out_letters_are_the_marker(variant):
    assert _whatsapp_is_silence_marker(variant) is True


@pytest.mark.parametrize("variant", SPACED_OUT_VARIANTS)
def test_spaced_out_letters_never_reach_a_contact(variant):
    assert _sanitize_gateway_final_response("whatsapp", variant) is None


@pytest.mark.parametrize("variant", SPACED_OUT_VARIANTS)
def test_spaced_out_letters_are_not_resent_before_a_queued_followup(variant):
    assert _queued_followup_resend_text("whatsapp", variant) == ""


def test_the_exact_string_nilvo_received():
    """Regression pin: 23 characters, 25/ago/2026 13:07 BRT, chat 557799711049.

    He had just written "Obrigado não é urgente ok" — a message the secretary
    was right to stay silent on. The silence decision was correct; only its
    spelling escaped.
    """
    leaked = "[ S I L E N C I O S O ]"
    assert len(leaked) == 23
    assert _whatsapp_is_silence_marker(leaked) is True
    assert _sanitize_gateway_final_response("whatsapp", leaked) is None
    assert _queued_followup_resend_text("whatsapp", leaked) == ""


def test_the_adapter_backstop_also_blocks_the_spaced_form():
    """Both roads share one recogniser, so fixing it fixes both at once.

    That was the design decision of 18/ago/2026 and this is the first time it
    paid off: the adapter's outbound block imports the same function, so it
    needed no change of its own.
    """
    from plugins.platforms.whatsapp.adapter import (
        is_whatsapp_silence_marker as adapter_recogniser,
    )

    assert adapter_recogniser is _whatsapp_is_silence_marker
    for variant in SPACED_OUT_VARIANTS:
        assert adapter_recogniser(variant) is True


def test_the_identity_prefixed_spaced_form_alcina_received():
    """09/ago/2026 10:45 BRT — 66 chars, and the shape that hid the delivery.

    The log reads ``response ready ... 23 chars`` then
    ``Sending response (66 chars)``, which looks at a glance like the marker
    was stopped and something else sent. It was not: 42 (the identity line) +
    1 + 23 (the marker) = 66. The finalizer had assembled identity around a
    marker the sanitizer let through — exactly the shape of the 09–11/ago
    leaks, in the spelling the 18/ago fix did not cover.

    So the assembled line must still be ordinary text (suppressing it would
    hide real replies), and the bare marker must stop before the identity step
    can build it.
    """
    identity = "Aqui é a assistente do Dr. Victor Almeida."
    marker = "[ S I L E N C I O S O ]"
    assembled = f"{identity} {marker}"
    assert len(assembled) == 66

    assert _whatsapp_is_silence_marker(assembled) is False
    assert _sanitize_gateway_final_response("whatsapp", marker) is None


# ---------------------------------------------------------------------------
# The class, not the spelling
# ---------------------------------------------------------------------------
#
# Victor, 25/ago/2026, after the third leak: "a decisão [ S I L E N C I O S O ]
# é pensamento e não pode ser mostrado ao contato. apenas executar a ação de
# ficar em silêncio."
#
# He is right that recognising one more spelling is not the fix. Every leak so
# far came from a recogniser that was a LIST, and a list is always a step
# behind the model. So the guard below asks a structural question — is the
# whole reply a delimited token instead of something a person would say? — and
# the failure mode flips: an unrecognised token now becomes silence, not a
# delivery.


UNKNOWN_CONTROL_TOKENS = [
    "[QUIETO]",
    "[NAO RESPONDER]",
    "[NÃO RESPONDER]",
    "[SEM RESPOSTA]",
    "[IGNORAR]",
    "[NO_ANSWER]",
    "[skip]",
    "{silence}",
    "<sem_resposta>",
    "(empty)",
    "**[AGUARDAR]**",
    "[ Q U I E T O ]",
]


@pytest.mark.parametrize("token", UNKNOWN_CONTROL_TOKENS)
def test_a_token_we_never_taught_it_is_still_never_delivered(token):
    """The whole point: no list of spellings is consulted here."""
    assert is_internal_control_artifact(token) is True
    assert _sanitize_gateway_final_response("whatsapp", token) is None
    assert _queued_followup_resend_text("whatsapp", token) == ""


# Prose in brackets is prose. The secretary may legitimately say these, and
# suppressing them would be the opposite failure: a real answer lost.
BRACKETED_PROSE = [
    "(Já enviei.)",
    "(Se preferir, posso pedir à recepção.)",
    "(71996691002)",
    "(71) 99669-1002",
    "[Confirmo o horário das 14h, tudo certo para amanhã.]",
]


@pytest.mark.parametrize("text", BRACKETED_PROSE)
def test_prose_in_brackets_is_still_delivered(text):
    assert is_internal_control_artifact(text) is False
    assert _sanitize_gateway_final_response("whatsapp", text) is not None


@pytest.mark.parametrize("text", REAL_TEXT)
def test_the_structural_guard_never_touches_ordinary_replies(text):
    assert is_internal_control_artifact(text) is False


def test_the_two_guards_are_complementary_not_redundant():
    """One knows the word and accepts any decoration; the other knows the
    shape and accepts any word. Neither subsumes the other, and that is why
    both are kept.
    """
    # Shape-only: the structural guard has never heard of "QUIETO".
    assert is_internal_control_artifact("[QUIETO]") is True
    assert _whatsapp_is_silence_marker("[QUIETO]") is False

    # Word-only: a bare marker has no delimiters at all, so the structural
    # guard does not see a token — the word guard is what catches it.
    assert _whatsapp_is_silence_marker("silencioso") is True
    assert is_internal_control_artifact("silencioso") is False

    # And a bare ordinary word must survive both: "Obrigada." is a real reply,
    # which is exactly why the structural guard requires the delimiters.
    assert _whatsapp_is_silence_marker("Obrigada.") is False
    assert is_internal_control_artifact("Obrigada.") is False
    assert _sanitize_gateway_final_response("whatsapp", "Obrigada.") is not None


def test_the_adapter_backstop_asks_both_questions_too():
    from plugins.platforms.whatsapp.adapter import (
        is_internal_control_artifact as adapter_structural,
        is_whatsapp_silence_marker as adapter_word,
    )

    assert adapter_structural is is_internal_control_artifact
    assert adapter_word is _whatsapp_is_silence_marker
    for token in UNKNOWN_CONTROL_TOKENS + SPACED_OUT_VARIANTS:
        assert adapter_structural(token) or adapter_word(token)


# ---------------------------------------------------------------------------
# The door: a decision stops being a string in flight
# ---------------------------------------------------------------------------
#
# Everything above this line is remediation — guards that recognise a token
# while it travels toward the contact. Victor, 25/ago/2026: "chega de
# remediação."
#
# The gateway always had an out-of-band channel for "this turn sends nothing":
# ``_whatsapp_silent_agent_result`` puts it in the envelope and
# ``is_intentional_silence_agent_result`` reads it there. Gateway-originated
# silence used it from the start. Model-originated silence did not — it rode
# inside the field that holds the contact's text. That is in-band signalling,
# and it failed the way in-band signalling always fails.
#
# ``_normalize_whatsapp_silence_decision`` runs once, where the agent's result
# is born, before any road reads it. After it, the token does not exist.


class _WhatsAppSource:
    class _Platform:
        value = "whatsapp"

    platform = _Platform()


class _TelegramSource:
    class _Platform:
        value = "telegram"

    platform = _Platform()


DECISIONS_THE_MODEL_MIGHT_WRITE = (
    SILENT_VARIANTS + UNKNOWN_CONTROL_TOKENS + ["NO_REPLY", "SECRETARY_MODEL_TEST_OK"]
)


@pytest.mark.parametrize("written", DECISIONS_THE_MODEL_MIGHT_WRITE)
def test_the_decision_never_survives_the_door_as_text(written):
    result = {"final_response": written, "messages": [], "api_calls": 1}

    _normalize_whatsapp_silence_decision(result, _WhatsAppSource())

    assert result["final_response"] == "", "the token is still a string in flight"
    assert result["gateway_intentional_silence"], "the decision must be on the envelope"
    # And the envelope is what the delivery path reads, so the empty text is
    # never mistaken for a model failure and rewritten into an error message.
    assert is_intentional_silence_agent_result(result, result["final_response"]) is True


@pytest.mark.parametrize("written", DECISIONS_THE_MODEL_MIGHT_WRITE)
def test_the_road_that_leaked_in_agosto_has_nothing_left_to_read(written):
    """03–18/ago/2026: the queued-follow-up resend read the RAW agent dict and
    resent it verbatim — six messages to three contacts.

    That road was fixed by teaching it to sanitize. This is the stronger
    statement: there is nothing there to sanitize any more.
    """
    result = {"final_response": written, "messages": [], "api_calls": 1}
    _normalize_whatsapp_silence_decision(result, _WhatsAppSource())

    assert _queued_followup_resend_text("whatsapp", result["final_response"]) == ""


@pytest.mark.parametrize("real", [
    "Bom dia. Recebi seu exame.",
    "Olá. Envie seu recado.",
    "Obrigada.",
    "A recepção atende pelo WhatsApp: 71 99669-1002.",
    "O exame precisa ser feito em ambiente silencioso.",
])
def test_real_speech_passes_the_door_untouched(real):
    result = {"final_response": real, "messages": [], "api_calls": 1}
    _normalize_whatsapp_silence_decision(result, _WhatsAppSource())
    assert result["final_response"] == real
    assert "gateway_intentional_silence" not in result


def test_a_failed_turn_is_not_a_decision():
    """A failure must stay visible: the error path exists to surface it, and
    silently swallowing it would hide outages behind "the model chose silence".
    """
    result = {"final_response": "[SILENCIOSO]", "failed": True, "messages": []}
    _normalize_whatsapp_silence_decision(result, _WhatsAppSource())
    assert result["final_response"] == "[SILENCIOSO]"
    assert "gateway_intentional_silence" not in result
    assert is_intentional_silence_agent_result(result, "[SILENCIOSO]") is False


def test_only_whatsapp_is_normalised():
    """The marker is the WhatsApp secretary's contract. Other platforms keep
    their own text untouched — this door is not a global rewrite.
    """
    result = {"final_response": "[SILENCIOSO]", "messages": []}
    _normalize_whatsapp_silence_decision(result, _TelegramSource())
    assert result["final_response"] == "[SILENCIOSO]"


def test_the_door_is_idempotent():
    """It runs once per turn today, but a second call must not invent silence
    out of the empty string it just wrote.
    """
    result = {"final_response": "[SILENCIOSO]", "messages": []}
    _normalize_whatsapp_silence_decision(result, _WhatsAppSource())
    first = dict(result)
    _normalize_whatsapp_silence_decision(result, _WhatsAppSource())
    assert result == first


# ---------------------------------------------------------------------------
# The positive contract
# ---------------------------------------------------------------------------
#
# Calibrated on all 762 replies the secretary has ever produced: every real one
# carries lowercase letters, every internal artifact carries none.


# Words, but not one lowercase letter among them. That is the whole test.
NOT_MESSAGES_FOR_A_PERSON = [
    "NO_REPLY",
    "NO REPLY",
    "SILENT",
    "SECRETARY_MODEL_TEST_OK",
    "[SILENCIOSO]",
    "ERROR",
    "OK_DONE",
    "XYZZY_PLUGH",
]


@pytest.mark.parametrize("artifact", NOT_MESSAGES_FOR_A_PERSON)
def test_machinery_is_not_a_message(artifact):
    assert reads_as_human_message(artifact) is False
    assert _sanitize_gateway_final_response("whatsapp", artifact) is None


# Terse, but not machinery: no word in them at all, so the prose contract
# abstains and lets the shape guards judge. A bare phone number withheld would
# be the opposite failure \u2014 a patient who asked for a number and got silence.
TERSE_BUT_REAL = [
    "71 99669-1002",
    "(71996691002)",
    "14h",
    "\ud83d\udc4d",
]


@pytest.mark.parametrize("text", TERSE_BUT_REAL)
def test_the_prose_contract_abstains_on_wordless_replies(text):
    assert reads_as_human_message(text) is True


def test_the_spaced_marker_is_caught_by_shape_not_by_prose():
    """Worth pinning because it looks like an omission and is not.

    "[ S I L E N C I O S O ]" has no run of two consecutive letters, so the
    prose contract abstains on it exactly as it abstains on a phone number.
    The word guard is what stops it \u2014 which is the layering working, each test
    answering the question it is competent to answer.
    """
    spaced = "[ S I L E N C I O S O ]"
    assert reads_as_human_message(spaced) is True
    assert _whatsapp_is_silence_marker(spaced) is True
    assert _sanitize_gateway_final_response("whatsapp", spaced) is None


@pytest.mark.parametrize("real", REAL_TEXT + [
    "Olá. Envie seu recado.",
    "Recebido. O Dr. Victor verificará.",
    "A recepção atende pelo WhatsApp: 71 99669-1002.",
    "Combinado, Nilvo! Tenha uma excelente semana! 👍",
])
def test_every_real_reply_reads_as_a_message(real):
    assert reads_as_human_message(real) is True


def test_the_contract_is_positive_not_a_longer_blocklist():
    """The point of the whole change: a token nobody has ever seen, that no
    guard names, is still withheld — because it is not prose, not because it
    is on a list.
    """
    invented = "XYZZY_PLUGH_42"
    assert whatsapp_reply_disposition(invented) == "not-a-message"
    assert _whatsapp_is_silence_marker(invented) is False
    assert is_internal_control_artifact(invented) is False
    assert _sanitize_gateway_final_response("whatsapp", invented) is None


def test_the_withholding_is_logged_loudly(caplog):
    """Silence toward the contact, noise in the log. A reply withheld without a
    trace would be the same mistake in the other direction.
    """
    import logging as _logging

    with caplog.at_level(_logging.WARNING):
        assert _sanitize_gateway_final_response("whatsapp", "SECRETARY_MODEL_TEST_OK") is None
    assert any(
        "does not read as a message" in r.getMessage() for r in caplog.records
    ), "a withheld reply must be visible in the log"
