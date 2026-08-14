"""Shared helpers for canonicalising WhatsApp sender identity.

WhatsApp's bridge can surface the same human under two different JID shapes
within a single conversation:

- LID form: ``999999999999999@lid``
- Phone form: ``15551234567@s.whatsapp.net``

Both the authorisation path (:mod:`gateway.run`) and the session-key path
(:mod:`gateway.session`) need to collapse these aliases to a single stable
identity. This module is the single source of truth for that resolution so
the two paths can never drift apart.

Public helpers:

- :func:`normalize_whatsapp_identifier` — strip JID/LID/device/plus syntax
  down to the bare numeric identifier.
- :func:`canonical_whatsapp_identifier` — walk the bridge's
  ``lid-mapping-*.json`` files and return a stable canonical identity
  across phone/LID variants.
- :func:`expand_whatsapp_aliases` — return the full alias set for an
  identifier. Used by authorisation code that needs to match any known
  form of a sender against an allow-list.

Plugins that need per-sender behaviour on WhatsApp (role-based routing,
per-contact authorisation, policy gating in a gateway hook) should use
``canonical_whatsapp_identifier`` so their bookkeeping lines up with
Hermes' own session keys.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Set

logger = logging.getLogger(__name__)

# WhatsApp JIDs are numeric (or plus-prefixed numeric) with optional
# ``@``, ``.`` and ``:`` separators. ``\w`` is pinned to ASCII so
# full-width digits / Unicode word chars can't sneak through.
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9@.+\-]+$")

from hermes_constants import get_hermes_dir


def normalize_whatsapp_identifier(value: str) -> str:
    """Strip WhatsApp JID/LID syntax down to its stable numeric identifier.

    Accepts any of the identifier shapes the WhatsApp bridge may emit:
    ``"60123456789@s.whatsapp.net"``, ``"60123456789:47@s.whatsapp.net"``,
    ``"60123456789@lid"``, or a bare ``"+601****6789"`` / ``"60123456789"``.
    Returns just the numeric identifier (``"60123456789"``) suitable for
    equality comparisons.

    Useful for plugins that want to match sender IDs against
    user-supplied config (phone numbers in ``config.yaml``) without
    worrying about which variant the bridge happens to deliver.
    """
    return (
        str(value or "")
        .strip()
        .replace("+", "", 1)
        .split(":", 1)[0]
        .split("@", 1)[0]
    )


# A target that is "just a phone number" — optional leading ``+`` then digits
# and the usual human separators (spaces, dots, dashes, parens). Anything that
# already carries an ``@`` is a fully-qualified JID and must pass through
# untouched (group ``@g.us``, LID ``@lid``, ``status@broadcast`` etc.).
_BARE_PHONE_RE = re.compile(r"^\+?[\d\s().\-]+$")


# Brazil added a ninth digit to mobile numbers, but WhatsApp only stores it
# for the DDDs where it was actually rolled out to the installed base — the
# São Paulo/Rio/Espírito Santo band, DDD 11–28.  From DDD 31 south and north
# (Minas, Bahia, the whole Northeast, Centro-Oeste, Sul, Norte) WhatsApp
# addresses the same person WITHOUT the 9, and a send to the nine-digit form
# addresses nobody: it does not bounce, it just never arrives.
#
# The boundary is not a guess.  Counted over the 6,000+ ``lid-mapping-*.json``
# files this account has accumulated: every DDD from 11 to 28 stores the
# nine-digit form (DDD 11: 1187 of 1220), and every DDD from 31 to 99 stores
# the eight-digit one (DDD 71: 1402 of 1404).  The handful of exceptions on
# each side are landlines, which are eight digits nationwide.
_BR_NINTH_DIGIT_MAX_DDD = 28


def brazilian_whatsapp_number(digits: str) -> str:
    """Put a Brazilian number in the shape WhatsApp addresses it by.

    Takes and returns bare digits including the ``55`` country code.  Numbers
    that are not Brazilian mobiles — other countries, landlines, anything of
    an unexpected length — are returned untouched: this only ever adds or
    removes the ninth digit, and only when it is certain which one applies.
    """
    number = re.sub(r"\D+", "", str(digits or ""))
    if not number.startswith("55") or len(number) not in (12, 13):
        return number
    ddd = number[2:4]
    if not ddd.isdigit() or not 11 <= int(ddd) <= 99:
        return number
    local = number[4:]
    keeps_ninth = int(ddd) <= _BR_NINTH_DIGIT_MAX_DDD
    # Only touch what is unambiguously a mobile.  A local part starting 2-5 is
    # a landline in every DDD and never carried the ninth digit; ``9`` followed
    # by ``2``/``3``/``0`` is a VoIP or non-geographic range that is genuinely
    # nine digits long — this account holds 26 of them, and stripping their
    # leading 9 would invent a number nobody answers.  A real ninth digit is a
    # ``9`` in front of an old eight-digit mobile, which always began 6-9.
    if len(local) == 9:
        if not keeps_ninth and local[0] == "9" and local[1] in "6789":
            return f"55{ddd}{local[1:]}"
        return number
    if len(local) == 8 and keeps_ninth and local[0] in "6789":
        return f"55{ddd}9{local}"
    return number


def to_whatsapp_jid(value: str) -> str:
    """Normalize an *outbound* WhatsApp target to a bridge-safe JID.

    Baileys' ``jidDecode`` crashes on a bare phone number — it expects a
    fully-qualified JID such as ``50766715226@s.whatsapp.net``. This helper
    is the inverse of :func:`normalize_whatsapp_identifier`: instead of
    stripping a JID down to its numeric core for comparison, it *builds* the
    JID a send must use.

    Behaviour:

    - ``"+50766715226"`` / ``"50766715226"`` → ``"50766715226@s.whatsapp.net"``
    - ``"50766715226@s.whatsapp.net"`` → unchanged
    - ``"group-id@g.us"`` / ``"130631430344750@lid"`` → unchanged
    - ``"user:device@s.whatsapp.net"`` style colon-before-``@`` → ``@`` form
    - anything that isn't a recognizable bare phone → returned unchanged so
      the bridge can surface a meaningful error rather than us mangling it.

    Returns ``""`` for an empty/whitespace input.
    """
    if not value:
        return ""

    normalized = str(value).strip()
    # Drop a device suffix before the domain: ``user:device@domain`` is a
    # legacy Baileys shape whose ``:device`` part is not addressable — collapse
    # it to ``user@domain``. (Mirrors normalize_whatsapp_identifier, which
    # splits the bare id on ``:`` for the same reason.)
    if ":" in normalized and "@" in normalized:
        prefix, _, domain = normalized.partition("@")
        normalized = f"{prefix.split(':', 1)[0]}@{domain}"

    # Already a fully-qualified JID — leave it alone.
    if "@" in normalized:
        return normalized

    if _BARE_PHONE_RE.fullmatch(normalized):
        digits = re.sub(r"\D+", "", normalized)
        if digits:
            return f"{digits}@s.whatsapp.net"

    return normalized


def to_whatsapp_phone_jid(value: str) -> str:
    """Build a phone JID for sending, in the ninth-digit shape its DDD uses.

    Use this for any *configured* outbound phone number.  A human writing a
    Bahia number into a config file writes it the way it is printed on a card
    — ``+55 71 99669-1002`` — and sending to that literal reaches nobody,
    silently.  LIDs, groups and non-Brazilian numbers pass through unchanged.
    """
    jid = to_whatsapp_jid(value)
    if not jid.endswith("@s.whatsapp.net"):
        return jid
    local, _, domain = jid.partition("@")
    return f"{brazilian_whatsapp_number(local)}@{domain}"


def expand_whatsapp_aliases(identifier: str) -> Set[str]:
    """Resolve WhatsApp phone/LID aliases via bridge session mapping files.

    Returns the set of all identifiers transitively reachable through the
    bridge's ``$HERMES_HOME/whatsapp/session/lid-mapping-*.json`` files,
    starting from ``identifier``. The result always includes the
    normalized input itself, so callers can safely ``in`` check against
    the return value without a separate fallback branch.

    Returns an empty set if ``identifier`` normalizes to empty.
    """
    normalized = normalize_whatsapp_identifier(identifier)
    if not normalized:
        return set()

    session_dir = get_hermes_dir("platforms/whatsapp/session", "whatsapp/session")
    resolved: Set[str] = set()
    queue = [normalized]

    while queue:
        current = queue.pop(0)
        if not current or current in resolved:
            continue
        # Defense-in-depth: reject identifiers that could sneak path
        # separators / traversal segments into the ``lid-mapping-{current}``
        # filename below. The hardcoded ``lid-mapping-`` prefix already
        # prevents escape via pathlib's component split (an attacker can't
        # create ``lid-mapping-..`` as a real directory in session_dir), but
        # this keeps the identifier space to the characters WhatsApp JIDs
        # actually use and avoids depending on that filesystem-layout
        # invariant.
        if not _SAFE_IDENTIFIER_RE.match(current):
            continue

        resolved.add(current)
        for suffix in ("", "_reverse"):
            mapping_path = session_dir / f"lid-mapping-{current}{suffix}.json"
            if not mapping_path.exists():
                continue
            try:
                mapped = normalize_whatsapp_identifier(
                    json.loads(mapping_path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("whatsapp_identity: failed to read %s: %s", mapping_path, exc)
                continue
            if mapped and mapped not in resolved:
                queue.append(mapped)

    return resolved


def canonical_whatsapp_identifier(identifier: str) -> str:
    """Return a stable WhatsApp sender identity across phone-JID/LID variants.

    WhatsApp may surface the same person under either a phone-format JID
    (``60123456789@s.whatsapp.net``) or a LID (``1234567890@lid``). This
    applies to a DM ``chat_id`` *and* to the ``participant_id`` of a
    member inside a group chat — both represent a user identity, and the
    bridge may flip between the two for the same human.

    This helper reads the bridge's ``whatsapp/session/lid-mapping-*.json``
    files, walks the mapping transitively, and picks the shortest
    (numeric-preferred) alias as the canonical identity.
    :func:`gateway.session.build_session_key` uses this for both WhatsApp
    DM chat_ids and WhatsApp group participant_ids, so callers get the
    same session-key identity Hermes itself uses.

    Plugins that need per-sender behaviour (role-based routing,
    authorisation, per-contact policy) should use this so their
    bookkeeping lines up with Hermes' session bookkeeping even when
    the bridge reshuffles aliases.

    Returns an empty string if ``identifier`` normalizes to empty. If no
    mapping files exist yet (fresh bridge install), returns the
    normalized input unchanged.
    """
    normalized = normalize_whatsapp_identifier(identifier)
    if not normalized:
        return ""

    # expand_whatsapp_aliases always includes `normalized` itself in the
    # returned set, so the min() below degrades gracefully to `normalized`
    # when no lid-mapping files are present.
    aliases = expand_whatsapp_aliases(normalized)
    return min(aliases, key=lambda candidate: (len(candidate), candidate))
