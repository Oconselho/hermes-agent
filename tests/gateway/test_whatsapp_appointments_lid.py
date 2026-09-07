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
# Where clinical questions go since 15/ago/2026 — and, because the secretary
# answers on Victor's own line, also the number the bridge is logged in as.
VICTOR = "557188048263@s.whatsapp.net"
VICTOR_LID_DIGITS = "118347958063114"
VICTOR_LID = f"{VICTOR_LID_DIGITS}@lid"

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


def _build_with_clinical_destination(feegow, tmp_path):
    """A handler wired the way production is: clinical notices go to Victor.

    Goes through the real constructor rather than assigning the attribute, so
    the config key itself stays covered — that wiring is the part that would
    silently stop working.
    """
    # Victor's own LID, so a self-chat arriving under either spelling is
    # recognised the way the bridge actually delivers it.
    _write_lid_mapping(phone="557188048263", lid=VICTOR_LID_DIGITS)
    return WhatsAppAppointmentsHandler(
        payment_config(clinical_notice_chat_id=VICTOR),
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


def test_phone_jid_needs_no_mapping():
    """Um JID de telefone se resolve sozinho, sem consultar mapeamento nenhum."""
    assert _chat_phone("5511988877766@s.whatsapp.net") == "11988877766"


def test_phone_jid_is_canonicalised_to_the_shape_its_ddd_uses():
    """Trocado em 07/set/2026: o telefone do chat sai na forma do DDD dele.

    Antes, esta função devolvia os dígitos como vieram. Isso bastava enquanto
    ninguém comparava o resultado com nada; mas o portão de identidade compara
    com o cadastro da Feegow, que escreve o mesmo número na outra grafia, e
    duas grafias nunca são iguais. Canonizar aqui é o que faz os dois lados
    falarem a mesma língua — ver ``_normalized_phone``.

    Em produção não muda nada: 100% das conversas chegam por LID e o mapeamento
    do bridge já entrega a forma certa. Muda para um JID escrito à mão.
    """
    # DDD 71 não usa o nono dígito no WhatsApp: escrito com ele, sai sem.
    assert _chat_phone("5571988877766@s.whatsapp.net") == "7188877766"
    # DDD 11 usa: escrito sem, sai com.
    assert _chat_phone("551188877766@s.whatsapp.net") == "11988877766"
    # Fixo não é celular em DDD nenhum, e não ganha nem perde dígito.
    assert _chat_phone("557132110000@s.whatsapp.net") == "7132110000"


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


def test_clinical_refusal_reaches_victor_and_never_reception(tmp_path):
    """"Vou encaminhar para a equipe" had no code behind it until now.

    And since 15/ago/2026 the code behind it addresses Victor: reception's
    WhatsApp carries booking requests and nothing else, and a patient raising
    a symptom is not a booking request.
    """
    _write_lid_mapping()
    handler = _build_with_clinical_destination(FakeFeegow(), tmp_path)
    handler._reception_chat_id = RECEPTION
    # The store only exists once the flow has touched it.
    handler.handle(
        event("Quero agendar uma consulta", message_id="clin-0", chat_id=LID_CHAT_KEY)
    )

    handler.note_clinical_escalation(
        event("meu marido descobriu diabetes", chat_id=LID_CHAT_KEY, user_name="Karina Costa")
    )

    chat_key, body, _ = _outbox(tmp_path)[-1]
    assert chat_key == VICTOR
    assert RECEPTION not in {row[0] for row in _outbox(tmp_path)}
    assert "Assunto clínico" in body
    assert "Karina" in body
    assert "71 98832-6547" in body
    assert "Abrir conversa: https://wa.me/557188326547" in body
    # The clinical content itself stays in the chat, not in the notice.
    assert "diabetes" not in body.lower()


def test_clinical_notice_without_a_destination_never_falls_back_to_reception(tmp_path):
    """Unset means nobody is paged — not "page reception instead".

    A fallback would quietly undo the one rule this setting exists to enforce,
    on the exact message class Victor asked to be taken off that number.
    """
    _write_lid_mapping()
    handler = _build(FakeFeegow(), tmp_path)  # no clinical_notice_chat_id
    handler._reception_chat_id = RECEPTION
    handler.handle(
        event("Quero agendar uma consulta", message_id="clin-n0", chat_id=LID_CHAT_KEY)
    )
    before = len(_outbox(tmp_path))

    handler.note_clinical_escalation(
        event("estou com dor", chat_id=LID_CHAT_KEY, user_name="Karina Costa")
    )

    assert len(_outbox(tmp_path)) == before


def test_victors_own_self_chat_never_notifies_itself(tmp_path):
    """The notice lands on the line the secretary itself answers on.

    Without this, Victor typing anything clinical into his own self-chat would
    page his own self-chat.
    """
    _write_lid_mapping()
    handler = _build_with_clinical_destination(FakeFeegow(), tmp_path)
    handler._reception_chat_id = RECEPTION
    handler.handle(
        event("Quero agendar uma consulta", message_id="clin-s0", chat_id=LID_CHAT_KEY)
    )
    before = len(_outbox(tmp_path))

    handler.note_clinical_escalation(event("dor de cabeça", chat_id=VICTOR))
    handler.note_clinical_escalation(event("dor de cabeça", chat_id=VICTOR_LID))

    assert len(_outbox(tmp_path)) == before


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
    handler = _build_with_clinical_destination(FakeFeegow(), tmp_path)
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
        payment_config(clinical_notice_chat_id=VICTOR),
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
    handler = _build_with_clinical_destination(FakeFeegow(), tmp_path)
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


# ------------------------------------------------- o cadastro como ele é hoje
#
# Georges Rocha, 05/set/2026 09:42 BRT. O conserto de 14/ago resolveu a
# resolução do LID: o telefone dele saiu certo (``7188503616``). Ele morreu na
# linha seguinte, no MESMO passo, e por outra razão — a comparação com o
# cadastro.
#
# O que a Feegow devolve de verdade para o paciente 1015 (medido em 07/set):
#
#     "telefone":  "71988503616"      <- COM o nono dígito
#     "celulares": ["71988503616"]
#     "telefones": ["7188503616"]     <- a forma exata do WhatsApp
#
# Dois defeitos somados, cada um fatal sozinho:
#
# 1. ``_patient_phones`` só lia as chaves no SINGULAR. As plurais são as que a
#    API de 02/set/2026 usa — e era nelas que estava o número certo.
# 2. ``_normalized_phone`` só tirava o ``55``. WhatsApp endereça DDD >= 31 SEM
#    o nono dígito e o cadastro guarda COM: comparar as duas formas cruas nunca
#    bate, para nenhum paciente de Salvador.
#
# Os testes de 14/ago não pegaram porque montam o cadastro com
# ``known_patient(celular=PHONE)`` — a chave singular, já na forma do WhatsApp.
# O mundo real não entrega nenhuma das duas coisas.

FEEGOW_PHONE_NINE = "71988503616"
FEEGOW_PHONE_EIGHT = "7188503616"
LID_BAHIA = "211995408220269"
LID_BAHIA_CHAT_KEY = f"{LID_BAHIA}@lid"
PHONE_BAHIA_WITH_COUNTRY = "55" + FEEGOW_PHONE_EIGHT


def _feegow_shaped_patient(**overrides):
    """O envelope de paciente do padrão de 02/set/2026, como ele chega."""
    patient = known_patient()
    patient.pop("celular", None)
    patient.update(
        {
            "telefone": FEEGOW_PHONE_NINE,
            "celulares": [FEEGOW_PHONE_NINE, None],
            "telefones": [FEEGOW_PHONE_EIGHT, None],
        }
    )
    patient.update(overrides)
    return patient


def _drive_bahia(handler, prefix):
    return _drive_to_phone_confirmation(
        handler, prefix, chat_key=LID_BAHIA_CHAT_KEY
    )


def test_ninth_digit_alone_must_not_send_a_bahian_patient_to_reception(tmp_path):
    """O caso do Georges: cadastro COM o nono dígito, WhatsApp SEM."""
    _write_lid_mapping(phone=PHONE_BAHIA_WITH_COUNTRY, lid=LID_BAHIA)
    feegow = FakeFeegow(
        slots=[dict(SLOT)],
        patients=[known_patient(celular=FEEGOW_PHONE_NINE)],
        procedures=[dict(PROC1)],
    )
    handler = _build(feegow, tmp_path)

    reply = _drive_bahia(handler, "nono-digito")

    assert DEAD_END not in reply
    assert "CONFIRMAR" in reply


def test_plural_feegow_phone_keys_are_read(tmp_path):
    """O número certo estava em ``telefones`` e ninguém lia essa chave."""
    _write_lid_mapping(phone=PHONE_BAHIA_WITH_COUNTRY, lid=LID_BAHIA)
    feegow = FakeFeegow(
        slots=[dict(SLOT)],
        patients=[_feegow_shaped_patient()],
        procedures=[dict(PROC1)],
    )
    handler = _build(feegow, tmp_path)

    reply = _drive_bahia(handler, "plural")

    assert DEAD_END not in reply
    assert "CONFIRMAR" in reply


def test_a_second_number_in_the_plural_list_still_identifies_the_patient(tmp_path):
    """Cadastro com dois números: o do WhatsApp é o segundo da lista."""
    _write_lid_mapping(phone=PHONE_BAHIA_WITH_COUNTRY, lid=LID_BAHIA)
    feegow = FakeFeegow(
        slots=[dict(SLOT)],
        patients=[
            _feegow_shaped_patient(
                telefone="7133334444",
                celulares=["7133334444", FEEGOW_PHONE_NINE],
                telefones=[],
            )
        ],
        procedures=[dict(PROC1)],
    )
    handler = _build(feegow, tmp_path)

    reply = _drive_bahia(handler, "segundo-numero")

    assert DEAD_END not in reply
    assert "CONFIRMAR" in reply


def test_a_different_persons_phone_still_fails_closed(tmp_path):
    """A guarda não pode virar peneira: outro número continua parando aqui.

    É o ponto do portão — o CPF pode ter sido digitado por outra pessoa. Sem
    este teste, o conserto acima seria indistinguível de desligar a guarda.
    """
    _write_lid_mapping(phone=PHONE_BAHIA_WITH_COUNTRY, lid=LID_BAHIA)
    feegow = FakeFeegow(
        slots=[dict(SLOT)],
        patients=[
            _feegow_shaped_patient(
                telefone="71977776666",
                celulares=["71977776666", None],
                telefones=["7177776666", None],
            )
        ],
        procedures=[dict(PROC1)],
    )
    handler = _build(feegow, tmp_path)

    reply = _drive_bahia(handler, "outra-pessoa")

    assert DEAD_END in reply


def test_landline_in_the_registration_is_never_rewritten_as_a_mobile(tmp_path):
    """Fixo tem 8 dígitos em todo DDD e não ganha nono dígito nenhum."""
    _write_lid_mapping(phone=PHONE_BAHIA_WITH_COUNTRY, lid=LID_BAHIA)
    feegow = FakeFeegow(
        slots=[dict(SLOT)],
        patients=[
            _feegow_shaped_patient(
                telefone="7132110000",
                celulares=["7132110000", None],
                telefones=["7132110000", None],
            )
        ],
        procedures=[dict(PROC1)],
    )
    handler = _build(feegow, tmp_path)

    reply = _drive_bahia(handler, "fixo")

    assert DEAD_END in reply


# ------------------------------------------------- da vaga ao comprovante
#
# ``operations``, ``reservations`` e ``payment_proofs`` estão em ZERO na base de
# produção desde 12/ago/2026: ninguém nunca passou do portão de telefone, então
# o resto do caminho da venda nunca rodou contra dado real. Estes testes fazem
# o percurso inteiro com o envelope que a Feegow devolve hoje — para o próximo
# defeito aparecer aqui, e não num paciente.

TELE_SLOT = {
    "id": "slot-tele",
    "procedimento_id": 3,
    "data": "2026-08-05",
    "horario": "14:00",
}
PROC_TELE = {"procedimento_id": 3, "nome": "Teleconsulta", "valor": 300}


def _feegow_real_envelope(**overrides):
    """Como o ``patient/search`` de 02/set/2026 responde, campo a campo.

    Já normalizado por ``_normalize_patient_rows`` — que é o que o handler
    recebe —, mas mantendo as chaves originais que a API manda junto: ``id``,
    ``nascimento`` em DD-MM-AAAA, e as listas de telefone.
    """
    patient = {
        "id": 1015,
        "paciente_id": 1015,
        "nome": "Paciente Exemplo",
        "cpf": CPF,
        "documentos": {"cpf": CPF},
        "nascimento": "01-02-1990",
        "telefone": FEEGOW_PHONE_NINE,
        "celulares": [FEEGOW_PHONE_NINE, None],
        "telefones": [FEEGOW_PHONE_EIGHT, None],
        "email": "paciente@example.invalid",
        "sexo": "F",
    }
    patient.update(overrides)
    return patient


def _tele_handler(tmp_path, feegow, clock):
    return WhatsAppAppointmentsHandler(
        payment_config(),
        db_path=tmp_path / "appointments.sqlite3",
        proofs_dir=tmp_path / "proofs",
        feegow_client=feegow,
        clock=clock,
    )


def _drive_tele_to_summary(handler, prefix):
    """Menu → agendar → teleconsulta → vaga → CPF → nascimento → SIM."""
    for index, text in enumerate(
        ("Quero agendar uma consulta", "1", "3", "1", CPF_FORMATTED, BIRTH_DATE),
        start=1,
    ):
        handler.handle(
            event(text, message_id=f"{prefix}-{index}", chat_id=LID_BAHIA_CHAT_KEY)
        )
    return handler.handle(
        event("SIM", message_id=f"{prefix}-7", chat_id=LID_BAHIA_CHAT_KEY)
    )


def _rows(tmp_path, query):
    with sqlite3.connect(tmp_path / "appointments.sqlite3") as connection:
        return connection.execute(query).fetchall()


def test_real_envelope_reaches_the_pix_charge_and_creates_the_reservation(tmp_path):
    """O caminho que nunca rodou: vaga → identidade → PIX → reserva viva."""
    _write_lid_mapping(phone=PHONE_BAHIA_WITH_COUNTRY, lid=LID_BAHIA)
    feegow = FakeFeegow(
        slots=[dict(TELE_SLOT)],
        patients=[_feegow_real_envelope()],
        procedures=[dict(PROC_TELE)],
    )
    handler = _tele_handler(tmp_path, feegow, MutableClock(NOW))

    summary = _drive_tele_to_summary(handler, "venda")
    assert DEAD_END not in summary
    assert "CONFIRMAR" in summary

    charge = handler.handle(
        event("CONFIRMAR", message_id="venda-8", chat_id=LID_BAHIA_CHAT_KEY)
    )

    assert DEAD_END not in charge
    assert "comprovante" in charge.lower()
    assert feegow.created_appointments, "a Feegow nunca foi chamada para criar"
    assert len(_rows(tmp_path, "SELECT state FROM reservations")) == 1
    operations = _rows(tmp_path, "SELECT kind, state FROM operations")
    assert operations and all(state == "SUCCEEDED" for _, state in operations)


def test_real_envelope_accepts_the_payment_proof_and_holds_the_slot(tmp_path):
    """Comprovante recebido: a reserva para de expirar e nada é cancelado."""
    _write_lid_mapping(phone=PHONE_BAHIA_WITH_COUNTRY, lid=LID_BAHIA)
    feegow = FakeFeegow(
        slots=[dict(TELE_SLOT)],
        patients=[_feegow_real_envelope()],
        procedures=[dict(PROC_TELE)],
    )
    handler = _tele_handler(tmp_path, feegow, MutableClock(NOW))
    _drive_tele_to_summary(handler, "prova")
    handler.handle(
        event("CONFIRMAR", message_id="prova-8", chat_id=LID_BAHIA_CHAT_KEY)
    )

    source = tmp_path / "comprovante.jpg"
    source.write_bytes(b"pix-bytes")
    reply = handler.handle(
        event(
            "Segue o comprovante",
            message_id="prova-9",
            chat_id=LID_BAHIA_CHAT_KEY,
            media_urls=[str(source)],
        )
    )

    assert "recebido" in reply.lower()
    assert _rows(tmp_path, "SELECT state FROM reservations")[0][0] == (
        "COMPROVANTE_RECEBIDO"
    )
    assert _rows(tmp_path, "SELECT COUNT(*) FROM payment_proofs")[0][0] == 1
    assert feegow.cancelled_appointments == []


def _with_ninth_digit(digits):
    """A grafia do cadastro: DDD 71 com o nono dígito de volta."""
    bare = "".join(character for character in str(digits) if character.isdigit())
    if len(bare) == 10 and bare[2] in "6789":
        return f"{bare[:2]}9{bare[2:]}"
    return bare


def test_new_patient_readback_survives_feegows_own_phone_spelling(tmp_path):
    """Paciente novo: a Feegow grava o telefone na grafia dela e devolve assim.

    O cadastro é criado com a forma do WhatsApp (sem o nono dígito) e volta com
    ele. Se o readback comparar as duas grafias cruas, o agendamento morre
    DEPOIS de já ter criado o paciente — o pior lugar possível para falhar.
    """
    _write_lid_mapping(phone=PHONE_BAHIA_WITH_COUNTRY, lid=LID_BAHIA)

    class _FeegowThatRewritesThePhone(FakeFeegow):
        def create_patient(self, **payload):
            result = super().create_patient(**payload)
            stored = dict(self.patients[0])
            stored.pop("telefone", None)
            stored["telefones"] = [_with_ninth_digit(payload["telefone"]), None]
            self.patients = [stored]
            return result

    feegow = _FeegowThatRewritesThePhone(
        slots=[dict(TELE_SLOT)], patients=[], procedures=[dict(PROC_TELE)]
    )
    handler = _tele_handler(tmp_path, feegow, MutableClock(NOW))

    for index, text in enumerate(
        ("Quero agendar uma consulta", "1", "3", "1", CPF_FORMATTED, BIRTH_DATE),
        start=1,
    ):
        handler.handle(
            event(text, message_id=f"novo-{index}", chat_id=LID_BAHIA_CHAT_KEY)
        )
    handler.handle(event("SIM", message_id="novo-7", chat_id=LID_BAHIA_CHAT_KEY))
    handler.handle(
        event("Paciente Exemplo", message_id="novo-8", chat_id=LID_BAHIA_CHAT_KEY)
    )
    handler.handle(event("F", message_id="novo-9", chat_id=LID_BAHIA_CHAT_KEY))
    summary = handler.handle(
        event(
            "paciente@example.invalid",
            message_id="novo-10",
            chat_id=LID_BAHIA_CHAT_KEY,
        )
    )
    assert "CONFIRMAR" in summary, summary

    charge = handler.handle(
        event("CONFIRMAR", message_id="novo-11", chat_id=LID_BAHIA_CHAT_KEY)
    )

    assert DEAD_END not in charge
    assert "comprovante" in charge.lower()
    assert feegow.created_patients
    assert feegow.created_appointments
