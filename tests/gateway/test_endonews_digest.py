from pathlib import Path
import io
import urllib.error

from gateway.endonews_digest import (
    _focus_scientific_sections,
    build_digest_prompt,
    canonical_session_jid,
    scientific_score,
    send_to_bridge,
)
from gateway.whatsapp_passive_monitor import PassiveMessageStore


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


def test_scientific_focus_keeps_abstract_discussion_and_conclusion():
    source = "Título\nAbstract\nResumo inicial.\nDiscussion\nInterpretação dos resultados.\nConclusion\nConclusão clínica."
    focused = _focus_scientific_sections(source)
    assert "Resumo inicial" in focused
    assert "Interpretação dos resultados" in focused
    assert "Conclusão clínica" in focused


def test_digest_prompt_marks_attachments_and_translation_policy():
    prompt = build_digest_prompt(
        [
            {
                "message_id": "paper-1",
                "timestamp": 1784671200,
                "sender_name": "Autor",
                "text": "",
                "media": [
                    {
                        "name": "paper-original.pdf",
                        "mime": "application/pdf",
                        "path": "/does/not/exist/paper-original.pdf",
                    }
                ],
            }
        ],
        now_timestamp=1784674800,
    )
    assert "paper-original.pdf" in prompt
    assert "paper original como fonte principal" in prompt
    assert "Discussão/interpretação" in prompt
    assert "Conclusão/implicação" in prompt


def test_passive_store_purge_all_removes_messages_and_private_media(tmp_path):
    store = PassiveMessageStore(tmp_path)
    attachment = store.attachments_dir / "paper.pdf"
    attachment.write_bytes(b"pdf")
    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO messages
                (message_id, chat_id, timestamp, sender_name, text, media_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "m1",
                "group@g.us",
                100,
                "Autor",
                "paper",
                '[{"path":"' + str(attachment) + '","mime":"application/pdf","name":"paper.pdf"}]',
            ),
        )
    assert store.purge_all() == 1
    assert not attachment.exists()
    assert store.list_messages(0, 200) == []


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
