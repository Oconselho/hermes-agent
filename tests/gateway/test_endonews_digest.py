from pathlib import Path
import io
import urllib.error

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


def test_send_to_bridge_retries_transient_503_then_succeeds():
    calls = []
    sleeps = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"success": true, "messageId": "m1"}'

    def opener(request, timeout):
        calls.append((request, timeout))
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 503, "unavailable", {}, io.BytesIO(b"")
            )
        return Response()

    result = send_to_bridge(
        "557188048263",
        "teste",
        opener=opener,
        sleep_fn=sleeps.append,
        retry_delays=(0.25,),
    )
    assert result["success"] is True
    assert len(calls) == 2
    assert sleeps == [0.25]


def test_send_to_bridge_does_not_retry_permanent_http_error():
    calls = []

    def opener(request, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(
            request.full_url, 500, "server error", {}, io.BytesIO(b"")
        )

    try:
        send_to_bridge(
            "557188048263",
            "teste",
            opener=opener,
            sleep_fn=lambda _: (_ for _ in ()).throw(AssertionError("slept")),
            retry_delays=(0,),
        )
    except RuntimeError as exc:
        assert "HTTP 500" in str(exc)
    else:
        raise AssertionError("permanent bridge error was accepted")
    assert len(calls) == 1


def test_canonical_session_jid_uses_phone_part_without_device_suffix(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    (session / "creds.json").write_text(
        '{"me":{"id":"557188048263:92@s.whatsapp.net"}}',
        encoding="utf-8",
    )
    assert canonical_session_jid(session) == "557188048263@s.whatsapp.net"
