from types import SimpleNamespace

import pytest
from gateway.config import Platform, load_gateway_config
from gateway.whatsapp_passive_monitor import PassiveMessageStore, is_monitored_group, normalize_destination_jid


def test_normalize_brazilian_private_destination():
    assert normalize_destination_jid("71 88048263") == "557188048263@s.whatsapp.net"


def test_destination_rejects_group_jid():
    with pytest.raises(ValueError, match="group"):
        normalize_destination_jid("120363012345678901@g.us")


def test_passive_monitor_requires_exact_group_jid():
    data = {"isGroup": True, "chatId": "120363012345678901@g.us"}
    assert is_monitored_group(data, ["120363012345678901@g.us"])
    assert not is_monitored_group(data, ["120363099999999999@g.us"])
    assert not is_monitored_group({"isGroup": False, "chatId": data["chatId"]}, [data["chatId"]])


def test_store_deduplicates_and_reads_captured_messages(tmp_path):
    store = PassiveMessageStore(tmp_path)
    event = SimpleNamespace(
        message_id="msg-1",
        text="Ensaio clínico randomizado sobre tireoide",
        source=SimpleNamespace(
            chat_id="120363012345678901@g.us",
            chat_type="group",
            user_name="Pesquisador",
        ),
        raw_message={"timestamp": 1784671200},
        media_urls=[],
        media_types=[],
    )

    assert store.capture(event, monitored_group_jid="120363012345678901@g.us") is True
    assert store.capture(event, monitored_group_jid="120363012345678901@g.us") is False
    rows = store.list_messages(1784671199, 1784671201)
    assert len(rows) == 1
    assert rows[0]["message_id"] == "msg-1"
    assert rows[0]["sender_name"] == "Pesquisador"
    assert rows[0]["text"] == "Ensaio clínico randomizado sobre tireoide"


def test_store_does_not_capture_wrong_group(tmp_path):
    store = PassiveMessageStore(tmp_path)
    event = SimpleNamespace(
        message_id="msg-2",
        text="texto",
        source=SimpleNamespace(
            chat_id="120363099999999999@g.us",
            chat_type="group",
            user_name="Pessoa",
        ),
        raw_message={"timestamp": 1784671200},
        media_urls=[],
        media_types=[],
    )
    assert store.capture(event, monitored_group_jid="120363012345678901@g.us") is False
    assert store.list_messages(0, 9999999999) == []


def test_config_bridges_passive_monitor_settings(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "whatsapp:\n"
        "  enabled: true\n"
        "  group_policy: disabled\n"
        "  passive_monitor:\n"
        "    group_jid: 120363012345678901@g.us\n"
        "    destination_jid: 5571988048263@s.whatsapp.net\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = load_gateway_config()
    assert config.platforms[Platform.WHATSAPP].extra["passive_monitor"]["group_jid"] == (
        "120363012345678901@g.us"
    )
