"""Tests for gateway.whatsapp_identity alias resolution path."""

import json

from gateway.whatsapp_identity import (
    brazilian_whatsapp_number,
    expand_whatsapp_aliases,
    to_whatsapp_phone_jid,
)


def test_aliases_resolve_on_modern_platforms_layout(tmp_path, monkeypatch):
    tmp_home = tmp_path / "hermes-home"
    mapping_dir = tmp_home / "platforms" / "whatsapp" / "session"
    mapping_dir.mkdir(parents=True, exist_ok=True)
    (mapping_dir / "lid-mapping-999999999999999.json").write_text(
        json.dumps("15551234567@s.whatsapp.net"),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_home))

    assert expand_whatsapp_aliases("999999999999999@lid") == {
        "999999999999999",
        "15551234567",
    }


def test_aliases_resolve_on_legacy_layout(tmp_path, monkeypatch):
    tmp_home = tmp_path / "hermes-home"
    mapping_dir = tmp_home / "whatsapp" / "session"
    mapping_dir.mkdir(parents=True, exist_ok=True)
    (mapping_dir / "lid-mapping-999999999999999.json").write_text(
        json.dumps("15551234567@s.whatsapp.net"),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_home))

    assert expand_whatsapp_aliases("999999999999999@lid") == {
        "999999999999999",
        "15551234567",
    }


def test_ninth_digit_is_dropped_for_the_ddds_whatsapp_drops_it_in():
    """DDD 31-99 addresses mobiles without the ninth digit.

    The reception number of the clinic is the case that mattered: written in
    config.yaml the way a human writes it, sends to it were accepted and
    delivered to nobody.
    """
    assert to_whatsapp_phone_jid("5571996691002@s.whatsapp.net") == (
        "557196691002@s.whatsapp.net"
    )
    for written, addressed in (
        ("5571981349420", "557181349420"),   # Salvador/BA
        ("5531988887777", "553188887777"),   # Belo Horizonte/MG
        ("5548999998888", "554899998888"),   # Florianópolis/SC
        ("5561987654321", "556187654321"),   # Brasília/DF
    ):
        assert brazilian_whatsapp_number(written) == addressed, written


def test_ninth_digit_is_kept_and_restored_for_ddd_11_to_28():
    for written, addressed in (
        ("5511945403044", "5511945403044"),  # already right, unchanged
        ("551198887777", "5511998887777"),   # human dropped it; put it back
        ("552199887766", "5521999887766"),
    ):
        assert brazilian_whatsapp_number(written) == addressed, written


def test_a_dropped_ninth_digit_is_not_restored_when_it_would_be_a_guess():
    """In DDD 11-28 an eight-digit local starting 2-5 is a landline.

    ``5511945403044`` with its 9 removed reads ``551145403044``, which is
    exactly the shape of a São Paulo landline. Nothing in the number says
    which it is, so it is left alone rather than turned into a mobile that
    may not exist.
    """
    assert brazilian_whatsapp_number("551145403044") == "551145403044"


def test_landlines_and_voip_never_gain_or_lose_a_digit():
    """Only unambiguous mobiles are touched.

    ``5534936187583`` is a real number on the clinic's account: DDD 34 with a
    nine-digit local part that is NOT a ninth digit — the ``93`` prefix is a
    VoIP range. Stripping its leading 9 would invent a number nobody answers.
    """
    for number in (
        "5534936187583",   # VoIP, nine digits by nature
        "5571920021858",   # VoIP in a DDD that drops the ninth digit
        "557133364898",    # Salvador landline
        "551133909017",    # São Paulo landline
        "5511987654321",   # already in its addressed form
    ):
        assert brazilian_whatsapp_number(number) == number, number


def test_non_brazilian_and_non_phone_targets_pass_through():
    for value in (
        "50766715226@s.whatsapp.net",
        "52802445381872@lid",
        "120363000000000000@g.us",
        "status@broadcast",
    ):
        assert to_whatsapp_phone_jid(value) == value, value
    assert to_whatsapp_phone_jid("") == ""
    assert brazilian_whatsapp_number(None) == ""
