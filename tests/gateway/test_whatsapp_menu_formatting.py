"""The deterministic secretary menus must survive the outbound pipeline.

Formatting the menu at the source had no visible effect before, because two
downstream steps destroyed it on every message:

  * ``_whatsapp_finalize_secretary_response`` ran ``re.sub(r"\\s+", " ", …)``,
    collapsing newlines, so a five-option menu arrived as one paragraph.
  * ``_sanitize_gateway_final_response`` deleted every ``*``, so WhatsApp bold
    was impossible — including the ``*Beneficiário:*`` / ``*Pagamento:*``
    labels the payment summary already intended to emphasize.

These tests pin the rendered result, not the source constant, so the layout
cannot regress again by a change made anywhere in the chain.
"""
from __future__ import annotations

import pytest

from gateway.platforms.whatsapp_appointments import (
    _INITIAL_MENU,
    _SERVICE_MENU,
    _SERVICES,
)
from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin
from gateway.run import (
    _sanitize_gateway_final_response,
    _whatsapp_finalize_secretary_response,
    _whatsapp_collapse_horizontal_space,
    _whatsapp_normalize_emphasis,
)


_IDENTIFIED_HISTORY = [
    {"role": "assistant", "content": "Aqui é a assistente do Dr. Victor Almeida."}
]


class _Formatter(WhatsAppBehaviorMixin):
    """Just the outbound formatter, without a live bridge."""

    def _sanitize_outbound_text(self, content: str) -> str:
        return content


def _delivered(text: str, *, trusted: bool = True) -> str:
    """Exactly the bytes a patient receives.

    Includes ``format_message`` on purpose. It is the last step before the
    bridge and it reads its input as Markdown, so leaving it out of these
    tests hides the failure it causes: a menu written ``*1*`` (WhatsApp bold)
    is read as Markdown *italic* there and shipped as ``_1_``. Emphasis is
    therefore authored as Markdown ``**1**`` and only becomes ``*1*`` here.
    """
    out = _sanitize_gateway_final_response("whatsapp", text, trusted_source=trusted)
    assert out is not None, "menu was suppressed outright"
    out = _whatsapp_finalize_secretary_response(out, _IDENTIFIED_HISTORY)
    return _Formatter().format_message(out)


class TestInitialMenuLayout:
    def test_every_option_is_on_its_own_line(self):
        delivered = _delivered(_INITIAL_MENU)
        for number in ("1", "2", "3", "4", "5"):
            matches = [
                line for line in delivered.splitlines()
                if line.startswith(f"*{number}*")
            ]
            assert len(matches) == 1, f"option {number} not alone on a line"

    def test_options_are_bold_in_whatsapp_syntax(self):
        delivered = _delivered(_INITIAL_MENU)
        assert "*1*" in delivered
        # Markdown double-asterisk renders literally in WhatsApp.
        assert "**" not in delivered

    def test_no_free_return_wording(self):
        """There is no free return — it is a sequential consultation."""
        delivered = _delivered(_INITIAL_MENU).lower()
        assert "gratuito" not in delivered
        assert "consulta sequencial" in delivered

    def test_closes_with_a_call_to_action(self):
        assert "número da opção" in _delivered(_INITIAL_MENU)

    def test_identity_line_survives(self):
        assert "assistente do Dr. Victor Almeida" in _delivered(_INITIAL_MENU)

    def test_blank_line_separates_intro_from_options(self):
        assert "\n\n*1*" in _delivered(_INITIAL_MENU)


class TestServiceMenuLayout:
    def test_every_option_is_on_its_own_line(self):
        delivered = _delivered(_SERVICE_MENU)
        for number in ("1", "2", "3"):
            matches = [
                line for line in delivered.splitlines()
                if line.startswith(f"*{number}*")
            ]
            assert len(matches) == 1, f"option {number} not alone on a line"

    def test_package_option_uses_the_corrected_wording(self):
        delivered = _delivered(_SERVICE_MENU).lower()
        assert "consulta sequencial" in delivered
        assert "gratuito" not in delivered

    def test_trailing_quote_is_not_chewed_off(self):
        """`.strip('… . " …')` used to leave the quote hanging open."""
        assert 'pergunte "quanto custa".' in _delivered(_SERVICE_MENU)


class TestServiceLabelWording:
    def test_package_label_has_no_free_return(self):
        label = _SERVICES[2]["label"].lower()
        assert "gratuito" not in label
        assert "sequencial" in label


class TestChoiceParsingUnaffectedByLayout:
    """Display changed; what a patient may type must not shrink."""

    @pytest.mark.parametrize(
        "typed", ["2", "800", "r$ 800", "pacote", "retorno", "sequencial"],
    )
    def test_package_choice_accepts_old_and_new_vocabulary(self, typed):
        from gateway.platforms.whatsapp_appointments import (
            WhatsAppAppointmentsHandler,
        )

        assert WhatsAppAppointmentsHandler._service_choice(typed) == 2

    @pytest.mark.parametrize("typed,expected", [("1", 1), ("3", 3)])
    def test_other_choices_still_resolve(self, typed, expected):
        from gateway.platforms.whatsapp_appointments import (
            WhatsAppAppointmentsHandler,
        )

        assert WhatsAppAppointmentsHandler._service_choice(typed) == expected

    def test_sequential_phrasing_is_recognized_as_appointment_intent(self):
        from gateway.platforms.whatsapp_appointments import _APPOINTMENT_PATTERNS

        for phrase in ("quero minha consulta sequencial", "retorno do pacote"):
            assert any(p.search(phrase) for p in _APPOINTMENT_PATTERNS), phrase


class TestHorizontalSpaceCollapse:
    def test_newlines_survive(self):
        assert _whatsapp_collapse_horizontal_space("a\nb\nc") == "a\nb\nc"

    def test_runs_of_spaces_and_tabs_still_collapse(self):
        assert _whatsapp_collapse_horizontal_space("a  \t b") == "a b"

    def test_line_edges_are_trimmed(self):
        assert _whatsapp_collapse_horizontal_space("a   \n   b") == "a\nb"

    def test_blank_line_kept_but_runs_collapse(self):
        assert _whatsapp_collapse_horizontal_space("a\n\n\n\nb") == "a\n\nb"

    def test_carriage_returns_normalized(self):
        assert _whatsapp_collapse_horizontal_space("a\r\nb") == "a\nb"


class TestEmphasisNormalization:
    def test_markdown_emphasis_passes_through_untouched(self):
        """Conversion belongs to format_message; doing it here double-converts."""
        assert _whatsapp_normalize_emphasis("**Total**") == "**Total**"
        assert _whatsapp_normalize_emphasis("*Total*") == "*Total*"

    def test_markdown_bold_becomes_whatsapp_bold_at_the_transport(self):
        assert "*Total*" in _delivered("**Total** a pagar")

    def test_markdown_bold_does_not_arrive_as_italic(self):
        """The regression this whole chain exists to prevent."""
        assert "_Total_" not in _delivered("**Total** a pagar")

    def test_unmatched_asterisk_is_dropped(self):
        # The helper only removes the character; the space run it leaves
        # behind is collapsed by the sanitizer immediately afterwards, so
        # assert the delivered form too rather than pinning the interim.
        assert "*" not in _whatsapp_normalize_emphasis("2 * 3 = 6")
        assert "2 3 = 6" in _delivered("O cálculo é 2 * 3 = 6.")

    def test_markdown_bullet_becomes_a_real_bullet(self):
        assert _whatsapp_normalize_emphasis("* item") == "• item"

    def test_text_without_asterisks_is_untouched(self):
        assert _whatsapp_normalize_emphasis("nada aqui") == "nada aqui"

    def test_payment_summary_labels_keep_their_bold(self):
        """These were written bold and silently flattened before."""
        delivered = _delivered(
            "Resumo:\n**Beneficiário:** Clínica\n**Pagamento:** PIX"
        )
        assert "*Beneficiário:*" in delivered
        assert "*Pagamento:*" in delivered
        assert "_Beneficiário:_" not in delivered


class TestTrustedDeterministicFlowClosesTheChain:
    """The booking funnel has to be able to reach its own last step.

    The price and fake-appointment guards were written for LLM output in
    jun/2026; the deterministic flow inherited them in ago/2026 and could no
    longer state a price or confirm a booking — a patient walked the whole
    chain and got "Obrigado. O Dr. Victor verificará sua mensagem
    pessoalmente." at the end.
    """

    _BLOCKED = "Obrigado. O Dr. Victor verificará sua mensagem pessoalmente."

    _STEPS = {
        "price list": "Os valores são:\nConsulta presencial — R$ 600",
        "summary in-person": (
            "Resumo do agendamento:\nConsulta presencial — R$ 600\n"
            "12/08/2026 às 14:00 (Brasília)\n"
            "Responda CONFIRMAR para autorizar uma única vez ou ALTERAR."
        ),
        "summary teleconsult": (
            "Resumo do agendamento:\nTeleconsulta — R$ 300\n"
            "12/08/2026 às 14:00 (Brasília)\n*Pagamento:* PIX"
        ),
        "confirmed": "Agendamento confirmado para 12/08/2026 às 14:00 (Brasília).",
    }

    @pytest.mark.parametrize("step", sorted(_STEPS))
    def test_trusted_flow_step_is_delivered(self, step):
        out = _sanitize_gateway_final_response(
            "whatsapp", self._STEPS[step], trusted_source=True
        )
        assert out is not None
        assert out.strip() != self._BLOCKED

    @pytest.mark.parametrize("step", sorted(_STEPS))
    def test_same_text_from_the_model_is_still_blocked(self, step):
        """The guards must not weaken for anything a model wrote."""
        out = _sanitize_gateway_final_response("whatsapp", self._STEPS[step])
        assert out.strip() == self._BLOCKED

    def test_default_is_untrusted(self):
        """Callers must opt in; forgetting the flag fails closed."""
        assert (
            _sanitize_gateway_final_response("whatsapp", self._STEPS["confirmed"]).strip()
            == self._BLOCKED
        )

    def test_trusted_still_strips_leaked_internals(self):
        """Trust covers money and bookings, not everything."""
        out = _sanitize_gateway_final_response(
            "whatsapp",
            "terminal: rm -rf /\nAgendamento confirmado para 12/08/2026 às 14:00.",
            trusted_source=True,
        )
        assert "terminal:" not in out

    def test_trusted_still_suppresses_provider_errors(self):
        out = _sanitize_gateway_final_response(
            "whatsapp",
            "HTTP 503: This model is currently experiencing high demand.",
            trusted_source=True,
        )
        assert out is None


class TestFinalizerStillGuards:
    """The layout fix must not weaken the fail-closed identity rules."""

    def test_identity_is_still_injected_when_absent(self):
        out = _whatsapp_finalize_secretary_response("Segue o horário.", [])
        assert "assistente do Dr. Victor Almeida" in out

    def test_empty_input_still_returns_none(self):
        assert _whatsapp_finalize_secretary_response("   \n  ", []) is None

    def test_multiline_input_keeps_its_lines(self):
        out = _whatsapp_finalize_secretary_response(
            "Bom dia.\n\n*1* - Uma\n*2* - Duas", _IDENTIFIED_HISTORY,
        )
        assert "*1* - Uma\n*2* - Duas" in out
