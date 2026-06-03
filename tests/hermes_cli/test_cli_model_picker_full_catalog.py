import cli as cli_module


class _FakePickerContext:
    user_providers = {}
    custom_providers = []

    def with_overrides(self, **kwargs):
        return self


def test_slash_model_picker_requests_full_provider_catalog(monkeypatch):
    """The CLI /model modal should not truncate provider model lists to 50.

    Provider rows already carry total_models for display; when a user opens a
    provider, the modal must receive the full model list so entries after the
    first 50 (for example gpt-5.5 on openai-api) remain selectable.
    """

    captured = {}

    def fake_build_models_payload(ctx, **kwargs):
        captured["max_models"] = kwargs.get("max_models")
        return {
            "providers": [
                {
                    "slug": "openai-api",
                    "name": "OpenAI API",
                    "is_current": True,
                    "models": [f"model-{i}" for i in range(120)],
                    "total_models": 120,
                    "source": "user-config",
                }
            ]
        }

    opened = {}

    def fake_open_model_picker(self, providers, current_model, current_provider, user_provs=None, custom_provs=None):
        opened["providers"] = providers

    monkeypatch.setattr("hermes_cli.inventory.load_picker_context", lambda: _FakePickerContext())
    monkeypatch.setattr("hermes_cli.inventory.build_models_payload", fake_build_models_payload)
    monkeypatch.setattr(cli_module.HermesCLI, "_open_model_picker", fake_open_model_picker)
    monkeypatch.setattr(cli_module, "_cprint", lambda *args, **kwargs: None)

    shell = object.__new__(cli_module.HermesCLI)
    shell.model = "gpt-5.5"
    shell.provider = "openai-api"
    shell.base_url = "https://api.openai.com/v1"
    shell.api_key = "test-key"

    shell._handle_model_switch("/model")

    assert captured["max_models"] is None
    assert len(opened["providers"][0]["models"]) == 120
