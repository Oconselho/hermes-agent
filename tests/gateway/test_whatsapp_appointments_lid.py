"""The booking flow must work when WhatsApp addresses the patient by LID.

Regression for the 13/ago/2026 lost lead. WhatsApp delivers a DM under either
a phone JID (``557188326547@s.whatsapp.net``) or a LID
(``52802445381872@lid``), and in production it is *always* the LID form — every
row in ``leads`` is ``@lid``. The phone-confirmation gate read those LID digits
as a phone number, which can only ever be 14-15 digits long, so it failed the
``{10, 11}`` length check and handed every single booking to reception. A
patient who had already chosen a slot and given CPF and birth date died one
line before the write.

The whole suite missed it because every helper addresses the patient by phone
JID (``appointment_helpers.CHAT_KEY``), which is the one shape production never
sends. These tests use the LID shape on purpose.

The same class of bug already hit the DM allowlist (see
``test_whatsapp_allowlist_lid_resolution``); both now resolve through the
bridge's ``lid-mapping-*.json`` files via ``gateway.whatsapp_identity``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.whatsapp_appointments import (
    WhatsAppAppointmentsHandler,
    _chat_phone,
)
from hermes_constants import get_hermes_home
from tests.gateway.appointment_helpers import (
    BIRTH_DATE,
    CPF,
    CPF_FORMATTED,
    FakeFeegow,
    MutableClock,
    event,
    known_patient,
    payment_config,
)

BRT = ZoneInfo("America/Bahia")
NOW = datetime(2026, 8, 1, 10, 0, tzinfo=BRT)

# The shapes production actually produces: an opaque LID, and the phone the
# bridge maps it back to. Deliberately an 8-digit local number (55 + DDD 71 +
# 8 digits = 12), like the lead that was lost.
LID = "52802445381872"
LID_CHAT_KEY = f"{LID}@lid"
PHONE_WITH_COUNTRY = "557188326547"
PHONE = "7188326547"
RECEPTION = "5571996691002@s.whatsapp.net"

PROC1 = {"procedimento_id": 1, "nome": "Consulta", "valor": 600}
SLOT = {"id": "slot-1", "procedimento_id": 1, "data": "2026-08-05", "horario": "14:00"}

# The dead-end reply itself. "recepção" alone is useless as a marker: the
# legitimate booking summary also mentions reception ("a recepção fará a
# confirmação final").
DEAD_END = "Não foi possível concluir este agendamento com segurança"


def _write_lid_mapping(phone=PHONE_WITH_COUNTRY, lid=LID):
    """Mirror what the JS bridge writes: phone→lid and lid→phone (reverse)."""
    session_dir = get_hermes_home() / "whatsapp" / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / f"lid-mapping-{phone}.json").write_text(
        json.dumps(lid), encoding="utf-8"
    )
    (session_dir / f"lid-mapping-{lid}_reverse.json").write_text(
        json.dumps(phone), encoding="utf-8"
    )


def _build(feegow, tmp_path):
    return WhatsAppAppointmentsHandler(
        payment_config(),
        db_path=tmp_path / "appointments.sqlite3",
        proofs_dir=tmp_path / "proofs",
        feegow_client=feegow,
        clock=MutableClock(NOW),
    )


def _drive_to_phone_confirmation(handler, prefix, chat_key=LID_CHAT_KEY):
    """Menu → consulta presencial → vaga → CPF → nascimento → SIM."""
    for index, text in enumerate(
        ("Quero agendar uma consulta", "1", "1", "1", CPF_FORMATTED, BIRTH_DATE),
        start=1,
    ):
        handler.handle(
            event(text, message_id=f"{prefix}-{index}", chat_id=chat_key)
        )
    return handler.handle(event("SIM", message_id=f"{prefix}-7", chat_id=chat_key))


# ------------------------------------------------------------------ resolution


def test_lid_chat_key_resolves_to_the_mapped_phone():
    _write_lid_mapping()

    assert _chat_phone(LID_CHAT_KEY) == PHONE


def test_phone_jid_needs_no_mapping_and_is_unchanged():
    assert _chat_phone("5571988877766@s.whatsapp.net") == "71988877766"


def test_unmapped_lid_keeps_failing_closed():
    """No mapping file means no identity — never a guessed phone number."""
    resolved = _chat_phone("999999999999999@lid")

    assert resolved == "999999999999999"
    assert len(resolved) not in {10, 11}


# ------------------------------------------------------------------- the lead


def test_booking_addressed_by_lid_reaches_the_summary(tmp_path):
    """The lost lead, replayed: LID chat, known patient, must not hit reception."""
    _write_lid_mapping()
    feegow = FakeFeegow(
        slots=[dict(SLOT)],
        patients=[known_patient(celular=PHONE)],
        procedures=[dict(PROC1)],
    )
    handler = _build(feegow, tmp_path)

    reply = _drive_to_phone_confirmation(handler, "lid-create")

    assert DEAD_END not in reply
    assert "CONFIRMAR" in reply


def test_lid_without_a_mapping_fails_closed_to_reception(tmp_path):
    """An unresolvable identity is a dead end, not a booking on a guessed number."""
    feegow = FakeFeegow(
        slots=[dict(SLOT)],
        patients=[known_patient(celular=PHONE)],
        procedures=[dict(PROC1)],
    )
    handler = _build(feegow, tmp_path)

    reply = _drive_to_phone_confirmation(handler, "lid-nomap")

    assert DEAD_END in reply


# --------------------------------------------------- unreadable ≠ nonexistent


class _UnreadablePatients(FakeFeegow):
    """Feegow answers the CPF lookup with an error, as it did with a 409."""

    def find_patient_by_cpf(self, cpf):
        self.calls.append(("find_patient_by_cpf", {"cpf": cpf}))
        raise RuntimeError("Conflito informado pela API Feegow.")


def test_unreadable_patient_base_goes_to_reception_instead_of_creating_a_patient(
    tmp_path,
):
    """An API failure must never be read as "this CPF has no record"."""
    _write_lid_mapping()
    feegow = _UnreadablePatients(
        slots=[dict(SLOT)],
        patients=[known_patient(celular=PHONE)],
        procedures=[dict(PROC1)],
    )
    handler = _build(feegow, tmp_path)

    for index, text in enumerate(
        ("Quero agendar uma consulta", "1", "1", "1", CPF_FORMATTED),
        start=1,
    ):
        handler.handle(
            event(text, message_id=f"unread-{index}", chat_id=LID_CHAT_KEY)
        )
    reply = handler.handle(
        event(BIRTH_DATE, message_id="unread-6", chat_id=LID_CHAT_KEY)
    )

    assert DEAD_END in reply
    # It must not have walked on as a brand new patient.
    assert "nome completo" not in reply.lower()
    assert [name for name, _ in feegow.calls if name == "create_patient"] == []


# --------------------------------------------------------- the notice content


def _outbox(tmp_path):
    with sqlite3.connect(tmp_path / "appointments.sqlite3") as connection:
        return connection.execute(
            "SELECT chat_key, body, state FROM outbox_events ORDER BY created_at"
        ).fetchall()


def _drive_to_dead_end(handler):
    """Karina's exact path: menu → serviço → vaga → CPF → nascimento → beco."""
    for index, text in enumerate(
        ("Quero agendar uma consulta", "1", "1", "1", CPF_FORMATTED, BIRTH_DATE),
        start=1,
    ):
        handler.handle(
            event(
                text,
                message_id=f"notice-{index}",
                chat_id=LID_CHAT_KEY,
                user_name="Karina Costa",
            )
        )


def test_dead_end_notice_tells_reception_who_to_call_and_what_was_answered(tmp_path):
    """The anonymous notice reception could not act on, replaced."""
    _write_lid_mapping()
    handler = _build(
        _UnreadablePatients(
            slots=[dict(SLOT)],
            patients=[known_patient(celular=PHONE)],
            procedures=[dict(PROC1)],
        ),
        tmp_path,
    )
    handler._reception_chat_id = RECEPTION

    _drive_to_dead_end(handler)

    (chat_key, body, state), = _outbox(tmp_path)
    assert (chat_key, state) == (RECEPTION, "PENDING")
    assert "Karina" in body
    # The dialable form, not WhatsApp's: DDD 71 is addressed without the ninth
    # digit, but a receptionist reading "71 8832-6547" off a phone sees a
    # number that looks truncated and cannot be dialled.
    assert "71 98832-6547" in body
    assert "Consulta presencial" in body
    assert "05/08/2026 às 14:00" in body
    assert "data de nascimento" in body
    assert "Ligar para o paciente" in body


def test_dead_end_notice_identifies_the_patient_it_is_about(tmp_path):
    """Reception was given the identification it needs to open the call.

    Withheld until 15/ago/2026 under a contract that told reception to look
    the patient up in Feegow instead — but this notice fires precisely when
    the booking never got that far, so there was nothing to look up. Victor
    asked for name, birth date, CPF and Feegow id in the message itself.
    """
    _write_lid_mapping()
    handler = _build(
        _UnreadablePatients(
            slots=[dict(SLOT)],
            patients=[known_patient(celular=PHONE)],
            procedures=[dict(PROC1)],
        ),
        tmp_path,
    )
    handler._reception_chat_id = RECEPTION

    _drive_to_dead_end(handler)

    (_, body, _), = _outbox(tmp_path)
    assert f"CPF: {CPF_FORMATTED}" in body
    # And a link that opens the patient's chat without retyping a number.
    assert "Abrir conversa: https://wa.me/557188326547" in body


def test_one_dead_end_notice_per_chat_per_day(tmp_path):
    """A patient who retries all afternoon is one call for reception, not six."""
    _write_lid_mapping()
    handler = _build(
        _UnreadablePatients(
            slots=[dict(SLOT)],
            patients=[known_patient(celular=PHONE)],
            procedures=[dict(PROC1)],
        ),
        tmp_path,
    )
    handler._reception_chat_id = RECEPTION

    _drive_to_dead_end(handler)
    handler.handle(
        event("Oi?", message_id="notice-again", chat_id=LID_CHAT_KEY)
    )

    assert len(_outbox(tmp_path)) == 1


# ------------------------------------------------------------ clinical promise


def test_clinical_refusal_actually_reaches_reception(tmp_path):
    """"Vou encaminhar para a equipe" had no code behind it until now."""
    _write_lid_mapping()
    handler = _build(FakeFeegow(), tmp_path)
    handler._reception_chat_id = RECEPTION
    # The store only exists once the flow has touched it.
    handler.handle(
        event("Quero agendar uma consulta", message_id="clin-0", chat_id=LID_CHAT_KEY)
    )

    handler.note_clinical_escalation(
        event("meu marido descobriu diabetes", chat_id=LID_CHAT_KEY, user_name="Karina Costa")
    )

    body = _outbox(tmp_path)[-1][1]
    assert "Assunto clínico" in body
    assert "Karina" in body
    assert "71 98832-6547" in body
    assert "Abrir conversa: https://wa.me/557188326547" in body
    # The clinical content itself stays in the chat, not in the notice.
    assert "diabetes" not in body.lower()


def test_clinical_refusal_from_a_partner_company_never_pages_reception(tmp_path):
    """Reception's WhatsApp is for patients — Victor, 15/ago/2026.

    The clinical refusal goes to whoever raises a clinical subject, partner
    companies included. On 14/ago/2026 at 22:06 BRT a telemedicine partner
    asking after one of its own members was paged straight to reception one
    second after the refusal went out — the second route by which a partner
    reached reception that day, and the one the scheduling-keyword fix does
    not cover. The partner still gets the refusal; reception is not told.
    """
    _write_lid_mapping()
    handler = _build(FakeFeegow(), tmp_path)
    handler._reception_chat_id = RECEPTION
    handler.handle(
        event("Quero agendar uma consulta", message_id="clin-p0", chat_id=LID_CHAT_KEY)
    )
    before = len(_outbox(tmp_path))

    handler.note_clinical_escalation(
        event(
            "Você tem algum retorno referente a vida, Kamylla Rodrigues?",
            chat_id=LID_CHAT_KEY,
            user_name="Rapidoc Telemedicina",
        )
    )

    assert len(_outbox(tmp_path)) == before


def test_clinical_notice_collapses_a_burst_but_not_a_later_message(tmp_path):
    _write_lid_mapping()
    clock = MutableClock(NOW)
    handler = WhatsAppAppointmentsHandler(
        payment_config(),
        db_path=tmp_path / "appointments.sqlite3",
        proofs_dir=tmp_path / "proofs",
        feegow_client=FakeFeegow(),
        clock=clock,
    )
    handler._reception_chat_id = RECEPTION
    handler.handle(
        event("Quero agendar uma consulta", message_id="clin-1", chat_id=LID_CHAT_KEY)
    )

    handler.note_clinical_escalation(event("a", chat_id=LID_CHAT_KEY))
    handler.note_clinical_escalation(event("b", chat_id=LID_CHAT_KEY))
    assert sum("Assunto clínico" in body for _, body, _ in _outbox(tmp_path)) == 1

    clock.value += timedelta(hours=3)
    handler.note_clinical_escalation(event("c", chat_id=LID_CHAT_KEY))
    assert sum("Assunto clínico" in body for _, body, _ in _outbox(tmp_path)) == 2


def test_reception_chat_never_notifies_itself(tmp_path):
    """The notification channel is not a patient."""
    handler = _build(FakeFeegow(), tmp_path)
    handler._reception_chat_id = RECEPTION
    handler.handle(
        event("Quero agendar uma consulta", message_id="self-1", chat_id=LID_CHAT_KEY)
    )

    handler.note_clinical_escalation(event("x", chat_id=RECEPTION))

    assert [body for _, body, _ in _outbox(tmp_path) if "Assunto clínico" in body] == []


# ------------------------------------------------------- the strict client read


def test_find_patient_by_cpf_propagates_instead_of_answering_empty():
    """``search_patients`` degrades gracefully; the identity gate must not."""
    from gateway.platforms.feegow_api import FeegowAPIError, FeegowClient

    client = FeegowClient(token="t", write_enabled=False)

    def boom(*args, **kwargs):
        raise FeegowAPIError("Conflito informado pela API Feegow.", status_code=409)

    client._request = boom

    assert client.search_patients(cpf=CPF) == []
    with pytest.raises(FeegowAPIError):
        client.find_patient_by_cpf(CPF)
