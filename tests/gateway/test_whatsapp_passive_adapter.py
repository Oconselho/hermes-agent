import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.config import Platform
from gateway.whatsapp_passive_monitor import PassiveMessageStore
from plugins.platforms.whatsapp.adapter import WhatsAppAdapter


def test_passive_capture_stores_event_without_dispatch(tmp_path):
    group_jid = "120363012345678901@g.us"
    store = PassiveMessageStore(tmp_path)
    adapter = object.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter._passive_monitor_group_jids = {group_jid}
    adapter._passive_store = store
    adapter._build_message_event = AsyncMock(
        return_value=SimpleNamespace(
            message_id="passive-1",
            text="artigo científico",
            source=SimpleNamespace(
                chat_id=group_jid,
                chat_type="group",
                user_name="Autor",
            ),
            raw_message={"timestamp": 1784671200},
            media_urls=[],
            media_types=[],
        )
    )
    adapter.handle_message = AsyncMock()

    data = {"isGroup": True, "chatId": group_jid}
    captured = asyncio.run(adapter._capture_passive_message(data))

    assert captured is True
    assert adapter._build_message_event.await_args.kwargs["bypass_policy"] is True
    adapter.handle_message.assert_not_awaited()
    assert len(store.list_messages(1784671199, 1784671201)) == 1
