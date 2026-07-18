"""Regression tests for the WhatsApp ignored-number denylist."""

from types import SimpleNamespace

from gateway.run import (
    _whatsapp_blocklist_match,
    _whatsapp_blocklist_status,
)


def test_blocklist_matches_phone_jid_and_lid_resolution():
    mapping = [("5511998877665", "123456789012345")]

    assert _whatsapp_blocklist_match(
        ["55 11 99887-7665@s.whatsapp.net"],
        ["5511998877665"],
        mapping,
    )
    assert _whatsapp_blocklist_match(
        ["123456789012345@lid"],
        ["5511998877665"],
        mapping,
    )
    assert not _whatsapp_blocklist_match(
        ["5511998877666@s.whatsapp.net"],
        ["5511998877665"],
        mapping,
    )


def test_blocklist_status_reads_active_profile_and_reverse_mapping(tmp_path, monkeypatch):
    whatsapp_dir = tmp_path / "whatsapp"
    session_dir = whatsapp_dir / "session"
    session_dir.mkdir(parents=True)
    (whatsapp_dir / "ignored_numbers.txt").write_text(
        "5511998877665\n", encoding="utf-8"
    )
    (session_dir / "lid-mapping-123456789012345_reverse.json").write_text(
        '"5511998877665"', encoding="utf-8"
    )
    monkeypatch.setattr("gateway.run.get_hermes_home", lambda: tmp_path)

    source = SimpleNamespace(
        user_id="123456789012345@lid",
        user_id_alt=None,
        chat_id="123456789012345@lid",
    )
    blocked, reason = _whatsapp_blocklist_status(source)

    assert blocked
    assert reason == "matched"


def test_blocklist_status_allows_sender_not_in_list(tmp_path, monkeypatch):
    whatsapp_dir = tmp_path / "whatsapp"
    whatsapp_dir.mkdir(parents=True)
    (whatsapp_dir / "ignored_numbers.txt").write_text(
        "5511998877665\n", encoding="utf-8"
    )
    monkeypatch.setattr("gateway.run.get_hermes_home", lambda: tmp_path)

    source = SimpleNamespace(
        user_id="5511998877666@s.whatsapp.net",
        user_id_alt=None,
        chat_id="5511998877666@s.whatsapp.net",
    )
    blocked, reason = _whatsapp_blocklist_status(source)

    assert not blocked
    assert reason == "not_matched"
