"""Regression tests for the WhatsApp secretary prompt text.

These checks protect against two regressions observed in the Benemax case:
- first-response greetings being emitted even when the user sent a file/document
- the "Obrigado, sem interesse." fallback being used for non-commercial requests
"""

from pathlib import Path


def test_whatsapp_secretary_prompt_mentions_document_handling_and_limits_commercial_rejection():
    source = Path(__file__).resolve().parents[2] / "gateway" / "run.py"
    text = source.read_text(encoding="utf-8")

    assert "DOCUMENTOS/PEDIDOS" in text
    assert "documento, arquivo" in text
    assert "cobrança, relatório" in text
    assert "somente para prospecção claramente comercial" in text
    assert "pedido de envio" in text
    assert "solicitação clara" in text
