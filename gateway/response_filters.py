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
_WHATSAPP_SILENCE_MARKER_RE = re.compile(
    r"^[\[\{\(<*_\s]*silencios[oa][\]\}\)>*_\s]*[.!]*$",
    re.IGNORECASE,
)


def is_whatsapp_silence_marker(text: Any) -> bool:
    """True when a WhatsApp reply is only the model's stay-silent marker."""
    if not text:
        return False
    candidate = unicodedata.normalize("NFKD", str(text))
    candidate = "".join(ch for ch in candidate if not unicodedata.combining(ch))
    # Strip the invisible/odd-space classes the transport strips anyway, so a
    # zero-width character cannot smuggle the marker past this check.
    candidate = re.sub(r"[\u200b\u2060\u2063\ufeff]", "", candidate)
    candidate = re.sub(
        r"[\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]", " ", candidate
    )
    return bool(_WHATSAPP_SILENCE_MARKER_RE.match(candidate.strip()))


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
