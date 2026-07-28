from hermes_state import SessionDB


def test_replace_latest_assistant_content_updates_only_matching_latest_row(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    session_id = "whatsapp-response-rewrite"
    db.create_session(session_id, "whatsapp:test")
    db.append_message(session_id, "user", "Pergunta", timestamp=100.0)
    db.append_message(session_id, "assistant", "Resposta genérica", timestamp=101.0)

    assert db.replace_latest_assistant_content(
        session_id,
        "Resposta genérica",
        "Resposta contextual",
    ) is True
    assert db.get_messages_as_conversation(session_id)[-1]["content"] == "Resposta contextual"
    assert db.replace_latest_assistant_content(
        session_id,
        "Resposta genérica",
        "Não deve aplicar",
    ) is False
    assert db.get_messages_as_conversation(session_id)[-1]["content"] == "Resposta contextual"
    db.close()