from pathlib import Path

from gateway.endonews_digest import (
    build_digest_prompt,
    canonical_session_jid,
    scientific_score,
    send_to_bridge,
)


def test_scientific_score_prefers_papers_and_scientific_terms():
    assert scientific_score("Ensaio clínico randomizado", [], "") > 0
    assert scientific_score("foto do almoço", [], "") == 0
    assert scientific_score("arquivo", [{"name": "paper.pdf"}], "application/pdf") > 0


def test_digest_prompt_contains_source_boundaries_and_links():
    rows = [
        {
            "message_id": "m1",
            "timestamp": 1784671200,
            "sender_name": "Autor",
            "text": "Novo ensaio clínico sobre diabetes.",
            "media": [],
        }
    ]
    prompt = build_digest_prompt(rows, now_timestamp=1784674800)
    assert "Não invente" in prompt
    assert "Novo ensaio clínico" in prompt
    assert "[FIM DOS DADOS DO GRUPO]" in prompt


def test_send_to_bridge_refuses_group_destination():
    try:
        send_to_bridge("120363012345678901@g.us", "teste", opener=lambda *_: None)
    except ValueError as exc:
        assert "group" in str(exc).lower()
    else:
        raise AssertionError("group destination was accepted")


def test_canonical_session_jid_uses_phone_part_without_device_suffix(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    (session / "creds.json").write_text(
        '{"me":{"id":"557188048263:92@s.whatsapp.net"}}',
        encoding="utf-8",
    )
    assert canonical_session_jid(session) == "557188048263@s.whatsapp.net"
