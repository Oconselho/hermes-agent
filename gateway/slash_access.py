"""Per-platform slash command access control.

This module sits beside the existing per-platform allowlist (``allow_from``)
and adds a second axis: of the users who are *allowed to talk to the
gateway*, which ones can run *which slash commands*.

Two lists per platform scope (DM vs group, mirroring ``allow_from`` vs
``group_allow_from``):

  - ``allow_admin_from``      — user IDs that get every registered slash
                                command (built-in + plugin-registered).
  - ``user_allowed_commands`` — slash command names non-admin users may
                                run. Empty / unset → non-admins get no
                                slash commands.

Backward compatibility:

  If ``allow_admin_from`` is not set for a scope, slash command gating
  is disabled entirely for that scope. Every allowed user can run every
  slash command, exactly like before. This means existing installs are
  unaffected until an operator opts in by listing at least one admin.

  EXCEPT on a public surface (``_PUBLIC_BY_DEFAULT_PLATFORMS``, or an
  explicit ``public_surface: true``). There the sender pool is the general
  public, so "unconfigured" cannot mean "unrestricted" — gating is on and
  an empty admin list means nobody runs commands. Set
  ``public_surface: false`` to restore the permissive rule for a platform
  that is actually operator-only.

The gate is applied at the slash command dispatch site in
``gateway/run.py`` so it covers BOTH built-in and plugin-registered
commands via the live registry. Gating slash commands does not affect
plain chat — non-admin users can still talk to the agent normally,
they just can't trigger commands outside ``user_allowed_commands``.

Authored as a slimmed-down salvage of PR #4443's permission tiers
(co-authored by @ReqX). The full tier system, audit log, usage
tracking, rate limiting, and tool filtering from that PR are not
included here — only the slash-command access split.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable, Optional, Tuple


# Slash commands that MUST stay reachable for any allowed user, even when
# slash gating is enabled and the user has no commands listed. Without this
# carve-out, a non-admin user has no way to discover what they can or
# can't do (``/help``, ``/whoami``) and no way to see what state the agent
# is in (``/status``). These mirror the smallest set of read-only commands
# we'd hand to a guest. Operators can still narrow this further by writing
# their own ``user_allowed_commands`` (this set is only the implicit
# fallback floor — anything in ``user_allowed_commands`` overrides it
# additively, never restrictively).
_ALWAYS_ALLOWED_FOR_USERS: FrozenSet[str] = frozenset({
    "help",
    "whoami",
})


@dataclass(frozen=True)
class SlashAccessPolicy:
    """Resolved access policy for a single (platform, scope) pair.

    ``scope`` is ``"dm"`` for direct messages and ``"group"`` for groups,
    channels, threads, and any other multi-user context. The mapping from
    SessionSource.chat_type → scope happens in ``policy_for_source``.
    """

    enabled: bool                      # gating active for this scope?
    admin_user_ids: FrozenSet[str]
    user_allowed_commands: FrozenSet[str]
    # Public-facing surface: the sender pool is the general public, not the
    # operator's team. Suppresses the ``_ALWAYS_ALLOWED_FOR_USERS`` floor —
    # ``/help`` and ``/whoami`` are a guest affordance among colleagues, but
    # on a clinic's public line they only advertise that a command surface
    # is on the other end.
    public_surface: bool = False

    def is_admin(self, user_id: Optional[str]) -> bool:
        if not self.enabled:
            # Gating disabled → treat every allowed user as admin so
            # downstream code can keep using ``is_admin`` / ``can_run``
            # uniformly.
            return True
        if not user_id:
            return False
        return str(user_id) in self.admin_user_ids

    def can_run(self, user_id: Optional[str], canonical_cmd: str) -> bool:
        if not self.enabled:
            return True
        if self.is_admin(user_id):
            return True
        if not canonical_cmd:
            return False
        if canonical_cmd in _ALWAYS_ALLOWED_FOR_USERS and not self.public_surface:
            return True
        return canonical_cmd in self.user_allowed_commands


_DM_CHAT_TYPES = frozenset({"dm", "direct", "private", ""})


# Platforms whose inbound traffic is, by default, the general public rather
# than an operator's own team. On these the backward-compat "no admin list →
# gating off" rule is exactly backwards: an unconfigured deployment hands
# every stranger the full command registry. Listed platforms therefore
# default to ``public_surface`` (fail closed), and an operator restores the
# permissive legacy behaviour explicitly with ``public_surface: false``.
#
# WhatsApp is here because the secretary profile answers a clinic's public
# line: on 06/ago/2026 a contact ran ``/reset`` there and got back the
# session banner naming the active model and provider.
_PUBLIC_BY_DEFAULT_PLATFORMS: FrozenSet[str] = frozenset({"whatsapp"})


def _coerce_id_list(raw: Any) -> FrozenSet[str]:
    """Normalize a YAML-loaded admin/user list into a frozenset of strings.

    Accepts ``None``, list, tuple, or comma-separated string. Stringifies
    each entry and strips whitespace; empty entries are dropped.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        items: Iterable[Any] = raw
    elif isinstance(raw, str):
        items = (s for s in raw.split(",") if s.strip())
    else:
        # single scalar (int user id, etc.)
        items = (raw,)
    out: list[str] = []
    for it in items:
        s = str(it).strip()
        if s:
            out.append(s)
    return frozenset(out)


def _coerce_command_list(raw: Any) -> FrozenSet[str]:
    """Normalize a slash command allowlist.

    Strips leading slashes so YAML can read either ``["help", "status"]``
    or ``["/help", "/status"]``. Lowercase canonicalization matches how
    ``resolve_command()`` stores names.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        items: Iterable[Any] = raw
    elif isinstance(raw, str):
        items = (s for s in raw.split(",") if s.strip())
    else:
        items = (raw,)
    out: list[str] = []
    for it in items:
        s = str(it).strip().lstrip("/").lower()
        if s:
            out.append(s)
    return frozenset(out)


def _platform_name(platform: Any) -> str:
    """Normalize a Platform enum / string / None to a lowercase name."""
    if platform is None:
        return ""
    value = getattr(platform, "value", platform)
    return str(value).strip().lower()


def _scope_for_chat_type(chat_type: Optional[str]) -> str:
    if chat_type and chat_type.lower() in _DM_CHAT_TYPES:
        return "dm"
    return "group"


def _platform_extra(platform_config: Any) -> dict:
    """Return the ``extra`` dict from a PlatformConfig-like object.

    Defensively handles None and non-PlatformConfig shapes so calling
    code can stay simple.
    """
    if platform_config is None:
        return {}
    extra = getattr(platform_config, "extra", None)
    if isinstance(extra, dict):
        return extra
    if isinstance(platform_config, dict):
        # Some test harnesses pass dicts directly.
        return platform_config
    return {}


def _keys_for_scope(scope: str) -> Tuple[str, str]:
    """Return (admin_key, user_cmd_key) names for a scope."""
    if scope == "group":
        return ("group_allow_admin_from", "group_user_allowed_commands")
    return ("allow_admin_from", "user_allowed_commands")


def policy_from_extra(
    extra: dict, scope: str, platform: Optional[str] = None
) -> SlashAccessPolicy:
    """Build a policy from a platform's ``extra`` dict for one scope.

    DM scope falls back to group scope keys ONLY for ``user_allowed_commands``
    when the DM scope didn't specify its own. This keeps the common case
    (operator wants the same command set DM and group) ergonomic without
    forcing duplication. Admin lists are NOT cross-scope: an admin in
    DMs is not implicitly an admin in a group.

    ``platform`` selects the default posture. On a public surface (see
    ``_PUBLIC_BY_DEFAULT_PLATFORMS``, or an explicit ``public_surface: true``)
    gating is on even with no admin list configured — an empty list then
    means "nobody runs commands here", which is the safe reading for a
    public line. Everywhere else the original backward-compatible rule
    stands: no admin list → no gating.
    """
    admin_key, cmd_key = _keys_for_scope(scope)
    admin_ids = _coerce_id_list(extra.get(admin_key))
    cmds = _coerce_command_list(extra.get(cmd_key))

    if scope == "dm" and not cmds:
        # DM didn't specify — let group's user_allowed_commands fall through
        # so operators only need to list it once if it's the same.
        cmds = _coerce_command_list(extra.get("group_user_allowed_commands"))

    raw_public = extra.get("public_surface")
    if raw_public is None:
        public = str(platform or "").strip().lower() in _PUBLIC_BY_DEFAULT_PLATFORMS
    else:
        public = bool(raw_public)

    enabled = bool(admin_ids) or public
    return SlashAccessPolicy(
        enabled=enabled,
        admin_user_ids=admin_ids,
        user_allowed_commands=cmds,
        public_surface=public,
    )


def policy_for_source(gateway_config: Any, source: Any) -> SlashAccessPolicy:
    """Resolve the access policy for a SessionSource.

    Returns a "disabled" policy (gating off, allow everything) when:
      - gateway_config is None
      - the platform has no PlatformConfig
      - the platform's PlatformConfig has no admin list set for the scope

    Callers should treat the returned policy as authoritative for slash
    command gating only. It does not gate plain chat messages.
    """
    if source is None:
        return SlashAccessPolicy(
            enabled=False,
            admin_user_ids=frozenset(),
            user_allowed_commands=frozenset(),
        )
    platform_name = _platform_name(getattr(source, "platform", None))
    if gateway_config is None:
        # No config to consult. A public-by-default platform must still fail
        # closed here — this is the path taken when the gateway hasn't
        # finished wiring config, and it is exactly when a stray command
        # must not slip through.
        return policy_from_extra({}, "dm", platform_name)
    platforms = getattr(gateway_config, "platforms", None)
    platform_config = None
    if platforms is not None:
        try:
            platform_config = platforms.get(source.platform)
        except Exception:
            platform_config = None
    extra = _platform_extra(platform_config)
    scope = _scope_for_chat_type(getattr(source, "chat_type", None))
    return policy_from_extra(extra, scope, platform_name)


def policy_allows_command_surface(policy: SlashAccessPolicy, user_id: Optional[str]) -> bool:
    """Whether ``user_id`` may have ``/…`` text treated as a command at all.

    Callers use this at ingestion to decide ``MessageEvent.commands_disabled``.
    False means the leading slash is just a character: the message goes to
    the agent as ordinary text and no command handler — including pre-gate
    ones like ``/status`` — ever sees it. That is deliberately different
    from *denying* a command: a patient who types ``/agendar`` gets a normal
    secretary reply rather than a refusal notice whose very wording reveals
    that an admin command surface exists.

    Only public surfaces can return False; everywhere else the policy is
    disabled or non-public and this is always True, preserving behaviour.
    """
    if not policy.enabled or not policy.public_surface:
        return True
    if policy.is_admin(user_id):
        return True
    # A public non-admin still reaches the command layer if the operator
    # explicitly published a command list for them.
    return bool(policy.user_allowed_commands)


def _fails_open(platform: Any) -> bool:
    """Entitlement to assume when the policy cannot be resolved at all."""
    return _platform_name(platform) not in _PUBLIC_BY_DEFAULT_PLATFORMS


def sender_may_run_commands(gateway_config: Any, source: Any) -> bool:
    """``policy_allows_command_surface`` resolved straight from a source."""
    try:
        policy = policy_for_source(gateway_config, source)
    except Exception:
        # An unresolvable policy must not silently re-open a public line.
        return _fails_open(getattr(source, "platform", None))
    return policy_allows_command_surface(policy, getattr(source, "user_id", None))


def command_surface_open(platform_config: Any, platform: Any, source: Any) -> bool:
    """Adapter-side entitlement, resolved from a platform's OWN config.

    An adapter holds a ``PlatformConfig``, not the whole ``GatewayConfig``,
    so it cannot use :func:`sender_may_run_commands`. Same decision, same
    fail-closed posture, taken from what the adapter actually has.
    """
    try:
        policy = policy_from_extra(
            _platform_extra(platform_config),
            _scope_for_chat_type(getattr(source, "chat_type", None)),
            _platform_name(platform),
        )
        return policy_allows_command_surface(
            policy, getattr(source, "user_id", None)
        )
    except Exception:
        return _fails_open(platform)


__all__ = [
    "SlashAccessPolicy",
    "policy_from_extra",
    "policy_for_source",
    "policy_allows_command_surface",
    "sender_may_run_commands",
    "command_surface_open",
]
