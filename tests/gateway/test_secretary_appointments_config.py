from pathlib import Path

import yaml


CONFIG_PATH = Path(__file__).parents[2] / "config" / "secretary-appointments.staging.yaml"


def test_staging_secretary_appointments_config_has_independent_closed_gates():
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    appointment = config["platforms"]["whatsapp"]["secretary_appointments"]

    assert appointment["enabled"] is True
    assert appointment["write_enabled"] is False
    assert appointment["payment"]["enabled"] is False
    assert appointment["rollout"] == {
        "environment": "staging",
        "activate_on_startup": False,
        "allow_real_mutations": False,
    }
    assert appointment["professional_id"] == 1
    assert appointment["specialty_id"] == 1
    assert appointment["local_id"] == 1
    assert appointment["channel_id"] == 3
    assert appointment["cancel_reason_id"] == 1
    assert appointment["slot_search_days"] == 15
    assert appointment["return_window_days"] == 60
