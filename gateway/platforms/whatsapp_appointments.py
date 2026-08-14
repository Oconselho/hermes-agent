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
    # A conversation being opened, with no concrete request yet ("Boa noite",
    # "Alguém aí?"). Answered by this flow rather than by the model: see
    # ``_is_opener``.
    OPENER = "opener"


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


class LeadStage(str, Enum):
    """CRM funnel stages, ordered from first contact to outcome.

    The funnel is a *reporting* view over the flow the patient is already
    walking — it never gates a transition. ``FlowState`` stays the single
    source of truth for what happens next, so a bug here can misreport a
    lead but can never misroute a booking.
    """

    NOVO = "NOVO"
    QUALIFICANDO = "QUALIFICANDO"
    ESCOLHENDO_SERVICO = "ESCOLHENDO_SERVICO"
    ESCOLHENDO_HORARIO = "ESCOLHENDO_HORARIO"
    IDENTIFICANDO = "IDENTIFICANDO"
    AGUARDANDO_AUTORIZACAO = "AGUARDANDO_AUTORIZACAO"
    AGUARDANDO_PAGAMENTO = "AGUARDANDO_PAGAMENTO"
    AGENDADO = "AGENDADO"
    ATENDIMENTO_HUMANO = "ATENDIMENTO_HUMANO"
    PERDIDO = "PERDIDO"


# Every FlowState maps to exactly one funnel stage. States absent here are
# terminal bookkeeping the funnel does not distinguish; they fall back to the
# lead's current stage rather than inventing a transition.
_FLOW_STAGE_MAP: dict[str, LeadStage] = {
    FlowState.AWAITING_APPOINTMENT_ACTION.value: LeadStage.QUALIFICANDO,
    FlowState.AWAITING_SERVICE.value: LeadStage.ESCOLHENDO_SERVICO,
    FlowState.AWAITING_SLOT.value: LeadStage.ESCOLHENDO_HORARIO,
    FlowState.AWAITING_RESCHEDULE_SLOT.value: LeadStage.ESCOLHENDO_HORARIO,
    FlowState.AWAITING_RETURN_SLOT.value: LeadStage.ESCOLHENDO_HORARIO,
    FlowState.AWAITING_RETURN_MODALITY.value: LeadStage.ESCOLHENDO_SERVICO,
    FlowState.AWAITING_APPOINTMENT_SELECTION.value: LeadStage.QUALIFICANDO,
    FlowState.AWAITING_CPF.value: LeadStage.IDENTIFICANDO,
    FlowState.AWAITING_BIRTH_DATE.value: LeadStage.IDENTIFICANDO,
    FlowState.AWAITING_PHONE_CONFIRMATION.value: LeadStage.IDENTIFICANDO,
    FlowState.AWAITING_NEW_PATIENT_NAME.value: LeadStage.IDENTIFICANDO,
    FlowState.AWAITING_NEW_PATIENT_SEX.value: LeadStage.IDENTIFICANDO,
    FlowState.AWAITING_NEW_PATIENT_EMAIL.value: LeadStage.IDENTIFICANDO,
    FlowState.AWAITING_EDIT_FIELD.value: LeadStage.IDENTIFICANDO,
    FlowState.AWAITING_EDIT_VALUE.value: LeadStage.IDENTIFICANDO,
    FlowState.AWAITING_AUTHORIZATION.value: LeadStage.AGUARDANDO_AUTORIZACAO,
    FlowState.AWAITING_CANCEL_AUTHORIZATION.value: LeadStage.AGUARDANDO_AUTORIZACAO,
    FlowState.AWAITING_RESCHEDULE_AUTHORIZATION.value: LeadStage.AGUARDANDO_AUTORIZACAO,
    FlowState.AWAITING_EDIT_AUTHORIZATION.value: LeadStage.AGUARDANDO_AUTORIZACAO,
    FlowState.RESERVA_CRIADA_STATUS_1.value: LeadStage.AGUARDANDO_PAGAMENTO,
    FlowState.AGUARDANDO_COMPROVANTE.value: LeadStage.AGUARDANDO_PAGAMENTO,
    FlowState.COMPROVANTE_RECEBIDO.value: LeadStage.AGUARDANDO_PAGAMENTO,
    FlowState.AGUARDANDO_VALIDACAO.value: LeadStage.AGUARDANDO_PAGAMENTO,
    FlowState.PENDENCIA_NO_COMPROVANTE.value: LeadStage.AGUARDANDO_PAGAMENTO,
    FlowState.PAGAMENTO_VALIDADO.value: LeadStage.AGENDADO,
    FlowState.CONFIRMADO_STATUS_7.value: LeadStage.AGENDADO,
    FlowState.COMPLETED.value: LeadStage.AGENDADO,
    FlowState.HANDOFF.value: LeadStage.ATENDIMENTO_HUMANO,
    FlowState.EXCECAO_RECEPCAO.value: LeadStage.ATENDIMENTO_HUMANO,
    FlowState.RECONCILIATION_REQUIRED.value: LeadStage.ATENDIMENTO_HUMANO,
    FlowState.RESERVA_CANCELADA.value: LeadStage.PERDIDO,
    FlowState.EXPIRACAO_INICIADA.value: LeadStage.PERDIDO,
}


@dataclass(frozen=True)
class Lead:
    chat_key: str
    stage: str
    stage_entered_at: str
    first_seen_at: str
    updated_at: str
    touches: int = 0
    intent_kind: str | None = None
    service_label: str | None = None
    greeted_at: str | None = None
    lost_reason: str | None = None


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
        "leads",
        "lead_events",
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

-- CRM funnel. Deliberately PII-free: ``chat_key`` is the same opaque WhatsApp
-- identifier already stored in flow_states/contacts, and no name, CPF, phone
-- or birth date is ever written here. That keeps the funnel reportable to
-- reception (and retainable past a flow's 24h TTL) without widening the data
-- class the SPEC's RNF2 restricts.
CREATE TABLE IF NOT EXISTS leads (
    chat_key TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    stage_entered_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    touches INTEGER NOT NULL DEFAULT 0,
    intent_kind TEXT,
    service_label TEXT,
    greeted_at TEXT,
    lost_reason TEXT
);

CREATE TABLE IF NOT EXISTS lead_events (
    id TEXT PRIMARY KEY,
    chat_key TEXT NOT NULL,
    from_stage TEXT,
    to_stage TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads (stage, updated_at);
CREATE INDEX IF NOT EXISTS idx_lead_events_chat ON lead_events (chat_key, created_at);
"""

_RECONCILIATION_SUBJECT = {
    "CREATE": "do agendamento",
    "CANCEL": "do cancelamento",
    "RESCHEDULE": "da remarcação",
    "EDIT": "da atualização de cadastro",
}

_INITIAL_STATE = FlowState.AWAITING_APPOINTMENT_ACTION.value
# Routes that may open the funnel on a chat with no flow in progress. An
# appointment intent says what it wants; an opener says only that someone is
# there — both deserve the menu, and both must be answered without a model
# round trip.
_FUNNEL_ROUTES = (Route.APPOINTMENT, Route.OPENER)
# Menu layout note: one option per line, number emphasized. Author emphasis
# as MARKDOWN (``**1**``) — never as WhatsApp's own ``*1*``. The transport's
# ``WhatsAppBehaviorMixin.format_message`` reads this text as Markdown on the
# way out and converts ``**b**`` → ``*b*`` (bold); a ``*1*`` written here is
# read as Markdown *italic* and shipped as ``_1_`` instead.
#
# Both the line breaks and the emphasis used to be destroyed downstream —
# ``_whatsapp_finalize_secretary_response`` collapsed every newline and the
# sanitizer deleted every asterisk — so the menu reached patients as one
# unreadable paragraph however it was written here. Fixed in gateway/run.py
# (``_whatsapp_collapse_horizontal_space``, ``_whatsapp_normalize_emphasis``)
# and pinned by tests/gateway/test_whatsapp_menu_formatting.py, which asserts
# the bytes AFTER format_message. Keep the closing "responda com o número"
# line: it is the prompt that keeps the funnel moving to the next step.
#
# Option 6 is the way out. Without it the menu is a trap: any reply that is
# not 1-5 re-prints the menu, so a partner or a colleague who opened with
# "boa noite" would bounce off the booking list until the flow TTL expired.
# It also keeps the cold open honest — the flow now answers people who never
# said they wanted an appointment, so it has to offer them somewhere to go.
_MENU_OPTIONS = (
    "**1** - Agendar uma consulta\n"
    "**2** - Consultar ou remarcar um agendamento\n"
    "**3** - Desmarcar uma consulta\n"
    "**4** - Agendar consulta sequencial do pacote de atendimento\n"
    "**5** - Atualizar telefone ou e-mail cadastrado\n"
    "**6** - Falar sobre outro assunto\n"
    "\n"
    "Responda com o número da opção desejada."
)
# "Como posso ajudar?" and not "com o seu agendamento": this menu now also
# answers a bare "boa noite", where assuming the subject would put words in
# the patient's mouth before they said anything. The options carry the
# context on their own.
_INITIAL_MENU_BODY = "Sou a assistente do Dr. Victor Almeida. Como posso ajudar?"
# Kept for the callers and tests that want the introduction without a live
# clock or contact — ``_opening_menu`` is what production uses.
_INITIAL_MENU = f"{_INITIAL_MENU_BODY}\n\n{_MENU_OPTIONS}"
# Same menu for a chat that has already been introduced to — by this flow or
# by the model pipeline, which prefixes its own identity line. Repeating the
# self-introduction is what made the secretary read as amnesiac: a patient who
# had just been greeted got "Sou a assistente do Dr. Victor Almeida" a second
# time, as if the previous turn had not happened.
_RETURNING_MENU = "Como posso ajudar?\n" "\n" f"{_MENU_OPTIONS}"
# Answer to option 6. The flow is dropped right after this line, so the next
# message reaches the model pipeline with its nine-category classification —
# the part of the system that knows what to do with someone who is not
# booking anything.
_OTHER_SUBJECT_REPLY = (
    "Certo. Me conte, por favor, do que você precisa, que eu encaminho ao "
    "Dr. Victor."
)
# Answer to a second greeting sent while the menu is still on screen.
_MENU_NUDGE = "Estou aqui. É só responder com o número da opção que você precisa."
_SERVICE_MENU = (
    "Escolha o serviço:\n"
    "\n"
    "**1** - Consulta presencial\n"
    "**2** - Consulta presencial + 1 consulta sequencial\n"
    "**3** - Teleconsulta\n"
    "\n"
    "Responda com o número da opção desejada.\n"
    "Para saber os valores, pergunte \"quanto custa\"."
)

_SERVICES: dict[int, dict[str, Any]] = {
    1: {"procedure_id": 1, "price": 600, "label": "Consulta presencial"},
    2: {
        "procedure_id": 9,
        "price": 800,
        # Patient-facing label (shows in the booking summary). "retorno
        # gratuito" was wrong — the package includes a sequential
        # consultation, nothing free. The Feegow-side preflight still matches
        # on Feegow's OWN procedure name, which is untouched by this wording.
        "label": "Consulta presencial + 1 consulta sequencial",
    },
    3: {"procedure_id": 3, "price": 300, "label": "Teleconsulta"},
}

_PRICE_LIST_TEXT = (
    "Os valores são:\n"
    "Consulta presencial — R$ 600\n"
    "Consulta presencial + 1 consulta sequencial — R$ 800\n"
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
        r"\b(?:quero|queria|gostaria|preciso|precisava|desejo|pode|poderia|posso|da\s+pra|gostaves)\s+"
        r"(?:de\s+)?(?:agendar|marcar|remarcar|reagendar|desmarcar|agenda)\b",
        r"\b(?:agendar|marcar|remarcar|reagendar|desmarcar)\s+(?:(?:uma|um|a|o|minha|meu)\s+)?"
        r"(?:consulta|agendamento|horario|atendimento|avaliacao|retorno|teleconsulta)\b",
        r"\bcancelar\s+(?:(?:uma|a|o|minha|meu)\s+)?(?:consulta|agendamento|horario)\b",
        r"\b(?:consultar|ver|confirmar|verificar)\s+(?:(?:a|o|minha|meu)\s+)?(?:consulta|agendamento|retorno)\b",
        r"\btenho\s+(?:uma\s+)?consulta\s+(?:agendada|marcada)\b",
        r"\bverificar\s+(?:o\s+|meu\s+)?retorno\b",
        # "retorno (gratuito)" stays recognized even though the menu no longer
        # says it: patients keep using the old wording, and the funnel must
        # still route them. "consulta sequencial" is the new phrasing the menu
        # teaches, so it has to be recognized too.
        r"\bretorno\s+(?:gratuito|do\s+pacote|inclus[oa])\b",
        # "retorno" as the patient's own answer to the qualification question,
        # which offers "agendar seu retorno" in words. Deliberately NOT a bare
        # \bretorno\b: "aguardo retorno" is how partners close a message, and
        # that must not drop them into the booking menu.
        r"^retornos?$",
        r"\b(?:meu|minha)\s+retorno\b",
        r"\b(?:quero|queria|gostaria|preciso|precisava|marcar|agendar|remarcar)\s+"
        r"(?:de\s+)?(?:o\s+|meu\s+)?retorno\b",
        r"\bconsulta\s+sequencial\b",
        r"\bsequencial\s+do\s+pacote\b",
        r"\b(?:editar|atualizar|alterar)\s+(?:o\s+|meu\s+|minha\s+)?cadastro\b",
        r"\b(?:atualizar|alterar)\s+(?:meu\s+|minha\s+)?(?:telefone|celular|e-?mail)\b",
        # --- lead intents -------------------------------------------------
        # Everything below recognizes a patient who wants to be scheduled but
        # never says the verb the older patterns demanded. Each of these was
        # answered by the model pipeline before, which is where the funnel
        # leaked: the deterministic flow only ever saw the tidy phrasings.
        r"\b(?:tele\s?consulta|tele\s?medicina|consulta\s+online|atendimento\s+online|"
        r"consulta\s+(?:por\s+)?video|video\s?chamada)\b",
        r"\b(?:tem|teria|tem\s+algum|ha|havera|existe|sobrou|abriu)\s+(?:algum\s+|alguma\s+)?"
        r"(?:horario|vaga|agenda|disponibilidade)\b",
        r"\b(?:horario|vaga|agenda|disponibilidade)s?\s+(?:disponivel|disponiveis|livre|livres|para\s+quando)\b",
        r"\b(?:quanto\s+custa|qual\s+(?:o\s+)?(?:valor|preco)|valor\s+da\s+consulta|preco\s+da\s+consulta)\b",
        r"\b(?:primeira|nova)\s+consulta\b",
        r"\bconsulta\s+(?:com\s+)?(?:o\s+)?(?:dr|doutor|medico|victor)\b",
        r"\b(?:quero|queria|gostaria|preciso|precisava|procuro)\s+(?:de\s+)?"
        r"(?:uma\s+|um\s+)?(?:consulta|atendimento|avaliacao|horario|agendamento)\b",
        r"\b(?:como\s+(?:faco|fazer|marco)|onde\s+(?:marco|agendo))\b.{0,20}"
        r"\b(?:consulta|agendar|marcar|atendimento)\b",
        # Bare verb/noun as a whole message ("agendar", "agendamento",
        # "remarcar") — the commonest opening line of all, and previously
        # unrecognized because no object followed the verb.
        r"^(?:agendar|agendamento|marcar|remarcar|reagendar|desmarcar|consulta|teleconsulta)$",
    )
)

_INTENT_DEGLUE_DIGIT_LETTER_RE = re.compile(r"(?<=\d)(?=[^\W\d_])")
_INTENT_DEGLUE_LETTER_DIGIT_RE = re.compile(r"(?<=[^\W\d_])(?=\d)")
_INTENT_PUNCTUATION_RE = re.compile(r"[^\w\s-]+")


def _intent_text(value: Any) -> str:
    """Normalize a message for intent matching, ungluing runs and punctuation.

    Every pattern above is ``\\b``-anchored, and a digit is a word character,
    so a stray leading digit silently defeats the match: the real message
    ``0quero agendar`` (12/ago/2026) never matched ``\\bquero``, fell through
    to the model pipeline, and the patient got a generic reply instead of the
    menu. Patients glue menu digits onto words constantly — they are replying
    to a numbered list — so this is a routine input shape, not a typo worth
    losing a booking over. Punctuation collapses for the same reason
    (``agendar,consulta``); hyphens survive because ``e-mail`` is matched with
    one.
    """

    text = _normalize(value)
    text = _INTENT_DEGLUE_DIGIT_LETTER_RE.sub(" ", text)
    text = _INTENT_DEGLUE_LETTER_DIGIT_RE.sub(" ", text)
    text = _INTENT_PUNCTUATION_RE.sub(" ", text)
    return " ".join(text.split())


# --- the cold open ---------------------------------------------------------
#
# The first line of a real patient contact is almost never the tidy phrasing
# _APPOINTMENT_PATTERNS demands. It is "Boa noite", "Oi", "Alguém aí?",
# "preciso de informações" — a conversation being opened, before any request
# is stated. Every one of those used to fall through to the model pipeline,
# and the live test of 12/ago/2026 shows exactly what that cost:
#
#   21:21:44  "Boa note"    → [SILENCIOSO] após 10,7 s  (nada foi enviado)
#   21:21:51  "Alguém ai?"  → frase livre 8,8 s depois, sem apresentação e
#                             sem número nenhum para o paciente responder
#
# Three separate defects, one cause: the opening message is the one message
# the deterministic flow refused to look at. It is also the one message where
# the secretary must introduce itself, offer something to press, and answer
# now — a model round trip cannot do any of the three reliably.
_OPENER_ANCHORS = frozenset(
    {
        "oi", "ola", "opa", "alo", "hey", "eai", "bom", "boa", "dia", "tarde",
        "noite", "alguem", "atendimento", "atende", "atendem",
    }
)
# Words that may keep an opener company without turning it into a request.
# Deliberately small: anything outside this set means the patient said
# something concrete, which belongs to the intent patterns or to the model.
_OPENER_FILLER = frozenset(
    {
        "a", "as", "ai", "aqui", "com", "da", "de", "do", "e", "esta", "gente",
        "hoje", "la", "o", "os", "para", "pra", "por", "favor", "gentileza",
        "prezados", "senhor", "senhora", "sr", "sra", "ta", "tem", "tudo",
        "bem", "vcs", "vc", "voce", "voces", "secretaria", "consultorio",
        "clinica", "dr", "doutor", "victor", "almeida", "agora",
    }
)
_OPENER_VOCABULARY = _OPENER_ANCHORS | _OPENER_FILLER
# An opener is short by nature. The bound is what keeps the vocabulary test
# from swallowing a long message that happens to use only common words.
_OPENER_MAX_WORDS = 8
# Openings that carry a word outside the vocabulary but still state no
# request — "preciso de informações", "pode me ajudar?", "queria falar com o
# Dr. Victor". Matched explicitly, never by vocabulary.
_OPENER_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^(?:tem\s+)?(?:alguem|gente)\b",
        r"\b(?:preciso|precisava|queria|quero|gostaria)\s+(?:de\s+)?"
        r"(?:uma\s+|umas\s+|algumas\s+|mais\s+)?informa(?:cao|coes)\b",
        r"^informa(?:cao|coes)$",
        r"\b(?:pode|poderia|consegue|conseguem|podem)\s+(?:me\s+)?ajudar\b",
        r"^(?:me\s+)?ajuda(?:r)?$",
        r"^preciso\s+de\s+ajuda$",
        r"\b(?:quero|queria|gostaria|preciso|precisava)\s+(?:de\s+)?falar\s+"
        r"(?:com\s+)?(?:o\s+|a\s+)?(?:dr|doutor|victor|medico|secretaria)\b",
    )
)


# --- courtesy: who we are talking to, and when ----------------------------
#
# All three of these read the clock in BRT (America/Bahia). The secretary
# answers a clinic in Salvador, so "boa noite" has to mean night *there* —
# the server runs on UTC, three hours ahead, which would greet the whole
# 21:00-23:59 BRT stretch as if it were the next morning.
def _time_greeting(now: datetime) -> str:
    """"Bom dia" / "Boa tarde" / "Boa noite" for the local hour."""

    hour = now.hour
    if 5 <= hour < 12:
        return "Bom dia"
    if 12 <= hour < 18:
        return "Boa tarde"
    return "Boa noite"


def _closing_wish(now: datetime) -> str:
    """The sign-off: weekend wish on Friday and Saturday, week wish otherwise.

    ``weekday()`` is Monday=0 … Sunday=6, so Friday and Saturday are 4 and 5.
    Sunday belongs to the week ahead, not the weekend behind — a patient
    written to on Sunday evening is starting their week.
    """

    return "Bom final de semana!" if now.weekday() in (4, 5) else "Boa semana!"


# A WhatsApp push name is whatever the contact typed into their own phone:
# it can be a person, a company, a phone number, an emoji, or a job title.
# Only the first case may be used to address someone by name — greeting a
# laboratory as "Bom dia, Laboratório!" reads worse than not greeting at all.
_NAME_TOKEN_RE = re.compile(r"^[^\W\d_][^\W\d_'-]+$")
_NON_PERSON_NAME_MARKERS = (
    "adm", "atendimento", "clinica", "comercial", "consultorio", "contato",
    "delivery", "distribuidora", "eireli", "empresa", "epi", "farmacia",
    "financeiro", "hospital", "imobiliaria", "lab", "laboratorio", "loja",
    "ltda", "marketing", "me", "mei", "oficial", "ortopedia", "recepcao",
    "rh", "sa", "salao", "seguros", "servicos", "suporte", "telemedicina",
    "vendas",
)


def _contact_first_name(source: Any) -> str | None:
    """The contact's first name, or ``None`` when it is not safely a person.

    Conservative on purpose: anything with a digit, an organizational word,
    or a shape that is not a plain name is refused, and the greeting simply
    goes out without a name. Addressing the wrong entity by name is a worse
    failure than addressing nobody.
    """

    raw = str(
        getattr(source, "user_name", "") or getattr(source, "chat_name", "") or ""
    ).strip()
    if not raw:
        return None
    normalized = _normalize(raw)
    if any(marker in normalized for marker in _INSTITUTIONAL_MARKERS):
        return None
    if any(
        marker == word
        for marker in _NON_PERSON_NAME_MARKERS
        for word in normalized.split()
    ):
        return None
    first = raw.split()[0].strip(".,;:!?")
    # "Dr", "Dra", "Sr" and friends title someone else — skip to the name.
    if _normalize(first).rstrip(".") in {"dr", "dra", "sr", "sra", "srta"}:
        parts = raw.split()
        if len(parts) < 2:
            return None
        first = parts[1].strip(".,;:!?")
    if len(first) < 2 or not _NAME_TOKEN_RE.match(first):
        return None
    return first[:1].upper() + first[1:]


def _within_one_edit(word: str, target: str) -> bool:
    """Whether ``word`` reaches ``target`` in at most one character edit."""

    if word == target:
        return True
    if abs(len(word) - len(target)) > 1:
        return False
    if len(word) == len(target):
        return sum(a != b for a, b in zip(word, target)) == 1
    shorter, longer = (word, target) if len(word) < len(target) else (target, word)
    index = offset = 0
    while index < len(shorter):
        if shorter[index] == longer[index + offset]:
            index += 1
            continue
        if offset:
            return False
        offset = 1
    return True


def _opener_text(value: Any) -> str:
    """Normalize an opening line, repairing one-character slips in greetings.

    "Boa note" is a real message (12/ago/2026) — "boa noite" with one letter
    missing. Openers are the shortest and fastest-typed messages a patient
    sends, so a single-character slip in one is routine input, not a typo
    worth losing the whole first impression over.

    The repair is bounded on purpose: only short messages, only against the
    small opener vocabulary, and only when exactly one vocabulary word is
    within one edit. An ambiguous slip is left alone rather than guessed at,
    so this can widen what counts as a greeting but never rewrite a message
    into a request the patient did not make.
    """

    text = _intent_text(value)
    words = text.split()
    if not words or len(words) > _OPENER_MAX_WORDS:
        return text
    repaired: list[str] = []
    for word in words:
        if word in _OPENER_VOCABULARY or len(word) < 3:
            repaired.append(word)
            continue
        candidates = {
            candidate
            for candidate in _OPENER_VOCABULARY
            if _within_one_edit(word, candidate)
        }
        repaired.append(candidates.pop() if len(candidates) == 1 else word)
    return " ".join(repaired)


def _is_opener(value: Any) -> bool:
    """Whether the message opens a conversation without stating a request yet.

    Checked only AFTER the appointment patterns, so a message that says what
    it wants ("bom dia, quero agendar") is routed by its intent and never
    demoted to a bare greeting.
    """

    text = _opener_text(value)
    if not text:
        return False
    if any(pattern.search(text) for pattern in _OPENER_PATTERNS):
        return True
    words = text.split()
    if len(words) > _OPENER_MAX_WORDS:
        return False
    return bool(
        _OPENER_ANCHORS.intersection(words) and set(words).issubset(_OPENER_VOCABULARY)
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

    # ------------------------------------------------------------------
    # CRM funnel
    # ------------------------------------------------------------------

    def load_lead(self, chat_key: str) -> Lead | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT chat_key, stage, stage_entered_at, first_seen_at, updated_at,"
                " touches, intent_kind, service_label, greeted_at, lost_reason"
                " FROM leads WHERE chat_key = ?",
                (chat_key,),
            ).fetchone()
        if row is None:
            return None
        return Lead(
            chat_key=str(row[0]),
            stage=str(row[1]),
            stage_entered_at=str(row[2]),
            first_seen_at=str(row[3]),
            updated_at=str(row[4]),
            touches=int(row[5] or 0),
            intent_kind=None if row[6] is None else str(row[6]),
            service_label=None if row[7] is None else str(row[7]),
            greeted_at=None if row[8] is None else str(row[8]),
            lost_reason=None if row[9] is None else str(row[9]),
        )

    def record_lead(
        self,
        chat_key: str,
        stage: LeadStage,
        *,
        now: datetime,
        intent_kind: str | None = None,
        service_label: str | None = None,
        note: str | None = None,
        greeted: bool = False,
        lost_reason: str | None = None,
    ) -> None:
        """Upsert the lead and journal a stage change, PII-free.

        Only a *change* of stage writes a ``lead_events`` row, so the history
        reads as a funnel path rather than one row per message; ``touches``
        carries the message count instead.
        """

        stamp = now.isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT stage, intent_kind, service_label, greeted_at FROM leads"
                " WHERE chat_key = ?",
                (chat_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO leads (chat_key, stage, stage_entered_at,"
                    " first_seen_at, updated_at, touches, intent_kind,"
                    " service_label, greeted_at, lost_reason)"
                    " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (
                        chat_key,
                        stage.value,
                        stamp,
                        stamp,
                        stamp,
                        intent_kind,
                        service_label,
                        stamp if greeted else None,
                        lost_reason,
                    ),
                )
                connection.execute(
                    "INSERT INTO lead_events (id, chat_key, from_stage, to_stage,"
                    " note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _opaque_id(chat_key, stage.value, stamp),
                        chat_key,
                        None,
                        stage.value,
                        note,
                        stamp,
                    ),
                )
                return

            previous_stage = str(row[0])
            connection.execute(
                "UPDATE leads SET stage = ?, updated_at = ?, touches = touches + 1,"
                " stage_entered_at = CASE WHEN stage = ? THEN stage_entered_at ELSE ? END,"
                " intent_kind = COALESCE(?, intent_kind),"
                " service_label = COALESCE(?, service_label),"
                " greeted_at = CASE WHEN ? THEN COALESCE(greeted_at, ?) ELSE greeted_at END,"
                " lost_reason = COALESCE(?, lost_reason)"
                " WHERE chat_key = ?",
                (
                    stage.value,
                    stamp,
                    stage.value,
                    stamp,
                    intent_kind,
                    service_label,
                    1 if greeted else 0,
                    stamp,
                    lost_reason,
                    chat_key,
                ),
            )
            if previous_stage != stage.value:
                connection.execute(
                    "INSERT INTO lead_events (id, chat_key, from_stage, to_stage,"
                    " note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _opaque_id(chat_key, previous_stage, stage.value, stamp),
                        chat_key,
                        previous_stage,
                        stage.value,
                        note,
                        stamp,
                    ),
                )

    def lead_was_greeted(self, chat_key: str, *, now: datetime, within_seconds: int) -> bool:
        """Whether this chat already got the institutional self-introduction."""

        lead = self.load_lead(chat_key)
        if lead is None or not lead.greeted_at:
            return False
        try:
            greeted_at = datetime.fromisoformat(lead.greeted_at)
        except ValueError:
            return False
        if greeted_at.tzinfo is None:
            greeted_at = greeted_at.replace(tzinfo=_BRT)
        return 0 <= (now - greeted_at).total_seconds() <= within_seconds

    def mark_lead_greeted(self, chat_key: str, *, now: datetime) -> None:
        """Record that the chat has been introduced to, without a stage change."""

        stamp = now.isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE leads SET greeted_at = COALESCE(greeted_at, ?), updated_at = ?"
                " WHERE chat_key = ?",
                (stamp, stamp, chat_key),
            )

    def funnel_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT stage, COUNT(*) FROM leads GROUP BY stage"
            ).fetchall()
        return {str(stage): int(count) for stage, count in rows}

    def stale_leads(
        self, *, now: datetime, older_than_seconds: int, stages: Sequence[str]
    ) -> list[Lead]:
        """Leads parked mid-funnel with no activity — the follow-up worklist."""

        if not stages:
            return []
        cutoff = (now - timedelta(seconds=older_than_seconds)).isoformat()
        placeholders = ",".join("?" for _ in stages)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT chat_key FROM leads WHERE updated_at < ?"
                f" AND stage IN ({placeholders}) ORDER BY updated_at",
                (cutoff, *stages),
            ).fetchall()
        leads = [self.load_lead(str(row[0])) for row in rows]
        return [lead for lead in leads if lead is not None]

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
        """Atomically persist one inbox result and its resulting flow state.

        ``contact_name`` is carried across the write when the caller does not
        supply it. Every other key in ``data`` is state for the current step
        and is meant to be dropped when the step changes — the steps rebuild
        the dict freely for exactly that reason. The contact's name is not
        step state: it is a property of the chat, known only at the cold open
        (it arrives on the live event, never from Feegow) and needed much
        later, when the reception handoff notice has to say who to call.
        """

        timestamp = (now or datetime.now(timezone.utc)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT response FROM inbox_events WHERE message_id = ?", (message_id,)
            ).fetchone()
            if existing is not None:
                return str(existing[0])
            merged = dict(data)
            if not merged.get("contact_name"):
                prior = connection.execute(
                    "SELECT data_json FROM flow_states WHERE chat_key = ?",
                    (chat_key,),
                ).fetchone()
                if prior is not None:
                    try:
                        carried = json.loads(str(prior[0])).get("contact_name")
                    except (TypeError, ValueError):
                        carried = None
                    if carried:
                        merged["contact_name"] = carried
            serialized = json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
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

    def enqueue_reception_booking(
        self,
        appointment_id: int | str,
        reception_chat_id: str,
        *,
        now: datetime,
    ) -> dict[str, str] | None:
        """Tell reception a booking was made and still needs confirming.

        Fires for appointments completed WITHOUT a payment hold — today the
        in-person ones, where the patient pays on site. Reception would
        otherwise only learn of the booking by watching Feegow.

        Same privacy contract as :meth:`enqueue_reception_receipt`: the body
        carries the Feegow appointment id and nothing else. No name, CPF,
        phone, or email — reception looks the id up in Feegow, which already
        holds that data under its own access control.
        """

        identifier = str(appointment_id)
        idempotency_key = _opaque_id("reception-booking", identifier)
        outbox_id = _opaque_id("outbox", idempotency_key)
        body = (
            f"Novo agendamento {identifier} feito pelo WhatsApp. "
            "Pagamento presencial. Confirmar na Feegow."
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

    def enqueue_reception_handoff(
        self,
        chat_key: str,
        reception_chat_id: str,
        *,
        now: datetime,
        details: Mapping[str, str] | None = None,
    ) -> dict[str, str] | None:
        """Tell reception an automated booking attempt gave up on this chat.

        Until now reception only heard about appointments that COMPLETED, so a
        patient whose booking hit a dead end was told "talk to reception" while
        reception was never told anything. With the agenda read broken, that
        was every single attempt: ``outbox_events`` held zero rows.

        The dead end must be visible to a human even when the cause is a bug,
        an outage, or a gate that is deliberately off — this notice is the
        fail-safe for exactly the case where the rest of the flow cannot be
        trusted.

        Idempotent per chat per day: a patient who retries several times in one
        afternoon produces one notice, not one per message, and a new day
        raises it again because that is a genuinely new attempt.

        Unlike the booking and receipt notices, this one has no Feegow
        appointment id — nothing was created — so "reception looks it up"
        resolves to nothing at all. Anonymous, it told reception that someone
        it could not name had given up, which is not something anyone can act
        on: on 13/ago/2026 a lead who had already chosen a slot was lost that
        way. ``details`` carries what reception needs to make the call, and
        nothing more: no CPF, no birth date, and none of the patient's own
        words. Those stay in Feegow and in the chat, each under its own
        access control.
        """

        day = now.date().isoformat()
        idempotency_key = _opaque_id("reception-handoff", chat_key, day)
        outbox_id = _opaque_id("outbox", idempotency_key)
        lines = [
            "Lead parado no agendamento automático pelo WhatsApp.",
            "",
        ]
        lines.extend(f"{label}: {value}" for label, value in (details or {}).items())
        if len(lines) > 2:
            lines.append("")
        lines.append(
            "Ligar para o paciente para concluir o agendamento e esclarecer dúvidas."
        )
        body = "\n".join(lines)
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

    def enqueue_reception_clinical(
        self,
        chat_key: str,
        reception_chat_id: str,
        *,
        now: datetime,
        details: Mapping[str, str] | None = None,
    ) -> dict[str, str] | None:
        """Make the clinical refusal's promise true.

        The secretary tells the patient "vou encaminhar sua mensagem para a
        equipe do Dr. Victor" whenever it refuses clinical conduct — and until
        now that sentence had no code behind it. Nothing was queued, nothing
        was sent, and a patient who wrote about a new diabetes diagnosis on
        13/ago/2026 was told they had been forwarded to a team that never
        heard of them.

        Bucketed by the hour, not by the day like the booking dead end: a
        burst collapses into one notice, but a patient who writes again three
        hours later is raising something new and reception has to hear it.

        The patient's own words are never carried. What they wrote is clinical
        content; reception reads it in the chat, which is where it already is.
        """

        bucket = now.strftime("%Y-%m-%dT%H")
        idempotency_key = _opaque_id("reception-clinical", chat_key, bucket)
        outbox_id = _opaque_id("outbox", idempotency_key)
        lines = [
            "Assunto clínico recebido pela secretária automática no WhatsApp.",
            "",
        ]
        lines.extend(f"{label}: {value}" for label, value in (details or {}).items())
        if len(lines) > 2:
            lines.append("")
        lines.append(
            "O paciente foi informado de que a mensagem seria encaminhada à "
            "equipe do Dr. Victor. Abrir a conversa no WhatsApp para ler o "
            "teor e retornar o contato."
        )
        body = "\n".join(lines)
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
    # Institutional exclusion is matched on the raw normalization first, so
    # ungluing below can never smuggle a partner contact into the funnel.
    if any(pattern.search(_intent_text(text)) for pattern in _APPOINTMENT_PATTERNS):
        return Route.APPOINTMENT
    # Intent first, opener second: "bom dia, quero agendar" is an appointment,
    # not a greeting. Only a message that states nothing reaches this line.
    if _is_opener(text):
        return Route.OPENER
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


def _reception_send_target(value: Any) -> str:
    """The reception JID to send to, in the shape WhatsApp will deliver to.

    Shared by this class and ``gateway.run`` so the deterministic funnel and
    the model pipeline can never page two different addresses — before this,
    ``run.py`` hardcoded the working form while the config carried another.
    """

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        from gateway.whatsapp_identity import to_whatsapp_phone_jid

        return to_whatsapp_phone_jid(raw)
    except Exception:
        logger.warning("reception JID normalization failed; using it as written")
        return raw


def _chat_phone(chat_key: str) -> str:
    """Return the patient's real phone digits for a WhatsApp chat key.

    WhatsApp addresses a DM either by phone JID (``557188326547@s.whatsapp.net``)
    or by LID (``52802445381872@lid``), and LID digits are not a phone number at
    all — they are an opaque 14/15-digit identity. Reading them as a phone made
    the confirmation step reject every LID-addressed patient, because an opaque
    id can never be 10 or 11 digits long. On 13/ago/2026 that killed a lead who
    had already picked a slot and given CPF and birth date: the flow died one
    line before the write, and every chat in the base is LID-addressed.

    The bridge already writes the LID→phone mapping this needs, and
    ``gateway.run`` / ``gateway.session`` already resolve identities through it.
    A LID with no mapping file resolves to itself and still fails the length
    check downstream — an unresolvable identity must fail closed to reception,
    never be guessed at.
    """

    raw = chat_key.split("@", 1)[0]
    direct = _normalized_phone(raw)
    if not chat_key.casefold().endswith("@lid"):
        return direct
    try:
        from gateway.whatsapp_identity import canonical_whatsapp_identifier

        resolved = _normalized_phone(canonical_whatsapp_identifier(chat_key))
    except Exception:
        logger.warning("LID phone resolution failed for a booking", exc_info=True)
        return direct
    return resolved or direct


def _format_phone(digits: str) -> str:
    """``7188326547`` → ``71 8832-6547``, so reception can dial it as written."""

    if len(digits) == 11:
        return f"{digits[:2]} {digits[2:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"{digits[:2]} {digits[2:6]}-{digits[6:]}"
    return digits


def _format_moment(value: Any) -> str:
    """An ISO timestamp as ``13/08 15:48``, or empty when it is unreadable."""

    try:
        return datetime.fromisoformat(str(value)).strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return ""


# Where the patient got to before the flow gave up. Reception opens the call
# knowing what was already answered, so the patient is not asked twice.
_HANDOFF_STEP_LABELS = {
    FlowState.AWAITING_APPOINTMENT_ACTION.value: "escolha do atendimento",
    FlowState.AWAITING_SERVICE.value: "escolha do serviço",
    FlowState.AWAITING_SLOT.value: "escolha da vaga",
    FlowState.AWAITING_CPF.value: "informação do CPF",
    FlowState.AWAITING_BIRTH_DATE.value: "data de nascimento",
    FlowState.AWAITING_PHONE_CONFIRMATION.value: "confirmação do telefone",
    FlowState.AWAITING_NEW_PATIENT_NAME.value: "nome para o cadastro",
    FlowState.AWAITING_NEW_PATIENT_SEX.value: "sexo para o cadastro",
    FlowState.AWAITING_NEW_PATIENT_EMAIL.value: "e-mail para o cadastro",
    FlowState.AWAITING_AUTHORIZATION.value: "confirmação do agendamento",
    FlowState.AWAITING_APPOINTMENT_SELECTION.value: "escolha do agendamento",
    FlowState.AWAITING_RESCHEDULE_SLOT.value: "escolha da nova vaga",
    FlowState.AWAITING_RESCHEDULE_AUTHORIZATION.value: "confirmação da remarcação",
    FlowState.AWAITING_CANCEL_AUTHORIZATION.value: "confirmação do cancelamento",
    FlowState.AWAITING_RETURN_MODALITY.value: "modalidade do retorno",
    FlowState.AWAITING_RETURN_SLOT.value: "escolha da vaga de retorno",
    FlowState.AWAITING_EDIT_FIELD.value: "escolha do dado a atualizar",
    FlowState.AWAITING_EDIT_VALUE.value: "novo valor do cadastro",
    FlowState.AWAITING_EDIT_AUTHORIZATION.value: "confirmação da atualização",
    FlowState.AGUARDANDO_COMPROVANTE.value: "envio do comprovante",
}


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
        # Normalized to the ninth-digit shape its DDD actually uses.  The
        # configured value is a Bahia number written the way a human writes it
        # — ``5571996691002``, with the 9 — and WhatsApp addresses DDD 71
        # without it.  A send to the nine-digit form does not bounce; it is
        # accepted, marked SENT, and delivered to nobody.  Every notice this
        # class queues went there.
        self._reception_chat_id_configured = str(
            settings.get("reception_chat_id") or ""
        ).strip()
        self._reception_chat_id = _reception_send_target(
            self._reception_chat_id_configured
        )
        self._reception_identities_cache = None
        self._flow_ttl_seconds = max(
            60, int(settings.get("flow_ttl_hours", 24)) * 3600
        )
        # How long an institutional self-introduction stays "already said" for
        # this chat, so neither the menu nor the model pipeline repeats it.
        self._greeting_ttl_seconds = max(
            60, int(settings.get("greeting_ttl_hours", 6)) * 3600
        )
        # The cold open uses this much shorter window instead: long enough to
        # collapse one burst of messages into a single introduction, short
        # enough that a contact coming back later is greeted like the new
        # conversation it is.
        self._opener_greeting_window_seconds = max(
            60, int(settings.get("opener_greeting_window_minutes", 10)) * 60
        )
        # A patient who states a NEW explicit intent after a dead end gets the
        # funnel reopened instead of the reception line on a loop. Kept above
        # zero so a burst of messages right after the handoff still collapses
        # into the single reception answer that burst deserves.
        self._handoff_reentry_seconds = max(
            0, int(settings.get("handoff_reentry_minutes", 30)) * 60
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

    def note_model_greeting(self, incoming: Any) -> None:
        """Record that the MODEL pipeline already introduced the service.

        The two answering paths used to be blind to each other, so a patient
        greeted by the model ("Aqui é a assistente do Dr. Victor Almeida")
        was greeted again by this flow's menu two messages later — the
        amnesiac feel reported on 12/ago/2026. Only the duplicate is dropped:
        the model's own institutional disclosure is never suppressed from
        here, so the automation-disclosure requirement keeps exactly one
        owner.

        Best-effort and side-effect-free on the reply path: never creates the
        database, never touches an excluded contact, and swallows failures.
        """

        if not self._enabled or not self._db_path.exists():
            return
        try:
            if classify_route(incoming) is Route.EXCLUDED:
                return
            source = getattr(incoming, "source", None)
            chat_key = str(getattr(source, "chat_id", "") or "")
            if not chat_key:
                return
            store = AppointmentStore(self._db_path)
            if store.load_lead(chat_key) is None:
                # NOVO is "reached us, not yet qualified as a lead" — the
                # funnel's inbox. Promotion happens when intent shows up.
                store.record_lead(
                    chat_key,
                    LeadStage.NOVO,
                    now=self._now(),
                    greeted=True,
                    note="apresentacao pelo modelo",
                )
            else:
                store.mark_lead_greeted(chat_key, now=self._now())
        except Exception:
            logger.warning("model greeting not recorded", exc_info=True)

    def note_clinical_escalation(self, incoming: Any) -> None:
        """Queue the reception notice the clinical refusal promises.

        The refusal is produced by the model pipeline, not by this flow, so
        this is the seam where the promise becomes an actual message. Kept
        here because the outbox — the only delivery path reception is watching
        — belongs to this store.

        Best-effort on the reply path, exactly like ``note_model_greeting``:
        never creates the database, and a failure costs the patient nothing.
        The patient still gets the refusal either way; what fails is only
        reception hearing about it, and that is logged loudly because it is
        the whole point of the call.
        """

        if not self._enabled or not self._reception_chat_id:
            return
        if not self._db_path.exists():
            return
        try:
            source = getattr(incoming, "source", None)
            chat_key = str(getattr(source, "chat_id", "") or "")
            if not chat_key or self._is_reception_chat(chat_key):
                return
            details: dict[str, str] = {}
            phone = _format_phone(_chat_phone(chat_key))
            name = _contact_first_name(source)
            if phone:
                details["Contato"] = f"{name} — {phone}" if name else phone
            details["Recebido"] = self._now().strftime("%d/%m %H:%M")
            AppointmentStore(self._db_path).enqueue_reception_clinical(
                chat_key,
                self._reception_chat_id,
                now=self._now(),
                details=details,
            )
        except Exception:
            logger.warning(
                "clinical escalation notice could not be queued", exc_info=True
            )

    def _menu_for(
        self,
        store: AppointmentStore,
        chat_key: str,
        *,
        within_seconds: int | None = None,
        source: Any = None,
    ) -> str:
        """The action menu, introducing the service only on a cold chat.

        Every re-prompt inside an open flow is by definition a chat that has
        already been greeted, so this is what stops the menu from opening
        with "Sou a assistente do Dr. Victor Almeida" over and over in the
        same conversation. ``within_seconds`` overrides how far back a
        greeting still counts; the cold open passes a much shorter window
        (see ``handle``) and the ``source`` it was greeted from, so the
        introduction can open with the hour and the contact's own name.
        """

        try:
            already_greeted = store.lead_was_greeted(
                chat_key,
                now=self._now(),
                within_seconds=(
                    self._greeting_ttl_seconds if within_seconds is None else within_seconds
                ),
            )
        except Exception:
            logger.warning("greeting lookup failed", exc_info=True)
            already_greeted = False
        if already_greeted:
            return _RETURNING_MENU
        return f"{self._opening_line(source)}\n\n{_MENU_OPTIONS}"

    def _opening_line(self, source: Any = None) -> str:
        """The first line of a cold open: the hour, the name, the identity.

        "Sou a assistente do Dr. Victor Almeida" alone is correct and cold.
        A clinic's first line is a greeting to a person, so it leads with the
        time of day and the contact's own name when WhatsApp gives us one it
        is safe to use — and degrades quietly to the bare introduction when
        it does not.
        """

        greeting = _time_greeting(self._now())
        name = _contact_first_name(source) if source is not None else None
        opening = f"{greeting}, {name}!" if name else f"{greeting}!"
        return f"{opening} {_INITIAL_MENU_BODY}"

    def already_greeted(self, source: Any) -> bool:
        """Whether THIS flow has already introduced itself in this chat.

        The mirror of ``note_model_greeting``, and the direction that was
        missing: the funnel's replies never enter the model's transcript
        (``state.db`` holds only the model's own turns), so the model had no
        way to know the secretary had just said "Sou a assistente do Dr.
        Victor Almeida" one message earlier. It introduced itself again, and
        the patient met the same secretary twice — the exact complaint of
        13/ago/2026.

        Read-only and best-effort: never creates the database, and any
        failure answers "not greeted", which at worst repeats a greeting
        rather than suppressing a required disclosure.
        """

        if not self._enabled or not self._db_path.exists():
            return False
        try:
            chat_key = str(getattr(source, "chat_id", "") or "")
            if not chat_key:
                return False
            return AppointmentStore(self._db_path).lead_was_greeted(
                chat_key,
                now=self._now(),
                within_seconds=self._greeting_ttl_seconds,
            )
        except Exception:
            logger.warning("greeting lookup failed", exc_info=True)
            return False

    def _sign_off(self, message: str) -> str:
        """Append the closing wish to a message that ends the conversation.

        Only the replies that actually close something get this — the
        reception hand-off, the finished booking, the exit to another
        subject. Adding it to a mid-flow prompt would wish the patient a good
        week while still asking them for a CPF.
        """

        return f"{message} {_closing_wish(self._now())}"

    def _is_reception_chat(self, chat_key: str) -> bool:
        """Whether this chat is the reception's own notification channel.

        Sending has exactly one right answer — the shape WhatsApp delivers to
        — but recognising has several: reception's own messages can arrive
        under the configured spelling, under the normalized one, or, as every
        chat in production does, under a LID. Matching only the send target
        would hand reception the booking menu, which the rule this guards
        exists to prevent.
        """

        if not self._reception_chat_id:
            return False
        return _digits(chat_key) in self._reception_identities()

    def _reception_identities(self) -> frozenset:
        """Every digit-form that means "this is reception"."""

        cached = getattr(self, "_reception_identities_cache", None)
        if cached is not None:
            return cached
        forms = {
            _digits(self._reception_chat_id),
            _digits(self._reception_chat_id_configured),
            _digits(_chat_phone(self._reception_chat_id)),
        }
        try:
            from gateway.whatsapp_identity import expand_whatsapp_aliases

            for alias in expand_whatsapp_aliases(self._reception_chat_id):
                forms.add(_digits(alias))
        except Exception:
            logger.warning("reception alias expansion failed", exc_info=True)
        identities = frozenset(form for form in forms if form)
        self._reception_identities_cache = identities
        return identities

    def _menu_is_fresh(self, flow: FlowSnapshot) -> bool:
        """Whether the menu was sent recently enough to still be on screen."""

        if not flow.updated_at:
            return False
        try:
            updated_at = datetime.fromisoformat(flow.updated_at)
        except ValueError:
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=_BRT)
        return (
            self._now() - updated_at
        ).total_seconds() <= self._opener_greeting_window_seconds

    def _handoff_is_reopenable(self, flow: FlowSnapshot, route: Route) -> bool:
        """Whether a handed-off chat may restart the funnel on this message.

        Requires BOTH a fresh unambiguous appointment intent and a cooling
        period since the handoff, so the reopen answers a patient who came
        back with a new request — not the next line of the same conversation
        that just failed.
        """

        if flow.state != FlowState.HANDOFF.value or route is not Route.APPOINTMENT:
            return False
        if not flow.updated_at:
            return False
        try:
            updated_at = datetime.fromisoformat(flow.updated_at)
        except ValueError:
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=_BRT)
        return (
            self._now() - updated_at
        ).total_seconds() >= self._handoff_reentry_seconds

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

        if route is Route.OPENER and self._is_reception_chat(chat_key):
            # The reception's own chat is this flow's outbound notification
            # channel, not a patient. Answering "bom dia" from it with the
            # booking menu would point the clinic's own number at a funnel
            # built for patients. An explicit appointment intent from that
            # number is left alone — it routes exactly as it did before.
            route = Route.OUT_OF_SCOPE

        if route not in _FUNNEL_ROUTES and not self._db_path.exists():
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
        if flow is not None and self._handoff_is_reopenable(flow, route):
            # A dead end must not become a permanent one. Before this, every
            # later message from a handed-off chat re-answered the reception
            # line for the whole 24h flow TTL — including a patient coming
            # back the next hour with a brand new, perfectly bookable request.
            store.purge_flow(chat_key)
            flow = None

        if route not in _FUNNEL_ROUTES and flow is None:
            return None

        message_id = _event_identity(incoming, chat_key)
        prior = store.inbox_response(message_id)
        if prior is not None:
            return prior

        if flow is None:
            # A cold open re-introduces the secretary. ``greeting_ttl_hours``
            # is the wrong clock here: it exists to stop the flow repeating
            # "Sou a assistente do Dr. Victor Almeida" *inside* an exchange,
            # and with no flow in progress there is no exchange to be inside
            # of. On 12/ago/2026 the long TTL turned into the opposite bug —
            # a contact reopened hours later got a menu from a secretary that
            # never said who it was. The short window still collapses the
            # double introduction the two pipelines can produce within one
            # burst, which is the case the TTL was actually written for.
            menu = self._menu_for(
                store,
                chat_key,
                within_seconds=self._opener_greeting_window_seconds,
                source=source,
            )
            # The contact's name lives only in the live event, and the
            # reception handoff notice is built much later, from the store
            # alone. Carrying it in the flow is what lets that notice name who
            # to call. ``leads`` is deliberately name-free (see its schema
            # comment); ``flow_states`` already holds the patient's name, CPF
            # and phone under the same 24h TTL, so this adds no data class.
            opening_data: dict[str, Any] = {}
            contact_name = _contact_first_name(source)
            if contact_name:
                opening_data["contact_name"] = contact_name
            self._track_lead(
                store,
                chat_key,
                FlowState.AWAITING_APPOINTMENT_ACTION,
                opening_data,
                greeted=True,
                note="entrada no funil",
            )
            return store.record_response(
                message_id,
                chat_key,
                menu,
                FlowState.AWAITING_APPOINTMENT_ACTION.value,
                opening_data,
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
                # Failing closed is right; failing closed in silence is not.
                # Every dead end reception was told about on 13/ago/2026 left
                # no trace of its cause, so the reason had to be reconstructed
                # from the databases afterwards. State is not logged — only
                # the exception — because the flow's state carries patient data.
                logger.warning(
                    "payment proof handling failed; reception notified",
                    exc_info=True,
                )
                return self._handoff(store, message_id, chat_key)

        text = str(getattr(incoming, "text", "") or "").strip()
        try:
            return self._advance(store, flow, message_id, chat_key, text)
        except Exception:
            logger.warning(
                "appointment flow failed; reception notified", exc_info=True
            )
            return self._handoff(store, message_id, chat_key)

    def _track_lead(
        self,
        store: AppointmentStore,
        chat_key: str,
        state: FlowState,
        data: Mapping[str, Any] | None = None,
        *,
        greeted: bool = False,
        note: str | None = None,
    ) -> None:
        """Move the CRM funnel alongside the flow, never blocking the reply.

        The funnel is observational: a failure here must cost the patient
        nothing, so it is logged and swallowed exactly like the reception
        notice in ``_handoff``.
        """

        try:
            stage = _FLOW_STAGE_MAP.get(state.value)
            if stage is None:
                lead = store.load_lead(chat_key)
                if lead is None:
                    return
                stage = LeadStage(lead.stage)
            service_label = None
            if isinstance(data, Mapping):
                raw_label = data.get("service_label")
                if raw_label:
                    service_label = str(raw_label)
            store.record_lead(
                chat_key,
                stage,
                now=self._now(),
                service_label=service_label,
                greeted=greeted,
                note=note,
                lost_reason=note if stage is LeadStage.PERDIDO else None,
            )
        except Exception:
            logger.warning("lead funnel update failed", exc_info=True)

    def _respond(
        self,
        store: AppointmentStore,
        message_id: str,
        chat_key: str,
        response: str,
        state: FlowState,
        data: Mapping[str, Any],
    ) -> str:
        self._track_lead(store, chat_key, state, data)
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
        # Queueing the notice must never cost the patient their reply, so a
        # failure here is logged and swallowed — same contract as the booking
        # notice in _complete_authorized_appointment.
        if self._reception_chat_id:
            try:
                store.enqueue_reception_handoff(
                    chat_key,
                    self._reception_chat_id,
                    now=self._now(),
                    details=self._handoff_details(store, chat_key),
                )
            except Exception:
                logger.warning(
                    "reception handoff notice could not be queued", exc_info=True
                )
        return self._respond(
            store, message_id, chat_key, self._sign_off(_RECEPTION),
            FlowState.HANDOFF, {},
        )

    def _handoff_details(
        self, store: AppointmentStore, chat_key: str
    ) -> dict[str, str]:
        """What reception needs to chase this lead by phone.

        Read before ``_respond`` wipes the flow, because the answers the
        patient already gave — which service, which slot, how far they got —
        are exactly what makes the call worth making. Best-effort: a notice
        with only a phone number still beats the anonymous one it replaces.
        """

        details: dict[str, str] = {}
        phone = _format_phone(_chat_phone(chat_key))
        if phone:
            details["Contato"] = phone
        try:
            flow = store.load_flow(chat_key)
            lead = store.load_lead(chat_key)
        except Exception:
            logger.warning("handoff details unavailable", exc_info=True)
            return details

        if flow is not None:
            name = str(flow.data.get("contact_name") or "").strip()
            if name and phone:
                details["Contato"] = f"{name} — {phone}"
            slot = flow.data.get("selected_slot")
            if isinstance(slot, Mapping):
                chosen = " às ".join(
                    part
                    for part in (
                        str(slot.get("display_date") or "").strip(),
                        str(slot.get("time") or "").strip(),
                    )
                    if part
                )
                if chosen:
                    details["Vaga escolhida"] = chosen
            step = _HANDOFF_STEP_LABELS.get(flow.state)
            if step:
                details["Parou em"] = step
        if lead is not None:
            if lead.service_label:
                details["Interesse"] = str(lead.service_label)
            seen = _format_moment(lead.first_seen_at)
            answered = _format_moment(lead.updated_at)
            if seen and answered and seen != answered:
                details["Respostas"] = f"de {seen} a {answered}"
            elif seen or answered:
                details["Respostas"] = str(answered or seen)
        return details

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
                f"Ainda não consegui confirmar o resultado "
                f"{_RECONCILIATION_SUBJECT.get(kind, 'da operação')} com segurança. "
                "Responda RECONCILIAR novamente ou fale com a recepção."
            )
        else:
            response = (
                f"Ainda estou confirmando o resultado "
                f"{_RECONCILIATION_SUBJECT.get(kind, 'da operação')} com o sistema. "
                "Responda RECONCILIAR para eu verificar, sem duplicar o pedido."
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
            response = f"Agendamento {appointment_id} desmarcado."
        elif kind == "RESCHEDULE":
            completed = {
                "appointment_id": appointment_id,
                "completion_kind": "RESCHEDULE",
            }
            response = f"Agendamento {appointment_id} remarcado."
        else:
            completed = {"completion_kind": "EDIT"}
            label = "telefone" if data["edit_field"] == "telefone" else "e-mail"
            response = f"Cadastro atualizado: {label} alterado com sucesso."
        return self._respond(
            store,
            message_id,
            chat_key,
            self._sign_off(response),
            FlowState.COMPLETED,
            completed,
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
                        f"Ainda estou confirmando o resultado "
                        f"{_RECONCILIATION_SUBJECT.get(data.get('reconcile_kind'), 'da operação')} "
                        "com o sistema. Responda RECONCILIAR para eu verificar, sem duplicar "
                        "o pedido."
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
                    f"Agendamento {data['appointment_id']} desmarcado."
                )
            elif data.get("completion_kind") == "RESCHEDULE":
                response = (
                    f"Agendamento {data['appointment_id']} remarcado."
                )
            elif data.get("completion_kind") == "EDIT":
                response = "Cadastro já atualizado. Nenhuma alteração adicional foi feita."
            else:
                response = (
                    "Seu pedido já foi registrado e será confirmado pela recepção."
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
                    f"{_PRICE_LIST_TEXT}\n\n{self._menu_for(store, chat_key)}",
                    FlowState.AWAITING_APPOINTMENT_ACTION,
                    data,
                )
            if _is_opener(text) and self._menu_is_fresh(flow):
                # Still saying hello, or nudging because the menu has not been
                # read yet. The real burst was "Boa note" and "Alguém ai?"
                # seven seconds apart (12/ago/2026): re-printing the whole
                # list at every nudge is exactly what makes a secretary read
                # as amnesiac, so answer the nudge and leave the list where
                # the patient can already see it.
                return self._respond(
                    store,
                    message_id,
                    chat_key,
                    _MENU_NUDGE,
                    FlowState.AWAITING_APPOINTMENT_ACTION,
                    data,
                )
            if normalized == "6":
                # Leave the funnel instead of looping the menu. The response
                # is recorded (so the delivery stays deduplicated) and the
                # flow row is then dropped, which is what lets the next
                # message be classified by the model pipeline as usual.
                response = store.record_response(
                    message_id,
                    chat_key,
                    _OTHER_SUBJECT_REPLY,
                    FlowState.COMPLETED.value,
                    {},
                    now=self._now(),
                )
                # No sign-off here: this line hands the conversation over and
                # asks the patient to keep talking. Wishing them a good week
                # in the same breath would be a goodbye and a question at once.
                self._track_lead(
                    store,
                    chat_key,
                    FlowState.HANDOFF,
                    {},
                    note="saiu do funil: outro assunto",
                )
                store.purge_flow(chat_key)
                return response
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
                    self._menu_for(store, chat_key),
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
            lines = ["Escolha uma das vagas disponíveis (horário de Brasília):", ""]
            lines.extend(
                f"**{index}** - {slot['display_date']} às {slot['time']}"
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
            # An unreadable base says nothing about this CPF. ``find_patient``
            # reads strictly for exactly that reason: an empty list here means
            # "no such patient" and books a brand new one, so a failed read
            # must raise and fail closed rather than register a second record
            # for someone Feegow already has.
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
                    "Escolha a nova vaga para o mesmo procedimento (horário de Brasília):",
                    "",
                ]
                lines.extend(
                    f"**{slot_index}** - {slot['display_date']} às {slot['time']}"
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
            lines = ["Escolha uma das vagas disponíveis (horário de Brasília):", ""]
            lines.extend(
                f"**{index}** - {slot['display_date']} às {slot['time']}"
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
        lines = ["Agendamentos futuros encontrados:", ""]
        lines.extend(
            (
                f"**{index}** - {appointment['display_date']} "
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
        # "retorno" kept alongside the new wording — the menu changed, the
        # vocabulary patients arrive with did not.
        if normalized in {
            "2", "800", "r$ 800", "pacote", "retorno", "sequencial",
            "consulta sequencial",
        }:
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
                f"Agendamento {appointment_id} criado.\n"
                f"Prazo para envio do comprovante: {deadline_text}.\n"
                f"**Beneficiário:** {self._payment_beneficiary}\n"
                f"**Pagamento:** {self._payment_instructions}\n"
                "Envie a imagem ou o PDF do comprovante até o prazo."
            )
        else:
            # No payment hold: the patient pays on site, so nothing later in
            # the flow would ever tell reception this booking exists. Queue
            # the notice on the same outbox the payment-proof notice uses —
            # it is idempotent by appointment id and retried by the watcher,
            # so a delivery failure here cannot lose the booking or the
            # patient's confirmation below.
            if self._reception_chat_id:
                try:
                    store.enqueue_reception_booking(
                        appointment_id,
                        self._reception_chat_id,
                        now=self._now(),
                    )
                except Exception:
                    logger.warning(
                        "reception booking notice could not be queued",
                        exc_info=True,
                    )
            response = (
                f"Agendamento {appointment_id} criado. "
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
                f"**Beneficiário:** {self._payment_beneficiary}\n"
                f"**Pagamento:** {self._payment_instructions}\n"
                "A reserva será criada. O prazo do comprovante expira "
                "automaticamente e, sem recebimento no prazo, a reserva será cancelada.\n"
                "Responda CONFIRMAR para autorizar uma única vez ou ALTERAR."
            )
        else:
            response = (
                "Resumo do agendamento:\n"
                f"{data['service_label']} — R$ {data['price']}\n"
                f"{selected['display_date']} às {selected['time']} (Brasília)\n"
                f"{new_patient_notice}"
                "A recepção fará a confirmação final.\n"
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
