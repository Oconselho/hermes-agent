"""Gateway response filtering helpers.

These helpers operate at the gateway boundary: they decide whether a completed
agent turn should be delivered to the chat, not what should be persisted in the
conversation history.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# Canonical model-emitted control token for intentional silence.
SILENT_REPLY_TOKEN = "NO_REPLY"

# Exact whole-response markers that mean "the agent intentionally chose not to
# reply".  Keep this list small and explicit; arbitrary empty output remains an
# error/empty-response path, not silence.
LIVE_GATEWAY_SILENT_MARKERS = frozenset({
    "[SILENT]",
    "SILENT",
    "NO_REPLY",
    "NO REPLY",
})


def _canonical_silence_candidate(text: str) -> str:
    return " ".join(text.strip().upper().split())


def _strip_edge_silence_punctuation(text: str) -> str:
    """Strip stray edge punctuation without erasing marker structure.

    Models sometimes emit ``.NO_REPLY`` or ``*NO_REPLY*`` instead of the exact
    marker. Keep square brackets structural so malformed ``[SILENT`` does not
    become ``SILENT``.
    """
    start = 0
    end = len(text)
    while start < end and text[start] not in "[]" and unicodedata.category(text[start]).startswith("P"):
        start += 1
    while end > start and text[end - 1] not in "[]" and unicodedata.category(text[end - 1]).startswith("P"):
        end -= 1
    return text[start:end].strip()


def _canonical_silence_candidates(text: str) -> tuple[str, ...]:
    exact = _canonical_silence_candidate(text)
    stripped = _strip_edge_silence_punctuation(text.strip())
    if stripped == text.strip():
        return (exact,)
    fallback = _canonical_silence_candidate(stripped)
    return (exact, fallback)


# ---------------------------------------------------------------------------
# The WhatsApp stay-silent marker
# ---------------------------------------------------------------------------
#
# The secretary's prompt asks for a bare ``[SILENCIOSO]`` — Portuguese, and NOT
# a member of ``LIVE_GATEWAY_SILENT_MARKERS`` above, which holds the
# platform-neutral tokens.  It lives here, next to them, because it has now
# reached real contacts twice by two different roads, and both times a second
# copy of "is this the marker?" was part of the story: once because a literal
# string comparison missed ``[ SILENCIOSO ]`` (four patients, 09–11/ago/2026),
# once because a resend path never asked at all (six messages, 03–18/ago/2026).
# One recogniser, imported by everyone who needs it — the gateway finalizer and
# the WhatsApp adapter's outbound backstop — cannot drift.

# Match the whole reply only. A response that merely mentions the word in a
# sentence is real text and must be delivered untouched; silence is the right
# outcome only when the marker IS the entire message.
#
# This recogniser ERASES decoration instead of enumerating it, and that is the
# whole lesson of the third leak. Version one compared the literal
# ``[SILENCIOSO]``. Version two (18/ago/2026) became a regex that allowed
# brackets, emphasis and spaces *around* the word. gpt-5.6-luna then wrote the
# letters themselves apart — ``[ S I L E N C I O S O ]`` — and a pattern
# describing wrappers has nothing to say about a separator INSIDE the word.
# Five of those, 09/ago–25/ago/2026; four were delivered, the last to a patient
# on 25/ago 13:07 BRT.
#
# So the test is now: drop everything that is not a letter or a digit, and
# require what is left to BE the word. That subsumes by construction every
# shape the previous versions listed — brackets, braces, angle brackets,
# markdown emphasis, trailing punctuation, every class of space, zero-width
# smuggling — plus the separations nobody thought to list.
_WHATSAPP_SILENCE_WORDS = frozenset({"SILENCIOSO", "SILENCIOSA"})


def is_whatsapp_silence_marker(text: Any) -> bool:
    """True when a WhatsApp reply is only the model's stay-silent marker."""
    if not text:
        return False
    candidate = unicodedata.normalize("NFKD", str(text))
    candidate = "".join(ch for ch in candidate if not unicodedata.combining(ch))
    # Keep letters and digits, drop everything else. Digits are kept on purpose
    # so ``silencioso2`` stays ordinary text; the safety comes from requiring
    # the remainder to equal the word exactly, which no real reply from the
    # secretary does — she has no message that reduces to this one adjective.
    core = "".join(ch for ch in candidate if ch.isalnum())
    return core.upper() in _WHATSAPP_SILENCE_WORDS


# ---------------------------------------------------------------------------
# Control tokens in general: a decision written where a sentence belongs
# ---------------------------------------------------------------------------
#
# ``[SILENCIOSO]`` is one instance of a class, and the class is the real bug.
# The model writes an internal DECISION into the reply channel and the gateway
# is supposed to read it and act. Every leak so far came from the recogniser
# being a LIST OF SPELLINGS, and a list is always one step behind the model:
# literal ``[SILENCIOSO]`` (jun–ago), then ``[ SILENCIOSO ]`` (four patients,
# 09–11/ago), then ``[ S I L E N C I O S O ]`` (five contacts, 09–25/ago).
# Tomorrow it is ``[QUIETO]`` or ``[NAO RESPONDER]`` and the list is behind
# again.
#
# Victor's rule, 25/ago/2026: *the decision is thinking, it must never be shown
# to the contact — just carry the action out.* So the question here is
# STRUCTURAL, not lexical: is the whole reply a delimited token rather than
# something a person would say? If it is, the contact gets silence whether or
# not we recognise the word inside.
#
# That inverts the failure mode, which is the point. Before: unrecognised
# token → delivered. After: unrecognised token → silence, and a loud log line
# so a decision we do not know about is something we find out about.
#
# Why the shape is safe to suppress: across all 757 replies the secretary has
# ever sent, the only whole-message delimited tokens are the 153 silence
# markers and 10 ``(empty)`` placeholders (never delivered — no
# ``Sending response (7 chars)`` exists in any log). Not one real reply has
# this shape. She answers in sentences.
_CONTROL_TOKEN_RE = re.compile(
    r"^[*_~`\s]*"                    # markdown emphasis the model adds sometimes
    r"[\[\{(<]"                       # opening delimiter
    r"([^\[\]\{\}()<>]{1,40})"         # the token itself, short and unnested
    r"[\]\})>]"                       # closing delimiter
    r"[*_~`\s]*[.!]*$"
)

# Sentence punctuation INSIDE the delimiters means it is prose in brackets, not
# a token — "(Já enviei.)" and "(Se preferir, falo com a recepção.)" are things
# the secretary may legitimately say. A control token has no sentence in it.
_SENTENCE_PUNCTUATION = frozenset(".,;:?!")


def is_internal_control_artifact(text: Any) -> bool:
    """True when the whole reply is a control token rather than a message.

    Deliberately narrower than :func:`is_whatsapp_silence_marker`: that one
    knows the word and accepts any decoration, this one knows the shape and
    accepts any word. Together they cover both "a spelling we did not predict"
    and "a token we did not predict".
    """
    if not text:
        return False
    candidate = unicodedata.normalize("NFKD", str(text))
    candidate = "".join(ch for ch in candidate if not unicodedata.combining(ch))
    match = _CONTROL_TOKEN_RE.match(candidate.strip())
    if not match:
        return False
    inside = match.group(1)
    if any(ch in _SENTENCE_PUNCTUATION for ch in inside):
        return False
    core = "".join(ch for ch in inside if ch.isalnum())
    # A bare number in brackets is not a decision — "(71996691002)" is data.
    if not core or not any(ch.isalpha() for ch in core):
        return False
    return len(core) <= 20


# ---------------------------------------------------------------------------
# The positive contract: is this a message for a person?
# ---------------------------------------------------------------------------
#
# Everything above is a blocklist — a list of things that must not go out. A
# blocklist is structurally always behind the model: it can only refuse what
# someone already watched leak. Three leaks in three weeks, each one a spelling
# the list did not have yet, and each fix taught it exactly one more.
#
# This asks the opposite question, and it is the one a delivery boundary should
# have been asking all along: does this read like something the secretary would
# SAY? Whatever fails that is not delivered — a token, a constant, a sentinel,
# a fragment of machinery nobody has thought of yet — without anyone having to
# name it first.
#
# The discriminator comes from the data, not from taste. Across the 762 replies
# the secretary has ever produced, every real one carries lowercase letters
# (the smallest, "Olá. Envie seu recado.", has 15) and every internal artifact
# carries none: ``NO_REPLY``, ``[SILENT]``, ``SECRETARY_MODEL_TEST_OK``,
# ``[SILENCIOSO]``. Portuguese written to a person has lowercase in it; machine
# tokens are SHOUTED, by a convention older than this codebase.
#
# But "no lowercase" alone would be too greedy, and a test caught it doing
# exactly that: a bare "71 99669-1002" has no lowercase either, and a phone
# number is terse, not machinery. So the rule is narrower and says only what
# the evidence supports — **a reply that contains WORDS but not one lowercase
# letter is a machine token**. Text with no word in it at all (a number, a
# time, an emoji) is not judged here; the other guards decide.
#
# The residual cost, stated plainly: a legitimate reply written entirely in
# capitals would be withheld. None of the 762 is one, the secretary is
# prompted to write formally, and every suppression is logged at WARNING — so
# that failure lands in the log where it can be seen, instead of on a
# patient's screen where it cannot.
_WORD_RUN_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)

_INVISIBLE_RE = re.compile(
    r"[\u200b\u2060\u2063\ufeff\u00ad\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]"
)


def reads_as_human_message(text: Any) -> bool:
    """False only for text that is clearly machinery rather than speech."""
    if not text:
        return False
    candidate = str(text)
    if not _WORD_RUN_RE.search(candidate):
        # No word in it at all — "71 99669-1002", "14h", an emoji. Terse, but
        # nothing here says machine. Let the other guards judge it.
        return True
    return any(ch.islower() for ch in candidate)


# Why a disposition and not a bool: the caller has to log WHY nothing was sent.
# "silence-marker" is the model doing its job ~150 times a month and deserves no
# noise; anything else is a thing we did not know about and must be loud.
SILENCE_REASON_EMPTY = "empty"
SILENCE_REASON_MARKER = "silence-marker"
SILENCE_REASON_CONTROL_TOKEN = "control-token"
SILENCE_REASON_NOT_A_MESSAGE = "not-a-message"
DELIVER = "deliver"


def whatsapp_reply_disposition(text: Any) -> str:
    """Decide what to do with a model reply: deliver it, or why not.

    The order matters only for the log line — every non-``deliver`` outcome is
    the same action, which is to send nothing.
    """
    if text is None:
        return SILENCE_REASON_EMPTY
    stripped = _INVISIBLE_RE.sub("", str(text)).strip()
    if not stripped:
        return SILENCE_REASON_EMPTY
    if is_whatsapp_silence_marker(stripped):
        return SILENCE_REASON_MARKER
    if is_internal_control_artifact(stripped):
        return SILENCE_REASON_CONTROL_TOKEN
    if not reads_as_human_message(stripped):
        return SILENCE_REASON_NOT_A_MESSAGE
    return DELIVER


def is_intentional_silence_response(response: Any) -> bool:
    """Return True only when ``response`` is exactly a silence marker.

    Substantive prose that merely mentions ``NO_REPLY`` or ``[SILENT]`` must be
    delivered normally.  A blank response is also not silence; blank output is
    handled by the empty-response failure path.
    """
    if not isinstance(response, str):
        return False
    stripped = response.strip()
    if not stripped:
        return False
    if len(stripped) > 64:
        return False
    return any(candidate in LIVE_GATEWAY_SILENT_MARKERS for candidate in _canonical_silence_candidates(stripped))


# The envelope key the gateway stamps once, at the door, when it has already
# decided this turn says nothing. See ``_normalize_whatsapp_silence_decision``
# in gateway/run.py: after that stamp the decision is a FACT ON THE RESULT, not
# a string in flight, and no downstream road can mistake it for text to send.
GATEWAY_SILENCE_KEY = "gateway_intentional_silence"


def is_intentional_silence_agent_result(agent_result: dict | None, response: Any) -> bool:
    """Silence markers suppress delivery only for successful agent turns."""
    if not isinstance(agent_result, dict):
        return False
    if agent_result.get("failed"):
        return False
    # Stamped at the door: the decision was made before anyone read the text.
    if agent_result.get(GATEWAY_SILENCE_KEY):
        return True
    return is_intentional_silence_response(response)


def is_partial_silence_marker(text: Any) -> bool:
    """Return True while ``text`` could still resolve to a silence marker.

    The streaming path accumulates the reply delta-by-delta and must decide,
    before the whole response is known, whether to show what it has so far.
    A buffer whose canonical form is a non-empty *prefix* of a silence marker
    (e.g. ``"NO"`` on the way to ``"NO_REPLY"``, or an exact marker that has
    not yet been terminated by stream-end) is held back so a raw marker is
    never edited onto the screen and then belatedly retracted.

    Anything that has already diverged from every marker (ordinary prose) —
    and anything longer than the marker cap — returns False so normal
    streaming resumes immediately.  This is the streaming counterpart to
    :func:`is_intentional_silence_response`, sharing the same marker set and
    canonicalization so the two never drift.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > 64:
        return False
    for candidate in _canonical_silence_candidates(stripped):
        if candidate and any(marker.startswith(candidate) for marker in LIVE_GATEWAY_SILENT_MARKERS):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# "Isto é o que eu acabei de mandar?"
#
# A estrada do modelo nunca teve essa pergunta. O funil de agendamento tem
# (``_is_immediate_repeat`` em gateway/platforms/whatsapp_appointments.py), e
# em 26/ago/2026 dava para ver as duas lado a lado no mesmo log: um chat ouviu
# "reply already on screen for this chat, staying silent instead of repeating
# it", e 25 minutos depois a Val (71 8774-9408) recebeu a MESMA recusa clínica
# de 223 caracteres cinco vezes em onze minutos, porque o funil não engatou
# (``scheduling=False``) e do lado do modelo não havia guarda nenhuma.
#
# A causa de raiz era de prompt — a REGRA #4 declara "esta regra vence
# qualquer outra instrução deste prompt", e as regras anti-repetição estão
# nesse mesmo prompt, então ela vencia elas também. O prompt foi corrigido.
# Isto aqui existe porque prompt não é garantia: é a mesma lição de
# 25/ago/2026, quando enumerar grafias do marcador de silêncio perdeu três
# vezes seguidas. Uma regra escrita pede; uma estrutura garante.
_IDENTITY_OPENING_RE = re.compile(
    r"^[^\n]{0,80}?sou a assistente do dr\.?\s*victor almeida[.!]?\s*",
    re.IGNORECASE,
)


def visible_message_signature(text: Any) -> str:
    """O que uma resposta *diz*, sem o cabeçalho de quem a diz.

    Descarta a apresentação inicial, acentos, caixa e espaço. Duas respostas
    com a mesma assinatura são, para quem lê o WhatsApp, a mesma mensagem
    repetida — a abertura fria carrega saudação e identificação, a reentrada
    carrega só o corpo, e byte a byte elas nunca batem.

    Espelha ``_response_signature`` do funil de propósito: as duas estradas
    precisam concordar sobre o que é "a mesma mensagem", e há um teste que
    pina isso (``test_whatsapp_repeat_guard``). Unificar as duas numa função
    só é a próxima dívida — hoje não dá, porque
    ``gateway/platforms/whatsapp_appointments.py`` carrega trabalho não
    commitado (o modelo de pagamento PIX-API de 15/ago) e mexer nele
    misturaria as duas coisas num commit só.
    """

    stripped = _IDENTITY_OPENING_RE.sub("", str(text or "").strip())
    decomposed = unicodedata.normalize("NFKD", stripped).casefold()
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(without_marks.split())


def says_the_same_as(previous: Any, candidate: Any) -> bool:
    """Se ``candidate`` é ``previous`` dito outra vez, para quem lê.

    Só responde à identidade do texto. Quem chama é que decide se repetir
    seria errado *aqui* — se houve tempo demais no meio, se o contato pediu
    de novo, se o fluxo mudou de estado. Manter a decisão fora daqui é o que
    permite ao funil ter a sua própria regra, mais rica, sem duplicar esta.
    """

    signature = visible_message_signature(candidate)
    if not signature:
        return False
    return signature == visible_message_signature(previous)
