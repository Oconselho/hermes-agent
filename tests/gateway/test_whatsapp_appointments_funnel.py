"""CRM funnel: intent capture, single introduction, dead-end re-entry.

Every case here traces to the 12/ago/2026 booking test, where a patient who
said "0quero agendar" was answered by the model pipeline, introduced to
twice, and finally parked in a handoff no later message could leave.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.whatsapp_appointments import (
    SILENCE,
    AppointmentStore,
    LeadStage,
    Route,
    WhatsAppAppointmentsHandler,
    _response_signature,
    classify_route,
    treatment_title,
)

from tests.gateway.appointment_helpers import FakeFeegow, MutableClock, event

BRT = ZoneInfo("America/Bahia")


def _leads(db_path):
    connection = sqlite3.connect(db_path)
    try:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT chat_key, stage FROM leads")
        }
    finally:
        connection.close()


def _lead_path(db_path, chat_key):
    connection = sqlite3.connect(db_path)
    try:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT to_stage FROM lead_events WHERE chat_key = ?"
                " ORDER BY created_at, rowid",
                (chat_key,),
            )
        ]
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Intent capture — the funnel's entry
# --------------------------------------------------------------------------


def test_digit_glued_to_verb_still_enters_the_funnel():
    """The exact message from the 12/ago booking test.

    ``0quero agendar`` never matched ``\\bquero`` because a digit is a word
    character, so the message escaped to the model pipeline and the patient
    got a generic reply instead of the menu.
    """

    assert classify_route(event("0quero agendar")) is Route.APPOINTMENT


@pytest.mark.parametrize(
    "text",
    [
        "0quero agendar",
        "1quero remarcar",
        "agendar",
        "agendamento",
        "teleconsulta",
        "telemedicina",
        "quero uma consulta",
        "queria marcar uma consulta",
        "gostaria de agendar",
        "tem horario disponivel?",
        "tem alguma vaga essa semana?",
        "quanto custa a consulta",
        "qual o valor da consulta?",
        "como faco para marcar consulta",
        "consulta com o dr victor",
        "primeira consulta",
        "agendar,consulta",
        "preciso remarcar minha consulta",
    ],
)
def test_lead_phrasings_are_recognized(text):
    assert classify_route(event(text)) is Route.APPOINTMENT


@pytest.mark.parametrize(
    "text",
    [
        # The qualification question the model asks offers these paths in
        # words ("agendar uma consulta, remarcar ou desmarcar, agendar seu
        # retorno"). Whatever the patient echoes back has to land in the
        # funnel, or qualifying just leaks the lead one turn later.
        "quero agendar uma consulta",
        "agendar",
        "remarcar",
        "quero desmarcar",
        "retorno",
        "meu retorno",
        "quero meu retorno",
        "agendar meu retorno",
        "marcar retorno",
    ],
)
def test_answers_to_the_qualification_question_reenter_the_funnel(text):
    assert classify_route(event(text)) is Route.APPOINTMENT


@pytest.mark.parametrize(
    "text",
    [
        # "retorno" is also how a partner closes a message. Matching it bare
        # would drop them into the booking menu.
        "aguardo retorno",
        "fico no aguardo do retorno",
        "obrigado pelo retorno",
        "aguardo seu retorno sobre a proposta",
    ],
)
def test_partner_sign_offs_are_not_booking_intent(text):
    assert classify_route(event(text)) is not Route.APPOINTMENT


@pytest.mark.parametrize(
    "text",
    [
        "obrigado!",
        "1",
        "meu exame está anexado",
        "vocês aceitam plano de saúde?",
        "529.982.247-25",
    ],
)
def test_non_lead_messages_still_stay_out_of_the_funnel(text):
    """Broadening intent must not swallow the model pipeline's traffic."""

    assert classify_route(event(text)) is Route.OUT_OF_SCOPE


# --------------------------------------------------------------------------
# The cold open — the 12/ago/2026 evening test
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # The two literal messages from the failing test, in order.
        "Boa note",
        "Alguém ai?",
        # The rest of how a conversation actually opens.
        "oi, tudo bem?",
        "bom dia",
        "boa tarde",
        "Boa noite",
        "olá",
        "oi",
        "opa",
        "bom dia, tudo bem?",
        "tem alguém?",
        "alguém aí",
        "preciso de informações",
        "queria uma informação",
        "pode me ajudar?",
        "boa tarde, por favor",
        "quero falar com o Dr. Victor",
        "oi doutor",
    ],
)
def test_opening_messages_are_answered_by_the_funnel(text):
    """An opening line must never need a model round trip to be answered."""

    assert classify_route(event(text)) is Route.OPENER


def test_greeting_typo_still_opens_the_conversation():
    """``Boa note`` — the real message, 12/ago/2026 21:21:44 UTC.

    One missing letter cost the patient the entire first impression: the
    message reached the model, which stayed silent for 10.7 seconds and then
    sent nothing at all.
    """

    assert classify_route(event("Boa note")) is Route.OPENER


@pytest.mark.parametrize(
    "text",
    [
        # A stated request is an appointment, not a greeting — the intent
        # patterns must keep winning, or the opener would flatten every
        # message into the same generic menu path.
        "bom dia, quero agendar uma consulta",
        "boa tarde, tem horário disponível?",
        "oi, quanto custa a consulta",
    ],
)
def test_a_greeting_that_states_a_request_is_still_an_appointment(text):
    assert classify_route(event(text)) is Route.APPOINTMENT


@pytest.mark.parametrize(
    "text",
    [
        # Typo repair is bounded: it may widen what counts as a greeting, but
        # it must never rewrite a message into something the sender did not
        # say. None of these is an opener under any single-character edit.
        "obrigado pelo retorno",
        "segue o comprovante",
        "meu nome é Ana",
        "vocês aceitam plano de saúde?",
        "está doendo muito",
    ],
)
def test_typo_repair_does_not_invent_an_opener(text):
    assert classify_route(event(text)) is not Route.OPENER


def test_institutional_contact_never_becomes_an_opener():
    """The partner exclusion runs before the greeting, exactly as before."""

    assert classify_route(event("Bom dia, somos da clinica parceira")) is Route.EXCLUDED


def test_opening_message_gets_the_introduction_and_the_numbered_options(tmp_path):
    """The whole 12/ago/2026 evening complaint, in one assertion block."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 18, 21, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    reply = handler.handle(event("Boa note", message_id="m-1"))

    assert reply is not None, "a greeting must be answered by the flow, not the model"
    assert "Sou a assistente do Dr. Victor Almeida" in reply
    for option in ("**1**", "**2**", "**3**", "**4**", "**5**", "**6**"):
        assert option in reply
    assert "Responda com o número da opção desejada." in reply


def test_the_opening_message_reaches_the_agenda_without_the_model(tmp_path):
    """``Boa noite`` → ``1`` → ``3`` must walk straight to the service menu."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 18, 21, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    handler.handle(event("Boa noite", message_id="m-1"))
    service_menu = handler.handle(event("1", message_id="m-2"))

    assert "Teleconsulta" in service_menu


def test_a_later_contact_is_introduced_to_again(tmp_path):
    """The greeting TTL must not leave a returning contact unintroduced.

    ``greeting_ttl_hours`` (6h) exists to stop the flow repeating itself
    inside one exchange. Applied to a cold open it produced the opposite
    defect on 12/ago/2026: a contact that came back hours later got a menu
    from a secretary that never said who it was.
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 13, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    first = handler.handle(event("bom dia", message_id="m-1"))
    assert "Sou a assistente do Dr. Victor Almeida" in first

    # Same burst: one introduction only.
    clock.value += timedelta(minutes=2)
    handler.handle(event("2", message_id="m-2"))

    # Hours later, with the flow long expired: a new conversation.
    clock.value += timedelta(hours=30)
    later = handler.handle(event("boa noite", message_id="m-3"))
    assert "Sou a assistente do Dr. Victor Almeida" in later


def test_a_second_greeting_does_not_reprint_the_whole_menu(tmp_path):
    """The real burst: "Boa note" and "Alguém ai?" seven seconds apart.

    Both are openers, and both arrive before the patient has read anything.
    Printing the five options twice in a row is the amnesiac behaviour the
    single-introduction rule exists to prevent.
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 18, 21, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    first = handler.handle(event("Boa note", message_id="m-1"))
    assert "**1**" in first

    clock.value += timedelta(seconds=7)
    second = handler.handle(event("Alguém ai?", message_id="m-2"))

    assert "**1**" not in second
    assert "Sou a assistente" not in second

    # The menu is still the live state: the next digit is read normally.
    clock.value += timedelta(seconds=20)
    assert "Teleconsulta" in handler.handle(event("1", message_id="m-3"))


def test_a_greeting_long_after_the_menu_gets_the_menu_again(tmp_path):
    """The nudge only replaces the menu while the menu is still on screen."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 18, 21, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    handler.handle(event("bom dia", message_id="m-1"))
    clock.value += timedelta(hours=3)
    later = handler.handle(event("oi", message_id="m-2"))

    assert "**1**" in later


# --------------------------------------------------------------------------
# Courtesy: the hour, the name, the sign-off (13/ago/2026)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hour,expected",
    [
        (5, "Bom dia"), (8, "Bom dia"), (11, "Bom dia"),
        (12, "Boa tarde"), (17, "Boa tarde"),
        (18, "Boa noite"), (23, "Boa noite"), (3, "Boa noite"),
    ],
)
def test_greeting_follows_the_local_hour(tmp_path, hour, expected):
    """The clinic is in Salvador; the server is three hours ahead of it."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 13, hour, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    reply = handler.handle(event("bom dia", message_id="m-1"))
    assert reply.startswith(expected)


def test_the_opening_uses_the_whatsapp_contact_name(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 13, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    reply = handler.handle(event("oi", message_id="m-1", user_name="Leonardo Souza"))

    # Pronome de tratamento a partir de 17/ago/2026: consultório trata
    # paciente por Sr./Sra., e por "Sr(a)." quando o nome não decide.
    assert reply.startswith("Bom dia, Sr. Leonardo!")
    assert "Sou a assistente do Dr. Victor Almeida" in reply


@pytest.mark.parametrize(
    "user_name",
    [
        # Addressing the wrong entity by name is worse than addressing none.
        # (An institutional name like "Laboratorio Central" never reaches
        # here at all — Route.EXCLUDED catches it first, asserted below.)
        "Clinica Sao Rafael",
        "Atendimento",
        "5571999999999",
        "Farmacia 24h",
        "",
    ],
)
def test_a_non_person_contact_is_greeted_without_a_name(tmp_path, user_name):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 13, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    reply = handler.handle(event("oi", message_id="m-1", user_name=user_name))

    assert reply.startswith("Bom dia! Sou a assistente")


def test_an_institutional_contact_is_not_greeted_at_all(tmp_path):
    """The partner exclusion runs before any courtesy."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler({"enabled": True}, db_path=db_path)

    assert (
        handler.handle(event("oi", message_id="m-1", user_name="Laboratorio Central"))
        is None
    )


def test_a_titled_contact_keeps_the_title_they_chose(tmp_path):
    """"Dra. Marina" tratada por "Sra. Marina" seria uma demotion.

    O nome do WhatsApp é escrito pelo próprio contato: quando ele traz o
    título, não há nada a adivinhar. O nome continua sendo o nome, nunca o
    título — era o que este teste garantia antes e continua garantindo.
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 13, 14, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    reply = handler.handle(event("oi", message_id="m-1", user_name="Dra. Marina Alves"))
    assert reply.startswith("Boa tarde, Dra. Marina!")


@pytest.mark.parametrize(
    "day,expected",
    [
        (9, "Boa semana!"),    # domingo
        (10, "Boa semana!"),   # segunda
        (13, "Boa semana!"),   # quinta
        (14, "Bom final de semana!"),  # sexta
        (15, "Bom final de semana!"),  # sábado
    ],
)
def test_a_closing_reply_carries_the_right_wish(tmp_path, day, expected):
    """Sunday starts the week ahead; only Friday and Saturday are weekend."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, day, 10, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    handler.handle(event("quero agendar", message_id=f"m-{day}-1"))
    handler.handle(event("1", message_id=f"m-{day}-2"))
    # No Feegow client wired: choosing a service dead-ends at reception,
    # which is a reply that closes the conversation.
    closing = handler.handle(event("3", message_id=f"m-{day}-3"))

    assert "recepção" in closing
    assert closing.endswith(expected)


def test_a_mid_flow_prompt_carries_no_sign_off(tmp_path):
    """Wishing a good week while still asking for a CPF is a goodbye mid-sentence."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 14, 10, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    opening = handler.handle(event("quero agendar", message_id="m-1"))
    prompt = handler.handle(event("2", message_id="m-2"))

    for text in (opening, prompt):
        assert "Boa semana" not in text
        assert "Bom final de semana" not in text


def test_the_funnel_tells_the_model_it_already_introduced_itself(tmp_path):
    """The missing direction: funnel -> model.

    ``state.db`` holds only the model's own turns, so after the funnel
    greeted, the model had no way to know and introduced itself a second
    time — the 13/ago/2026 complaint.
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 13, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )
    opening = event("oi", message_id="m-1")

    assert handler.already_greeted(opening.source) is False
    handler.handle(opening)
    assert handler.already_greeted(opening.source) is True


def test_already_greeted_never_creates_the_database(tmp_path):
    """The lookup runs on every model turn; it must stay read-only."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler({"enabled": True}, db_path=db_path)

    assert handler.already_greeted(event("oi").source) is False
    assert not db_path.exists()


def test_the_reception_chat_never_gets_the_booking_menu(tmp_path):
    """Reception is this flow's outbound channel, not a patient."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "reception_chat_id": "5571996691002@s.whatsapp.net"},
        db_path=db_path,
        clock=MutableClock(datetime(2026, 8, 12, 18, 21, tzinfo=BRT)),
    )

    reception = event(
        "bom dia", chat_id="5571996691002@s.whatsapp.net", message_id="r-1"
    )
    assert handler.handle(reception) is None


def test_option_six_leaves_the_funnel_instead_of_looping_the_menu(tmp_path):
    """Without an exit, the menu is a trap for whoever is not booking."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 18, 21, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    handler.handle(event("boa noite", message_id="m-1"))
    exit_reply = handler.handle(event("6", message_id="m-2"))

    assert "**1**" not in exit_reply, "option 6 must not re-print the menu"

    # The flow is gone, so the next message is the model pipeline's again.
    clock.value += timedelta(minutes=1)
    assert handler.handle(event("é sobre uma parceria", message_id="m-3")) is None


def test_option_six_holds_against_an_opener_on_the_very_next_message(tmp_path):
    """Sair pelo 6 tem de SEGURAR — e o que o quebrava era uma abertura.

    Georges Rocha, 12/set/2026: apertou 6 às 13:02 BRT, ouviu "me conte", e a
    mensagem seguinte — uma dúvida clínica — levou o menu inteiro de volta.
    Repetiu às 13:08 e recebeu o menu de novo. ``api_calls=0`` nos quatro
    turnos, porque o funil respondeu todos; e como as três rotas de
    escalonamento do ``run.py`` leem o texto ENTREGUE, que nunca foi do
    modelo, ninguém foi avisado da dúvida.

    A causa não era o 6 responder errado. Era ``purge_flow`` deixar o chat SEM
    linha nenhuma — e ``flow is None`` é exatamente a condição de abertura
    fria. Sair do funil apagando a marca de que se saiu é voltar para ele na
    mensagem seguinte.
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 9, 12, 12, 59, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    handler.handle(event("boa tarde", message_id="g-1"))
    handler.handle(event("6", message_id="g-2"))

    # "preciso de uma informação" é Route.OPENER — a mesma classe de mensagem
    # que trouxe o Georges de volta. Antes deste conserto isto devolvia o menu.
    clock.value += timedelta(minutes=1)
    depois = handler.handle(event("preciso de uma informação", message_id="g-3"))
    assert depois is None, f"o funil recapturou quem tinha saído: {depois!r}"

    # E segue segurando: não é um passe livre de uma mensagem só.
    clock.value += timedelta(minutes=5)
    assert handler.handle(event("pode me ajudar?", message_id="g-4")) is None


def test_option_six_no_longer_makes_the_secretary_reintroduce_itself(tmp_path):
    """Efeito colateral do conserto acima, e vale pinar: a apresentação some.

    ``disclosure_already_made`` responde "já me apresentei?" perguntando se
    existe fluxo vivo. Como o ramo do 6 chamava ``purge_flow`` ANTES de a
    resposta ser finalizada, a pergunta caía num chat sem fluxo e o ``run.py``
    colava "Aqui é a assistente do Dr. Victor Almeida." na frente — 43
    caracteres, medidos duas vezes em 12/set/2026 (a resposta do funil tem 80
    e o Georges recebeu 123). Com a marca gravada em vez do fluxo apagado, a
    apresentação não volta.
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 9, 12, 12, 59, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    abertura = handler.handle(event("boa tarde", message_id="d-1"))
    assert "Sou a assistente do Dr. Victor Almeida" in abertura

    saida = handler.handle(event("6", message_id="d-2"))
    assert saida is not None

    source = getattr(event("6", message_id="d-3"), "source", None)
    assert handler.disclosure_already_made(source) is True, (
        "sem fluxo vivo o run.py recola os 43 caracteres da apresentação"
    )


def test_an_explicit_booking_request_reopens_the_funnel_after_option_six(tmp_path):
    """A marca segura abertura, não pedido. Quem diz "quero agendar" volta."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 9, 12, 12, 59, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    handler.handle(event("boa tarde", message_id="v-1"))
    handler.handle(event("6", message_id="v-2"))

    clock.value += timedelta(minutes=2)
    volta = handler.handle(event("quero agendar uma consulta", message_id="v-3"))
    assert volta is not None, "pedido explícito de agendamento tem de reabrir o funil"
    assert "**1**" in volta


def test_the_out_of_funnel_mark_expires_like_any_other_flow(tmp_path):
    """Passado o TTL, quem escreve é alguém novo chegando — e o menu é certo.

    A marca não pode virar exílio permanente: sem prazo, um contato que pediu
    "outro assunto" em agosto nunca mais conseguiria marcar consulta pelo
    caminho determinístico.
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 9, 12, 12, 59, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    handler.handle(event("boa tarde", message_id="t-1"))
    handler.handle(event("6", message_id="t-2"))

    clock.value += timedelta(hours=25)
    reaberto = handler.handle(event("boa tarde", message_id="t-3"))
    assert reaberto is not None and "**1**" in reaberto


def test_a_shared_link_never_answers_for_the_patient(tmp_path):
    """Quem perguntou preço foi o endereço da reportagem, não o paciente.

    14/set/2026, 07:06 BRT: Georges Rocha mandou uma matéria da Folha e
    recebeu a tabela de valores seguida do menu, 479 caracteres. O slug de uma
    reportagem é escrito com hífen entre as palavras, e ``\\b`` trata hífen,
    barra e ponto como fronteira — então cada palavra do endereço vira palavra
    solta para qualquer padrão de intenção.
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 9, 14, 7, 6, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    handler.handle(event("bom dia", message_id="l-1"))

    clock.value += timedelta(minutes=1)
    link = (
        "https://www1.folha.uol.com.br/equilibrioesaude/2026/09/"
        "anvisa-aprova-12-novas-canetas-emagrecedoras-veja-precos.shtml"
    )
    resposta = handler.handle(event(link, message_id="l-2")) or ""
    assert "Os valores são" not in resposta, (
        f"a tabela de preços saiu para um link: {resposta!r}"
    )

    # E o conserto não pode custar a pergunta de verdade — nem quando ela vem
    # na mesma mensagem que o link.
    clock.value += timedelta(minutes=1)
    com_pergunta = handler.handle(
        event(f"olha isso {link} quanto custa?", message_id="l-3")
    ) or ""
    assert "Os valores são" in com_pergunta

    clock.value += timedelta(minutes=1)
    sozinha = handler.handle(event("qual o preço?", message_id="l-4")) or ""
    assert "Os valores são" in sozinha


def test_a_link_does_not_open_the_funnel_by_itself():
    """Mesma causa, um degrau antes: o slug não pode virar rota de paciente."""

    link = (
        "https://exemplo.com.br/saude/2026/09/"
        "como-agendar-consulta-com-endocrinologista.html"
    )
    assert classify_route(event(link)) is Route.OUT_OF_SCOPE
    assert classify_route(event(f"{link} quero agendar")) is Route.APPOINTMENT


def test_institutional_exclusion_still_beats_a_glued_intent():
    """Ungluing must never smuggle a partner contact into the funnel."""

    assert (
        classify_route(event("Somos da clinica parceira, 0quero agendar"))
        is Route.EXCLUDED
    )


# --------------------------------------------------------------------------
# One introduction per conversation
# --------------------------------------------------------------------------


def test_menu_introduces_once_and_then_stops_repeating_itself(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    first = handler.handle(event("quero agendar", message_id="m-1"))
    assert "Sou a assistente do Dr. Victor Almeida" in first

    # Same chat comes back inside the greeting window with a new intent.
    clock.value += timedelta(hours=2)
    handler.handle(event("obrigado", message_id="m-2"))
    second = handler.handle(event("quero agendar", message_id="m-3"))
    assert "Sou a assistente do Dr. Victor Almeida" not in second
    # O que este teste protege — não se reapresentar — continua valendo.
    # O que mudou em 03/set/2026: um pedido ESCRITO ("quero agendar") deixou de
    # reimprimir o menu e passa a avançar, como se o paciente tivesse apertado
    # 1. Medido às 17:27:47 BRT: ele disse exatamente isso e recebeu o mesmo
    # menu de volta. Só vale depois da apresentação, por isso aqui funciona.
    assert "Escolha o serviço" in second
    assert "**1** - Consulta presencial" in second


def test_model_greeting_suppresses_the_menu_introduction(tmp_path):
    """The exact 12/ago shape: model greets first, menu greets again."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    # Message 1 was answered by the model pipeline, which introduced itself.
    handler.handle(event("bom dia", message_id="g-0"))  # out of scope, no state
    AppointmentStore(db_path)  # the flow's db exists once anything ran
    handler.note_model_greeting(event("bom dia", message_id="g-0"))

    clock.value += timedelta(minutes=1)
    menu = handler.handle(event("quero agendar", message_id="g-1"))
    # A garantia que dá nome ao teste: o modelo já cumprimentou, então o funil
    # NÃO se reapresenta.
    assert "Sou a assistente do Dr. Victor Almeida" not in menu
    # E, desde 03/set/2026, o pedido escrito avança em vez de reimprimir a
    # lista — o contato já foi cumprimentado, então o atalho falado vale.
    assert "Escolha o serviço" in menu


def test_model_greeting_is_ignored_for_excluded_contacts(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True},
        db_path=db_path,
        clock=lambda: datetime(2026, 8, 12, 9, tzinfo=BRT),
    )
    AppointmentStore(db_path)

    handler.note_model_greeting(
        event("Somos do laboratorio parceiro", chat_id="lab@lid")
    )
    assert _leads(db_path) == {}


def test_model_greeting_never_creates_the_database(tmp_path):
    """A disabled/never-used funnel must not be provisioned by a greeting."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True},
        db_path=db_path,
        clock=lambda: datetime(2026, 8, 12, 9, tzinfo=BRT),
    )

    handler.note_model_greeting(event("bom dia"))
    assert not db_path.exists()


def test_introduction_returns_after_the_greeting_window_expires(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "greeting_ttl_hours": 6}, db_path=db_path, clock=clock
    )

    assert "Sou a assistente" in handler.handle(
        event("quero agendar", message_id="m-1")
    )

    clock.value += timedelta(hours=7)
    assert "Sou a assistente" in handler.handle(
        event("quero agendar", message_id="m-2")
    )


# --------------------------------------------------------------------------
# A dead end is not permanent
# --------------------------------------------------------------------------


def test_handoff_reopens_for_a_new_intent_after_the_cooldown(tmp_path):
    """The 12/ago chat was locked into the reception line for 24h."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 9, 0, tzinfo=BRT))
    # write_enabled False forces the handoff branch deterministically.
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "write_enabled": False, "handoff_reentry_minutes": 30},
        db_path=db_path,
        clock=clock,
    )

    handler.handle(event("quero agendar", message_id="m-1"))
    handler.handle(event("1", message_id="m-2"))
    dead_end = handler.handle(event("1", message_id="m-3"))
    assert "recepção" in dead_end

    # Immediately after, the funnel stays closed: this is the same failed
    # conversation, and repeating the menu would loop the patient.
    clock.value += timedelta(minutes=5)
    assert "recepção" in handler.handle(event("quero agendar", message_id="m-4"))

    # An hour later, a fresh explicit intent reopens the funnel.
    clock.value += timedelta(hours=1)
    reopened = handler.handle(event("quero agendar uma consulta", message_id="m-5"))
    assert "**1** - Agendar uma consulta" in reopened


def test_handoff_without_new_intent_is_never_reopened(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "write_enabled": False, "handoff_reentry_minutes": 30},
        db_path=db_path,
        clock=clock,
    )
    handler.handle(event("quero agendar", message_id="m-1"))
    handler.handle(event("1", message_id="m-2"))
    assert "recepção" in handler.handle(event("1", message_id="m-3"))

    clock.value += timedelta(hours=5)
    assert "recepção" in handler.handle(event("e ai?", message_id="m-4"))


# --------------------------------------------------------------------------
# Funnel bookkeeping
# --------------------------------------------------------------------------


def test_funnel_records_the_stage_path_without_pii(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 12, 9, 0, tzinfo=BRT))
    feegow = FakeFeegow(
        slots=[{"data": "2026-08-20", "horario": "14:00", "procedimento_id": 3}],
        procedures=[{"procedimento_id": 3, "nome": "Teleconsulta", "valor": 300}],
    )
    handler = WhatsAppAppointmentsHandler(
        {
            "enabled": True,
            "write_enabled": True,
            "payment": {
                "enabled": True,
                "beneficiary": "Clínica Exemplo",
                "instructions": "PIX 000",
            },
        },
        db_path=db_path,
        feegow_client=feegow,
        clock=clock,
    )

    chat_key = "5571999999999@s.whatsapp.net"
    handler.handle(event("quero agendar", message_id="f-1"))
    assert _leads(db_path)[chat_key] == LeadStage.QUALIFICANDO.value

    handler.handle(event("1", message_id="f-2"))
    assert _leads(db_path)[chat_key] == LeadStage.ESCOLHENDO_SERVICO.value

    handler.handle(event("3", message_id="f-3"))
    assert _leads(db_path)[chat_key] == LeadStage.ESCOLHENDO_HORARIO.value

    assert _lead_path(db_path, chat_key) == [
        LeadStage.QUALIFICANDO.value,
        LeadStage.ESCOLHENDO_SERVICO.value,
        LeadStage.ESCOLHENDO_HORARIO.value,
    ]

    # The funnel is reportable but PII-free: nothing beyond the opaque chat
    # key that flow_states already stores may appear in these tables.
    connection = sqlite3.connect(db_path)
    try:
        dump = " ".join(
            str(value)
            for table in ("leads", "lead_events")
            for row in connection.execute(f"SELECT * FROM {table}")
            for value in row
        )
    finally:
        connection.close()
    assert "52998224725" not in dump
    assert "Paciente" not in dump


def test_service_label_is_carried_into_the_lead(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    feegow = FakeFeegow(
        slots=[{"data": "2026-08-20", "horario": "14:00", "procedimento_id": 1}],
        procedures=[{"procedimento_id": 1, "nome": "Consulta", "valor": 600}],
    )
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "write_enabled": True},
        db_path=db_path,
        feegow_client=feegow,
        clock=lambda: datetime(2026, 8, 12, 9, 0, tzinfo=BRT),
    )
    handler.handle(event("quero agendar", message_id="s-1"))
    handler.handle(event("1", message_id="s-2"))
    handler.handle(event("1", message_id="s-3"))

    store = AppointmentStore(db_path)
    lead = store.load_lead("5571999999999@s.whatsapp.net")
    assert lead is not None
    assert lead.service_label == "Consulta presencial"
    assert lead.touches >= 3


def test_funnel_counts_and_stale_leads_feed_the_worklist(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    store = AppointmentStore(db_path)
    now = datetime(2026, 8, 12, 9, 0, tzinfo=BRT)

    store.record_lead("a@lid", LeadStage.ESCOLHENDO_HORARIO, now=now)
    store.record_lead("b@lid", LeadStage.AGENDADO, now=now)
    store.record_lead("c@lid", LeadStage.ESCOLHENDO_HORARIO, now=now)

    assert store.funnel_counts() == {
        LeadStage.ESCOLHENDO_HORARIO.value: 2,
        LeadStage.AGENDADO.value: 1,
    }

    stale = store.stale_leads(
        now=now + timedelta(days=1),
        older_than_seconds=3600,
        stages=[LeadStage.ESCOLHENDO_HORARIO.value],
    )
    assert sorted(lead.chat_key for lead in stale) == ["a@lid", "c@lid"]

    fresh = store.stale_leads(
        now=now + timedelta(minutes=5),
        older_than_seconds=3600,
        stages=[LeadStage.ESCOLHENDO_HORARIO.value],
    )
    assert fresh == []


def test_funnel_failure_never_costs_the_patient_a_reply(tmp_path, monkeypatch):
    """The funnel is observational: a broken write must not eat the answer."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=lambda: datetime(2026, 8, 12, 9, tzinfo=BRT)
    )

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("funnel is down")

    monkeypatch.setattr(AppointmentStore, "record_lead", explode)

    reply = handler.handle(event("quero agendar", message_id="x-1"))
    assert "**1** - Agendar uma consulta" in reply


# --------------------------------------------------------------------------
# Repetição — uma rajada, uma resposta (17/ago/2026)
# --------------------------------------------------------------------------


def test_a_second_line_of_the_same_request_is_not_answered_twice(tmp_path):
    """A conversa do Eduardo Mandelli, 17/ago/2026 18:25 BRT.

    Duas mensagens com seis segundos de diferença — "Temos que marcar uma
    consulta nova pra monitorar o progresso do mounjaro" e "E renovar a
    receita" — receberam o mesmo menu duas vezes. Cada mensagem entra no
    fluxo sozinha, e a segunda caiu no ramo que reimprime o menu para
    qualquer texto que não seja um número.

    A comparação byte a byte não resolveria: a primeira resposta abre com
    "Boa noite, Sr. Eduardo! Sou a assistente..." e a segunda não. São a
    mesma mensagem para quem lê.
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 17, 18, 25, 38, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    first = handler.handle(
        event(
            "Temos que marcar uma consulta nova pra monitorar o progresso do mounjaro",
            message_id="m-1",
            user_name="Eduardo Mandelli",
        )
    )
    assert "**1** - Agendar uma consulta" in first

    clock.value += timedelta(seconds=6)
    second = handler.handle(
        event("E renovar a receita", message_id="m-2", user_name="Eduardo Mandelli")
    )

    assert second is SILENCE
    assert second == ""

    # Silêncio no envio, não no estado: a mensagem fica tratada e o menu
    # segue de pé, então o próximo dígito é lido normalmente.
    clock.value += timedelta(seconds=20)
    service_menu = handler.handle(
        event("1", message_id="m-3", user_name="Eduardo Mandelli")
    )
    assert "Teleconsulta" in service_menu


def test_the_menu_is_printed_again_for_someone_who_comes_back_later(tmp_path):
    """A supressão cobre a rajada, não a conversa.

    Quem volta minutos depois sem ter entendido merece o menu de novo — é a
    diferença entre não se repetir e não responder.
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 17, 18, 25, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    handler.handle(event("quero agendar", message_id="m-1"))

    clock.value += timedelta(minutes=10)
    later = handler.handle(event("E renovar a receita", message_id="m-2"))

    assert later is not SILENCE
    assert "**1** - Agendar uma consulta" in later


def test_a_step_that_changed_is_never_silenced(tmp_path):
    """Repetir uma frase é ruim; esconder uma mudança de estado é pior.

    Dois CPFs inválidos seguidos produzem o mesmo texto — "CPF inválido." —
    mas o segundo é resposta a uma tentativa nova do paciente. O silêncio ali
    seria lido como "o sistema caiu", não como "já respondi".
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 17, 18, 25, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    handler.handle(event("quero agendar", message_id="m-1"))
    clock.value += timedelta(seconds=4)
    assert "Informe o CPF do paciente." in handler.handle(
        event("2", message_id="m-2")
    )

    clock.value += timedelta(seconds=4)
    first_try = handler.handle(event("111", message_id="m-3"))
    assert "CPF" in first_try

    clock.value += timedelta(seconds=4)
    second_try = handler.handle(event("222", message_id="m-4"))

    assert second_try is not SILENCE
    assert "CPF" in second_try


def test_the_repeat_guard_can_be_turned_off(tmp_path):
    """``repeat_window_seconds: 0`` devolve o comportamento anterior."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 17, 18, 25, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True, "repeat_window_seconds": 0},
        db_path=db_path,
        clock=clock,
    )

    handler.handle(event("quero agendar", message_id="m-1"))
    clock.value += timedelta(seconds=6)
    second = handler.handle(event("E renovar a receita", message_id="m-2"))

    assert second is not SILENCE
    assert "**1** - Agendar uma consulta" in second


def test_the_identity_header_does_not_hide_a_repetition():
    """As duas formas do menu são a mesma mensagem, e a assinatura sabe."""

    cold = (
        "Boa noite, Sr. Eduardo! Sou a assistente do Dr. Victor Almeida. "
        "Como posso ajudar?\n\n**1** - Agendar uma consulta"
    )
    returning = "Como posso ajudar?\n\n**1** - Agendar uma consulta"

    assert _response_signature(cold) == _response_signature(returning)
    assert _response_signature(cold) != _response_signature("Informe o CPF.")


# --------------------------------------------------------------------------
# Pronome de tratamento (17/ago/2026)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Eduardo", "Sr."),
        ("Diego", "Sr."),
        ("Carlos", "Sr."),
        ("Edimilson", "Sr."),
        ("Milton", "Sr."),
        ("Samuel", "Sr."),
        ("Djalma", "Sr."),
        ("Aline", "Sra."),
        ("Daniela", "Sra."),
        ("Rosangela", "Sra."),
        ("Thais", "Sra."),
        ("Isabel", "Sra."),
        ("Socorro", "Sra."),
        # Nomes que o Brasil usa para os dois sexos: nunca um palpite.
        ("Alex", "Sr(a)."),
        ("Ariel", "Sr(a)."),
        ("Darci", "Sr(a)."),
        ("Marion", "Sr(a)."),
        ("Arlen", "Sr(a)."),
        # Iniciais e restos ilegíveis descem para o neutro.
        ("Jo", "Sr(a)."),
        ("", "Sr(a)."),
        (None, "Sr(a)."),
    ],
)
def test_treatment_title_never_guesses(name, expected):
    assert treatment_title(name) == expected


def test_the_opening_addresses_the_patient_formally(tmp_path):
    """Regra do Victor: consultório trata paciente por Sr./Sra."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 17, 18, 25, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    reply = handler.handle(
        event("quero agendar", message_id="m-1", user_name="Eduardo Mandelli")
    )

    assert reply.startswith("Boa noite, Sr. Eduardo!")


def test_an_undecidable_name_is_addressed_neutrally(tmp_path):
    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 17, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    reply = handler.handle(
        event("quero agendar", message_id="m-1", user_name="Ariel Souza")
    )

    assert reply.startswith("Bom dia, Sr(a). Ariel!")


def test_a_nameless_contact_is_still_greeted(tmp_path):
    """Sem nome não há pronome — a saudação não pode virar "Bom dia, Sr(a).!"."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 17, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    reply = handler.handle(
        event("quero agendar", message_id="m-1", user_name="+55 71 9999-9999")
    )

    assert reply.startswith("Bom dia! Sou a assistente")


def test_silence_does_not_chain_past_the_window(tmp_path):
    """A âncora é o que está na tela, não o que ficou gravado.

    Antes desta regra, cada mensagem nova comparava com a anterior — que
    também havia sido suprimida — e a janela de dois minutos se estendia
    indefinidamente. No replay de 17/ago/2026 isso levou um "Preciso de
    urgência" a um silêncio ancorado seis minutos antes.
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 17, 18, 25, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    handler.handle(event("quero agendar", message_id="m-1"))

    clock.value += timedelta(seconds=90)
    assert handler.handle(event("E renovar a receita", message_id="m-2")) is SILENCE

    # +90s da suprimida, mas +180s da última que o paciente realmente viu.
    clock.value += timedelta(seconds=90)
    third = handler.handle(event("e o atestado tambem", message_id="m-3"))

    assert third is not SILENCE
    assert "**1** - Agendar uma consulta" in third


def test_a_silenced_message_stays_silent_on_redelivery(tmp_path):
    """A reentrega repete o silêncio, não devolve uma resposta vazia."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 17, 18, 25, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    handler.handle(event("quero agendar", message_id="m-1"))
    clock.value += timedelta(seconds=6)
    assert handler.handle(event("E renovar a receita", message_id="m-2")) is SILENCE

    clock.value += timedelta(seconds=1)
    assert handler.handle(event("E renovar a receita", message_id="m-2")) is SILENCE


def test_a_job_title_is_not_a_name(tmp_path):
    """"vendedora Mariana" era cumprimentada como "Sra. Vendedora"."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 17, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    reply = handler.handle(
        event("quero agendar", message_id="m-1", user_name="vendedora Mariana")
    )

    assert reply.startswith("Bom dia! Sou a assistente")


def test_dona_is_a_title_and_not_the_name(tmp_path):
    """"Dona Luiza" era lida como o nome "Dona"."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 17, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    reply = handler.handle(
        event("quero agendar", message_id="m-1", user_name="Dona Luiza")
    )

    assert reply.startswith("Bom dia, Dona Luiza!")


# --------------------------------------------------------------------------
# Empresa não recebe pronome (18/ago/2026)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "user_name",
    [
        "Lojão Das Bicicletas 🚲",
        "Gráfica Gmota Persona",
        "Minimal Design",
        "Bela Bike Shop",
        "Life Consultórios Torre Londres",
        "Conquista Administrativo",
        "Isabela Monitoramento",
        "Sandra - Operação Médica",
    ],
)
def test_a_company_is_greeted_without_a_treatment_title(tmp_path, user_name):
    """Regra do Victor: nome de empresa não leva Sr./Sra./Sr(a).

    O nome continua saindo — o que cai é o pronome. "Sr. Lojão" é o que sai
    de tratar um CNPJ como gente.
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 18, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    reply = handler.handle(
        event("quero agendar", message_id="m-1", user_name=user_name)
    )

    if reply is None:  # já excluído como institucional: melhor ainda
        return
    assert reply.startswith("Bom dia, ")
    for title in ("Sr. ", "Sra. ", "Sr(a). "):
        assert not reply.startswith(f"Bom dia, {title}"), reply[:60]


def test_a_person_keeps_the_treatment_title(tmp_path):
    """A regra da empresa não pode custar o pronome de uma pessoa."""

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 18, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    reply = handler.handle(
        event("quero agendar", message_id="m-1", user_name="Eduardo Mandelli")
    )

    assert reply.startswith("Bom dia, Sr. Eduardo!")


def test_a_company_word_inside_a_message_never_demotes_a_patient(tmp_path):
    """O nome exibido decide, nunca o corpo da mensagem.

    Um paciente pode muito bem falar de onde trabalha; dizer a palavra não
    pode custar a ele o tratamento. (Palavras como "laboratório" no corpo já
    excluíam a mensagem por outra regra, anterior a esta — por isso o teste
    usa termos que só a lista de nome de empresa conhece.)
    """

    db_path = tmp_path / "state" / "appointments.sqlite3"
    clock = MutableClock(datetime(2026, 8, 18, 9, 0, tzinfo=BRT))
    handler = WhatsAppAppointmentsHandler(
        {"enabled": True}, db_path=db_path, clock=clock
    )

    reply = handler.handle(
        event(
            "quero agendar, trabalho numa grafica e tenho um pet shop",
            message_id="m-1",
            user_name="Eduardo Mandelli",
        )
    )

    assert reply.startswith("Bom dia, Sr. Eduardo!")
