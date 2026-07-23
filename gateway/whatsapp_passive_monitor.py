"""Passive, fail-closed storage for allowlisted WhatsApp group messages.

This module deliberately has no gateway dispatch or outbound transport code. It
only stores an already-extracted MessageEvent when its group JID exactly matches
the configured monitor JID.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable


_GROUP_SUFFIX = "@g.us"
_DEFAULT_MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024


def normalize_jid(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_destination_jid(value: Any) -> str:
    """Normalize a private Brazilian WhatsApp destination and reject groups."""
    raw = normalize_jid(value)
    if raw.endswith(_GROUP_SUFFIX):
        raise ValueError("group JID is not a valid private digest destination")
    if "@" in raw:
        local, suffix = raw.split("@", 1)
        if suffix not in {"s.whatsapp.net", "lid"} or not local.isdigit():
            raise ValueError("invalid private WhatsApp destination")
        return f"{local}@{suffix}"

    digits = re.sub(r"\D", "", raw)
    if len(digits) in {10, 11}:
        digits = "55" + digits
    if not digits.isdigit() or len(digits) < 10:
        raise ValueError("invalid private WhatsApp destination number")
    return f"{digits}@s.whatsapp.net"


def is_monitored_group(data: dict[str, Any], monitored_jids: Iterable[str]) -> bool:
    """Return true only for an explicitly configured WhatsApp group JID."""
    if not bool(data.get("isGroup")):
        return False
    chat_id = normalize_jid(data.get("chatId"))
    if not chat_id.endswith(_GROUP_SUFFIX):
        return False
    configured = {normalize_jid(item) for item in monitored_jids if normalize_jid(item)}
    return chat_id in configured


def _event_timestamp(event: Any) -> int:
    raw = getattr(event, "raw_message", None) or {}
    value = raw.get("timestamp") or raw.get("messageTimestamp") or 0
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp > 10**12:
        timestamp //= 1000
    return timestamp


def _safe_filename(value: Any) -> str:
    name = Path(str(value or "attachment")).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:160] or "attachment"


class PassiveMessageStore:
    """SQLite-backed store with message-id deduplication and private files."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_attachment_bytes: int = _DEFAULT_MAX_ATTACHMENT_BYTES,
        media_path_validator: Callable[[str], bool] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.attachments_dir = self.root / "attachments"
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.attachments_dir, 0o700)
        self.db_path = self.root / "messages.sqlite3"
        self.max_attachment_bytes = int(max_attachment_bytes)
        self.media_path_validator = media_path_validator
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    sender_name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    media_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch())
                );
                CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                    ON messages(timestamp);
                CREATE TABLE IF NOT EXISTS digests (
                    digest_key TEXT PRIMARY KEY,
                    sent_at INTEGER NOT NULL,
                    content_hash TEXT NOT NULL
                );
                """
            )
        os.chmod(self.db_path, 0o600)

    def capture(self, event: Any, *, monitored_group_jid: str) -> bool:
        """Persist one event if it belongs to the exact monitored group.

        Returns True only when a new row is inserted. No outbound action is
        performed here.
        """
        source = getattr(event, "source", None)
        chat_id = normalize_jid(getattr(source, "chat_id", ""))
        configured = normalize_jid(monitored_group_jid)
        if not chat_id or chat_id != configured or not chat_id.endswith(_GROUP_SUFFIX):
            return False
        message_id = str(getattr(event, "message_id", "") or "").strip()
        if not message_id:
            return False

        timestamp = _event_timestamp(event)
        sender_name = str(getattr(source, "user_name", "") or "Participante").strip()[:200]
        text = str(getattr(event, "text", "") or "").strip()
        media_urls = list(getattr(event, "media_urls", None) or [])
        media_types = list(getattr(event, "media_types", None) or [])

        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone():
                return False

            media = []
            for index, raw_path in enumerate(media_urls):
                path = Path(str(raw_path))
                if not path.is_absolute() or not path.exists() or not path.is_file():
                    continue
                if self.media_path_validator and not self.media_path_validator(str(path)):
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size < 0 or size > self.max_attachment_bytes:
                    continue
                destination = self.attachments_dir / (
                    f"{_safe_filename(message_id)}_{index}_{_safe_filename(path.name)}"
                )
                try:
                    shutil.copy2(path, destination)
                    os.chmod(destination, 0o600)
                except OSError:
                    continue
                media.append(
                    {
                        "path": str(destination),
                        "mime": str(media_types[index] if index < len(media_types) else ""),
                        "name": path.name,
                    }
                )

            conn.execute(
                """
                INSERT INTO messages
                    (message_id, chat_id, timestamp, sender_name, text, media_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    chat_id,
                    timestamp,
                    sender_name,
                    text,
                    json.dumps(media, ensure_ascii=False),
                ),
            )
        return True

    def list_messages(self, start_timestamp: int, end_timestamp: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, chat_id, timestamp, sender_name, text, media_json
                FROM messages
                WHERE timestamp >= ? AND timestamp < ?
                ORDER BY timestamp ASC, message_id ASC
                """,
                (int(start_timestamp), int(end_timestamp)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["media"] = json.loads(item.pop("media_json"))
            except (TypeError, ValueError):
                item["media"] = []
                item.pop("media_json", None)
            result.append(item)
        return result

    def digest_was_sent(self, digest_key: str) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM digests WHERE digest_key = ?", (digest_key,)
            ).fetchone() is not None

    def record_digest(self, digest_key: str, sent_at: int, content_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO digests(digest_key, sent_at, content_hash) VALUES (?, ?, ?)",
                (digest_key, int(sent_at), str(content_hash)),
            )

    def cleanup(self, before_timestamp: int) -> int:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT media_json FROM messages WHERE timestamp < ?", (int(before_timestamp),)
            ).fetchall()
            removed = conn.execute(
                "DELETE FROM messages WHERE timestamp < ?", (int(before_timestamp),)
            ).rowcount
        for row in rows:
            try:
                media = json.loads(row[0])
            except (TypeError, ValueError):
                media = []
            for item in media:
                try:
                    path = Path(item.get("path", "")).resolve()
                    if path.is_relative_to(self.attachments_dir) and path.exists():
                        path.unlink()
                except (OSError, ValueError, AttributeError):
                    pass
        return int(removed or 0)
