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


def is_intentional_silence_agent_result(agent_result: dict | None, response: Any) -> bool:
    """Silence markers suppress delivery only for successful agent turns."""
    if not isinstance(agent_result, dict):
        return False
    if agent_result.get("failed"):
        return False
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
