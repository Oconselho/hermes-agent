"""Deterministic, fail-closed WhatsApp scheduling for in-person appointments.

The handler owns a small persisted state machine and talks only to an injected
Feegow-compatible client.  It never constructs a network client itself.  Every
write is preceded by a single-use authorization and read-only preflight, is
recorded in an idempotency ledger, and is followed by exact-ID readback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

__all__ = [
    "AppointmentStore",
    "FlowState",
    "Route",
    "WhatsAppAppointmentsHandler",
    "classify_route",
    "drain_appointment_outbox",
    "filter_eligible_slots",
    "is_valid_cpf",
    "run_appointment_watcher",
]

logger = logging.getLogger(__name__)


_BRT = ZoneInfo("America/Bahia")
_RECEPTION = (
    "Não foi possível concluir este agendamento com segurança. "
    "Por favor, fale com a recepção pelo WhatsApp 71 99669-1002."
)


class _PreflightRejected(RuntimeError):
    """A read-only preflight refused the mutation; nothing was written.

    Distinguishing this from an ambiguous transport failure matters: there
    is nothing remote to reconcile, so the public flow must fail closed to
    reception instead of offering ``RECONCILIAR``.
    """


class _WriteDisabled(_PreflightRejected):
    """The write kill switch was closed at the last check before a mutation."""


class Route(Enum):
    """Result of the side-effect-free inbound routing decision."""

    EXCLUDED = "excluded"
    OUT_OF_SCOPE = "out_of_scope"
    APPOINTMENT = "appointment"


class FlowState(str, Enum):
    """Persisted states in the deterministic in-person scheduling flow."""

    AWAITING_APPOINTMENT_ACTION = "AWAITING_APPOINTMENT_ACTION"
    AWAITING_SERVICE = "AWAITING_SERVICE"
    AWAITING_SLOT = "AWAITING_SLOT"
    AWAITING_CPF = "AWAITING_CPF"
    AWAITING_BIRTH_DATE = "AWAITING_BIRTH_DATE"
    AWAITING_PHONE_CONFIRMATION = "AWAITING_PHONE_CONFIRMATION"
    AWAITING_APPOINTMENT_SELECTION = "AWAITING_APPOINTMENT_SELECTION"
    AWAITING_CANCEL_AUTHORIZATION = "AWAITING_CANCEL_AUTHORIZATION"
    AWAITING_RESCHEDULE_SLOT = "AWAITING_RESCHEDULE_SLOT"
    AWAITING_RESCHEDULE_AUTHORIZATION = "AWAITING_RESCHEDULE_AUTHORIZATION"
    AWAITING_NEW_PATIENT_NAME = "AWAITING_NEW_PATIENT_NAME"
    AWAITING_NEW_PATIENT_SEX = "AWAITING_NEW_PATIENT_SEX"
    AWAITING_NEW_PATIENT_EMAIL = "AWAITING_NEW_PATIENT_EMAIL"
    AWAITING_AUTHORIZATION = "AWAITING_AUTHORIZATION"
    AWAITING_EDIT_FIELD = "AWAITING_EDIT_FIELD"
    AWAITING_EDIT_VALUE = "AWAITING_EDIT_VALUE"
    AWAITING_EDIT_AUTHORIZATION = "AWAITING_EDIT_AUTHORIZATION"
    AWAITING_RETURN_MODALITY = "AWAITING_RETURN_MODALITY"
    AWAITING_RETURN_SLOT = "AWAITING_RETURN_SLOT"
    RESERVA_CRIADA_STATUS_1 = "RESERVA_CRIADA_STATUS_1"
    AGUARDANDO_COMPROVANTE = "AGUARDANDO_COMPROVANTE"
    COMPROVANTE_RECEBIDO = "COMPROVANTE_RECEBIDO"
    AGUARDANDO_VALIDACAO = "AGUARDANDO_VALIDACAO"
    PAGAMENTO_VALIDADO = "PAGAMENTO_VALIDADO"
    PENDENCIA_NO_COMPROVANTE = "PENDENCIA_NO_COMPROVANTE"
    EXPIRACAO_INICIADA = "EXPIRACAO_INICIADA"
    CANCELAMENTO_FEEGOW_PENDENTE = "CANCELAMENTO_FEEGOW_PENDENTE"
    RESERVA_CANCELADA = "RESERVA_CANCELADA"
    CONFIRMADO_STATUS_7 = "CONFIRMADO_STATUS_7"
    EXCECAO_RECEPCAO = "EXCECAO_RECEPCAO"
    COMPLETED = "COMPLETED"
    HANDOFF = "HANDOFF"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True)
class FlowSnapshot:
    state: str
    data: dict[str, Any]
    updated_at: str | None = None


_REQUIRED_TABLES = frozenset(
    {
        "inbox_events",
        "contacts",
        "flow_states",
        "reservations",
        "operations",
        "payment_proofs",
        "reminders",
        "outbox_events",
        "audit_log",
        "watcher_leases",
        "authorizations",
        "returns_ledger",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_events (
    message_id TEXT PRIMARY KEY,
    chat_key TEXT NOT NULL,
    received_at TEXT NOT NULL,
    handled_at TEXT NOT NULL,
    response TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    chat_key TEXT PRIMARY KEY,
    external_id TEXT,
    is_quarantined INTEGER NOT NULL DEFAULT 0,
    quarantined_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flow_states (
    chat_key TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS reservations (
    appointment_id TEXT PRIMARY KEY,
    chat_key TEXT NOT NULL,
    state TEXT NOT NULL,
    deadline_at TEXT,
    reminder_at TEXT,
    proof_received_at TEXT,
    version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    target_id TEXT,
    payload_hash TEXT,
    state TEXT NOT NULL,
    remote_id TEXT,
    error_class TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_proofs (
    message_id TEXT PRIMARY KEY,
    appointment_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    private_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    appointment_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS outbox_events (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    chat_key TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    owner TEXT,
    claim_token TEXT,
    lease_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT
);

CREATE TABLE IF NOT EXISTS returns_ledger (
    base_appointment_id TEXT PRIMARY KEY,
    chat_key TEXT NOT NULL,
    base_date TEXT NOT NULL,
    state TEXT NOT NULL,
    modality TEXT,
    return_appointment_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    operation_id TEXT,
    event TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watcher_leases (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS authorizations (
    id TEXT PRIMARY KEY,
    chat_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_id TEXT,
    payload_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);
"""

_RECONCILIATION_SUBJECT = {
    "CREATE": "do agendamento",
    "CANCEL": "do cancelamento",
    "RESCHEDULE": "da remarcação",
    "EDIT": "da atualização de cadastro",
}

_INITIAL_STATE = FlowState.AWAITING_APPOINTMENT_ACTION.value
_INITIAL_MENU = (
    "Olá, sou a assistente do Dr. Victor Almeida. "
    "Como posso ajudar com o seu agendamento?\n"
    "1 - Agendar uma consulta\n"
    "2 - Consultar ou remarcar um agendamento\n"
    "3 - Desmarcar uma consulta\n"
    "4 - Verificar retorno gratuito do pacote\n"
    "5 - Atualizar telefone ou e-mail cadastrado"
)
_SERVICE_MENU = (
    "Escolha o serviço:\n"
    "1 - Consulta presencial\n"
    "2 - Consulta presencial + 1 retorno\n"
    "3 - Teleconsulta\n"
    "Para saber os valores, pergunte \"quanto custa\"."
)

_SERVICES: dict[int, dict[str, Any]] = {
    1: {"procedure_id": 1, "price": 600, "label": "Consulta presencial"},
    2: {
        "procedure_id": 9,
        "price": 800,
        "label": "Consulta presencial + 1 retorno",
    },
    3: {"procedure_id": 3, "price": 300, "label": "Teleconsulta"},
}

_PRICE_LIST_TEXT = (
    "Os valores são:\n"
    "Consulta presencial — R$ 600\n"
    "Consulta presencial + 1 retorno — R$ 800\n"
    "Teleconsulta — R$ 300"
)

_PRICE_QUESTION_RE = re.compile(
    r"\b(?:quanto\s+(?:custa|(?:e|é)|fica|sai)|qual\s+(?:e|é)\s+o\s+valor|"
    r"quais\s+(?:sao|são)\s+os\s+valores|valor\s+da\s+consulta|"
    r"pre[cç]os?)\b"
)

_INSTITUTIONAL_MARKERS = (
    "clinica parceira",
    "contato institucional",
    "empresa",
    "fornecedor",
    "institucional",
    "laboratorio",
    "parceiro",
    "plataforma",
    "rapidoc",
    "somos da clinica",
    "somos do laboratorio",
)

_APPOINTMENT_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(?:quero|gostaria|preciso|desejo)\s+(?:de\s+)?(?:agendar|marcar|remarcar|reagendar|desmarcar)\b",
        r"\b(?:agendar|marcar|remarcar|reagendar|desmarcar)\s+(?:(?:uma|a|minha|meu)\s+)?(?:consulta|agendamento|horario)\b",
        r"\bcancelar\s+(?:(?:uma|a|o|minha|meu)\s+)?(?:consulta|agendamento|horario)\b",
        r"\b(?:consultar|ver|confirmar|verificar)\s+(?:(?:a|o|minha|meu)\s+)?(?:consulta|agendamento|retorno)\b",
        r"\btenho\s+(?:uma\s+)?consulta\s+(?:agendada|marcada)\b",
        r"\bverificar\s+(?:o\s+|meu\s+)?retorno\b",
        r"\bretorno\s+(?:gratuito|do\s+pacote|inclus[oa])\b",
        r"\b(?:editar|atualizar|alterar)\s+(?:o\s+|meu\s+|minha\s+)?cadastro\b",
        r"\b(?:atualizar|alterar)\s+(?:meu\s+|minha\s+)?(?:telefone|celular|e-?mail)\b",
    )
)


class AppointmentStore:
    """SQLite state, inbox deduplication, authorization, and operation ledger."""

    def __init__(self, db_path: str | os.PathLike[str]):
        self._path = Path(db_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self._path.parent, 0o700)
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(descriptor)
        os.chmod(self._path, 0o600)
        with self._connect() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise sqlite3.OperationalError("could not enable SQLite WAL mode")
            connection.executescript(_SCHEMA)
            self._migrate_additive_columns(connection)
        os.chmod(self._path, 0o600)

    @staticmethod
    def _migrate_additive_columns(connection: sqlite3.Connection) -> None:
        """Add columns to a pre-existing table from an older release.

        ``CREATE TABLE IF NOT EXISTS`` only creates missing tables, so a
        database already provisioned under the prior ``outbox_events``
        schema needs an explicit, idempotent ``ALTER TABLE`` for the new
        claim-lease/retry columns.
        """

        existing = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(outbox_events)")
        }
        for column, ddl in (
            ("owner", "ALTER TABLE outbox_events ADD COLUMN owner TEXT"),
            (
                "claim_token",
                "ALTER TABLE outbox_events ADD COLUMN claim_token TEXT",
            ),
            (
                "lease_expires_at",
                "ALTER TABLE outbox_events ADD COLUMN lease_expires_at TEXT",
            ),
            (
                "attempts",
                "ALTER TABLE outbox_events ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "next_attempt_at",
                "ALTER TABLE outbox_events ADD COLUMN next_attempt_at TEXT",
            ),
        ):
            if column not in existing:
                connection.execute(ddl)

        contact_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(contacts)")
        }
        for column, ddl in (
            (
                "is_quarantined",
                "ALTER TABLE contacts ADD COLUMN is_quarantined INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "quarantined_at",
                "ALTER TABLE contacts ADD COLUMN quarantined_at TEXT",
            ),
        ):
            if column not in contact_columns:
                connection.execute(ddl)

    def inbox_response(self, message_id: str) -> str | None:
        """Return the original response for an already handled delivery."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT response FROM inbox_events WHERE message_id = ?", (message_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    def load_flow(self, chat_key: str) -> FlowSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state, data_json, updated_at FROM flow_states WHERE chat_key = ?",
                (chat_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(str(row[1]))
        except (TypeError, ValueError):
            data = {}
        return FlowSnapshot(
            str(row[0]), data if isinstance(data, dict) else {}, str(row[2])
        )

    def purge_flow(self, chat_key: str) -> None:
        """Silently drop one expired/abandoned flow so it restarts clean."""

        with self._connect() as connection:
            connection.execute("DELETE FROM flow_states WHERE chat_key = ?", (chat_key,))

    def quarantine_contact(self, chat_key: str, *, now: datetime) -> None:
        """Atomically suppress all local automation state for an excluded contact.

        This deliberately performs no Feegow mutation. Claimed and pending outbox
        rows are deleted so a stale institutional flow cannot emit WhatsApp
        messages after the deterministic handler yields to the legacy pipeline.
        """

        timestamp = now.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            proof_rows = connection.execute(
                """
                SELECT p.private_path
                  FROM payment_proofs p
                  JOIN reservations r ON r.appointment_id = p.appointment_id
                 WHERE r.chat_key = ?
                """,
                (chat_key,),
            ).fetchall()
            connection.execute(
                """
                INSERT INTO contacts
                    (chat_key, external_id, is_quarantined, quarantined_at, created_at, updated_at)
                VALUES (?, NULL, 1, ?, ?, ?)
                ON CONFLICT(chat_key) DO UPDATE SET
                    is_quarantined = 1,
                    quarantined_at = excluded.quarantined_at,
                    updated_at = excluded.updated_at
                """,
                (chat_key, timestamp, timestamp, timestamp),
            )
            connection.execute("DELETE FROM flow_states WHERE chat_key = ?", (chat_key,))
            connection.execute("DELETE FROM inbox_events WHERE chat_key = ?", (chat_key,))
            connection.execute(
                "UPDATE authorizations SET consumed_at = COALESCE(consumed_at, ?) WHERE chat_key = ?",
                (timestamp, chat_key),
            )
            connection.execute(
                """
                UPDATE reminders
                   SET state = 'QUARANTINED'
                 WHERE appointment_id IN (
                    SELECT appointment_id FROM reservations WHERE chat_key = ?
                 ) AND state != 'SENT'
                """,
                (chat_key,),
            )
            connection.execute(
                """
                DELETE FROM payment_proofs
                 WHERE appointment_id IN (
                    SELECT appointment_id FROM reservations WHERE chat_key = ?
                 )
                """,
                (chat_key,),
            )
            connection.execute(
                "UPDATE reservations SET state = 'QUARANTINED', version = version + 1 WHERE chat_key = ?",
                (chat_key,),
            )
            connection.execute(
                "UPDATE returns_ledger SET state = 'QUARANTINED', updated_at = ? WHERE chat_key = ?",
                (timestamp, chat_key),
            )
            connection.execute("DELETE FROM outbox_events WHERE chat_key = ?", (chat_key,))
        for (private_path,) in proof_rows:
            try:
                Path(str(private_path)).unlink(missing_ok=True)
            except OSError:
                logger.warning("appointment quarantine could not remove proof file")

    def is_contact_quarantined(self, chat_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT is_quarantined FROM contacts WHERE chat_key = ?", (chat_key,)
            ).fetchone()
        return bool(row and int(row[0]) == 1)

    def purge_inactive_flows(self, *, now: datetime, retention_days: int) -> int:
        """Delete flow state untouched for ``retention_days``; no PII lives here."""

        cutoff = (now - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM flow_states WHERE updated_at < ?", (cutoff,)
            )
        return cursor.rowcount

    def purge_expired_proofs(self, *, now: datetime, retention_days: int) -> int:
        """Delete proof rows/files past retention, except EXCECAO_RECEPCAO holds."""

        cutoff = (now - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT p.message_id, p.private_path
                  FROM payment_proofs p
                  LEFT JOIN reservations r ON r.appointment_id = p.appointment_id
                 WHERE p.received_at < ?
                   AND COALESCE(r.state, '') != ?
                """,
                (cutoff, FlowState.EXCECAO_RECEPCAO.value),
            ).fetchall()
            for message_id, _private_path in rows:
                connection.execute(
                    "DELETE FROM payment_proofs WHERE message_id = ?", (message_id,)
                )
        for _message_id, private_path in rows:
            try:
                Path(str(private_path)).unlink(missing_ok=True)
            except OSError:
                logger.warning("appointment proof purge could not remove file")
        return len(rows)

    def record_response(
        self,
        message_id: str,
        chat_key: str,
        response: str,
        state: str,
        data: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> str:
        """Atomically persist one inbox result and its resulting flow state."""

        timestamp = (now or datetime.now(timezone.utc)).isoformat()
        serialized = json.dumps(dict(data), ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT response FROM inbox_events WHERE message_id = ?", (message_id,)
            ).fetchone()
            if existing is not None:
                return str(existing[0])
            connection.execute(
                """
                INSERT INTO inbox_events
                    (message_id, chat_key, received_at, handled_at, response)
                VALUES (?, ?, ?, ?, ?)
                """,
                (message_id, chat_key, timestamp, timestamp, response),
            )
            connection.execute(
                """
                INSERT INTO flow_states
                    (chat_key, state, data_json, updated_at, expires_at)
                VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT(chat_key) DO UPDATE SET
                    state = excluded.state,
                    data_json = excluded.data_json,
                    updated_at = excluded.updated_at,
                    expires_at = NULL
                """,
                (chat_key, state, serialized, timestamp),
            )
        return response

    def _accept_inbox_event(self, message_id: str, chat_key: str, response: str) -> str:
        """Compatibility wrapper for the original minimal-state slice."""

        return self.record_response(message_id, chat_key, response, _INITIAL_STATE, {})

    def create_authorization(
        self,
        chat_key: str,
        *,
        kind: str,
        target_id: str,
        payload_hash: str,
        expires_at: datetime,
        now: datetime,
    ) -> str:
        """Create (or reuse) a deterministic authorization for one payload."""

        auth_id = _opaque_id("authorization", chat_key, kind, target_id, payload_hash)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO authorizations
                    (id, chat_key, kind, target_id, payload_hash, expires_at,
                     consumed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    auth_id,
                    chat_key,
                    kind,
                    target_id,
                    payload_hash,
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            )
        return auth_id

    def authorization_is_valid(
        self,
        authorization_id: str,
        chat_key: str,
        payload_hash: str,
        *,
        now: datetime,
    ) -> bool:
        """Check an authorization without consuming it.

        Expiry, a payload change, a foreign chat and an already-consumed
        row must all block the mutation *before* it runs, while the actual
        consumption has to wait until the write plus its readback (or a
        later reconciliation) succeeded.
        """

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM authorizations
                 WHERE id = ? AND chat_key = ? AND payload_hash = ?
                   AND consumed_at IS NULL AND expires_at >= ?
                """,
                (authorization_id, chat_key, payload_hash, now.isoformat()),
            ).fetchone()
        return row is not None

    def consume_authorization(
        self,
        authorization_id: str,
        chat_key: str,
        payload_hash: str,
        *,
        now: datetime,
    ) -> bool:
        """Consume an unexpired, matching authorization exactly once."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE authorizations
                   SET consumed_at = ?
                 WHERE id = ? AND chat_key = ? AND payload_hash = ?
                   AND consumed_at IS NULL AND expires_at >= ?
                """,
                (
                    now.isoformat(),
                    authorization_id,
                    chat_key,
                    payload_hash,
                    now.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def invalidate_authorization(
        self, authorization_id: str, *, now: datetime
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE authorizations SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now.isoformat(), authorization_id),
            )
        return cursor.rowcount == 1

    def get_operation(self, idempotency_key: str) -> dict[str, Any] | None:
        """Read one operation without claiming or creating it."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, state, remote_id, error_class, kind, target_id, payload_hash
                  FROM operations WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "state": str(row[1]),
            "remote_id": row[2],
            "error_class": row[3],
            "kind": str(row[4]),
            "target_id": row[5],
            "payload_hash": row[6],
        }

    def begin_operation(
        self,
        *,
        idempotency_key: str,
        kind: str,
        target_id: str,
        payload_hash: str,
        now: datetime,
    ) -> tuple[bool, dict[str, Any]]:
        """Claim an operation; an existing ledger row is never claimed twice."""

        operation_id = _opaque_id("operation", idempotency_key)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id, state, remote_id, error_class FROM operations
                 WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return False, {
                    "id": str(existing[0]),
                    "state": str(existing[1]),
                    "remote_id": existing[2],
                    "error_class": existing[3],
                }
            connection.execute(
                """
                INSERT INTO operations
                    (id, idempotency_key, kind, target_id, payload_hash, state,
                     remote_id, error_class, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'PENDING', NULL, NULL, ?, ?)
                """,
                (
                    operation_id,
                    idempotency_key,
                    kind,
                    target_id,
                    payload_hash,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return True, {"id": operation_id, "state": "PENDING", "remote_id": None}

    def finish_operation(
        self,
        operation_id: str,
        *,
        state: str,
        now: datetime,
        remote_id: str | int | None = None,
        error_class: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE operations
                   SET state = ?, remote_id = ?, error_class = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    state,
                    None if remote_id is None else str(remote_id),
                    error_class,
                    now.isoformat(),
                    operation_id,
                ),
            )

    def audit(
        self,
        event: str,
        *,
        operation_id: str | None,
        now: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Append allow-listed operational metadata only; never accept PII keys."""

        allowed_keys = {"kind", "procedure_id", "state", "status_id"}
        safe_metadata = {
            key: value
            for key, value in (metadata or {}).items()
            if key in allowed_keys and isinstance(value, (str, int, float, bool, type(None)))
        }
        audit_id = _opaque_id(
            "audit",
            operation_id or "none",
            event,
            now.isoformat(),
            json.dumps(safe_metadata, sort_keys=True),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO audit_log
                    (id, operation_id, event, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    operation_id,
                    event,
                    json.dumps(safe_metadata, separators=(",", ":"), sort_keys=True),
                    now.isoformat(),
                ),
            )

    def create_reservation(
        self,
        appointment_id: int | str,
        chat_key: str,
        *,
        deadline_at: datetime,
        reminder_at: datetime,
    ) -> None:
        """Persist one immutable teleconsultation reservation after status-1 readback."""

        identifier = str(appointment_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO reservations
                    (appointment_id, chat_key, state, deadline_at, reminder_at,
                     proof_received_at, version)
                VALUES (?, ?, 'AGUARDANDO_COMPROVANTE', ?, ?, NULL, 0)
                ON CONFLICT(appointment_id) DO NOTHING
                """,
                (identifier, chat_key, deadline_at.isoformat(), reminder_at.isoformat()),
            )
            row = connection.execute(
                """
                SELECT chat_key, deadline_at, reminder_at
                  FROM reservations WHERE appointment_id = ?
                """,
                (identifier,),
            ).fetchone()
            if row is None or tuple(map(str, row)) != (
                chat_key,
                deadline_at.isoformat(),
                reminder_at.isoformat(),
            ):
                raise RuntimeError("reservation identity mismatch")

    def active_reservation(self, chat_key: str) -> dict[str, Any] | None:
        """Return the newest payment reservation linked to this exact chat."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT appointment_id, chat_key, state, deadline_at, reminder_at,
                       proof_received_at, version
                  FROM reservations
                 WHERE chat_key = ?
                   AND state NOT IN ('RESERVA_CANCELADA', 'CONFIRMADO_STATUS_7')
                 ORDER BY deadline_at DESC, appointment_id DESC
                 LIMIT 1
                """,
                (chat_key,),
            ).fetchone()
        if row is None:
            return None
        keys = (
            "appointment_id",
            "chat_key",
            "state",
            "deadline_at",
            "reminder_at",
            "proof_received_at",
            "version",
        )
        return dict(zip(keys, row))

    def accept_payment_proof(
        self,
        *,
        message_id: str,
        appointment_id: int | str,
        chat_key: str,
        received_at: datetime,
        sha256: str,
        private_path: str,
    ) -> str:
        """Atomically let an on-time proof defeat an uncompleted expiration."""

        identifier = str(appointment_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reservation = connection.execute(
                """
                SELECT state, deadline_at, proof_received_at
                  FROM reservations
                 WHERE appointment_id = ? AND chat_key = ?
                """,
                (identifier, chat_key),
            ).fetchone()
            if reservation is None:
                return "missing"
            state, deadline_text, proof_received = map(
                lambda item: None if item is None else str(item), reservation
            )
            deadline = datetime.fromisoformat(str(deadline_text))
            if received_at > deadline:
                return "late"
            if state in {"RESERVA_CANCELADA", "CONFIRMADO_STATUS_7"}:
                return "closed"

            existing = connection.execute(
                """
                SELECT message_id, appointment_id FROM payment_proofs
                 WHERE message_id = ? OR sha256 = ?
                """,
                (message_id, sha256),
            ).fetchone()
            if existing is not None:
                return "duplicate" if str(existing[1]) == identifier else "conflict"
            if proof_received is not None:
                return "duplicate"

            connection.execute(
                """
                INSERT INTO payment_proofs
                    (message_id, appointment_id, received_at, sha256, private_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    identifier,
                    received_at.isoformat(),
                    sha256,
                    private_path,
                ),
            )
            updated = connection.execute(
                """
                UPDATE reservations
                   SET state = 'COMPROVANTE_RECEBIDO', proof_received_at = ?,
                       version = version + 1
                 WHERE appointment_id = ? AND chat_key = ?
                   AND proof_received_at IS NULL
                   AND state NOT IN ('RESERVA_CANCELADA', 'CONFIRMADO_STATUS_7')
                """,
                (received_at.isoformat(), identifier, chat_key),
            )
            if updated.rowcount != 1:
                raise RuntimeError("payment proof lost reservation race")
        return "accepted"

    def acquire_watcher_lease(
        self, name: str, owner: str, *, now: datetime, lease_seconds: int
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner, expires_at FROM watcher_leases WHERE name = ?", (name,)
            ).fetchone()
            if row is not None and datetime.fromisoformat(str(row[1])) > now:
                return False
            connection.execute(
                """
                INSERT INTO watcher_leases (name, owner, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    owner = excluded.owner,
                    expires_at = excluded.expires_at
                """,
                (name, owner, (now + timedelta(seconds=lease_seconds)).isoformat()),
            )
        return True

    def release_watcher_lease(self, name: str, owner: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM watcher_leases WHERE name = ? AND owner = ?", (name, owner)
            )

    def due_reservations(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT appointment_id, chat_key, state, deadline_at, reminder_at,
                       proof_received_at, version
                  FROM reservations
                 WHERE proof_received_at IS NULL
                   AND state IN (
                       'AGUARDANDO_COMPROVANTE', 'EXPIRACAO_INICIADA',
                       'CANCELAMENTO_FEEGOW_PENDENTE'
                   )
                 ORDER BY deadline_at, appointment_id
                """
            ).fetchall()
        keys = (
            "appointment_id",
            "chat_key",
            "state",
            "deadline_at",
            "reminder_at",
            "proof_received_at",
            "version",
        )
        return [dict(zip(keys, row)) for row in rows]

    def enqueue_payment_reminder(
        self, appointment_id: int | str, chat_key: str, *, now: datetime
    ) -> dict[str, str] | None:
        identifier = str(appointment_id)
        reminder_id = _opaque_id("payment-reminder", identifier)
        outbox_id = _opaque_id("outbox", "payment-reminder", identifier)
        body = (
            "Lembrete: envie a imagem ou o PDF do comprovante antes do vencimento "
            "da reserva da teleconsulta."
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reservation = connection.execute(
                """
                SELECT deadline_at, reminder_at, state, proof_received_at
                  FROM reservations WHERE appointment_id = ? AND chat_key = ?
                """,
                (identifier, chat_key),
            ).fetchone()
            if reservation is None:
                return None
            deadline = datetime.fromisoformat(str(reservation[0]))
            reminder_at = datetime.fromisoformat(str(reservation[1]))
            if (
                now < reminder_at
                or now >= deadline
                or reservation[3] is not None
                or str(reservation[2]) != "AGUARDANDO_COMPROVANTE"
            ):
                return None
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO reminders
                    (id, appointment_id, due_at, state, created_at, sent_at)
                VALUES (?, ?, ?, 'OUTBOXED', ?, NULL)
                """,
                (reminder_id, identifier, reminder_at.isoformat(), now.isoformat()),
            )
            if inserted.rowcount != 1:
                return None
            connection.execute(
                """
                INSERT INTO outbox_events
                    (id, idempotency_key, chat_key, body, state, created_at, sent_at)
                VALUES (?, ?, ?, ?, 'PENDING', ?, NULL)
                """,
                (outbox_id, reminder_id, chat_key, body, now.isoformat()),
            )
        return {"id": outbox_id, "chat_key": chat_key, "body": body}

    def enqueue_reception_receipt(
        self,
        appointment_id: int | str,
        reception_chat_id: str,
        *,
        now: datetime,
    ) -> dict[str, str] | None:
        """Notify reception a proof arrived, by opaque appointment id only.

        The body carries no patient name/CPF/phone/email and no proof file
        path — only the numeric Feegow appointment id, which reception can
        already look up in Feegow itself.
        """

        identifier = str(appointment_id)
        idempotency_key = _opaque_id("reception-receipt", identifier)
        outbox_id = _opaque_id("outbox", idempotency_key)
        body = (
            f"Comprovante recebido para o agendamento {identifier}. "
            "Validar o pagamento na Feegow."
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO outbox_events
                    (id, idempotency_key, chat_key, body, state, created_at, sent_at)
                VALUES (?, ?, ?, ?, 'PENDING', ?, NULL)
                """,
                (outbox_id, idempotency_key, reception_chat_id, body, now.isoformat()),
            )
        return {"id": outbox_id, "chat_key": reception_chat_id, "body": body}

    def claim_expiration(
        self,
        appointment_id: int | str,
        *,
        now: datetime,
        grace_seconds: int,
    ) -> bool:
        identifier = str(appointment_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT deadline_at, state, proof_received_at
                  FROM reservations WHERE appointment_id = ?
                """,
                (identifier,),
            ).fetchone()
            if row is None or row[2] is not None:
                return False
            if str(row[1]) not in {
                "AGUARDANDO_COMPROVANTE",
                "EXPIRACAO_INICIADA",
                "CANCELAMENTO_FEEGOW_PENDENTE",
            }:
                return False
            deadline = datetime.fromisoformat(str(row[0]))
            if now < deadline + timedelta(seconds=grace_seconds):
                return False
            updated = connection.execute(
                """
                UPDATE reservations
                   SET state = 'EXPIRACAO_INICIADA', version = version + 1
                 WHERE appointment_id = ? AND proof_received_at IS NULL
                   AND state IN (
                       'AGUARDANDO_COMPROVANTE', 'EXPIRACAO_INICIADA',
                       'CANCELAMENTO_FEEGOW_PENDENTE'
                   )
                """,
                (identifier,),
            )
            return updated.rowcount == 1

    def reservation_has_proof(self, appointment_id: int | str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT proof_received_at FROM reservations WHERE appointment_id = ?",
                (str(appointment_id),),
            ).fetchone()
        return row is None or row[0] is not None

    def set_reservation_state(
        self, appointment_id: int | str, state: str, *, require_no_proof: bool = True
    ) -> bool:
        suffix = " AND proof_received_at IS NULL" if require_no_proof else ""
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE reservations SET state = ?, version = version + 1
                 WHERE appointment_id = ?{suffix}
                """,
                (state, str(appointment_id)),
            )
        return cursor.rowcount == 1

    def finalize_expiration_cancellation(
        self, appointment_id: int | str, chat_key: str, *, now: datetime
    ) -> dict[str, str] | None:
        identifier = str(appointment_id)
        outbox_key = _opaque_id("expired-reservation-cancelled", identifier)
        outbox_id = _opaque_id("outbox", outbox_key)
        # Wording is deliberately limited to "no longer reserved" — it must
        # never imply that a slot is, or will be, available again.
        body = (
            f"A reserva da teleconsulta {identifier} não está mais reservada: "
            "o comprovante não foi recebido dentro do prazo."
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE reservations
                   SET state = 'RESERVA_CANCELADA', version = version + 1
                 WHERE appointment_id = ? AND chat_key = ?
                   AND proof_received_at IS NULL
                   AND state != 'CONFIRMADO_STATUS_7'
                """,
                (identifier, chat_key),
            )
            if updated.rowcount != 1:
                return None
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO outbox_events
                    (id, idempotency_key, chat_key, body, state, created_at, sent_at)
                VALUES (?, ?, ?, ?, 'PENDING', ?, NULL)
                """,
                (outbox_id, outbox_key, chat_key, body, now.isoformat()),
            )
            if inserted.rowcount != 1:
                return None
        return {"id": outbox_id, "chat_key": chat_key, "body": body}

    def claim_outbox_batch(
        self,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int,
        limit: int = 10,
    ) -> list[dict[str, str]]:
        """Transactionally claim due outbox rows; two owners never overlap.

        A row is eligible when it is ``PENDING`` (or ``CLAIMED`` under an
        expired lease from a crashed worker) and its retry backoff, if any,
        has elapsed. The select and the claiming update run inside a single
        ``BEGIN IMMEDIATE`` transaction, so a concurrent claimant blocks
        until this one commits instead of racing it.
        """

        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        claimed: list[dict[str, str]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id, chat_key, body FROM outbox_events
                 WHERE (next_attempt_at IS NULL OR next_attempt_at <= ?)
                   AND (
                       state = 'PENDING'
                       OR (state = 'CLAIMED' AND (lease_expires_at IS NULL OR lease_expires_at < ?))
                   )
                 ORDER BY created_at, id
                 LIMIT ?
                """,
                (now_text, now_text, int(limit)),
            ).fetchall()
            for outbox_id, chat_key, body in rows:
                claim_token = os.urandom(16).hex()
                cursor = connection.execute(
                    """
                    UPDATE outbox_events
                       SET state = 'CLAIMED', owner = ?, claim_token = ?,
                           lease_expires_at = ?
                     WHERE id = ?
                       AND (
                           state = 'PENDING'
                           OR (state = 'CLAIMED' AND (lease_expires_at IS NULL OR lease_expires_at < ?))
                       )
                    """,
                    (str(owner), claim_token, lease_until, outbox_id, now_text),
                )
                if cursor.rowcount == 1:
                    claimed.append(
                        {
                            "id": str(outbox_id),
                            "chat_key": str(chat_key),
                            "body": str(body),
                            "claim_token": claim_token,
                        }
                    )
        return claimed

    def ack_outbox_sent(
        self,
        outbox_id: str,
        *,
        owner: str,
        claim_token: str,
        now: datetime,
    ) -> bool:
        """Acknowledge only the exact claim generation that performed the send."""

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_events
                   SET state = 'SENT', sent_at = ?, owner = NULL,
                       claim_token = NULL, lease_expires_at = NULL
                 WHERE id = ? AND state = 'CLAIMED' AND owner = ?
                   AND claim_token = ?
                """,
                (
                    now.isoformat(),
                    str(outbox_id),
                    str(owner),
                    str(claim_token),
                ),
            )
        return cursor.rowcount == 1

    def release_outbox_failure(
        self,
        outbox_id: str,
        *,
        owner: str,
        claim_token: str,
        now: datetime,
        backoff_seconds: int,
    ) -> bool:
        """Release only the exact claim generation that attempted the send."""

        next_attempt = (now + timedelta(seconds=max(0, backoff_seconds))).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_events
                   SET state = 'PENDING', owner = NULL, claim_token = NULL,
                       lease_expires_at = NULL,
                       attempts = attempts + 1, next_attempt_at = ?
                 WHERE id = ? AND state = 'CLAIMED' AND owner = ?
                   AND claim_token = ?
                """,
                (next_attempt, str(outbox_id), str(owner), str(claim_token)),
            )
        return cursor.rowcount == 1

    def create_return_ledger_entry(
        self,
        base_appointment_id: int | str,
        chat_key: str,
        *,
        base_date: datetime,
        now: datetime,
    ) -> None:
        """Record one automation-created procedure-9 base as return-eligible."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO returns_ledger
                    (base_appointment_id, chat_key, base_date, state, modality,
                     return_appointment_id, created_at, updated_at)
                VALUES (?, ?, ?, 'OPEN', NULL, NULL, ?, ?)
                ON CONFLICT(base_appointment_id) DO NOTHING
                """,
                (
                    str(base_appointment_id),
                    chat_key,
                    base_date.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

    def open_return_for_chat(self, chat_key: str) -> dict[str, Any] | None:
        """Return the newest open return-eligible base linked to this chat."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT base_appointment_id, chat_key, base_date, state
                  FROM returns_ledger
                 WHERE chat_key = ? AND state = 'OPEN'
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (chat_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "base_appointment_id": str(row[0]),
            "chat_key": str(row[1]),
            "base_date": str(row[2]),
            "state": str(row[3]),
        }

    def close_return_ledger(
        self, base_appointment_id: int | str, *, state: str, now: datetime
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE returns_ledger SET state = ?, updated_at = ?
                 WHERE base_appointment_id = ? AND state = 'OPEN'
                """,
                (state, now.isoformat(), str(base_appointment_id)),
            )

    def claim_return_ledger(
        self,
        base_appointment_id: int | str,
        *,
        claim_token: str,
        modality: str,
        now: datetime,
    ) -> bool:
        """Atomically reserve one free-return entitlement before remote write.

        The operation id is the durable claim token. Exact retries of an
        ambiguous mutation may reuse it, while a different slot/operation is
        rejected before it can call Feegow.
        """

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE returns_ledger
                   SET state = 'RESERVED', return_appointment_id = ?, modality = ?,
                       updated_at = ?
                 WHERE base_appointment_id = ? AND state = 'OPEN'
                """,
                (
                    str(claim_token),
                    modality,
                    now.isoformat(),
                    str(base_appointment_id),
                ),
            )
            if cursor.rowcount == 1:
                return True
            row = connection.execute(
                """
                SELECT state, return_appointment_id
                  FROM returns_ledger WHERE base_appointment_id = ?
                """,
                (str(base_appointment_id),),
            ).fetchone()
        if row is None:
            return False
        state, remote_or_claim = str(row[0]), row[1]
        return state == "RESERVED" and str(remote_or_claim) == str(claim_token) or state == "CONSUMED"

    def consume_return_ledger(
        self,
        base_appointment_id: int | str,
        return_appointment_id: int | str,
        *,
        claim_token: str,
        modality: str,
        now: datetime,
    ) -> bool:
        """Consume only the entitlement reserved by this exact operation."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE returns_ledger
                   SET state = 'CONSUMED', return_appointment_id = ?, modality = ?,
                       updated_at = ?
                 WHERE base_appointment_id = ? AND state = 'RESERVED'
                   AND return_appointment_id = ?
                """,
                (
                    str(return_appointment_id),
                    modality,
                    now.isoformat(),
                    str(base_appointment_id),
                    str(claim_token),
                ),
            )
            if cursor.rowcount == 1:
                return True
            row = connection.execute(
                """
                SELECT state, return_appointment_id
                  FROM returns_ledger WHERE base_appointment_id = ?
                """,
                (str(base_appointment_id),),
            ).fetchone()
        return bool(
            row is not None
            and str(row[0]) == "CONSUMED"
            and str(row[1]) == str(return_appointment_id)
        )

    def return_modality_for_appointment(self, return_appointment_id: int | str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT modality FROM returns_ledger
                 WHERE return_appointment_id = ? AND state = 'CONSUMED'
                """,
                (str(return_appointment_id),),
            ).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def reopen_return_after_cancel(
        self, return_appointment_id: int | str, *, now: datetime, within_days: int
    ) -> None:
        """Reopen a cancelled return's base ledger if still inside the window."""

        identifier = str(return_appointment_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT base_appointment_id, base_date FROM returns_ledger
                 WHERE return_appointment_id = ? AND state = 'CONSUMED'
                """,
                (identifier,),
            ).fetchone()
            if row is None:
                return
            base_date = datetime.fromisoformat(str(row[1]))
            new_state = "OPEN" if now <= base_date + timedelta(days=within_days) else "EXPIRED"
            connection.execute(
                """
                UPDATE returns_ledger
                   SET state = ?, return_appointment_id = NULL, modality = NULL,
                       updated_at = ?
                 WHERE base_appointment_id = ? AND return_appointment_id = ?
                """,
                (new_state, now.isoformat(), str(row[0]), identifier),
            )

    def count(self, table_name: str) -> int:
        """Return a row count for a known application table only."""

        if table_name not in _REQUIRED_TABLES:
            raise ValueError(f"unknown appointment table: {table_name!r}")
        with self._connect() as connection:
            row = connection.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
        return int(row[0])


def _normalize(value: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or "")).casefold()
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(without_marks.split())


def _digits(value: Any) -> str:
    return "".join(re.findall(r"\d", str(value or "")))


def _opaque_id(*parts: Any) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_excluded_chat(source: Any) -> bool:
    chat_type = _normalize(getattr(source, "chat_type", ""))
    chat_id = _normalize(getattr(source, "chat_id", ""))
    return (
        chat_type in {"group", "broadcast", "status"}
        or chat_id.endswith("@g.us")
        or chat_id.endswith("@broadcast")
        or chat_id == "status"
    )


def classify_route(incoming: Any) -> Route:
    """Classify an event without touching durable state or external services."""

    source = getattr(incoming, "source", None)
    if _is_excluded_chat(source):
        return Route.EXCLUDED
    text = _normalize(getattr(incoming, "text", ""))
    user_name = _normalize(getattr(source, "user_name", ""))
    institutional_context = f"{user_name} {text}"
    if any(marker in institutional_context for marker in _INSTITUTIONAL_MARKERS):
        return Route.EXCLUDED
    if any(pattern.search(text) for pattern in _APPOINTMENT_PATTERNS):
        return Route.APPOINTMENT
    return Route.OUT_OF_SCOPE


def _event_identity(incoming: Any, chat_key: str) -> str:
    message_id = getattr(incoming, "message_id", None)
    if message_id:
        return str(message_id)
    digest_source = "\x1f".join(
        (
            chat_key,
            str(getattr(incoming, "text", "") or ""),
            repr(tuple(getattr(incoming, "media_urls", ()) or ())),
        )
    )
    return "derived:" + hashlib.sha256(digest_source.encode("utf-8")).hexdigest()


def is_valid_cpf(value: Any) -> bool:
    """Validate a Brazilian CPF, accepting punctuation but rejecting repeats."""

    cpf = _digits(value)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for length in (9, 10):
        total = sum(int(cpf[index]) * (length + 1 - index) for index in range(length))
        digit = (total * 10 % 11) % 10
        if digit != int(cpf[length]):
            return False
    return True


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    iso_candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate).date()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time(value: Any) -> time | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "T" in text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(_BRT)
            return parsed.time().replace(tzinfo=None)
        except ValueError:
            pass
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _collection(result: Any, *, allow_none: bool = False) -> list[Any]:
    if result is None:
        if allow_none:
            return []
        raise ValueError("missing client result")
    if isinstance(result, (list, tuple)):
        return list(result)
    if isinstance(result, Mapping):
        if result.get("error") or result.get("success") is False:
            raise ValueError("client reported an error")
        for key in ("content", "data", "results", "items", "slots"):
            if key not in result:
                continue
            value = result[key]
            if value is None and ("success" in result or "error" in result):
                return []
            if isinstance(value, list):
                return value
            if isinstance(value, Mapping):
                return [dict(value)]
            # A raw resource can legitimately have a scalar field named
            # ``data`` (the appointment date); only collection-shaped values
            # are response envelopes.
        return [dict(result)] if result else []
    raise ValueError("invalid client result")


def _extract_status_id(result: Any) -> int | None:
    """Extract Feegow's status ID from numeric, nested, or textual shapes."""
    if isinstance(result, (list, tuple)):
        if len(result) != 1:
            return None
        return _extract_status_id(result[0])
    if isinstance(result, Mapping):
        for key in ("status_id", "statusId", "status"):
            if key not in result:
                continue
            value = result[key]
            if isinstance(value, Mapping):
                for nested_key in (
                    "id", "status_id", "statusId", "codigo", "value",
                    "nome", "nome_status", "descricao", "description", "label",
                ):
                    if nested_key in value:
                        parsed = _extract_status_id(value[nested_key])
                        if parsed is not None:
                            return parsed
            else:
                parsed = _extract_status_id(value)
                if parsed is not None:
                    return parsed
        return None
    if isinstance(result, bool) or result is None:
        return None
    if isinstance(result, int):
        return result
    text = _normalize(result)
    if text.isdigit():
        return int(text)
    for marker, status_id in (
        ("nao confirmado", 1),
        ("desmarcado", 11),
        ("cancelado", 11),
        ("remarcado", 15),
        ("finalizado", 3),
        ("confirmado", 7),
        ("marcado", 1),
    ):
        if marker in text:
            return status_id
    return None


def _parse_instant(value: Any) -> datetime | None:
    """Parse one aware/naive ISO instant and express it in BRT."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(_BRT)


def _slot_date_and_time(slot: Mapping[str, Any]) -> tuple[date | None, time | None]:
    raw_datetime = next(
        (slot[key] for key in ("datetime", "date_time", "inicio", "start") if slot.get(key)),
        None,
    )
    raw_date = next(
        (slot[key] for key in ("data", "date", "dia") if slot.get(key)), None
    )
    raw_time = next(
        (slot[key] for key in ("horario", "hora", "time") if slot.get(key)), None
    )
    if raw_date is None and raw_time is None and raw_datetime is not None:
        # A single instant must yield a single local moment: converting the
        # time to BRT while keeping the UTC calendar date would report the
        # wrong day whenever the offset crosses midnight.
        instant = _parse_instant(raw_datetime)
        if instant is not None:
            return instant.date(), instant.time().replace(tzinfo=None)
    return (
        _parse_date(raw_date if raw_date is not None else raw_datetime),
        _parse_time(raw_time if raw_time is not None else raw_datetime),
    )


def filter_eligible_slots(
    slots: Any, procedure_id: int, *, limit: int = 3, modality: str | None = None
) -> list[dict[str, Any]]:
    """Return real policy-eligible slots, capped at three.

    In-person procedures 1 and 9 remain restricted to Wed/Thu 14:00–18:00
    BRT. Teleconsultation procedure 3 accepts the real schedule returned by
    Feegow and gives Saturday-morning slots priority without fabricating any.

    ``modality`` ("presencial"/"tele") lets a caller apply the same policy
    to a procedure id outside {1,3,9} — the configurable free-return
    procedure shares one Feegow id across both modalities, so the id alone
    cannot select a policy branch the way it does for 1/3/9.
    """

    procedure_id = int(procedure_id)
    if modality not in (None, "presencial", "tele"):
        return []
    if modality is None:
        if procedure_id not in {1, 3, 9}:
            return []
        modality = "tele" if procedure_id == 3 else "presencial"
    eligible: list[dict[str, Any]] = []
    for raw in _collection(slots, allow_none=True):
        if not isinstance(raw, Mapping):
            continue
        raw_procedure = next(
            (raw.get(key) for key in ("procedimento_id", "procedure_id") if raw.get(key) is not None),
            None,
        )
        if raw_procedure is not None and str(raw_procedure) != str(procedure_id):
            continue
        if any(raw.get(key) is False for key in ("available", "disponivel", "livre")):
            continue
        slot_date, slot_time = _slot_date_and_time(raw)
        if slot_date is None or slot_time is None:
            continue
        if modality == "presencial" and (
            slot_date.weekday() not in {2, 3}
            or slot_time < time(14, 0)
            or slot_time > time(18, 0)
        ):
            continue
        slot_id = str(
            next(
                (raw.get(key) for key in ("id", "slot_id", "agenda_id") if raw.get(key) is not None),
                _opaque_id("slot", procedure_id, slot_date.isoformat(), slot_time.isoformat()),
            )
        )
        eligible.append(
            {
                "id": slot_id,
                "date": slot_date.isoformat(),
                "time": slot_time.strftime("%H:%M"),
                "display_date": slot_date.strftime("%d/%m/%Y"),
            }
        )
    if modality == "tele":
        eligible.sort(
            key=lambda slot: (
                not (
                    date.fromisoformat(slot["date"]).weekday() == 5
                    and time.fromisoformat(slot["time"]) < time(12, 0)
                ),
                slot["date"],
                slot["time"],
                slot["id"],
            )
        )
    else:
        eligible.sort(key=lambda slot: (slot["date"], slot["time"], slot["id"]))
    return eligible[: max(0, min(int(limit), 3))]


def _normalized_phone(value: Any) -> str:
    phone = _digits(value)
    if phone.startswith("55") and len(phone) in {12, 13}:
        phone = phone[2:]
    return phone


def _chat_phone(chat_key: str) -> str:
    return _normalized_phone(chat_key.split("@", 1)[0])


def _patient_phones(patient: Mapping[str, Any]) -> set[str]:
    values: list[Any] = []
    for key in ("telefone", "celular", "phone", "mobile", "telefone_celular"):
        value = patient.get(key)
        if isinstance(value, (list, tuple, set)):
            values.extend(value)
        elif value:
            values.append(value)
    return {phone for phone in (_normalized_phone(value) for value in values) if phone}


def _patient_id(patient: Mapping[str, Any]) -> int | None:
    for key in ("paciente_id", "patient_id", "id"):
        value = patient.get(key)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _has_legacy_or_ambiguous_return(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = _normalize(key).replace(" ", "_")
            if normalized_key in {
                "legacy_return",
                "retorno_legado",
                "ambiguous_return",
                "retorno_ambiguo",
            } and bool(item):
                return True
            if "retorno" in normalized_key and any(
                marker in _normalize(item) for marker in ("legado", "ambiguo", "ambiguous")
            ):
                return True
        return any(_has_legacy_or_ambiguous_return(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_legacy_or_ambiguous_return(item) for item in value)
    return False


def _extract_id(result: Any, keys: Sequence[str]) -> int | None:
    candidates = _collection(result)
    if len(candidates) != 1 or not isinstance(candidates[0], Mapping):
        return None
    item = candidates[0]
    for key in keys:
        if item.get(key) not in (None, ""):
            try:
                return int(item[key])
            except (TypeError, ValueError):
                return None
    return None


def _money(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    text = str(value or "")
    match = re.search(r"\d[\d.]*?(?:,\d{1,2})?(?!\d)", text)
    if not match:
        return None
    normalized = match.group(0).replace(".", "").replace(",", ".")
    try:
        return int(round(float(normalized)))
    except ValueError:
        return None


class WhatsAppAppointmentsHandler:
    """Run the deterministic proc-1/proc-3/proc-9 scheduling state machine."""

    def __init__(
        self,
        config: Mapping[str, Any] | None,
        *,
        db_path: str | os.PathLike[str],
        proofs_dir: str | os.PathLike[str] | None = None,
        feegow_client: Any = None,
        clock: Callable[[], datetime] | None = None,
    ):
        settings = config or {}
        self._enabled = settings.get("enabled", False) is True
        self._write_enabled = settings.get("write_enabled", False) is True
        self._db_path = Path(db_path)
        self._proofs_dir = (
            Path(proofs_dir)
            if proofs_dir is not None
            else self._db_path.parent / "payment-proofs"
        )
        self._feegow_client = feegow_client
        self._clock = clock or (lambda: datetime.now(_BRT))
        self._professional_id = int(settings.get("professional_id", 1))
        self._specialty_id = int(settings.get("specialty_id", 1))
        self._local_id = int(settings.get("local_id", 1))
        self._channel_id = int(settings.get("channel_id", 3))
        self._cancel_reason_id = int(settings.get("cancel_reason_id", 1))
        self._search_days = max(1, int(settings.get("slot_search_days", 15)))
        self._authorization_minutes = max(
            1, int(settings.get("authorization_ttl_minutes", 15))
        )
        try:
            return_procedure_id = int(settings.get("return_procedure_id", 2))
        except (TypeError, ValueError):
            return_procedure_id = 2
        self._return_procedure_id = return_procedure_id
        self._return_enabled = return_procedure_id not in {1, 3, 9}
        raw_no_show_ids = settings.get("return_no_show_status_ids")
        no_show_ids: set[int] = set()
        if isinstance(raw_no_show_ids, (list, tuple, set)):
            for item in raw_no_show_ids:
                try:
                    no_show_ids.add(int(item))
                except (TypeError, ValueError):
                    continue
        self._return_no_show_status_ids = frozenset(no_show_ids)
        self._return_window_days = max(
            1, int(settings.get("return_window_days", 60))
        )
        self._reception_chat_id = str(settings.get("reception_chat_id") or "").strip()
        self._flow_ttl_seconds = max(
            60, int(settings.get("flow_ttl_hours", 24)) * 3600
        )
        self._flow_retention_days = max(
            1, int(settings.get("flow_retention_days", 30))
        )
        self._proof_retention_days = max(
            1, int(settings.get("proof_retention_days", 180))
        )
        self._retention_lease_seconds = max(
            60, int(settings.get("retention_interval_seconds", 86400))
        )
        self._outbox_lease_seconds = max(
            10, int(settings.get("outbox_lease_seconds", 120))
        )
        self._outbox_backoff_seconds = max(
            1, int(settings.get("outbox_backoff_seconds", 60))
        )
        payment = settings.get("payment")
        if not isinstance(payment, Mapping):
            payment = {}
        self._payment_beneficiary = str(payment.get("beneficiary") or "").strip()
        self._payment_instructions = str(payment.get("instructions") or "").strip()
        self._payment_ready = (
            payment.get("enabled") is True
            and bool(self._payment_beneficiary)
            and bool(self._payment_instructions)
        )
        self._payment_grace_seconds = max(
            0, int(payment.get("ingestion_grace_seconds", 60))
        )
        self._watcher_lease_seconds = max(
            1, int(payment.get("watcher_lease_seconds", 30))
        )

    @property
    def watcher_enabled(self) -> bool:
        """Whether background reservation/outbox work is safe to start."""

        return self._enabled and self._write_enabled and self._payment_ready

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=_BRT)
        return current.astimezone(_BRT)

    def _flow_is_expired(self, flow: FlowSnapshot) -> bool:
        if not flow.updated_at:
            return False
        try:
            updated_at = datetime.fromisoformat(flow.updated_at)
        except ValueError:
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=_BRT)
        return (self._now() - updated_at).total_seconds() > self._flow_ttl_seconds

    def handle(self, incoming: Any) -> str | None:
        """Handle an explicit intent or active flow; ``None`` preserves old routing."""

        route = classify_route(incoming)
        if not self._enabled:
            return None
        source = getattr(incoming, "source", None)
        chat_key = str(getattr(source, "chat_id", "") or "")
        if not chat_key:
            return None
        if route is Route.EXCLUDED:
            # Do not create state for a never-seen institutional contact, but
            # quarantine any state left by an earlier contaminated flow.
            if self._db_path.exists():
                AppointmentStore(self._db_path).quarantine_contact(
                    chat_key, now=self._now()
                )
            return None

        if route is not Route.APPOINTMENT and not self._db_path.exists():
            return None
        store = AppointmentStore(self._db_path)
        flow = store.load_flow(chat_key)
        if flow is not None and self._flow_is_expired(flow):
            # Precedence: an expired state is dropped silently. It only
            # resumes if this same message also carries an unambiguous new
            # appointment intent (handled below via the ``flow is None``
            # fresh-menu branch); a stray follow-up gets no reply at all.
            store.purge_flow(chat_key)
            flow = None
        if route is not Route.APPOINTMENT and flow is None:
            return None

        message_id = _event_identity(incoming, chat_key)
        prior = store.inbox_response(message_id)
        if prior is not None:
            return prior

        if flow is None:
            return store.record_response(
                message_id,
                chat_key,
                _INITIAL_MENU,
                FlowState.AWAITING_APPOINTMENT_ACTION.value,
                {},
                now=self._now(),
            )

        media_urls = tuple(getattr(incoming, "media_urls", ()) or ())
        if (
            media_urls
            and self._payment_ready
            and store.active_reservation(chat_key) is not None
        ):
            try:
                return self._handle_payment_proof(
                    store, flow, message_id, chat_key, incoming
                )
            except Exception:
                return self._handoff(store, message_id, chat_key)

        text = str(getattr(incoming, "text", "") or "").strip()
        try:
            return self._advance(store, flow, message_id, chat_key, text)
        except Exception:
            return self._handoff(store, message_id, chat_key)

    def _respond(
        self,
        store: AppointmentStore,
        message_id: str,
        chat_key: str,
        response: str,
        state: FlowState,
        data: Mapping[str, Any],
    ) -> str:
        return store.record_response(
            message_id,
            chat_key,
            response,
            state.value,
            data,
            now=self._now(),
        )

    def _handoff(
        self, store: AppointmentStore, message_id: str, chat_key: str
    ) -> str:
        return self._respond(
            store, message_id, chat_key, _RECEPTION, FlowState.HANDOFF, {}
        )

    def _enter_reconciliation(
        self,
        store: AppointmentStore,
        message_id: str,
        chat_key: str,
        data: dict[str, Any],
        kind: str,
        *,
        retry: bool = False,
    ) -> str:
        """Keep an ambiguous mutation publicly reconcilable instead of handing off.

        The authorization is deliberately left unconsumed and the operation
        ledger keeps its ``RECONCILE_REQUIRED`` row, so ``RECONCILIAR``
        reads the exact remote id first and can never issue a second write.
        """

        data = dict(data)
        data["reconcile_kind"] = kind
        if retry:
            response = (
                "A reconciliação ainda não pôde ser concluída sem risco de "
                "duplicidade. Responda RECONCILIAR novamente ou fale com a recepção."
            )
        else:
            response = (
                f"O resultado {_RECONCILIATION_SUBJECT.get(kind, 'da operação')} "
                "ficou pendente de validação. Responda RECONCILIAR para "
                "consultar o Feegow sem repetir a escrita."
            )
        return self._respond(
            store,
            message_id,
            chat_key,
            response,
            FlowState.RECONCILIATION_REQUIRED,
            data,
        )

    def _run_authorized_mutation(
        self,
        store: AppointmentStore,
        message_id: str,
        chat_key: str,
        data: dict[str, Any],
        kind: str,
        *,
        reconciling: bool = False,
    ) -> str:
        """Execute (or reconcile) one authorized cancel/reschedule/edit.

        Every executor is idempotency-ledger fenced: once an operation row
        exists, the same call reads the exact remote id back instead of
        writing again, so this is the single public entry point for both
        ``CONFIRMAR`` and ``RECONCILIAR``.
        """

        if not self._authorization_still_valid(store, chat_key, data):
            return self._handoff(store, message_id, chat_key)
        appointment_id: int | None = None
        try:
            if kind == "CANCEL":
                appointment_id = self._execute_cancel(store, data)
            elif kind == "RESCHEDULE":
                appointment_id = self._execute_reschedule(store, data)
            else:
                self._execute_edit(store, data)
        except _PreflightRejected:
            # Nothing was written and nothing remote can be reconciled.
            return self._handoff(store, message_id, chat_key)
        except Exception:
            # The remote result is ambiguous: keep the authorization
            # unconsumed and expose RECONCILIAR instead of a dead handoff.
            return self._enter_reconciliation(
                store, message_id, chat_key, data, kind, retry=reconciling
            )
        # Consume only after the remote mutation and its independent
        # readback (or reconciliation) succeeded.
        store.consume_authorization(
            data["authorization_id"],
            chat_key,
            data["payload_hash"],
            now=self._now(),
        )
        if kind == "CANCEL":
            completed = {"appointment_id": appointment_id, "completion_kind": "CANCEL"}
            response = f"Agendamento {appointment_id} desmarcado em status 11."
        elif kind == "RESCHEDULE":
            completed = {
                "appointment_id": appointment_id,
                "completion_kind": "RESCHEDULE",
            }
            response = f"Agendamento {appointment_id} remarcado em status 15."
        else:
            completed = {"completion_kind": "EDIT"}
            label = "telefone" if data["edit_field"] == "telefone" else "e-mail"
            response = f"Cadastro atualizado: {label} alterado com sucesso."
        return self._respond(
            store, message_id, chat_key, response, FlowState.COMPLETED, completed
        )

    def _authorization_still_valid(
        self,
        store: AppointmentStore,
        chat_key: str,
        data: Mapping[str, Any],
    ) -> bool:
        """Gate every write on an unexpired, unconsumed, payload-bound grant."""

        authorization_id = data.get("authorization_id")
        payload_hash = data.get("payload_hash")
        if not authorization_id or not payload_hash:
            return False
        return store.authorization_is_valid(
            str(authorization_id), chat_key, str(payload_hash), now=self._now()
        )

    def _reconcile_pending_mutation(
        self,
        store: AppointmentStore,
        message_id: str,
        chat_key: str,
        data: dict[str, Any],
        kind: str,
    ) -> str:
        return self._run_authorized_mutation(
            store, message_id, chat_key, data, kind, reconciling=True
        )

    def _event_received_at(self, incoming: Any) -> datetime:
        raw = getattr(incoming, "timestamp", None)
        if isinstance(raw, datetime):
            received_at = raw
        elif raw:
            received_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        else:
            received_at = self._now()
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=_BRT)
        return received_at.astimezone(_BRT)

    def _handle_payment_proof(
        self,
        store: AppointmentStore,
        flow: FlowSnapshot,
        message_id: str,
        chat_key: str,
        incoming: Any,
    ) -> str:
        """Persist one on-time local image/PDF proof without exposing its name."""

        reservation = store.active_reservation(chat_key)
        if reservation is None:
            return self._handoff(store, message_id, chat_key)
        received_at = self._event_received_at(incoming)
        deadline = datetime.fromisoformat(str(reservation["deadline_at"]))
        if received_at > deadline:
            return self._handoff(store, message_id, chat_key)
        if reservation.get("proof_received_at") is not None:
            return self._respond(
                store,
                message_id,
                chat_key,
                "Comprovante recebido. A recepção fará a validação.",
                FlowState.COMPLETED,
                flow.data,
            )

        media_urls = tuple(getattr(incoming, "media_urls", ()) or ())
        media_types = tuple(getattr(incoming, "media_types", ()) or ())
        source: Path | None = None
        for index, raw_path in enumerate(media_urls):
            media_type = (
                str(media_types[index]).casefold()
                if index < len(media_types)
                else ""
            )
            if media_type.startswith("image/") or media_type == "application/pdf":
                candidate = Path(str(raw_path))
                if candidate.is_file():
                    source = candidate
                    break
        if source is None:
            return self._handoff(store, message_id, chat_key)

        digest = hashlib.sha256()
        with source.open("rb") as proof_source:
            for chunk in iter(lambda: proof_source.read(1024 * 1024), b""):
                digest.update(chunk)
        sha256 = digest.hexdigest()
        self._proofs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._proofs_dir, 0o700)
        private_path = self._proofs_dir / sha256
        created_file = False
        try:
            descriptor = os.open(
                private_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            pass
        else:
            created_file = True
            try:
                with os.fdopen(descriptor, "wb") as proof_destination:
                    with source.open("rb") as proof_source:
                        shutil.copyfileobj(proof_source, proof_destination)
            except Exception:
                private_path.unlink(missing_ok=True)
                raise
        os.chmod(private_path, 0o600)

        accepted = store.accept_payment_proof(
            message_id=message_id,
            appointment_id=reservation["appointment_id"],
            chat_key=chat_key,
            received_at=received_at,
            sha256=sha256,
            private_path=str(private_path),
        )
        if accepted not in {"accepted", "duplicate"}:
            if created_file:
                private_path.unlink(missing_ok=True)
            return self._handoff(store, message_id, chat_key)
        if accepted == "accepted" and self._reception_chat_id:
            store.enqueue_reception_receipt(
                reservation["appointment_id"], self._reception_chat_id, now=self._now()
            )
        return self._respond(
            store,
            message_id,
            chat_key,
            "Comprovante recebido. A recepção fará a validação.",
            FlowState.COMPLETED,
            flow.data,
        )

    @staticmethod
    def _appointment_readback_status(result: Any, appointment_id: int) -> int:
        rows = _collection(result)
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise RuntimeError("ambiguous appointment readback")
        readback = rows[0]
        actual_id = _extract_id(
            readback, ("agendamento_id", "appointment_id", "id")
        )
        status_id = _extract_status_id(readback)
        if status_id is None:
            raise RuntimeError("appointment readback is incomplete")
        if actual_id != appointment_id:
            raise RuntimeError("appointment readback id mismatch")
        return status_id

    def process_due(self, *, worker_id: str) -> list[dict[str, str]]:
        """Outbox one reminder or expire unpaid reservations under a short lease."""

        if (
            not self._enabled
            or not self._write_enabled
            or not self._payment_ready
            or not self._db_path.exists()
        ):
            return []
        store = AppointmentStore(self._db_path)
        now = self._now()
        lease_name = "teleconsultation-payment-watcher"
        if not store.acquire_watcher_lease(
            lease_name,
            str(worker_id),
            now=now,
            lease_seconds=self._watcher_lease_seconds,
        ):
            return []

        emitted: list[dict[str, str]] = []
        try:
            for reservation in store.due_reservations():
                appointment_id = int(reservation["appointment_id"])
                chat_key = str(reservation["chat_key"])
                reminder = store.enqueue_payment_reminder(
                    appointment_id, chat_key, now=now
                )
                if reminder is not None:
                    emitted.append(reminder)

                if not store.claim_expiration(
                    appointment_id,
                    now=now,
                    grace_seconds=self._payment_grace_seconds,
                ):
                    continue
                if store.reservation_has_proof(appointment_id):
                    continue

                client = self._require_client()
                try:
                    status_id = self._appointment_readback_status(
                        client.get_appointment(appointment_id), appointment_id
                    )
                except Exception:
                    store.set_reservation_state(
                        appointment_id, FlowState.EXCECAO_RECEPCAO.value
                    )
                    continue

                if status_id == 7:
                    store.set_reservation_state(
                        appointment_id, FlowState.CONFIRMADO_STATUS_7.value
                    )
                    continue
                if status_id == 11:
                    expiration_key = _opaque_id(
                        "expire_teleconsultation", appointment_id
                    )
                    previous_operation = store.get_operation(expiration_key)
                    if (
                        previous_operation is not None
                        and previous_operation["state"] == "RECONCILE_REQUIRED"
                    ):
                        store.finish_operation(
                            previous_operation["id"],
                            state="SUCCEEDED",
                            remote_id=appointment_id,
                            now=now,
                        )
                        store.audit(
                            "operation_reconciled",
                            operation_id=previous_operation["id"],
                            now=now,
                            metadata={
                                "kind": "EXPIRE_TELECONSULTATION",
                                "state": "SUCCEEDED",
                                "status_id": 11,
                            },
                        )
                    event = store.finalize_expiration_cancellation(
                        appointment_id, chat_key, now=now
                    )
                    if event is not None:
                        emitted.append(event)
                    continue
                if status_id != 1:
                    store.set_reservation_state(
                        appointment_id, FlowState.EXCECAO_RECEPCAO.value
                    )
                    continue

                store.set_reservation_state(
                    appointment_id, FlowState.CANCELAMENTO_FEEGOW_PENDENTE.value
                )
                idempotency_key = _opaque_id(
                    "expire_teleconsultation", appointment_id
                )
                payload_hash = _payload_hash(
                    {
                        "appointment_id": appointment_id,
                        "reason_id": self._cancel_reason_id,
                    }
                )
                claimed, operation = store.begin_operation(
                    idempotency_key=idempotency_key,
                    kind="EXPIRE_TELECONSULTATION",
                    target_id=str(appointment_id),
                    payload_hash=payload_hash,
                    now=now,
                )
                if not claimed:
                    try:
                        status_id = self._appointment_readback_status(
                            client.get_appointment(appointment_id), appointment_id
                        )
                    except Exception:
                        continue
                    if status_id == 11:
                        if operation["state"] != "SUCCEEDED":
                            store.finish_operation(
                                operation["id"],
                                state="SUCCEEDED",
                                remote_id=appointment_id,
                                now=now,
                            )
                            store.audit(
                                "operation_reconciled",
                                operation_id=operation["id"],
                                now=now,
                                metadata={
                                    "kind": "EXPIRE_TELECONSULTATION",
                                    "state": "SUCCEEDED",
                                    "status_id": 11,
                                },
                            )
                        event = store.finalize_expiration_cancellation(
                            appointment_id, chat_key, now=now
                        )
                        if event is not None:
                            emitted.append(event)
                    elif status_id == 7:
                        store.set_reservation_state(
                            appointment_id, FlowState.CONFIRMADO_STATUS_7.value
                        )
                    elif status_id != 1:
                        store.set_reservation_state(
                            appointment_id, FlowState.EXCECAO_RECEPCAO.value
                        )
                    # Status 1 after an ambiguous write remains fail-closed in
                    # RECONCILE_REQUIRED. Never issue a second cancellation.
                    continue

                try:
                    # Re-check the kill switch immediately before the only
                    # remote write this loop performs; a tick can outlive the
                    # gate that let it start.
                    self._require_write_enabled()
                    result = client.cancel_appointment(
                        appointment_id=appointment_id,
                        motivo_id=self._cancel_reason_id,
                    )
                    _collection(result)
                    post_status = self._appointment_readback_status(
                        client.get_appointment(appointment_id), appointment_id
                    )
                    if post_status != 11:
                        raise RuntimeError("expiration cancellation readback mismatch")
                except Exception as exc:
                    store.finish_operation(
                        operation["id"],
                        state="RECONCILE_REQUIRED",
                        error_class=type(exc).__name__,
                        now=now,
                    )
                    store.audit(
                        "operation_requires_reconciliation",
                        operation_id=operation["id"],
                        now=now,
                        metadata={
                            "kind": "EXPIRE_TELECONSULTATION",
                            "state": "RECONCILE_REQUIRED",
                        },
                    )
                    continue

                store.finish_operation(
                    operation["id"],
                    state="SUCCEEDED",
                    remote_id=appointment_id,
                    now=now,
                )
                store.audit(
                    "operation_succeeded",
                    operation_id=operation["id"],
                    now=now,
                    metadata={
                        "kind": "EXPIRE_TELECONSULTATION",
                        "state": "SUCCEEDED",
                        "status_id": 11,
                    },
                )
                event = store.finalize_expiration_cancellation(
                    appointment_id, chat_key, now=now
                )
                if event is not None:
                    emitted.append(event)
        finally:
            store.release_watcher_lease(lease_name, str(worker_id))
        return emitted

    def run_retention_cleanup(self, *, worker_id: str) -> dict[str, int]:
        """Purge inactive flows (30d) and expired proofs (180d) under a lease.

        Independent of ``write_enabled``/payment readiness — this is local
        privacy hygiene, not a Feegow mutation — but it still never creates
        the database file: an unused/never-initialized deployment stays at
        zero side effects.
        """

        zero = {"flows_purged": 0, "proofs_purged": 0}
        if not self._enabled or not self._db_path.exists():
            return zero
        store = AppointmentStore(self._db_path)
        now = self._now()
        lease_name = "appointment-retention-cleanup"
        if not store.acquire_watcher_lease(
            lease_name,
            str(worker_id),
            now=now,
            lease_seconds=self._retention_lease_seconds,
        ):
            return zero
        try:
            flows_purged = store.purge_inactive_flows(
                now=now, retention_days=self._flow_retention_days
            )
            proofs_purged = store.purge_expired_proofs(
                now=now, retention_days=self._proof_retention_days
            )
        finally:
            store.release_watcher_lease(lease_name, str(worker_id))
        return {"flows_purged": flows_purged, "proofs_purged": proofs_purged}

    def is_contact_quarantined(self, chat_key: str) -> bool:
        if not self._db_path.exists():
            return False
        return AppointmentStore(self._db_path).is_contact_quarantined(chat_key)

    def claim_outbox(
        self, *, worker_id: str, limit: int = 10
    ) -> list[dict[str, str]]:
        """Claim due outbox rows for delivery; never creates the database."""

        if not self.watcher_enabled or not self._db_path.exists():
            return []
        store = AppointmentStore(self._db_path)
        return store.claim_outbox_batch(
            owner=str(worker_id),
            now=self._now(),
            lease_seconds=self._outbox_lease_seconds,
            limit=limit,
        )

    def ack_outbox(
        self, outbox_id: str, *, worker_id: str, claim_token: str
    ) -> bool:
        if not self._db_path.exists():
            return False
        return AppointmentStore(self._db_path).ack_outbox_sent(
            outbox_id,
            owner=str(worker_id),
            claim_token=str(claim_token),
            now=self._now(),
        )

    def release_outbox(
        self, outbox_id: str, *, worker_id: str, claim_token: str
    ) -> bool:
        if not self._db_path.exists():
            return False
        return AppointmentStore(self._db_path).release_outbox_failure(
            outbox_id,
            owner=str(worker_id),
            claim_token=str(claim_token),
            now=self._now(),
            backoff_seconds=self._outbox_backoff_seconds,
        )

    def _advance(
        self,
        store: AppointmentStore,
        flow: FlowSnapshot,
        message_id: str,
        chat_key: str,
        text: str,
    ) -> str:
        state = flow.state
        data = dict(flow.data)
        normalized = _normalize(text)

        if state == FlowState.RECONCILIATION_REQUIRED.value:
            if normalized != "reconciliar":
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    (
                        "O resultado do agendamento ficou pendente de validação. "
                        "Responda RECONCILIAR para consultar o Feegow sem repetir a escrita."
                    ),
                    FlowState.RECONCILIATION_REQUIRED,
                    data,
                )
            if not self._write_enabled:
                return self._handoff(store, message_id, chat_key)
            reconcile_kind = str(data.get("reconcile_kind") or "CREATE")
            if reconcile_kind in {"CANCEL", "RESCHEDULE", "EDIT"}:
                return self._reconcile_pending_mutation(
                    store, message_id, chat_key, data, reconcile_kind
                )
            payment_terms: tuple[timedelta, timedelta] | None = None
            if int(data["procedure_id"]) == 3:
                if not self._payment_ready:
                    return self._handoff(store, message_id, chat_key)
                payment_terms = self._tele_payment_terms(data["selected_slot"])
                if payment_terms is None:
                    return self._handoff(store, message_id, chat_key)
            if not self._authorization_still_valid(store, chat_key, data):
                return self._handoff(store, message_id, chat_key)
            try:
                appointment_id = self._execute_authorized(store, chat_key, data)
            except _PreflightRejected:
                # A read-only preflight refused: nothing was written and
                # there is nothing remote to reconcile.
                return self._handoff(store, message_id, chat_key)
            except Exception:
                return self._enter_reconciliation(
                    store, message_id, chat_key, data, "CREATE", retry=True
                )
            return self._complete_authorized_appointment(
                store,
                message_id,
                chat_key,
                data,
                appointment_id,
                payment_terms,
            )
        if state == FlowState.HANDOFF.value:
            return self._handoff(store, message_id, chat_key)
        if state == FlowState.COMPLETED.value:
            if data.get("completion_kind") == "CANCEL":
                response = (
                    f"Agendamento {data['appointment_id']} desmarcado em status 11."
                )
            elif data.get("completion_kind") == "RESCHEDULE":
                response = (
                    f"Agendamento {data['appointment_id']} remarcado em status 15."
                )
            elif data.get("completion_kind") == "EDIT":
                response = "Cadastro já atualizado. Nenhuma alteração adicional foi feita."
            else:
                response = (
                    "Seu pedido já foi registrado em status 1 e será confirmado pela recepção."
                )
            return self._respond(
                store,
                message_id,
                chat_key,
                response,
                FlowState.COMPLETED,
                data,
            )
        if state == FlowState.AWAITING_APPOINTMENT_ACTION.value:
            if _PRICE_QUESTION_RE.search(normalized):
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    f"{_PRICE_LIST_TEXT}\n\n{_INITIAL_MENU}",
                    FlowState.AWAITING_APPOINTMENT_ACTION,
                    data,
                )
            if normalized != "1":
                action = {"2": "MANAGE", "3": "CANCEL", "4": "RETURN", "5": "EDIT"}.get(
                    normalized
                )
                if action is not None:
                    return self._respond(
                        store,
                        message_id,
                        chat_key,
                        "Informe o CPF do paciente.",
                        FlowState.AWAITING_CPF,
                        {"appointment_action": action},
                    )
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    _INITIAL_MENU,
                    FlowState.AWAITING_APPOINTMENT_ACTION,
                    data,
                )
            return self._respond(
                store,
                message_id,
                chat_key,
                _SERVICE_MENU,
                FlowState.AWAITING_SERVICE,
                {},
            )

        if state == FlowState.AWAITING_SERVICE.value:
            if _PRICE_QUESTION_RE.search(normalized):
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    f"{_PRICE_LIST_TEXT}\n\n{_SERVICE_MENU}",
                    FlowState.AWAITING_SERVICE,
                    data,
                )
            service_choice = self._service_choice(normalized)
            if service_choice is None:
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    _SERVICE_MENU,
                    FlowState.AWAITING_SERVICE,
                    data,
                )
            if not self._write_enabled:
                return self._handoff(store, message_id, chat_key)
            service = _SERVICES[service_choice]
            if service["procedure_id"] == 3 and not self._payment_ready:
                return self._handoff(store, message_id, chat_key)
            slots = self.available_slots(service["procedure_id"])
            if not slots:
                return self._handoff(store, message_id, chat_key)
            next_data = {
                "procedure_id": service["procedure_id"],
                "price": service["price"],
                "service_label": service["label"],
                "slots": slots,
            }
            lines = ["Escolha uma das vagas disponíveis (horário de Brasília):"]
            lines.extend(
                f"{index} - {slot['display_date']} às {slot['time']}"
                for index, slot in enumerate(slots, 1)
            )
            return self._respond(
                store,
                message_id,
                chat_key,
                "\n".join(lines),
                FlowState.AWAITING_SLOT,
                next_data,
            )

        if state == FlowState.AWAITING_SLOT.value:
            try:
                index = int(normalized) - 1
                selected = data["slots"][index]
                if index < 0:
                    raise IndexError
            except (ValueError, TypeError, IndexError, KeyError):
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "Escolha uma vaga pelo número informado.",
                    FlowState.AWAITING_SLOT,
                    data,
                )
            if (
                int(data["procedure_id"]) == 3
                and self._tele_payment_terms(selected) is None
            ):
                return self._handoff(store, message_id, chat_key)
            data["selected_slot"] = selected
            data.pop("slots", None)
            return self._respond(
                store,
                message_id,
                chat_key,
                "Informe o CPF do paciente.",
                FlowState.AWAITING_CPF,
                data,
            )

        if state == FlowState.AWAITING_CPF.value:
            if not is_valid_cpf(text):
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "CPF inválido. Confira os 11 dígitos e envie novamente.",
                    FlowState.AWAITING_CPF,
                    data,
                )
            data["cpf"] = _digits(text)
            return self._respond(
                store,
                message_id,
                chat_key,
                "Informe a data de nascimento (DD/MM/AAAA).",
                FlowState.AWAITING_BIRTH_DATE,
                data,
            )

        if state == FlowState.AWAITING_BIRTH_DATE.value:
            birth = _parse_date(text)
            if birth is None or birth >= self._now().date():
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "Data de nascimento inválida. Use DD/MM/AAAA.",
                    FlowState.AWAITING_BIRTH_DATE,
                    data,
                )
            data["birth_date"] = birth.strftime("%d/%m/%Y")
            patients = self.find_patient(data["cpf"])
            if _has_legacy_or_ambiguous_return(patients) or len(patients) > 1:
                return self._handoff(store, message_id, chat_key)
            if patients:
                patient = patients[0]
                patient_birth = _parse_date(
                    next(
                        (
                            patient.get(key)
                            for key in ("data_nascimento", "birth_date", "nascimento")
                            if patient.get(key)
                        ),
                        None,
                    )
                )
                patient_identifier = _patient_id(patient)
                if patient_identifier is None or patient_birth != birth:
                    return self._handoff(store, message_id, chat_key)
                data["patient_id"] = patient_identifier
                data["patient_phones"] = sorted(_patient_phones(patient))
                data["new_patient"] = False
            else:
                if data.get("appointment_action"):
                    return self._handoff(store, message_id, chat_key)
                data["new_patient"] = True
            return self._respond(
                store,
                message_id,
                chat_key,
                "Para validar o telefone WhatsApp deste atendimento, responda SIM.",
                FlowState.AWAITING_PHONE_CONFIRMATION,
                data,
            )

        if state == FlowState.AWAITING_PHONE_CONFIRMATION.value:
            if normalized not in {"sim", "confirmar"}:
                return self._handoff(store, message_id, chat_key)
            whatsapp_phone = _chat_phone(chat_key)
            if len(whatsapp_phone) not in {10, 11}:
                return self._handoff(store, message_id, chat_key)
            if not data.get("new_patient") and whatsapp_phone not in set(
                data.get("patient_phones", [])
            ):
                return self._handoff(store, message_id, chat_key)
            data.pop("patient_phones", None)
            data["phone"] = whatsapp_phone
            action = data.get("appointment_action")
            if action == "RETURN":
                return self._start_return_flow(store, message_id, chat_key, data)
            if action == "EDIT":
                return self._start_edit_flow(store, message_id, chat_key, data)
            if action:
                return self._list_future_appointments(
                    store, message_id, chat_key, data
                )
            if data.get("new_patient"):
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "Informe o nome completo para o novo cadastro.",
                    FlowState.AWAITING_NEW_PATIENT_NAME,
                    data,
                )
            return self._summarize(store, message_id, chat_key, data)

        if state == FlowState.AWAITING_APPOINTMENT_SELECTION.value:
            appointment_action = data.get("appointment_action")
            if appointment_action == "CANCEL":
                selection = normalized
                invalid_selection_response = (
                    "Escolha um agendamento pelo número informado."
                )
            elif appointment_action == "MANAGE":
                match = re.fullmatch(r"(?:remarcar|reagendar)\s+(\d+)", normalized)
                if match is None:
                    return self._respond(
                        store,
                        message_id,
                        chat_key,
                        "Para remarcar, envie REMARCAR seguido do número do agendamento.",
                        FlowState.AWAITING_APPOINTMENT_SELECTION,
                        data,
                    )
                selection = match.group(1)
                invalid_selection_response = (
                    "Escolha um agendamento válido usando REMARCAR e o número informado."
                )
            else:
                return self._handoff(store, message_id, chat_key)
            try:
                index = int(selection) - 1
                selected = data["appointments"][index]
                if index < 0:
                    raise IndexError
            except (ValueError, TypeError, IndexError, KeyError):
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    invalid_selection_response,
                    FlowState.AWAITING_APPOINTMENT_SELECTION,
                    data,
                )
            starts_at = datetime.combine(
                date.fromisoformat(selected["date"]),
                time.fromisoformat(selected["time"]),
                tzinfo=_BRT,
            )
            if starts_at - self._now() < timedelta(hours=24):
                return self._handoff(store, message_id, chat_key)
            if appointment_action == "MANAGE":
                procedure_id = int(selected["procedure_id"])
                modality = None
                if procedure_id == self._return_procedure_id:
                    # A single configurable id serves both return
                    # modalities, so only our own ledger can say which
                    # policy applied; an unlinked (legacy) match fails
                    # closed to reception instead of guessing.
                    modality = store.return_modality_for_appointment(selected["id"])
                    if modality is None:
                        return self._handoff(store, message_id, chat_key)
                slots = self.available_slots(procedure_id, modality=modality)
                if not slots:
                    return self._handoff(store, message_id, chat_key)
                data["selected_appointment"] = selected
                data["procedure_id"] = procedure_id
                data["slots"] = slots
                data.pop("appointments", None)
                lines = [
                    "Escolha a nova vaga para o mesmo procedimento (horário de Brasília):"
                ]
                lines.extend(
                    f"{slot_index} - {slot['display_date']} às {slot['time']}"
                    for slot_index, slot in enumerate(slots, 1)
                )
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "\n".join(lines),
                    FlowState.AWAITING_RESCHEDULE_SLOT,
                    data,
                )
            intent = {
                "kind": "CANCEL_APPOINTMENT",
                "appointment": selected,
                "reason_id": self._cancel_reason_id,
            }
            payload_hash = _payload_hash(intent)
            authorization_id = store.create_authorization(
                chat_key,
                kind="CANCEL_APPOINTMENT",
                target_id=str(selected["id"]),
                payload_hash=payload_hash,
                expires_at=self._now()
                + timedelta(minutes=self._authorization_minutes),
                now=self._now(),
            )
            data["selected_appointment"] = selected
            data.pop("appointments", None)
            data["payload_hash"] = payload_hash
            data["authorization_id"] = authorization_id
            return self._respond(
                store,
                message_id,
                chat_key,
                (
                    "Resumo do cancelamento:\n"
                    f"Agendamento {selected['id']} — {selected['display_date']} "
                    f"às {selected['time']} (Brasília).\n"
                    "Responda CONFIRMAR para autorizar uma única vez."
                ),
                FlowState.AWAITING_CANCEL_AUTHORIZATION,
                data,
            )

        if state == FlowState.AWAITING_RESCHEDULE_SLOT.value:
            try:
                index = int(normalized) - 1
                selected_slot = data["slots"][index]
                if index < 0:
                    raise IndexError
            except (ValueError, TypeError, IndexError, KeyError):
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "Escolha uma nova vaga pelo número informado.",
                    FlowState.AWAITING_RESCHEDULE_SLOT,
                    data,
                )
            selected_appointment = data["selected_appointment"]
            intent = {
                "kind": "RESCHEDULE_APPOINTMENT",
                "appointment": selected_appointment,
                "selected_slot": selected_slot,
            }
            payload_hash = _payload_hash(intent)
            authorization_id = store.create_authorization(
                chat_key,
                kind="RESCHEDULE_APPOINTMENT",
                target_id=str(selected_appointment["id"]),
                payload_hash=payload_hash,
                expires_at=self._now()
                + timedelta(minutes=self._authorization_minutes),
                now=self._now(),
            )
            data["selected_slot"] = selected_slot
            data.pop("slots", None)
            data["payload_hash"] = payload_hash
            data["authorization_id"] = authorization_id
            return self._respond(
                store,
                message_id,
                chat_key,
                (
                    "Resumo da remarcação:\n"
                    f"Agendamento {selected_appointment['id']} — "
                    f"{selected_slot['display_date']} às {selected_slot['time']} "
                    "(Brasília).\n"
                    "Responda CONFIRMAR para autorizar uma única vez."
                ),
                FlowState.AWAITING_RESCHEDULE_AUTHORIZATION,
                data,
            )

        if state == FlowState.AWAITING_CANCEL_AUTHORIZATION.value:
            if normalized != "confirmar":
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "Responda CONFIRMAR para autorizar uma única vez.",
                    FlowState.AWAITING_CANCEL_AUTHORIZATION,
                    data,
                )
            if not self._write_enabled:
                return self._handoff(store, message_id, chat_key)
            return self._run_authorized_mutation(
                store, message_id, chat_key, data, "CANCEL"
            )

        if state == FlowState.AWAITING_RESCHEDULE_AUTHORIZATION.value:
            if normalized != "confirmar":
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "Responda CONFIRMAR para autorizar uma única vez.",
                    FlowState.AWAITING_RESCHEDULE_AUTHORIZATION,
                    data,
                )
            if not self._write_enabled:
                return self._handoff(store, message_id, chat_key)
            return self._run_authorized_mutation(
                store, message_id, chat_key, data, "RESCHEDULE"
            )

        if state == FlowState.AWAITING_NEW_PATIENT_NAME.value:
            if len(text.split()) < 2 or any(char.isdigit() for char in text):
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "Informe nome e sobrenome, sem números.",
                    FlowState.AWAITING_NEW_PATIENT_NAME,
                    data,
                )
            data["name"] = " ".join(text.split())
            return self._respond(
                store,
                message_id,
                chat_key,
                "Informe o sexo cadastral: M (masculino) ou F (feminino).",
                FlowState.AWAITING_NEW_PATIENT_SEX,
                data,
            )

        if state == FlowState.AWAITING_NEW_PATIENT_SEX.value:
            sex = {"m": "M", "masculino": "M", "f": "F", "feminino": "F"}.get(
                normalized
            )
            if sex is None:
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "Use M para masculino ou F para feminino.",
                    FlowState.AWAITING_NEW_PATIENT_SEX,
                    data,
                )
            data["sex"] = sex
            return self._respond(
                store,
                message_id,
                chat_key,
                "Informe o e-mail para comunicados.",
                FlowState.AWAITING_NEW_PATIENT_EMAIL,
                data,
            )

        if state == FlowState.AWAITING_NEW_PATIENT_EMAIL.value:
            email = text.strip().casefold()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "E-mail inválido. Confira e envie novamente.",
                    FlowState.AWAITING_NEW_PATIENT_EMAIL,
                    data,
                )
            data["email"] = email
            return self._summarize(store, message_id, chat_key, data)

        if state == FlowState.AWAITING_AUTHORIZATION.value:
            if normalized == "alterar":
                store.invalidate_authorization(data["authorization_id"], now=self._now())
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    _SERVICE_MENU,
                    FlowState.AWAITING_SERVICE,
                    {},
                )
            if normalized != "confirmar":
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "Responda CONFIRMAR para autorizar uma única vez ou ALTERAR.",
                    FlowState.AWAITING_AUTHORIZATION,
                    data,
                )
            if not self._write_enabled:
                return self._handoff(store, message_id, chat_key)
            payment_terms: tuple[timedelta, timedelta] | None = None
            if int(data["procedure_id"]) == 3:
                if not self._payment_ready:
                    return self._handoff(store, message_id, chat_key)
                payment_terms = self._tele_payment_terms(data["selected_slot"])
                if payment_terms is None:
                    return self._handoff(store, message_id, chat_key)
            if not self._authorization_still_valid(store, chat_key, data):
                return self._handoff(store, message_id, chat_key)
            try:
                appointment_id = self._execute_authorized(store, chat_key, data)
            except _PreflightRejected:
                # Slot gone, duplicate, or a changed immutable procedure:
                # no write happened, so there is nothing to reconcile.
                return self._handoff(store, message_id, chat_key)
            except Exception:
                return self._enter_reconciliation(
                    store, message_id, chat_key, data, "CREATE"
                )
            return self._complete_authorized_appointment(
                store,
                message_id,
                chat_key,
                data,
                appointment_id,
                payment_terms,
            )

        if state == FlowState.AWAITING_RETURN_MODALITY.value:
            modality = {
                "1": "presencial",
                "presencial": "presencial",
                "2": "tele",
                "teleconsulta": "tele",
                "tele": "tele",
            }.get(normalized)
            if modality is None:
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "Escolha 1 para presencial ou 2 para teleconsulta.",
                    FlowState.AWAITING_RETURN_MODALITY,
                    data,
                )
            slots = self.available_slots(self._return_procedure_id, modality=modality)
            if not slots:
                return self._handoff(store, message_id, chat_key)
            data["procedure_id"] = self._return_procedure_id
            data["price"] = 0
            data["service_label"] = (
                "Retorno gratuito (presencial)"
                if modality == "presencial"
                else "Retorno gratuito (teleconsulta)"
            )
            data["return_modality"] = modality
            data["slots"] = slots
            lines = ["Escolha uma das vagas disponíveis (horário de Brasília):"]
            lines.extend(
                f"{index} - {slot['display_date']} às {slot['time']}"
                for index, slot in enumerate(slots, 1)
            )
            return self._respond(
                store,
                message_id,
                chat_key,
                "\n".join(lines),
                FlowState.AWAITING_RETURN_SLOT,
                data,
            )

        if state == FlowState.AWAITING_RETURN_SLOT.value:
            # Identity (CPF/birth/WhatsApp phone) was already established
            # before ``_start_return_flow`` ran — unlike the new-appointment
            # AWAITING_SLOT branch, this must go straight to the summary
            # instead of re-asking for CPF.
            try:
                index = int(normalized) - 1
                selected = data["slots"][index]
                if index < 0:
                    raise IndexError
            except (ValueError, TypeError, IndexError, KeyError):
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "Escolha uma vaga pelo número informado.",
                    FlowState.AWAITING_RETURN_SLOT,
                    data,
                )
            data["selected_slot"] = selected
            data.pop("slots", None)
            return self._summarize(store, message_id, chat_key, data)

        if state == FlowState.AWAITING_EDIT_FIELD.value:
            field = {
                "1": "telefone",
                "telefone": "telefone",
                "celular": "telefone",
                "2": "email",
                "email": "email",
                "e-mail": "email",
            }.get(normalized)
            if field is None:
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "Escolha 1 para telefone ou 2 para e-mail.",
                    FlowState.AWAITING_EDIT_FIELD,
                    data,
                )
            data["edit_field"] = field
            prompt = (
                "Informe o novo telefone com DDD."
                if field == "telefone"
                else "Informe o novo e-mail."
            )
            return self._respond(
                store, message_id, chat_key, prompt, FlowState.AWAITING_EDIT_VALUE, data
            )

        if state == FlowState.AWAITING_EDIT_VALUE.value:
            field = data["edit_field"]
            if field == "telefone":
                value = _normalized_phone(text)
                if len(value) not in {10, 11}:
                    return self._respond(
                        store,
                        message_id,
                        chat_key,
                        "Telefone inválido. Informe DDD e número.",
                        FlowState.AWAITING_EDIT_VALUE,
                        data,
                    )
            else:
                value = text.strip().casefold()
                if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
                    return self._respond(
                        store,
                        message_id,
                        chat_key,
                        "E-mail inválido. Confira e envie novamente.",
                        FlowState.AWAITING_EDIT_VALUE,
                        data,
                    )
            data["edit_value"] = value
            intent = {
                "kind": "EDIT_PATIENT",
                "patient_id": data["patient_id"],
                "field": field,
                "value": value,
            }
            payload_hash = _payload_hash(intent)
            authorization_id = store.create_authorization(
                chat_key,
                kind="EDIT_PATIENT",
                target_id=str(data["patient_id"]),
                payload_hash=payload_hash,
                expires_at=self._now() + timedelta(minutes=self._authorization_minutes),
                now=self._now(),
            )
            data["payload_hash"] = payload_hash
            data["authorization_id"] = authorization_id
            label = "telefone" if field == "telefone" else "e-mail"
            # The confirmation deliberately does not restate the new value:
            # persisted flow/inbox rows must never carry contact PII.
            return self._respond(
                store,
                message_id,
                chat_key,
                (
                    f"Confirma a atualização do {label} cadastrado?\n"
                    "Responda CONFIRMAR para autorizar uma única vez."
                ),
                FlowState.AWAITING_EDIT_AUTHORIZATION,
                data,
            )

        if state == FlowState.AWAITING_EDIT_AUTHORIZATION.value:
            if normalized != "confirmar":
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    "Responda CONFIRMAR para autorizar uma única vez.",
                    FlowState.AWAITING_EDIT_AUTHORIZATION,
                    data,
                )
            if not self._write_enabled:
                return self._handoff(store, message_id, chat_key)
            return self._run_authorized_mutation(
                store, message_id, chat_key, data, "EDIT"
            )

        return self._handoff(store, message_id, chat_key)

    def _start_return_flow(
        self,
        store: AppointmentStore,
        message_id: str,
        chat_key: str,
        data: dict[str, Any],
    ) -> str:
        if not self._return_enabled:
            return self._handoff(store, message_id, chat_key)
        ledger = store.open_return_for_chat(chat_key)
        if ledger is None:
            return self._handoff(store, message_id, chat_key)
        base_id = int(ledger["base_appointment_id"])
        client = self._require_client()
        try:
            status_id = self._appointment_readback_status(
                client.get_appointment(base_id), base_id
            )
        except Exception:
            return self._handoff(store, message_id, chat_key)
        base_date = datetime.fromisoformat(ledger["base_date"])
        if status_id != 3:
            # Only close the ledger on an explicitly configured no-show
            # status; without that config we cannot safely infer a
            # no-show, so the ledger is left untouched for reception.
            if status_id in self._return_no_show_status_ids:
                store.close_return_ledger(base_id, state="VOID", now=self._now())
            return self._handoff(store, message_id, chat_key)
        if self._now() > base_date + timedelta(days=self._return_window_days):
            store.close_return_ledger(base_id, state="EXPIRED", now=self._now())
            return self._handoff(store, message_id, chat_key)
        data["appointment_action"] = "RETURN"
        data["base_appointment_id"] = base_id
        return self._respond(
            store,
            message_id,
            chat_key,
            "Retorno disponível. Escolha o tipo:\n1 - Presencial\n2 - Teleconsulta",
            FlowState.AWAITING_RETURN_MODALITY,
            data,
        )

    def _start_edit_flow(
        self,
        store: AppointmentStore,
        message_id: str,
        chat_key: str,
        data: dict[str, Any],
    ) -> str:
        return self._respond(
            store,
            message_id,
            chat_key,
            "O que deseja atualizar?\n1 - Telefone\n2 - E-mail",
            FlowState.AWAITING_EDIT_FIELD,
            data,
        )

    def _execute_edit(self, store: AppointmentStore, data: Mapping[str, Any]) -> None:
        self._require_write_enabled()
        patient_id = int(data["patient_id"])
        field = data["edit_field"]
        value = data["edit_value"]
        claimed, operation = store.begin_operation(
            idempotency_key=_opaque_id("edit_patient", patient_id, data["payload_hash"]),
            kind="EDIT_PATIENT",
            target_id=str(patient_id),
            payload_hash=data["payload_hash"],
            now=self._now(),
        )
        if not claimed:
            if operation["state"] == "SUCCEEDED":
                return
            patients = self.find_patient(data["cpf"])
            self._verify_patient_edit_readback(
                patients, patient_id=patient_id, field=field, value=value
            )
            store.finish_operation(
                operation["id"],
                state="SUCCEEDED",
                remote_id=patient_id,
                now=self._now(),
            )
            store.audit(
                "operation_reconciled",
                operation_id=operation["id"],
                now=self._now(),
                metadata={"kind": "EDIT_PATIENT", "state": "SUCCEEDED"},
            )
            return
        client = self._require_client()
        try:
            result = client.edit_patient(patient_id, **{field: value})
            _collection(result)
            patients = self.find_patient(data["cpf"])
            self._verify_patient_edit_readback(
                patients, patient_id=patient_id, field=field, value=value
            )
        except Exception as exc:
            store.finish_operation(
                operation["id"],
                state="RECONCILE_REQUIRED",
                remote_id=patient_id,
                error_class=type(exc).__name__,
                now=self._now(),
            )
            store.audit(
                "operation_requires_reconciliation",
                operation_id=operation["id"],
                now=self._now(),
                metadata={"kind": "EDIT_PATIENT", "state": "RECONCILE_REQUIRED"},
            )
            raise
        store.finish_operation(
            operation["id"], state="SUCCEEDED", remote_id=patient_id, now=self._now()
        )
        store.audit(
            "operation_succeeded",
            operation_id=operation["id"],
            now=self._now(),
            metadata={"kind": "EDIT_PATIENT", "state": "SUCCEEDED"},
        )

    @staticmethod
    def _verify_patient_edit_readback(
        patients: Iterable[Mapping[str, Any]],
        *,
        patient_id: int,
        field: str,
        value: str,
    ) -> None:
        matches = [patient for patient in patients if _patient_id(patient) == patient_id]
        if len(matches) != 1:
            raise RuntimeError("ambiguous patient edit readback")
        patient = matches[0]
        if field == "telefone":
            if _normalized_phone(value) not in _patient_phones(patient):
                raise RuntimeError("patient phone readback mismatch")
        else:
            actual_email = _normalize(patient.get("email", ""))
            if actual_email != _normalize(value):
                raise RuntimeError("patient email readback mismatch")

    def _list_future_appointments(
        self,
        store: AppointmentStore,
        message_id: str,
        chat_key: str,
        data: dict[str, Any],
    ) -> str:
        client = self._require_client()
        start = self._now().date()
        end = start + timedelta(days=365)
        method = getattr(client, "search_appointments", None)
        if not callable(method):
            raise RuntimeError("injected Feegow client has no appointment reader")
        result = method(start.strftime("%d-%m-%Y"), end.strftime("%d-%m-%Y"))
        patient_id = int(data["patient_id"])
        appointments: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for raw in _collection(result, allow_none=True):
            if not isinstance(raw, Mapping):
                continue
            try:
                appointment_id = int(
                    next(
                        raw[key]
                        for key in ("agendamento_id", "appointment_id", "id")
                        if raw.get(key) not in (None, "")
                    )
                )
                raw_patient_id = int(raw.get("paciente_id", raw.get("patient_id")))
                procedure_id = int(
                    raw.get("procedimento_id", raw.get("procedure_id"))
                )
                status_id = _extract_status_id(raw)
                if status_id is None:
                    raise RuntimeError("ambiguous appointment")
            except (StopIteration, TypeError, ValueError):
                raise RuntimeError("ambiguous appointment")
            appointment_date, appointment_time = _slot_date_and_time(raw)
            if appointment_date is None or appointment_time is None:
                raise RuntimeError("ambiguous appointment")
            if raw_patient_id != patient_id:
                continue
            starts_at = datetime.combine(
                appointment_date, appointment_time, tzinfo=_BRT
            )
            if starts_at <= self._now():
                continue
            if appointment_id in seen_ids:
                raise RuntimeError("ambiguous appointment id")
            seen_ids.add(appointment_id)
            appointments.append(
                {
                    "id": appointment_id,
                    "patient_id": patient_id,
                    "procedure_id": procedure_id,
                    "status_id": status_id,
                    "date": appointment_date.isoformat(),
                    "display_date": appointment_date.strftime("%d/%m/%Y"),
                    "time": appointment_time.strftime("%H:%M"),
                }
            )
        appointments.sort(
            key=lambda appointment: (
                appointment["date"], appointment["time"], appointment["id"]
            )
        )
        appointments = appointments[:3]
        if not appointments:
            raise RuntimeError("no unambiguous future appointment")
        data["appointments"] = appointments
        lines = ["Agendamentos futuros encontrados:"]
        lines.extend(
            (
                f"Agendamento {index} - {appointment['display_date']} "
                f"às {appointment['time']}"
            )
            for index, appointment in enumerate(appointments, 1)
        )
        return self._respond(
            store,
            message_id,
            chat_key,
            "\n".join(lines),
            FlowState.AWAITING_APPOINTMENT_SELECTION,
            data,
        )

    @staticmethod
    def _service_choice(normalized: str) -> int | None:
        if normalized in {"1", "600", "r$ 600", "consulta", "presencial"}:
            return 1
        if normalized in {"2", "800", "r$ 800", "pacote", "retorno"}:
            return 2
        if normalized in {"3", "300", "r$ 300", "teleconsulta", "tele"}:
            return 3
        return None

    def _tele_payment_terms(
        self, selected_slot: Mapping[str, Any]
    ) -> tuple[timedelta, timedelta] | None:
        """Return the payment window and reminder notice for a future slot."""

        try:
            starts_at = datetime.combine(
                date.fromisoformat(str(selected_slot["date"])),
                time.fromisoformat(str(selected_slot["time"])),
                tzinfo=_BRT,
            )
        except (KeyError, TypeError, ValueError):
            return None
        lead_time = starts_at - self._now()
        if lead_time > timedelta(hours=48):
            return timedelta(hours=12), timedelta(hours=2)
        if lead_time > timedelta(hours=24):
            return timedelta(hours=4), timedelta(hours=1)
        if lead_time > timedelta(hours=3):
            return timedelta(hours=1), timedelta(minutes=15)
        return None

    def available_slots(
        self, procedure_id: int, *, modality: str | None = None
    ) -> list[dict[str, Any]]:
        """Read and policy-filter slots from the injected client only."""

        client = self._require_client()
        start = self._now().date()
        end = start + timedelta(days=self._search_days)
        method = getattr(client, "list_available_slots", None)
        if callable(method):
            result = method(
                procedure_id=int(procedure_id),
                professional_id=self._professional_id,
                specialty_id=self._specialty_id,
                local_id=self._local_id,
                start_date=start.strftime("%d-%m-%Y"),
                end_date=end.strftime("%d-%m-%Y"),
            )
        else:
            method = getattr(client, "search_appointments", None)
            if not callable(method):
                raise RuntimeError("injected Feegow client has no slot reader")
            result = method(start.strftime("%d-%m-%Y"), end.strftime("%d-%m-%Y"))
        return filter_eligible_slots(result, int(procedure_id), limit=3, modality=modality)

    def find_patient(self, cpf: str) -> list[dict[str, Any]]:
        """Find exact CPF candidates through an explicit injected-client method."""

        client = self._require_client()
        method = getattr(client, "find_patient_by_cpf", None)
        if callable(method):
            result = method(cpf)
        else:
            method = getattr(client, "search_patients", None)
            if not callable(method):
                raise RuntimeError("injected Feegow client has no patient reader")
            result = method(cpf=cpf)
        patients = _collection(result, allow_none=True)
        return [dict(patient) for patient in patients if isinstance(patient, Mapping)]

    def _complete_authorized_appointment(
        self,
        store: AppointmentStore,
        message_id: str,
        chat_key: str,
        data: Mapping[str, Any],
        appointment_id: int,
        payment_terms: tuple[timedelta, timedelta] | None,
    ) -> str:
        """Commit local completion only after the remote operation is verified.

        Validity was already gated before the write (see
        ``_authorization_still_valid``) and the idempotency ledger fences
        the remote call, so the consumption here is the durable single-use
        mark rather than the gate — a lost race must not discard a create
        that Feegow already confirmed by exact-id readback.
        """
        store.consume_authorization(
            str(data["authorization_id"]),
            chat_key,
            str(data["payload_hash"]),
            now=self._now(),
        )
        completed: dict[str, Any] = {
            "appointment_id": appointment_id,
            "procedure_id": data["procedure_id"],
        }
        if payment_terms is not None:
            payment_window, reminder_notice = payment_terms
            confirmed_at = self._now()
            deadline_at = confirmed_at + payment_window
            reminder_at = deadline_at - reminder_notice
            store.create_reservation(
                appointment_id,
                chat_key,
                deadline_at=deadline_at,
                reminder_at=reminder_at,
            )
            completed["payment_pending"] = True
            deadline_text = deadline_at.strftime("%d/%m/%Y %H:%M") + " (horário de Brasília)"
            response = (
                f"Agendamento {appointment_id} criado em status 1.\n"
                f"Prazo para envio do comprovante: {deadline_text}.\n"
                f"Beneficiário: {self._payment_beneficiary}\n"
                f"Pagamento: {self._payment_instructions}\n"
                "Envie a imagem ou o PDF do comprovante até o prazo."
            )
        else:
            response = (
                f"Agendamento {appointment_id} criado em status 1. "
                "A confirmação final será feita pela recepção."
            )
        return self._respond(
            store,
            message_id,
            chat_key,
            response,
            FlowState.COMPLETED,
            completed,
        )

    def _summarize(
        self,
        store: AppointmentStore,
        message_id: str,
        chat_key: str,
        data: dict[str, Any],
    ) -> str:
        intent = {
            key: data[key]
            for key in (
                "procedure_id",
                "price",
                "selected_slot",
                "cpf",
                "birth_date",
                "phone",
                "new_patient",
                "patient_id",
                "name",
                "sex",
                "email",
            )
            if key in data
        }
        payload_hash = _payload_hash(intent)
        selected = data["selected_slot"]
        is_teleconsultation = int(data["procedure_id"]) == 3
        if is_teleconsultation and (
            not self._payment_ready
            or self._tele_payment_terms(selected) is None
        ):
            return self._handoff(store, message_id, chat_key)
        authorization_id = store.create_authorization(
            chat_key,
            kind=(
                "CREATE_TELECONSULTATION"
                if is_teleconsultation
                else "CREATE_IN_PERSON_APPOINTMENT"
            ),
            target_id=selected["id"],
            payload_hash=payload_hash,
            expires_at=self._now() + timedelta(minutes=self._authorization_minutes),
            now=self._now(),
        )
        data["payload_hash"] = payload_hash
        data["authorization_id"] = authorization_id
        new_patient_notice = (
            "Será criado um novo cadastro de paciente com os dados informados antes do agendamento.\n"
            if data.get("new_patient")
            else ""
        )
        if is_teleconsultation:
            response = (
                "Resumo do agendamento:\n"
                f"{data['service_label']} — R$ {data['price']}\n"
                f"{selected['display_date']} às {selected['time']} (Brasília)\n"
                f"{new_patient_notice}"
                f"Beneficiário: {self._payment_beneficiary}\n"
                f"Pagamento: {self._payment_instructions}\n"
                "A reserva será criada em status 1. O prazo do comprovante expira "
                "automaticamente e, sem recebimento no prazo, a reserva será cancelada.\n"
                "Responda CONFIRMAR para autorizar uma única vez ou ALTERAR."
            )
        else:
            response = (
                "Resumo do agendamento:\n"
                f"{data['service_label']} — R$ {data['price']}\n"
                f"{selected['display_date']} às {selected['time']} (Brasília)\n"
                f"{new_patient_notice}"
                "Criação inicial em status 1; a recepção fará a confirmação final.\n"
                "Responda CONFIRMAR para autorizar uma única vez ou ALTERAR."
            )
        return self._respond(
            store,
            message_id,
            chat_key,
            response,
            FlowState.AWAITING_AUTHORIZATION,
            data,
        )

    def _require_client(self) -> Any:
        if self._feegow_client is None:
            raise RuntimeError("Feegow client must be injected")
        return self._feegow_client

    def _require_write_enabled(self) -> None:
        """Re-check the kill switch immediately before any remote write path.

        The injected client enforces this again at its own HTTP boundary;
        this handler-level check keeps a mis-wired caller from even
        claiming an operation row.
        """

        if not self._enabled or not self._write_enabled:
            raise _WriteDisabled("Feegow writes are disabled")

    def _verify_procedure(self, procedure_id: int, price: int) -> None:
        client = self._require_client()
        method = getattr(client, "list_procedures", None)
        if not callable(method):
            raise RuntimeError("procedure preflight unavailable")
        result = method(
            unidade_id=self._local_id,
            especialidade_id=self._specialty_id,
            profissional_id=self._professional_id,
        )
        procedures = _collection(result)
        matches = [
            procedure
            for procedure in procedures
            if isinstance(procedure, Mapping)
            and str(
                next(
                    (
                        procedure.get(key)
                        for key in ("procedimento_id", "procedure_id", "id")
                        if procedure.get(key) is not None
                    ),
                    "",
                )
            )
            == str(procedure_id)
        ]
        if len(matches) != 1:
            raise _PreflightRejected("procedure preflight mismatch")
        procedure = matches[0]
        name = _normalize(
            next(
                (procedure.get(key) for key in ("nome", "name", "descricao") if procedure.get(key)),
                "",
            )
        )
        actual_price = _money(
            next(
                (procedure.get(key) for key in ("valor", "price", "preco") if procedure.get(key) is not None),
                None,
            )
        )
        expected_name_ok = (
            procedure_id == 1 and "consulta" in name
            or procedure_id == 3 and "teleconsulta" in name.replace(" ", "")
            or procedure_id == 9 and "consulta" in name and "retorno" in name
            or (
                self._return_enabled
                and procedure_id == self._return_procedure_id
                and "retorno" in name
            )
        )
        if not expected_name_ok or actual_price != price:
            raise _PreflightRejected("immutable procedure definition changed")

    def _verify_slot(self, data: Mapping[str, Any]) -> None:
        selected = data["selected_slot"]
        current = self.available_slots(
            int(data["procedure_id"]),
            modality=(
                str(data["return_modality"])
                if data.get("appointment_action") == "RETURN"
                and data.get("return_modality") is not None
                else None
            ),
        )
        if not any(
            slot["id"] == selected["id"]
            and slot["date"] == selected["date"]
            and slot["time"] == selected["time"]
            for slot in current
        ):
            raise _PreflightRejected("selected slot is no longer available")

    def _verify_no_duplicate(self, data: Mapping[str, Any]) -> None:
        client = self._require_client()
        method = getattr(client, "find_duplicate_appointments", None)
        if not callable(method):
            raise RuntimeError("duplicate preflight unavailable")
        selected = data["selected_slot"]
        result = method(
            paciente_id=data.get("patient_id"),
            cpf=data["cpf"],
            data=selected["date"],
            horario=selected["time"],
            profissional_id=self._professional_id,
        )
        duplicates = _collection(result, allow_none=True)
        if _has_legacy_or_ambiguous_return(duplicates) or duplicates:
            raise _PreflightRejected("duplicate or ambiguous appointment")

    def _execute_cancel(
        self, store: AppointmentStore, data: Mapping[str, Any]
    ) -> int:
        self._require_write_enabled()
        selected = data["selected_appointment"]
        appointment_id = int(selected["id"])
        claimed, operation = store.begin_operation(
            idempotency_key=_opaque_id(
                "cancel_appointment", appointment_id, data["payload_hash"]
            ),
            kind="CANCEL_APPOINTMENT",
            target_id=str(appointment_id),
            payload_hash=data["payload_hash"],
            now=self._now(),
        )
        if not claimed:
            if operation["state"] == "SUCCEEDED":
                return appointment_id
            client = self._require_client()
            self._verify_mutation_readback(
                client.get_appointment(appointment_id),
                selected=selected,
                expected_status=11,
                appointment_id=appointment_id,
            )
            store.finish_operation(
                operation["id"],
                state="SUCCEEDED",
                remote_id=appointment_id,
                now=self._now(),
            )
            store.audit(
                "operation_reconciled",
                operation_id=operation["id"],
                now=self._now(),
                metadata={
                    "kind": "CANCEL_APPOINTMENT",
                    "state": "SUCCEEDED",
                    "status_id": 11,
                },
            )
            store.reopen_return_after_cancel(
                appointment_id,
                now=self._now(),
                within_days=self._return_window_days,
            )
            return appointment_id
        client = self._require_client()
        try:
            result = client.cancel_appointment(
                appointment_id=appointment_id, motivo_id=self._cancel_reason_id
            )
            _collection(result)
            self._verify_mutation_readback(
                client.get_appointment(appointment_id),
                selected=selected,
                expected_status=11,
            )
        except Exception as exc:
            store.finish_operation(
                operation["id"],
                state="RECONCILE_REQUIRED",
                remote_id=appointment_id,
                error_class=type(exc).__name__,
                now=self._now(),
            )
            store.audit(
                "operation_requires_reconciliation",
                operation_id=operation["id"],
                now=self._now(),
                metadata={"kind": "CANCEL_APPOINTMENT", "state": "RECONCILE_REQUIRED"},
            )
            raise
        store.finish_operation(
            operation["id"],
            state="SUCCEEDED",
            remote_id=appointment_id,
            now=self._now(),
        )
        store.audit(
            "operation_succeeded",
            operation_id=operation["id"],
            now=self._now(),
            metadata={
                "kind": "CANCEL_APPOINTMENT",
                "state": "SUCCEEDED",
                "status_id": 11,
            },
        )
        # No-op unless this id is a linked return appointment; preserves the
        # ledger for the base and any other cancellation untouched.
        store.reopen_return_after_cancel(
            appointment_id, now=self._now(), within_days=self._return_window_days
        )
        return appointment_id

    def _execute_reschedule(
        self, store: AppointmentStore, data: Mapping[str, Any]
    ) -> int:
        self._require_write_enabled()
        selected_appointment = data["selected_appointment"]
        selected_slot = data["selected_slot"]
        appointment_id = int(selected_appointment["id"])
        operation_key = _opaque_id(
            "reschedule_appointment", appointment_id, data["payload_hash"]
        )
        operation = store.get_operation(operation_key)
        if operation is not None:
            if operation["state"] == "SUCCEEDED":
                return appointment_id
            client = self._require_client()
            self._verify_mutation_readback(
                client.get_appointment(appointment_id),
                selected=selected_appointment,
                expected_status=15,
                appointment_id=appointment_id,
                expected_slot=selected_slot,
            )
            store.finish_operation(
                operation["id"],
                state="SUCCEEDED",
                remote_id=appointment_id,
                now=self._now(),
            )
            store.audit(
                "operation_reconciled",
                operation_id=operation["id"],
                now=self._now(),
                metadata={
                    "kind": "RESCHEDULE_APPOINTMENT",
                    "procedure_id": int(data["procedure_id"]),
                    "state": "SUCCEEDED",
                    "status_id": 15,
                },
            )
            return appointment_id
        self._verify_slot(data)
        self._verify_no_duplicate(data)
        claimed, operation = store.begin_operation(
            idempotency_key=operation_key,
            kind="RESCHEDULE_APPOINTMENT",
            target_id=str(appointment_id),
            payload_hash=data["payload_hash"],
            now=self._now(),
        )
        if not claimed:
            raise RuntimeError("reschedule operation claim was lost")
        client = self._require_client()
        try:
            result = client.reschedule_appointment(
                appointment_id=appointment_id,
                data=selected_slot["date"],
                horario=selected_slot["time"],
            )
            _collection(result)
            self._verify_mutation_readback(
                client.get_appointment(appointment_id),
                selected=selected_appointment,
                expected_status=15,
                appointment_id=appointment_id,
                expected_slot=selected_slot,
            )
        except Exception as exc:
            store.finish_operation(
                operation["id"],
                state="RECONCILE_REQUIRED",
                remote_id=appointment_id,
                error_class=type(exc).__name__,
                now=self._now(),
            )
            store.audit(
                "operation_requires_reconciliation",
                operation_id=operation["id"],
                now=self._now(),
                metadata={
                    "kind": "RESCHEDULE_APPOINTMENT",
                    "state": "RECONCILE_REQUIRED",
                },
            )
            raise
        store.finish_operation(
            operation["id"],
            state="SUCCEEDED",
            remote_id=appointment_id,
            now=self._now(),
        )
        store.audit(
            "operation_succeeded",
            operation_id=operation["id"],
            now=self._now(),
            metadata={
                "kind": "RESCHEDULE_APPOINTMENT",
                "procedure_id": int(data["procedure_id"]),
                "state": "SUCCEEDED",
                "status_id": 15,
            },
        )
        return appointment_id

    @staticmethod
    def _verify_mutation_readback(
        result: Any,
        *,
        selected: Mapping[str, Any],
        expected_status: int,
        appointment_id: int | None = None,
        expected_slot: Mapping[str, Any] | None = None,
    ) -> None:
        rows = _collection(result)
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise RuntimeError("ambiguous appointment readback")
        readback = rows[0]
        actual_appointment_id = _extract_id(
            readback, ("agendamento_id", "appointment_id", "id")
        )
        expected_appointment_id = int(
            appointment_id if appointment_id is not None else selected["id"]
        )
        try:
            status_id = _extract_status_id(readback)
            if status_id is None:
                raise ValueError("missing status")
            patient_id = int(
                readback.get("paciente_id", readback.get("patient_id"))
            )
            procedure_id = int(
                readback.get("procedimento_id", readback.get("procedure_id"))
            )
        except (TypeError, ValueError):
            raise RuntimeError("appointment readback is incomplete") from None
        if (
            actual_appointment_id != expected_appointment_id
            or status_id != int(expected_status)
            or patient_id != int(selected["patient_id"])
            or procedure_id != int(selected["procedure_id"])
        ):
            raise RuntimeError("appointment mutation readback mismatch")
        if expected_slot is not None:
            readback_date = readback.get("data", readback.get("date"))
            readback_time = readback.get("horario", readback.get("time"))
            expected_date = expected_slot.get("date", expected_slot.get("data"))
            expected_time = expected_slot.get("time", expected_slot.get("horario"))
            if (
                readback_date is None
                or expected_date is None
                or _parse_date(readback_date) != _parse_date(expected_date)
            ):
                raise RuntimeError("appointment date readback mismatch")
            if (
                readback_time is None
                or expected_time is None
                or _parse_time(readback_time) != _parse_time(expected_time)
            ):
                raise RuntimeError("appointment time readback mismatch")

    @staticmethod
    def _verify_patient_creation_readback(
        result: Any,
        *,
        data: Mapping[str, Any],
        expected_id: int | None = None,
    ) -> int:
        """Require one exact independent patient readback after create/timeout."""

        def sex_code(value: Any) -> str:
            normalized = _normalize(value)
            if normalized in {"m", "masculino", "1"}:
                return "M"
            if normalized in {"f", "feminino", "2"}:
                return "F"
            return normalized.upper()

        expected_birth = _parse_date(data["birth_date"])
        expected_phone = _normalized_phone(data["phone"])
        expected_name = _normalize(data["name"])
        expected_email = str(data["email"]).strip().casefold()
        expected_sex = sex_code(data["sex"])
        matches: list[int] = []
        for patient in _collection(result, allow_none=True):
            if not isinstance(patient, Mapping):
                continue
            patient_id = _patient_id(patient)
            if patient_id is None or (expected_id is not None and patient_id != expected_id):
                continue
            patient_cpf = _digits(patient.get("cpf"))
            patient_name = _normalize(
                patient.get("nome", patient.get("name", patient.get("nome_completo")))
            )
            birth_value = patient.get(
                "data_nascimento", patient.get("birth_date", patient.get("nascimento"))
            )
            email_value = str(patient.get("email") or "").strip().casefold()
            sex_value = patient.get("sexo", patient.get("sex"))
            if (
                patient_cpf != _digits(data["cpf"])
                or patient_name != expected_name
                or _parse_date(birth_value) != expected_birth
                or expected_phone not in _patient_phones(patient)
                or email_value != expected_email
                or sex_code(sex_value) != expected_sex
            ):
                continue
            matches.append(patient_id)
        if len(matches) != 1:
            raise RuntimeError("patient creation readback is ambiguous or mismatched")
        return matches[0]

    def _reconcile_patient_operation(
        self,
        store: AppointmentStore,
        operation: Mapping[str, Any],
        data: Mapping[str, Any],
    ) -> int:
        expected_id = (
            int(operation["remote_id"]) if operation.get("remote_id") not in (None, "") else None
        )
        patient_id = self._verify_patient_creation_readback(
            self.find_patient(str(data["cpf"])),
            data=data,
            expected_id=expected_id,
        )
        store.finish_operation(
            str(operation["id"]),
            state="SUCCEEDED",
            remote_id=patient_id,
            now=self._now(),
        )
        store.audit(
            "operation_reconciled",
            operation_id=str(operation["id"]),
            now=self._now(),
            metadata={"kind": "CREATE_PATIENT", "state": "SUCCEEDED"},
        )
        return patient_id

    def _reconcile_appointment_operation(
        self,
        store: AppointmentStore,
        operation: Mapping[str, Any],
        *,
        patient_id: int,
        data: Mapping[str, Any],
    ) -> int:
        client = self._require_client()
        candidates: list[int] = []
        if operation.get("remote_id") not in (None, ""):
            candidate_ids = [int(operation["remote_id"])]
        else:
            selected = data["selected_slot"]
            duplicate_result = client.find_duplicate_appointments(
                paciente_id=patient_id,
                cpf=data["cpf"],
                data=selected["date"],
                horario=selected["time"],
                profissional_id=self._professional_id,
            )
            candidate_ids = [
                candidate_id
                for item in _collection(duplicate_result, allow_none=True)
                if isinstance(item, Mapping)
                for candidate_id in [
                    _extract_id(item, ("agendamento_id", "appointment_id", "id"))
                ]
                if candidate_id is not None
            ]
        for candidate_id in dict.fromkeys(candidate_ids):
            try:
                self._verify_readback(
                    client.get_appointment(candidate_id),
                    appointment_id=candidate_id,
                    patient_id=patient_id,
                    data=data,
                )
            except Exception:
                continue
            candidates.append(candidate_id)
        if len(candidates) != 1:
            raise RuntimeError("appointment creation requires manual reconciliation")
        appointment_id = candidates[0]
        store.finish_operation(
            str(operation["id"]),
            state="SUCCEEDED",
            remote_id=appointment_id,
            now=self._now(),
        )
        store.audit(
            "operation_reconciled",
            operation_id=str(operation["id"]),
            now=self._now(),
            metadata={
                "kind": "CREATE_APPOINTMENT",
                "procedure_id": int(data["procedure_id"]),
                "state": "SUCCEEDED",
                "status_id": 1,
            },
        )
        return appointment_id

    def _execute_authorized(
        self, store: AppointmentStore, chat_key: str, data: dict[str, Any]
    ) -> int:
        """Run immutable preflight, mutations, and exact appointment readback."""

        self._require_write_enabled()
        self._verify_procedure(int(data["procedure_id"]), int(data["price"]))
        client = self._require_client()
        patient_id = data.get("patient_id")

        if data.get("new_patient"):
            patient_key = _opaque_id("create_patient", data["cpf"])
            operation = store.get_operation(patient_key)
            if operation is not None:
                if operation["state"] == "SUCCEEDED" and operation["remote_id"]:
                    patient_id = int(operation["remote_id"])
                else:
                    patient_id = self._reconcile_patient_operation(store, operation, data)
            else:
                claimed, operation = store.begin_operation(
                    idempotency_key=patient_key,
                    kind="CREATE_PATIENT",
                    target_id="new-patient",
                    payload_hash=data["payload_hash"],
                    now=self._now(),
                )
                if not claimed:
                    raise RuntimeError("patient operation claim was lost")
                patient_id: int | None = None
                try:
                    result = client.create_patient(
                        nome=data["name"],
                        cpf=data["cpf"],
                        data_nascimento=data["birth_date"],
                        telefone=data["phone"],
                        email=data["email"],
                        sexo=data["sex"],
                    )
                    patient_id = _extract_id(
                        result, ("paciente_id", "patient_id", "id")
                    )
                    if patient_id is None:
                        raise RuntimeError("patient create result has no exact id")
                    patient_id = self._verify_patient_creation_readback(
                        self.find_patient(str(data["cpf"])),
                        data=data,
                        expected_id=patient_id,
                    )
                except Exception as exc:
                    store.finish_operation(
                        operation["id"],
                        state="RECONCILE_REQUIRED",
                        remote_id=patient_id,
                        error_class=type(exc).__name__,
                        now=self._now(),
                    )
                    store.audit(
                        "operation_requires_reconciliation",
                        operation_id=operation["id"],
                        now=self._now(),
                        metadata={
                            "kind": "CREATE_PATIENT",
                            "state": "RECONCILE_REQUIRED",
                        },
                    )
                    raise
                store.finish_operation(
                    operation["id"],
                    state="SUCCEEDED",
                    remote_id=patient_id,
                    now=self._now(),
                )
                store.audit(
                    "operation_succeeded",
                    operation_id=operation["id"],
                    now=self._now(),
                    metadata={"kind": "CREATE_PATIENT", "state": "SUCCEEDED"},
                )

        if patient_id is None:
            raise RuntimeError("patient identity unavailable")
        patient_id = int(patient_id)
        selected = data["selected_slot"]
        appointment_key = _opaque_id(
            "create_appointment",
            data["cpf"],
            data["procedure_id"],
            selected["id"],
        )
        operation = store.get_operation(appointment_key)
        is_return = data.get("appointment_action") == "RETURN"
        return_modality = str(data.get("return_modality"))
        if operation is not None:
            if is_return and not store.claim_return_ledger(
                data["base_appointment_id"],
                claim_token=str(operation["id"]),
                modality=return_modality,
                now=self._now(),
            ):
                raise RuntimeError("free return entitlement is no longer available")
            if operation["state"] == "SUCCEEDED" and operation["remote_id"]:
                appointment_id = int(operation["remote_id"])
            else:
                appointment_id = self._reconcile_appointment_operation(
                    store, operation, patient_id=patient_id, data=data
                )
        else:
            # A new mutation requires a fresh slot preflight. An existing
            # ambiguous operation does not: a successful prior POST may have
            # consumed that slot, so its remote state must be reconciled first.
            self._verify_slot(data)
            self._verify_no_duplicate(data)
            claimed, operation = store.begin_operation(
                idempotency_key=appointment_key,
                kind="CREATE_APPOINTMENT",
                target_id=selected["id"],
                payload_hash=data["payload_hash"],
                now=self._now(),
            )
            if not claimed:
                raise RuntimeError("appointment operation claim was lost")
            if is_return and not store.claim_return_ledger(
                data["base_appointment_id"],
                claim_token=str(operation["id"]),
                modality=return_modality,
                now=self._now(),
            ):
                store.finish_operation(
                    operation["id"],
                    state="BLOCKED",
                    error_class="ReturnEntitlementUnavailable",
                    now=self._now(),
                )
                raise RuntimeError("free return entitlement is no longer available")
            appointment_id: int | None = None
            try:
                result = client.create_appointment(
                    paciente_id=patient_id,
                    profissional_id=self._professional_id,
                    unidade_id=self._local_id,
                    local_id=self._local_id,
                    especialidade_id=self._specialty_id,
                    procedimento_id=int(data["procedure_id"]),
                    canal_id=self._channel_id,
                    data=selected["date"],
                    horario=selected["time"],
                    valor=int(data["price"]),
                    status_id=1,
                )
                appointment_id = _extract_id(
                    result, ("agendamento_id", "appointment_id", "id")
                )
                if appointment_id is None:
                    raise RuntimeError("appointment create result has no exact id")
                self._verify_readback(
                    client.get_appointment(appointment_id),
                    appointment_id=appointment_id,
                    patient_id=patient_id,
                    data=data,
                )
            except Exception as exc:
                store.finish_operation(
                    operation["id"],
                    state="RECONCILE_REQUIRED",
                    remote_id=appointment_id,
                    error_class=type(exc).__name__,
                    now=self._now(),
                )
                store.audit(
                    "operation_requires_reconciliation",
                    operation_id=operation["id"],
                    now=self._now(),
                    metadata={
                        "kind": "CREATE_APPOINTMENT",
                        "procedure_id": int(data["procedure_id"]),
                        "state": "RECONCILE_REQUIRED",
                    },
                )
                raise
            store.finish_operation(
                operation["id"],
                state="SUCCEEDED",
                remote_id=appointment_id,
                now=self._now(),
            )
            store.audit(
                "operation_succeeded",
                operation_id=operation["id"],
                now=self._now(),
                metadata={
                    "kind": "CREATE_APPOINTMENT",
                    "procedure_id": int(data["procedure_id"]),
                    "state": "SUCCEEDED",
                    "status_id": 1,
                },
            )
        # Ledger hooks are idempotent (INSERT/UPDATE guarded by ledger
        # state) so they are safe to re-run on an idempotent replay too.
        if int(data["procedure_id"]) == 9:
            base_starts_at = datetime.combine(
                date.fromisoformat(selected["date"]),
                time.fromisoformat(selected["time"]),
                tzinfo=_BRT,
            )
            store.create_return_ledger_entry(
                appointment_id, chat_key, base_date=base_starts_at, now=self._now()
            )
        elif is_return:
            if not store.consume_return_ledger(
                data["base_appointment_id"],
                appointment_id,
                claim_token=str(operation["id"]),
                modality=return_modality,
                now=self._now(),
            ):
                raise RuntimeError("free return entitlement finalization mismatch")
        return appointment_id

    @staticmethod
    def _verify_readback(
        result: Any,
        *,
        appointment_id: int,
        patient_id: int,
        data: Mapping[str, Any],
    ) -> None:
        rows = _collection(result)
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise RuntimeError("ambiguous appointment readback")
        readback = rows[0]
        actual_id = _extract_id(readback, ("agendamento_id", "appointment_id", "id"))
        status = _extract_status_id(readback)
        if status is None:
            status = -1
        if actual_id != appointment_id or status != 1:
            raise RuntimeError("appointment readback mismatch")
        if readback.get("paciente_id") is not None and int(readback["paciente_id"]) != patient_id:
            raise RuntimeError("appointment patient readback mismatch")
        if readback.get("procedimento_id") is not None and int(
            readback["procedimento_id"]
        ) != int(data["procedure_id"]):
            raise RuntimeError("appointment procedure readback mismatch")
        selected = data["selected_slot"]
        readback_date = readback.get("data", readback.get("date"))
        readback_time = readback.get("horario", readback.get("time"))
        if (
            readback_date is None
            or _parse_date(readback_date) != _parse_date(selected["date"])
        ):
            raise RuntimeError("appointment date readback mismatch")
        if (
            readback_time is None
            or _parse_time(readback_time) != _parse_time(selected["time"])
        ):
            raise RuntimeError("appointment time readback mismatch")


async def drain_appointment_outbox(
    handler: WhatsAppAppointmentsHandler,
    adapter: Any,
    *,
    worker_id: str,
    batch_limit: int = 10,
) -> int:
    """Claim due outbox rows and deliver them through ``adapter.send``.

    Acks only on ``SendResult.success``; a failed or exceptional send
    releases the row for retry with backoff instead of losing it. This is a
    pure function of its arguments (handler + adapter), so the delivery
    contract — claim once, ack-or-release, never duplicate — is testable
    without a running gateway.
    """

    claimed = await asyncio.to_thread(
        handler.claim_outbox, worker_id=worker_id, limit=batch_limit
    )
    delivered = 0
    for item in claimed:
        if await asyncio.to_thread(
            handler.is_contact_quarantined, item["chat_key"]
        ):
            continue
        try:
            result = await adapter.send(item["chat_key"], item["body"])
            success = bool(getattr(result, "success", False))
        except Exception:
            success = False
        if success:
            acknowledged = await asyncio.to_thread(
                handler.ack_outbox,
                item["id"],
                worker_id=worker_id,
                claim_token=item["claim_token"],
            )
            if acknowledged:
                delivered += 1
        else:
            await asyncio.to_thread(
                handler.release_outbox,
                item["id"],
                worker_id=worker_id,
                claim_token=item["claim_token"],
            )
    return delivered


async def run_appointment_watcher(
    handler: WhatsAppAppointmentsHandler,
    get_adapter: Callable[[], Any],
    shutdown_event: Any,
    *,
    interval: float = 30.0,
    worker_id: str = "appointment-watcher",
) -> None:
    """Tick reservation-expiry/retention/outbox-drain until shutdown.

    Ticks under a DB-level lease (see ``process_due``/``run_retention_cleanup``
    /``claim_outbox``), so two overlapping callers of this loop — e.g. two
    gateway processes — never duplicate work. A per-tick exception is
    isolated and logged rather than killing the loop; cancellation
    (``asyncio.CancelledError``) always propagates so the caller can await a
    clean shutdown.
    """

    if not handler.watcher_enabled:
        return

    while not shutdown_event.is_set():
        try:
            await asyncio.to_thread(handler.process_due, worker_id=worker_id)
            await asyncio.to_thread(handler.run_retention_cleanup, worker_id=worker_id)
            adapter = get_adapter()
            if adapter is not None:
                await drain_appointment_outbox(handler, adapter, worker_id=worker_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WhatsApp appointment watcher tick failed")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
