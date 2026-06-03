import importlib
import types


def test_tui_model_options_requests_full_model_catalog(monkeypatch):
    """TUI model.options must not truncate model lists to 50 entries."""
    server = importlib.import_module("tui_gateway.server")
    captured = {}

    class FakeContext:
        def with_overrides(self, **kwargs):
            return self

    def fake_build_models_payload(ctx, **kwargs):
        captured.update(kwargs)
        return {"providers": [{"slug": "openai-api", "models": [f"m{i}" for i in range(120)], "total_models": 120}]}

    monkeypatch.setattr("hermes_cli.inventory.load_picker_context", lambda: FakeContext())
    monkeypatch.setattr("hermes_cli.inventory.build_models_payload", fake_build_models_payload)
    monkeypatch.setattr(server, "_resolve_model", lambda: "gpt-5.5")
    monkeypatch.setattr(server, "_sessions", {})

    response = server._methods["model.options"]("rid-1", {})

    assert response["result"]["providers"][0]["total_models"] == 120
    assert captured["max_models"] is None
    assert captured["include_unconfigured"] is True
    assert captured["picker_hints"] is True
    assert captured["canonical_order"] is True


def test_tui_model_save_key_refresh_requests_full_model_catalog(monkeypatch):
    """After saving a key, the returned provider row should also be complete."""
    server = importlib.import_module("tui_gateway.server")
    captured = {}

    class FakeContext:
        def with_overrides(self, **kwargs):
            return self

    def fake_build_models_payload(ctx, **kwargs):
        captured.update(kwargs)
        return {"providers": [{"slug": "openai-api", "models": [f"m{i}" for i in range(120)], "total_models": 120}]}

    monkeypatch.setattr(
        "hermes_cli.auth.PROVIDER_REGISTRY",
        {"openai-api": types.SimpleNamespace(auth_type="api_key", api_key_env_vars=["OPENAI_API_KEY"], name="OpenAI API")},
    )
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.config.save_env_value", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.inventory.load_picker_context", lambda: FakeContext())
    monkeypatch.setattr("hermes_cli.inventory.build_models_payload", fake_build_models_payload)
    monkeypatch.setattr(server, "_resolve_model", lambda: "gpt-5.5")
    monkeypatch.setattr(server, "_sessions", {})

    response = server._methods["model.save_key"]("rid-2", {"slug": "openai-api", "api_key": "sk-test"})

    provider = response["result"]["provider"]
    assert provider["total_models"] == 120
    assert provider["authenticated"] is True
    assert captured["max_models"] is None
    assert captured["picker_hints"] is True
