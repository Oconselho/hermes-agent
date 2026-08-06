"""Regressions for the 06/ago/2026 WhatsApp secretary incident.

Four independent defects let the clinic's public WhatsApp line misbehave:

1. Slash gating was opt-in, so with no ``allow_admin_from`` configured a
   patient could run ``/reset`` — and the reset banner names the active
   model and provider.
2. Raw provider failure envelopes ("HTTP 503: This model is currently
   experiencing high demand.") reached contacts: the WhatsApp branch of
   ``_sanitize_gateway_final_response`` returns before the provider-error
   rewrite that every other surface gets.
3. ``_summarize_api_error`` raised ``ResponseNotRead`` on streaming
   responses — from inside an ``except`` block, so it destroyed the retry
   path it was supposed to describe.
4. Agent-result dicts reached ``extract_media``, which crashed on them; the
   adapter then suppressed the reply and the contact got silence.
"""
from __future__ import annotations

import pytest

from gateway.slash_access import (
    policy_allows_command_surface,
    policy_from_extra,
)


# ---------------------------------------------------------------------------
# 1. Command surface is closed by default on a public platform
# ---------------------------------------------------------------------------


class TestPublicSurfaceDefaults:
    def test_whatsapp_gates_without_any_admin_list(self):
        """The exact production config: no keys at all under extra."""
        p = policy_from_extra({}, "dm", "whatsapp")
        assert p.enabled is True
        assert p.public_surface is True
        assert p.can_run("557199999999", "reset") is False

    def test_non_public_platform_keeps_backward_compatible_default(self):
        p = policy_from_extra({}, "dm", "telegram")
        assert p.enabled is False
        assert p.public_surface is False
        assert p.can_run("anyone", "reset") is True

    def test_platform_omitted_keeps_legacy_behaviour(self):
        """Existing callers that pass no platform must not change meaning."""
        p = policy_from_extra({}, "dm")
        assert p.enabled is False
        assert p.can_run("anyone", "reset") is True

    def test_operator_can_restore_permissive_behaviour(self):
        p = policy_from_extra({"public_surface": False}, "dm", "whatsapp")
        assert p.enabled is False
        assert p.can_run("anyone", "reset") is True

    def test_help_and_whoami_floor_does_not_apply_to_public(self):
        """The guest floor advertises a command surface to strangers."""
        p = policy_from_extra({}, "dm", "whatsapp")
        assert p.can_run("557199999999", "help") is False
        assert p.can_run("557199999999", "whoami") is False

    def test_floor_still_applies_to_a_gated_private_platform(self):
        p = policy_from_extra({"allow_admin_from": ["1"]}, "dm", "telegram")
        assert p.can_run("999", "help") is True
        assert p.can_run("999", "stop") is False

    def test_configured_admin_keeps_full_access_on_whatsapp(self):
        p = policy_from_extra(
            {"allow_admin_from": ["557188048263"]}, "dm", "whatsapp"
        )
        assert p.is_admin("557188048263") is True
        assert p.can_run("557188048263", "reset") is True
        assert p.can_run("557199999999", "reset") is False


class TestCommandSurfaceEntitlement:
    def test_public_non_admin_loses_the_command_surface(self):
        p = policy_from_extra({}, "dm", "whatsapp")
        assert policy_allows_command_surface(p, "557199999999") is False

    def test_public_admin_keeps_it(self):
        p = policy_from_extra(
            {"allow_admin_from": ["557188048263"]}, "dm", "whatsapp"
        )
        assert policy_allows_command_surface(p, "557188048263") is True

    def test_published_user_commands_reopen_the_surface(self):
        p = policy_from_extra(
            {"user_allowed_commands": ["agendar"]}, "dm", "whatsapp"
        )
        assert policy_allows_command_surface(p, "557199999999") is True

    def test_private_platform_always_keeps_the_surface(self):
        p = policy_from_extra({"allow_admin_from": ["1"]}, "dm", "telegram")
        assert policy_allows_command_surface(p, "999") is True

    def test_sender_may_run_commands_resolves_from_a_source(self):
        from gateway.config import Platform
        from gateway.session import SessionSource
        from gateway.slash_access import sender_may_run_commands

        wa = SessionSource(
            platform=Platform.WHATSAPP, chat_id="c", user_id="557199999999",
            chat_type="dm",
        )
        tg = SessionSource(
            platform=Platform.TELEGRAM, chat_id="c", user_id="999",
            chat_type="dm",
        )
        # No gateway config at all — WhatsApp still fails closed.
        assert sender_may_run_commands(None, wa) is False
        assert sender_may_run_commands(None, tg) is True

    def test_unresolvable_policy_fails_closed_on_whatsapp(self):
        from gateway.config import Platform
        from gateway.session import SessionSource
        from gateway.slash_access import sender_may_run_commands

        class _Exploding:
            @property
            def platforms(self):
                raise RuntimeError("config is broken")

        wa = SessionSource(
            platform=Platform.WHATSAPP, chat_id="c", user_id="1", chat_type="dm",
        )
        assert sender_may_run_commands(_Exploding(), wa) is False


class TestDispatchGateRefusesSilently:
    """Second layer: a denial notice must never reach a public contact."""

    def _runner_check(self, extra: dict, platform, cmd: str):
        from gateway.config import GatewayConfig, PlatformConfig
        from gateway.run import GatewayRunner
        from gateway.session import SessionSource

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={platform: PlatformConfig(enabled=True, extra=extra)}
        )
        source = SessionSource(
            platform=platform, chat_id="c", user_id="557199999999", chat_type="dm",
        )
        return runner._check_slash_access(source, cmd)

    def test_public_surface_denial_is_empty_not_explanatory(self):
        from gateway.config import Platform

        assert self._runner_check({}, Platform.WHATSAPP, "reset") == ""

    def test_private_surface_still_explains_the_denial(self):
        from gateway.config import Platform

        out = self._runner_check(
            {"allow_admin_from": ["someone-else"]}, Platform.TELEGRAM, "reset"
        )
        assert out is not None
        assert "admin-only" in out


class TestMessageEventCommandNeutralization:
    def _event(self, text: str):
        from gateway.platforms.base import MessageEvent, MessageType
        from gateway.session import SessionSource
        from gateway.config import Platform

        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=SessionSource(
                platform=Platform.WHATSAPP,
                chat_id="557199999999@s.whatsapp.net",
                user_id="557199999999",
                chat_type="dm",
            ),
        )

    def test_slash_text_is_ordinary_text_when_disabled(self):
        ev = self._event("/reset")
        assert ev.is_command() is True
        ev.commands_disabled = True
        assert ev.is_command() is False
        assert ev.get_command() is None
        # The text still reaches the agent verbatim, as normal conversation.
        assert ev.text == "/reset"

    def test_command_args_fall_back_to_whole_text(self):
        ev = self._event("/reset agora")
        ev.commands_disabled = True
        assert ev.get_command_args() == "/reset agora"

    def test_default_is_enabled_so_other_platforms_are_untouched(self):
        assert self._event("/reset").commands_disabled is False

    def test_plaintext_restart_coercion_is_blocked(self):
        """"restart gateway" would otherwise bounce the live gateway."""
        from gateway.platforms.base import coerce_plaintext_gateway_command

        ev = self._event("restart gateway")
        ev.commands_disabled = True
        coerce_plaintext_gateway_command(ev)
        assert ev.text == "restart gateway"

    def test_plaintext_restart_coercion_still_works_when_entitled(self):
        from gateway.platforms.base import coerce_plaintext_gateway_command

        ev = self._event("restart gateway")
        coerce_plaintext_gateway_command(ev)
        assert ev.text == "/restart"


class TestAdapterAppliesEntitlement:
    """End-to-end through the adapter hook that runs on every inbound message."""

    def _adapter(self, extra: dict):
        from gateway.config import Platform, PlatformConfig
        from gateway.platforms.base import BasePlatformAdapter

        class _Adapter(BasePlatformAdapter):
            name = "Whatsapp"

            async def connect(self):  # pragma: no cover - abstract stubs
                pass

            async def disconnect(self):  # pragma: no cover
                pass

            async def get_chat_info(self, chat_id):  # pragma: no cover
                return {}

            async def send(self, *a, **kw):  # pragma: no cover
                pass

        return _Adapter(
            PlatformConfig(enabled=True, extra=extra), Platform.WHATSAPP
        )

    def _event(self, text: str):
        from gateway.config import Platform
        from gateway.platforms.base import MessageEvent, MessageType
        from gateway.session import SessionSource

        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=SessionSource(
                platform=Platform.WHATSAPP,
                chat_id="557199999999@s.whatsapp.net",
                user_id="557199999999",
                chat_type="dm",
            ),
        )

    def test_production_extra_disables_commands_for_a_patient(self):
        """The secretary profile's real ``extra`` — only appointment config."""
        adapter = self._adapter({"secretary_appointments": {"enabled": True}})
        ev = self._event("/reset")
        adapter._apply_command_entitlement(ev)
        assert ev.commands_disabled is True
        assert ev.is_command() is False

    def test_configured_admin_keeps_commands(self):
        adapter = self._adapter({"allow_admin_from": ["557199999999"]})
        ev = self._event("/reset")
        adapter._apply_command_entitlement(ev)
        assert ev.commands_disabled is False
        assert ev.get_command() == "reset"

    def test_internal_events_are_never_neutralized(self):
        adapter = self._adapter({})
        ev = self._event("/reset")
        ev.internal = True
        adapter._apply_command_entitlement(ev)
        assert ev.commands_disabled is False

    def test_broken_config_fails_closed_on_whatsapp(self):
        adapter = self._adapter({})
        adapter.config = object()  # no .extra at all
        ev = self._event("/reset")
        adapter._apply_command_entitlement(ev)
        assert ev.commands_disabled is True


# ---------------------------------------------------------------------------
# 2. Provider failure envelopes never reach a contact
# ---------------------------------------------------------------------------


class TestProviderErrorSuppression:
    def _sanitize(self, text: str):
        from gateway.run import _sanitize_gateway_final_response

        return _sanitize_gateway_final_response("whatsapp", text)

    @pytest.mark.parametrize(
        "text",
        [
            # The literal string from the incident.
            "HTTP 503: This model is currently experiencing high demand. "
            "Spikes in demand are usually temporary. Please try again later.",
            "Gemini HTTP 503 (UNAVAILABLE): This model is currently "
            "experiencing high demand.",
            "⚠️ API call failed (attempt 3/3): GeminiAPIError",
            "HTTP 429: rate limited",
            "Error code: 500 - internal server error",
            "invalid api key",
            "RESOURCE_EXHAUSTED",
        ],
    )
    def test_provider_envelopes_are_silenced(self, text):
        assert self._sanitize(text) is None

    def test_long_envelope_is_still_caught(self):
        """The generic detector bails past 400 chars; the public line can't."""
        text = "HTTP 503: " + ("this model is experiencing high demand. " * 20)
        assert len(text) > 400
        assert self._sanitize(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "Bom dia! Aqui é a assistente do Dr. Victor Almeida.",
            "Para verificar a agenda, pode me informar seu CPF (apenas números)?",
            "Vou repassar sua solicitação ao Dr. Victor.",
            "O senhor pode comparecer 15 minutos antes da consulta.",
            "Recebi seu exame interno de rotina, vou encaminhar ao médico.",
        ],
    )
    def test_normal_secretary_replies_pass_through(self, text):
        out = self._sanitize(text)
        assert out is not None
        assert out.strip() != ""

    def test_status_token_match_is_case_sensitive(self):
        """Portuguese prose containing 'internal' must not be silenced."""
        out = self._sanitize("O exame de medicina internal foi recebido.")
        assert out is not None


# ---------------------------------------------------------------------------
# 3. Summarizing an API error can never itself raise
# ---------------------------------------------------------------------------


class TestSummarizeApiErrorIsTotal:
    def test_streaming_response_does_not_raise(self):
        """httpx raises ResponseNotRead for .text on an unread stream."""
        import httpx
        from run_agent import AIAgent

        response = httpx.Response(503, content=b"boom")
        # Simulate a streaming response whose body was never read.
        response.is_stream_consumed = False
        del response._content

        error = RuntimeError("upstream exploded")
        error.response = response
        error.status_code = 503

        with pytest.raises(httpx.ResponseNotRead):
            _ = response.text  # precondition: the hazard is real

        summary = AIAgent._summarize_api_error(error)
        assert isinstance(summary, str)
        assert summary

    def test_pathological_error_object_still_summarizes(self):
        from run_agent import AIAgent

        class Hostile(Exception):
            def __str__(self):
                raise ValueError("cannot stringify")

        assert isinstance(AIAgent._summarize_api_error(Hostile()), str)


# ---------------------------------------------------------------------------
# 4. Agent-result dicts never reach the string-only media pipeline
# ---------------------------------------------------------------------------


class TestHandlerResponseNormalization:
    def _normalize(self, value):
        from gateway.platforms.base import BasePlatformAdapter

        return BasePlatformAdapter._normalize_handler_response(value)

    def test_silent_blocklist_shape_becomes_none(self):
        """A truthy dict previously passed `if response:` and then crashed."""
        result = {
            "final_response": "",
            "messages": [],
            "api_calls": 0,
            "silent": True,
        }
        assert bool(result) is True  # the trap
        assert self._normalize(result) is None

    def test_silence_marker_shape_becomes_none(self):
        result = {"final_response": "SILENT", "messages": [], "api_calls": 0}
        assert self._normalize(result) is None

    def test_real_text_is_unwrapped(self):
        result = {"final_response": "Bom dia!", "messages": [], "api_calls": 0}
        assert self._normalize(result) == "Bom dia!"

    def test_failed_turn_keeps_its_text(self):
        """A failure is not intentional silence — text must survive."""
        result = {"final_response": "SILENT", "failed": True, "messages": []}
        assert self._normalize(result) == "SILENT"

    def test_strings_and_none_pass_through(self):
        assert self._normalize("hello") == "hello"
        assert self._normalize(None) is None

    def test_ephemeral_reply_passes_through_untouched(self):
        from gateway.platforms.base import EphemeralReply

        reply = EphemeralReply("banner")
        assert self._normalize(reply) is reply

    def test_normalized_result_survives_extract_media(self):
        """End-to-end: the shape that crashed now reaches extract_media safely."""
        from gateway.platforms.base import BasePlatformAdapter

        raw = {"final_response": "", "messages": [], "api_calls": 0, "silent": True}
        response = self._normalize(raw)
        if response:  # the adapter's own guard
            BasePlatformAdapter.extract_media(response)
        assert response is None
