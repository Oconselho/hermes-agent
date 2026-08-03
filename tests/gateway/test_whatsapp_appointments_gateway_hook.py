"""The gateway ownership boundary: ``None`` means legacy, exactly once.

``GatewayRunner._handle_message`` runs the deterministic handler and then
takes one of two mutually exclusive branches: use the deterministic reply,
or call ``_run_agent`` (the legacy model pipeline). These tests pin the
decision method that feeds that branch, so an institutional contact can
never be answered deterministically and can never be dropped.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import gateway.run as gateway_run


def _runner(handler=None, *, ready=True):
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner._appointment_handler = handler
    runner._appointment_handler_ready = ready
    return runner


def _event(text="Quero agendar uma consulta", platform_value="whatsapp"):
    source = SimpleNamespace(
        platform=SimpleNamespace(value=platform_value) if platform_value else None,
        chat_id="5571999999999@s.whatsapp.net",
        chat_type="dm",
        user_id="5571999999999@s.whatsapp.net",
        user_name="Paciente",
    )
    return SimpleNamespace(text=text, source=source, message_id="hook-1", media_urls=[])


class RecordingHandler:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def handle(self, incoming):
        self.calls.append(incoming)
        return self.response


class RaisingHandler:
    def __init__(self):
        self.calls = []

    def handle(self, incoming):
        self.calls.append(incoming)
        raise RuntimeError("deterministic layer is broken")


def test_none_from_handler_preserves_legacy_and_consults_handler_once():
    handler = RecordingHandler(None)
    runner = _runner(handler)
    incoming = _event("Somos da clínica parceira")

    result = asyncio.run(
        runner._deterministic_appointment_response(incoming, incoming.source)
    )

    assert result is None  # -> ``_handle_message`` calls ``_run_agent``
    assert len(handler.calls) == 1


def test_deterministic_response_short_circuits_the_model_pipeline():
    handler = RecordingHandler("Olá, sou a assistente virtual.")
    runner = _runner(handler)
    incoming = _event()

    result = asyncio.run(
        runner._deterministic_appointment_response(incoming, incoming.source)
    )

    assert result == "Olá, sou a assistente virtual."
    assert len(handler.calls) == 1


def test_handler_exception_falls_back_to_legacy_without_propagating():
    handler = RaisingHandler()
    runner = _runner(handler)
    incoming = _event()

    result = asyncio.run(
        runner._deterministic_appointment_response(incoming, incoming.source)
    )

    assert result is None
    assert len(handler.calls) == 1


@pytest.mark.parametrize("platform_value", ["telegram", "signal", "discord", None])
def test_non_whatsapp_platforms_never_reach_the_handler(platform_value):
    handler = RecordingHandler("nunca")
    runner = _runner(handler)
    incoming = _event(platform_value=platform_value)

    result = asyncio.run(
        runner._deterministic_appointment_response(incoming, incoming.source)
    )

    assert result is None
    assert handler.calls == []


def test_unconfigured_handler_returns_none_without_building_anything(tmp_path):
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner.config = {"platforms": {"whatsapp": {}}}
    runner._appointment_handler = None
    runner._appointment_handler_ready = False

    assert runner._get_appointment_handler() is None
    # Cached: a second call does not retry construction.
    assert runner._appointment_handler_ready is True
    assert runner._get_appointment_handler() is None

    incoming = _event()
    assert (
        asyncio.run(
            runner._deterministic_appointment_response(incoming, incoming.source)
        )
        is None
    )


@pytest.mark.parametrize(
    "block",
    [
        {},
        {"enabled": False},
        {"enabled": "true"},  # only a real boolean opens the gate
        {"write_enabled": True},  # ``enabled`` still missing
    ],
)
def test_feature_gate_is_closed_unless_enabled_is_exactly_true(block):
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner.config = {"platforms": {"whatsapp": {"secretary_appointments": block}}}
    runner._appointment_handler = None
    runner._appointment_handler_ready = False

    assert runner._get_appointment_handler() is None


def _real_gateway_config(block=None):
    extra = {} if block is None else {"secretary_appointments": block}
    return SimpleNamespace(
        platforms={
            gateway_run.Platform.WHATSAPP: SimpleNamespace(extra=extra),
        }
    )


def _write_fake_feegow_token(monkeypatch, tmp_path):
    (tmp_path / "feegow_token.txt").write_text("fake-feegow-token", encoding="utf-8")
    monkeypatch.setattr(
        gateway_run.os.path,
        "abspath",
        lambda _path: str(tmp_path / "gateway" / "run.py"),
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))


def test_real_gateway_config_reads_enabled_appointments_from_platform_extra(
    monkeypatch, tmp_path
):
    """Production GatewayConfig shape must build the deterministic handler."""
    _write_fake_feegow_token(monkeypatch, tmp_path)
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner.config = _real_gateway_config({"enabled": True})
    runner._appointment_handler = None
    runner._appointment_handler_ready = False

    assert runner._get_appointment_handler() is not None


def test_real_gateway_config_without_appointments_extra_fails_closed():
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner.config = _real_gateway_config()
    runner._appointment_handler = None
    runner._appointment_handler_ready = False

    assert runner._get_appointment_handler() is None


@pytest.mark.parametrize("enabled", [False, "true", 1, None])
def test_real_gateway_config_requires_enabled_to_be_exactly_true(enabled):
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner.config = _real_gateway_config({"enabled": enabled})
    runner._appointment_handler = None
    runner._appointment_handler_ready = False

    assert runner._get_appointment_handler() is None
