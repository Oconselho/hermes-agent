"""Regression tests for the WhatsApp secretary's clinical-scope guard.

Incident 09-10/ago/2026. Two patients wrote to the clinic's WhatsApp about an
insulin pump. The secretary — a scheduling secretary — answered with clinical
conduct: capillary glucose measurement, ketone checks, backup insulin, pump
handling. Nothing in the pipeline stopped it.

Cause was two changes that only became an incident together:
  * 7a9579f21 (28/jul) removed the prompt's containment rule
    ("NUNCA responda perguntas. NUNCA dê informações além do template.")
    in favour of "responda ao ponto quando for seguro" — handing the judgement
    of what is safe to the model.
  * 6c0b3c42c (07/ago) switched the WhatsApp model from gemini-3.5-flash to
    gpt-5.6-luna, a reasoning model that took that judgement and answered.

Every ``BLOCKED_*`` string below is text the secretary ACTUALLY sent to a real
patient. They are fixtures, not inventions — if this file ever goes green
because someone loosened the detector, the clinic ships medical advice again.

The ``ALLOWED_*`` strings are the secretary's own approved templates. Escalating
to reception or to emergency services is the correct behaviour and must never be
caught by the guard.
"""

import pytest

from gateway.run import (
    _WHATSAPP_CLINICAL_REFUSAL as REFUSAL,
    _WHATSAPP_SANITIZER_FALLBACKS,
    _sanitize_gateway_final_response,
    _whatsapp_safe_transcript_echo,
)
from gateway.config import Platform


# ── Sent to 'Vinicius Teles' (5575…) on 10/ago/2026 06:58-07:00 BRT ──────────
BLOCKED_VINICIUS = [
    "Entendi: o sensor não está funcionando. Enquanto isso, não tome decisões "
    "pela leitura do sensor; se tiver glicosímetro e tiras, faça medições de "
    "glicemia capilar e registre os valores. Não altere a programação ou as "
    "doses da bomba sem orientação.",
    "Não altere as configurações da bomba. Se a glicemia estiver muito alta, "
    "houver cetonas, náuseas, vômitos, dor abdominal, respiração acelerada ou "
    "sonolência, procure uma emergência imediatamente ou ligue 192.",
    "Não reinicie a bomba nem altere doses por conta própria. Enquanto aguarda, "
    "faça glicemia capilar, se possível, e verifique cetonas conforme o plano "
    "prescrito.",
]

# ── Sent to 'more 🤎' (5575…) on 09/ago/2026 22:01-22:07 BRT ─────────────────
BLOCKED_MORE = [
    "Como a bomba resetou e o sensor não foi reconhecido, verifique agora a "
    "glicemia com aparelho de ponta de dedo e siga seu plano de contingência "
    "previamente orientado para falha da bomba, sem interromper a insulina basal.",
    "Siga o procedimento de troca do conjunto e o plano de contingência "
    "orientado para falha da bomba, usando sua insulina de backup conforme "
    "prescrição.",
    "Não reinicie a infusão pela bomba nem administre doses adicionais por ela "
    "sem confirmar a configuração e o funcionamento, para evitar duplicidade "
    "de insulina.",
    "Mantenha o esquema pela caneta exatamente conforme a orientação já "
    "prescrita e monitore a glicemia com mais frequência durante a noite, "
    "inclusive antes de dormir e ao acordar.",
    "Mantenha as medições por glicemia capilar enquanto o sensor não for "
    "reconhecido e siga o procedimento de ativação/reconexão indicado para "
    "esse modelo.",
]

# ── The secretary's own approved templates (gateway/run.py prompt) ───────────
ALLOWED_TEMPLATES = [
    # Category F — the approved emergency escalation. Must survive.
    "Este canal não atende urgência. Procure emergência imediatamente ou ligue 192.",
    # Category A — patient redirected to reception.
    "Bom dia. Aqui é a assistente do Dr. Victor Almeida. Para falar sobre "
    "consultas, relatórios ou agendamento, por favor entre em contato com a "
    "recepção pelo WhatsApp 71996691002. Elas poderão ajudar com todas as "
    "informações.",
    # Category G step 1 — scheduling identification.
    "Boa tarde. Aqui é a assistente do Dr. Victor Almeida. Para verificar a "
    "agenda e os horários disponíveis, vou precisar confirmar seu cadastro. "
    "Pode me informar seu CPF (apenas números)?",
    # Category C — spam.
    "Bom dia. Aqui é a assistente do Dr. Victor Almeida. Agradecemos o "
    "contato, mas não temos interesse. Obrigada.",
    # Address — plain institutional info.
    "Bom dia. O consultório fica no CEO Salvador Shopping, Torre Londres, "
    "Sala 1616.",
    # Exam logistics are the secretary's job, not clinical conduct.
    "Boa tarde. O exame deve ser feito em jejum de 8 horas. Qualquer dúvida, "
    "a recepção pode orientar.",
    "Bom dia. Para remarcar seu exame, entre em contato com a recepção pelo "
    "WhatsApp 71996691002.",
]


@pytest.mark.parametrize("text", BLOCKED_VINICIUS + BLOCKED_MORE)
def test_clinical_conduct_is_replaced_with_the_refusal(text):
    """Real advice the secretary sent must never reach a patient again."""
    result = _sanitize_gateway_final_response(Platform.WHATSAPP, text)
    assert result == REFUSAL, f"clinical text survived the guard: {text[:80]!r}"


@pytest.mark.parametrize("text", BLOCKED_VINICIUS + BLOCKED_MORE)
def test_clinical_conduct_leaks_no_original_wording(text):
    """The refusal must not carry fragments of the blocked advice."""
    result = _sanitize_gateway_final_response(Platform.WHATSAPP, text)
    for term in ("glicemia", "cetona", "insulina", "bomba", "sensor", "dose"):
        assert term not in result.lower(), f"{term!r} leaked into the refusal"


@pytest.mark.parametrize("text", ALLOWED_TEMPLATES)
def test_approved_templates_are_not_blocked(text):
    """Escalating to reception or emergency is correct, not clinical conduct."""
    result = _sanitize_gateway_final_response(Platform.WHATSAPP, text)
    assert result != REFUSAL, f"guard swallowed an approved template: {text[:80]!r}"
    assert result, "approved template must not be silenced"


def test_refusal_is_idempotent():
    """The refusal must survive its own guard, or a retry could loop it away."""
    assert _sanitize_gateway_final_response(Platform.WHATSAPP, REFUSAL) == REFUSAL


def test_emergency_escalation_keeps_192():
    """The one number a patient must always still receive."""
    text = "Este canal não atende urgência. Procure emergência imediatamente ou ligue 192."
    assert "192" in _sanitize_gateway_final_response(Platform.WHATSAPP, text)


def test_trusted_feegow_text_bypasses_the_clinical_guard():
    """The deterministic booking flow composes its own literals — never a model.

    ``trusted_source`` already exempts the price and fake-appointment guards
    (b5616249a); the clinical guard must behave the same way or the booking
    chain breaks again the moment a procedure name mentions a clinical term.
    """
    text = "Consulta de controle de diabetes agendada. Ajuste de dose de insulina."
    assert _sanitize_gateway_final_response(
        Platform.WHATSAPP, text, trusted_source=True
    ) != REFUSAL


def test_clinical_guard_does_not_apply_to_other_surfaces():
    """Telegram is Victor's own admin channel — it must keep raw text."""
    text = "faça glicemia capilar e verifique cetonas"
    assert _sanitize_gateway_final_response(Platform.TELEGRAM, text) != REFUSAL


# ── Transcript echo: the outbound path that bypassed every sanitizer ─────────

def test_transcript_echo_is_sanitized_on_whatsapp():
    echo = _whatsapp_safe_transcript_echo(Platform.WHATSAPP, '🎙️ "Bom dia, tudo certo"')
    assert echo is not None
    assert "🎙️" not in echo, "emoji must be stripped on the clinic's public line"


def test_transcript_echo_drops_clinical_content():
    """A voice note echoed verbatim is an unaudited outbound path.

    It is not stored in state.db and not counted in the 'Sending response' log
    line, so anything it ships is invisible to an audit. Fail closed.
    """
    echo = _whatsapp_safe_transcript_echo(
        Platform.WHATSAPP, '🎙️ "faça glicemia capilar e verifique cetonas"'
    )
    assert echo is None


def test_transcript_echo_drops_canned_fallbacks():
    """A fallback echoed back would read as if the contact had said it."""
    for fallback in _WHATSAPP_SANITIZER_FALLBACKS:
        assert _whatsapp_safe_transcript_echo(Platform.WHATSAPP, fallback) is None


def test_transcript_echo_untouched_on_other_platforms():
    """The STT echo call site is shared; only WhatsApp changes behaviour."""
    raw = '🎙️ "qualquer coisa"'
    assert _whatsapp_safe_transcript_echo(Platform.TELEGRAM, raw) == raw
