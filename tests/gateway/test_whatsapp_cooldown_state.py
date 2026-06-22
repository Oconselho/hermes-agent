"""WhatsApp cooldown state compatibility regressions."""

import pytest

from gateway import run as gateway_run


def test_whatsapp_cooldown_dict_without_until_is_not_active():
    """New spam-state dict entries must not be compared directly to floats."""
    state_entry = {
        "message_count": 3,
        "first_message_ts": 1780423954.7284663,
        "last_message_ts": 1780439077.6294668,
        "bot_pattern_count": 0,
        "reject_count": 0,
        "disengaged": False,
    }

    assert gateway_run._whatsapp_cooldown_until_seconds(state_entry) == 0.0


@pytest.mark.parametrize(
    ("state_entry", "expected"),
    [
        (1234.5, 1234.5),
        ({"cooldown_until": 2345.5}, 2345.5),
        ({"silence_until": 3456.5}, 3456.5),
        (1782171906359, 1782171906.359),
        ({"cooldown_until": 1782171906359}, 1782171906.359),
    ],
)
def test_whatsapp_cooldown_until_seconds_accepts_legacy_and_current_shapes(
    state_entry,
    expected,
):
    assert gateway_run._whatsapp_cooldown_until_seconds(state_entry) == expected


def test_whatsapp_silent_agent_result_is_intentional_silence():
    result = gateway_run._whatsapp_silent_agent_result()

    assert result == {"final_response": "SILENT", "messages": [], "api_calls": 0}
