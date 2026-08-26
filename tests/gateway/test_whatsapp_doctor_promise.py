"""A promise that names Dr. Victor has to reach Dr. Victor.

RICKY (73 8131-2183) asked for a psychiatry referral on 26/ago/2026 at 17:51
BRT and was told "vou registrar seu pedido de encaminhamento para psiquiatria
para o Dr. Victor avaliar e retornar". Nothing was registered: ``outbox_events``
held no row for him, and the lead sat at QUALIFICANDO where it had been since
21/ago. The fourth empty promise in this codebase, after the booking dead end
(13/ago), the clinical refusal (13/ago) and the lead question (24/ago).

The lead-question matcher could not have caught it. Its discriminator is the
word ``equipe``, chosen on 24/ago precisely because the colleague, vendor,
bank, partner and press templates all name Dr. Victor directly — so widening
IT would have pushed suppliers and press invitations onto reception's
WhatsApp, which is the 14/ago bug that the 15/ago rule closed.

So this is a third route with a third destination. A replay of every model
reply since 01/ago/2026 is what settled the design: 21 of 377 promise Dr.
Victor personally, and they are a mixture — RICKY, Niara's teaching
certificate and Graciela's instalments sitting beside a supplier's insulin
sensors, a speaking invitation and Rapidoc. Only Victor can sort those apart,
and they arrive on the line he already answers them on.

The regression that matters most here is negative: the set of replies routed
to RECEPTION must not change by one element.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.whatsapp_appointments import (
    AppointmentStore,
    WhatsAppAppointmentsHandler,
)
from gateway.run import (
    _WHATSAPP_CLINICAL_ESCALATION_RE,
    _WHATSAPP_DOCTOR_ESCALATION_RE,
    _whatsapp_promises_doctor_followup,
    _whatsapp_promises_team_followup,
)
from hermes_constants import get_hermes_home
from tests.gateway.appointment_helpers import (
    FakeFeegow,
    MutableClock,
    event,
    payment_config,
)

BRT = ZoneInfo("America/Bahia")
NOW = datetime(2026, 8, 26, 17, 51, tzinfo=BRT)

RECEPTION = "5571996691002@s.whatsapp.net"
VICTOR = "557188048263@s.whatsapp.net"
RICKY_LID = "20383998673035"
RICKY = f"{RICKY_LID}@lid"
# DDD 73, so WhatsApp stores the number WITHOUT the ninth digit while a human
# dials it with one. Both spellings appear below on purpose.
RICKY_PHONE = "557381312183"

# The reply as delivered, copied out of ``state.db`` row 2023.
RICKY_PROMISE = (
    "Boa tarde! Aqui é a assistente do Dr. Victor Almeida. Sr(a). Ricky, vou "
    "registrar seu pedido de encaminhamento para psiquiatria para o Dr. "
    "Victor avaliar e retornar. Boa semana!"
)


def _write_lid_mapping(phone=RICKY_PHONE, lid=RICKY_LID):
    """Mirror what the JS bridge writes: phone→lid and lid→phone (reverse).

    Without it the notice carries raw LID digits and no link — the 14/ago
    lesson, and the reason this route is exercised through the mapping rather
    than around it.  ``conftest`` points HERMES_HOME at a per-test tempdir, so
    this never touches the live session directory.
    """
    session_dir = get_hermes_home() / "whatsapp" / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / f"lid-mapping-{phone}.json").write_text(
        json.dumps(lid), encoding="utf-8"
    )
    (session_dir / f"lid-mapping-{lid}_reverse.json").write_text(
        json.dumps(phone), encoding="utf-8"
    )


def _handler(tmp_path, *, victor=VICTOR, reception=RECEPTION):
    # The notice routes never create the database — a promise is always
    # preceded by an inbound message, so by the time one is made the store is
    # already there. Bootstrapping it explicitly keeps these tests off the
    # booking flow, whose own side effects would muddy the outbox assertions.
    AppointmentStore(tmp_path / "appointments.sqlite3")
    return WhatsAppAppointmentsHandler(
        payment_config(clinical_notice_chat_id=victor, reception_chat_id=reception),
        db_path=tmp_path / "appointments.sqlite3",
        proofs_dir=tmp_path / "proofs",
        feegow_client=FakeFeegow(),
        clock=MutableClock(NOW),
    )


def _outbox(tmp_path):
    with sqlite3.connect(tmp_path / "appointments.sqlite3") as connection:
        return connection.execute(
            "SELECT chat_key, body FROM outbox_events ORDER BY created_at"
        ).fetchall()


# ── What the matcher must and must not see ──────────────────────────────────

class TestTheMatcher:
    def test_rickys_promise_is_recognised(self):
        assert _whatsapp_promises_doctor_followup(RICKY_PROMISE)

    def test_and_no_existing_route_would_have_taken_it(self):
        """Why the bug survived two earlier repairs of the same shape."""
        assert not _whatsapp_promises_team_followup(RICKY_PROMISE)
        assert not _WHATSAPP_CLINICAL_ESCALATION_RE.search(RICKY_PROMISE)

    def test_the_honorific_dot_is_load_bearing_here(self):
        """The promise names "Dr. Victor", so the dot sits inside the clause.

        The team matcher flattens honorifics defensively; this one cannot work
        at all without it, which is why the helper — never the bare pattern —
        is what the call site uses.
        """
        assert not _WHATSAPP_DOCTOR_ESCALATION_RE.search(RICKY_PROMISE)
        assert _whatsapp_promises_doctor_followup(RICKY_PROMISE)

    @pytest.mark.parametrize(
        "reply",
        [
            # Category 4 — supplier, the template RICKY's reply was shaped like.
            "Recebi a mensagem sobre a nota fiscal. Vou registrar para o Dr. "
            "Victor avaliar e retornar.",
            # Category 3 — a colleague. A person, not an organization, which is
            # why the sender lock alone could never have sorted this out.
            "Dr. Nilo, recebi sua mensagem sobre o caso. Vou encaminhar ao Dr. "
            "Victor, que retorna diretamente.",
            # Category 5 — bank.
            "Recebi a mensagem sobre a fatura. Vou registrar para o Dr. Victor "
            "tratar diretamente.",
            # Feminine honorific, and the reason the alternation is
            # longest-first: "dra" must not be eaten as "dr" + a stray "a".
            "Vou encaminhar à Dra. Victor Almeida para avaliar e retornar.",
        ],
    )
    def test_every_promise_that_names_him_counts(self, reply):
        assert _whatsapp_promises_doctor_followup(reply)

    @pytest.mark.parametrize(
        "reply",
        [
            # Category 8 — no promise at all, so nothing to keep.
            "Agradecemos o contato, mas não temos interesse. Obrigada.",
            # A promise three sentences later is not one clause.
            "Vou registrar o pedido. Tenha uma boa semana. O Dr. Victor "
            "trabalha às quartas e retorna quando puder.",
            "",
        ],
    )
    def test_and_nothing_else_does(self, reply):
        assert not _whatsapp_promises_doctor_followup(reply)


class TestReceptionKeepsItsOwn:
    """The negative regression: 15/ago said reception's WhatsApp is patients.

    The patient pendency template names the team AND Dr. Victor in one clause.
    It has belonged to reception since 24/ago and must stay there, which is
    what the call site's ``elif`` order buys — the team route is tested first
    and this one only ever sees what it declined.
    """

    PENDENCY = "Recebi o problema. Vou registrar para a equipe do Dr. Victor resolver."

    def test_the_pendency_template_still_belongs_to_reception(self):
        assert _whatsapp_promises_team_followup(self.PENDENCY)

    def test_even_though_this_matcher_would_also_take_it(self):
        """Documents the overlap, so the elif order is never "simplified"."""
        assert _whatsapp_promises_doctor_followup(self.PENDENCY)

    def test_the_lead_question_promise_is_untouched(self):
        reply = "Vou registrar seu pedido e a equipe vai retornar com a informação."

        assert _whatsapp_promises_team_followup(reply)


# ── Where the notice actually goes ──────────────────────────────────────────

class TestTheNotice:
    def test_it_reaches_victor_and_not_reception(self, tmp_path):
        handler = _handler(tmp_path)

        handler.note_doctor_escalation(
            event(
                "Pode me mandar encaminhamento pra o psiquiatra",
                chat_id=RICKY,
                user_name="RICKY",
            )
        )

        queued = _outbox(tmp_path)
        assert len(queued) == 1
        assert queued[0][0] == VICTOR
        assert RECEPTION not in {row[0] for row in queued}

    def test_the_notice_says_who_asked_and_what_for(self, tmp_path):
        _write_lid_mapping()
        handler = _handler(tmp_path)

        handler.note_doctor_escalation(
            event(
                "Pode me mandar encaminhamento pra o psiquiatra",
                chat_id=RICKY,
                user_name="RICKY",
            )
        )

        body = _outbox(tmp_path)[0][1]
        assert "RICKY" in body
        assert "psiquiatra" in body
        # Victor's line CAN open the thread — it is the same WhatsApp the
        # conversation is already in — so unlike reception's notice this one
        # keeps the link.
        assert f"https://wa.me/{RICKY_PHONE}" in body
        # Displayed dialable (with the ninth digit), addressed as WhatsApp
        # stores it (without) — the distinction that made three earlier
        # notices reach nobody.
        assert "73 98131-2183" in body
        # And it says plainly that nobody else was told.
        assert "recepção" in body.lower()

    def test_a_company_is_paged_too(self, tmp_path):
        """The deliberate asymmetry, and the line to read before changing this.

        Both older routes drop organizations because they end at reception.
        This one ends at Victor, who already answers Rapidoc and the bank on
        this very number — dropping them here would recreate the silent unkept
        promise at his own line.
        """
        handler = _handler(tmp_path)

        handler.note_doctor_escalation(
            event(
                "Recebemos a demanda do paciente",
                chat_id="113455889699014@lid",
                user_name="Clínica Meu Sorriso",
            )
        )

        assert len(_outbox(tmp_path)) == 1

    def test_victors_own_line_never_pages_itself(self, tmp_path):
        """He answers on this number, so his own promises must not loop back.

        The August replay caught this happening twice, on 03/ago and 12/ago.
        """
        handler = _handler(tmp_path)

        handler.note_doctor_escalation(
            event("Vou registrar para o Dr. Victor", chat_id=VICTOR, user_name="Victor")
        )

        assert _outbox(tmp_path) == []

    def test_reception_is_never_the_fallback(self, tmp_path):
        """Unset means nobody is paged — not "send it to reception instead"."""
        handler = _handler(tmp_path, victor="")

        handler.note_doctor_escalation(
            event("Pode me mandar o encaminhamento", chat_id=RICKY, user_name="RICKY")
        )

        assert _outbox(tmp_path) == []

    def test_a_burst_collapses_into_one_notice(self, tmp_path):
        handler = _handler(tmp_path)

        for index in range(3):
            handler.note_doctor_escalation(
                event(
                    "Pode me mandar o encaminhamento",
                    message_id=f"m-{index}",
                    chat_id=RICKY,
                    user_name="RICKY",
                )
            )

        assert len(_outbox(tmp_path)) == 1

    def test_but_the_next_hour_is_heard_again(self, tmp_path):
        handler = _handler(tmp_path)

        handler.note_doctor_escalation(
            event("Pode me mandar o encaminhamento", chat_id=RICKY, user_name="RICKY")
        )
        handler._clock.value = NOW + timedelta(hours=1)
        handler.note_doctor_escalation(
            event("E o encaminhamento?", chat_id=RICKY, user_name="RICKY")
        )

        assert len(_outbox(tmp_path)) == 2
