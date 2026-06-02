#!/usr/bin/env python3
"""Rebuild WhatsApp secretary functionality in gateway/run.py after git checkout wipe."""
import os, re

FILE = '/home/ubuntu/.hermes/hermes-agent/gateway/run.py'
with open(FILE) as f:
    text = f.read()

# -------------------------------------------------------
# BLOCK A: WhatsApp sanitization in _sanitize_gateway_final_response
# Replace the function to handle WhatsApp too
# -------------------------------------------------------
old_a = '''def _sanitize_gateway_final_response(platform: Any, text: str) -> str:
    """Sanitize final gateway replies before sending them to high-noise chats.

    Telegram is Bob's mobile inbox, so it should receive concise, safe provider
    failure categories instead of raw HTTP bodies, request IDs, or policy text.
    Other platforms keep the existing behaviour for now.
    """
    if not text:
        return text
    if _gateway_platform_value(platform) != "telegram":
        return text

    redacted = _redact_gateway_user_facing_secrets(str(text))
    if _looks_like_gateway_provider_error(redacted):
        return _gateway_provider_error_reply(redacted)
    return redacted'''

new_a = '''def _sanitize_gateway_final_response(platform: Any, text: str) -> str:
    """Sanitize final gateway replies before sending them to high-noise chats."""
    if not text:
        return text
    platform_value = _gateway_platform_value(platform)
    if platform_value == "whatsapp":
        cleaned = _redact_gateway_user_facing_secrets(str(text))
        # Block leaked tool names
        if re.search(r"(?im)^\\s*(terminal|execute_code|search_files|read_file|browser_[a-z_]+|skill_view|session_search)\\s*:", cleaned):
            return "Recebi sua mensagem. O Dr. Victor verificará assim que possível."
        # Block leaked XML tool call blocks (DeepSeek hallucination)
        cleaned = re.sub(r"<function_calls>.*?</function_calls>", "", cleaned, flags=re.S | re.I)
        cleaned = re.sub(r"<invoke[^>]*>.*?</invoke>", "", cleaned, flags=re.S | re.I)
        cleaned = re.sub(r"<tool_calls>.*?</tool_calls>", "", cleaned, flags=re.S | re.I)
        cleaned = re.sub(r"<parameter[^>]*>.*?</parameter>", "", cleaned, flags=re.S | re.I)
        if re.search(r"<\\s*(function_calls|invoke|tool_calls|parameter)", cleaned, re.I):
            return "Recebi sua mensagem. O Dr. Victor verificará assim que possível."
        # Block leaked internal reasoning
        internal_reasoning_re = re.compile(
            r"(?is)(\\b[oae] usu[aá]ri[oa]\\b.{0,220}\\b(indica|mensagens anteriores|tom|intera[cç][aã]o|pedido)\\b)"
            r"|(\\bcomo assistente\\b.{0,220}\\b(n[aã]o devo|devo|regra|responder|seguir)\\b)"
            r"|(\\b(n[aã]o h[aá] necessidade|preciso|devo)\\b.{0,220}\\b(coletar|recado|responder|decis[aã]o|regra)\\b)"
            r"|(\\b(racioc[ií]nio|pensamento|l[oó]gica interna|decis[aã]o interna|mensagens anteriores)\\b)",
        )
        if internal_reasoning_re.search(cleaned):
            return "Obrigado. O Dr. Victor verificará sua mensagem pessoalmente."
        cleaned = re.sub(r"```.*?```", "", cleaned, flags=re.S)
        cleaned = cleaned.replace("`", "").replace("*", "").replace("!", ".")
        cleaned = re.sub(r"[\\U0001F300-\\U0001FAFF\\u2600-\\u27BF]", "", cleaned)
        cleaned = re.sub(r"[ \\t]+", " ", cleaned)
        cleaned = re.sub(r"\\n{3,}", "\\n\\n", cleaned).strip()
        return cleaned or "Recebi sua mensagem. O Dr. Victor verificará assim que possível."
    if platform_value != "telegram":
        return text
    redacted = _redact_gateway_user_facing_secrets(str(text))
    if _looks_like_gateway_provider_error(redacted):
        return _gateway_provider_error_reply(redacted)
    return redacted'''

if old_a in text:
    text = text.replace(old_a, new_a)
    print("A: WhatsApp sanitization DONE")
else:
    print("A: FAILED")

# -------------------------------------------------------
# BLOCK B: WhatsApp status message suppression
# -------------------------------------------------------
old_b = '''def _prepare_gateway_status_message(platform: Any, event_type: str, message: str) -> Optional[str]:
    """Filter/sanitize agent status callbacks before platform delivery."""
    text = str(message or "").strip()
    if not text:
        return None
    if _gateway_platform_value(platform) != "telegram":
        return text'''

new_b = '''def _prepare_gateway_status_message(platform: Any, event_type: str, message: str) -> Optional[str]:
    """Filter/sanitize agent status callbacks before platform delivery."""
    text = str(message or "").strip()
    if not text:
        return None
    if _gateway_platform_value(platform) == "whatsapp":
        return None
    if _gateway_platform_value(platform) != "telegram":
        return text'''

if old_b in text:
    text = text.replace(old_b, new_b)
    print("B: WhatsApp status suppression DONE")
else:
    print("B: FAILED")

# Write back
with open(FILE, 'w') as f:
    f.write(text)

# Verify
import py_compile
try:
    py_compile.compile(FILE, doraise=True)
    print("SYNTAX OK")
except py_compile.PyCompileError as e:
    print(f"SYNTAX ERROR: {e}")
