"""O repasse de lead lê a conversa inteira, não a última frase da secretária.

31/ago/2026, 15:08 BRT: a recepção recebeu "❓ *Lead quer agendar*" sobre a
enfermeira de um convênio. A conversa inteira — quatro mensagens, 11:47 às
15:07 — era reposição de insumo: em falta, 20 dias de prazo, "se o Sr puder me
mandar o pedido de novo". A palavra consulta não aparece uma vez.

O que disparou não foi nada que a contato escreveu. Foi a resposta da própria
secretária — "vou encaminhá-la à equipe do Dr. Victor para avaliar e
retornar" — casando ``_whatsapp_promises_team_followup``, cujo destino era a
recepção sem mais nenhuma pergunta. E o aviso afirmava, como texto literal,
"Tem interesse em marcar consulta e quer tirar dúvidas antes".

O sistema já sabia que era falso: ``Feegow: scheduling=False`` nas quatro
mensagens, lead parado em NOVO. Duas partes do mesmo processo discordaram e
nada as consultou. A terceira trava — ``contact_is_organization`` — é lista de
16 palavras genéricas, e o contato tinha nome de marca; por isso nada aqui
depende do nome do remetente.

A trava é determinística de propósito (o funil é determinístico e fail-closed,
o LLM só fala) e NÃO pode ser ``_whatsapp_has_scheduling_intent`` sozinho:
isso reabriria 24/ago, onde "Como funciona a consulta?" é uma lead legítima
que não pede vaga nenhuma. Por isso quatro rotas, e só uma termina na recepção.

Os textos abaixo são a forma linguística das mensagens reais, não a cópia
delas: o assunto original é dado clínico do Victor, que pela regra de
29/ago/2026 não entra em commit. A rota não lê nada disso — lê pedido de papel,
pedido de vaga e assunto de consultório.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.whatsapp_appointments import (
    AppointmentStore,
    WhatsAppAppointmentsHandler,
)
from gateway.run import (
    _whatsapp_contact_thread_texts,
    _whatsapp_demands_document,
    _whatsapp_has_scheduling_intent,
    _whatsapp_lead_handoff_route,
    _whatsapp_promises_team_followup,
    _whatsapp_with_interest_question,
)
from tests.gateway.appointment_helpers import (
    FakeFeegow,
    MutableClock,
    event,
    payment_config,
)

BRT = ZoneInfo("America/Bahia")
NOW = datetime(2026, 8, 31, 15, 7, tzinfo=BRT)

# Endereços sintéticos, mas com a FORMA de um celular de verdade: DDD 71 com
# nono dígito na config e sem ele na entrega, porque essa diferença já custou
# uma entrega a ninguém (14/ago/2026). Um número inventado sem essa forma
# atravessa a normalização intacto e o teste passa a medir outra coisa.
RECEPTION = "5571996690001@s.whatsapp.net"
RECEPTION_DELIVERED = "557196690001@s.whatsapp.net"
VICTOR = "557188040001@s.whatsapp.net"
SUPPLIER = "100000000000001@lid"
CONTACT = "100000000000002@lid"

# A promessa que a secretária fez às 15:07 — a forma exata que casou a rota.
SUPPLY_PROMISE = (
    "Boa tarde, Sra. Mara. Recebi sua solicitação de reenvio do pedido de "
    "insumos e vou encaminhá-la à equipe do Dr. Victor para avaliar e retornar."
)

# As quatro mensagens da contato, na ordem em que o log as registrou.
SUPPLY_THREAD = [
    "Bom dia\nMeu nome é Mara enfermeira do convênio\nEstou entrando em "
    "contato por causa dos insumos",
    "Precisa confirmar se o senhor precisa receber agora se já tem um ano",
    "Porque está em falta, o fornecedor está pedindo uns 20 dias para entrega",
    "Obrigada pelo retorno , vou pedir vai demorar o este tempo que mencionei "
    "mas será o que está no pedido\nSe o Sr puder me mandar o pedido de novo "
    "assim verifica pra ir tudo certo ,\nMeu nome é Mara estou a disposição",
]


def _history(texts):
    """O histórico como o gateway o entrega: dicts com ``role`` e ``content``."""
    return [{"role": "user", "content": text} for text in texts]


def _handler(tmp_path):
    handler = WhatsAppAppointmentsHandler(
        payment_config(reception_chat_id=RECEPTION),
        db_path=tmp_path / "appointments.sqlite3",
        feegow_client=FakeFeegow(slots=[], patients=[], procedures=[]),
        clock=MutableClock(NOW),
    )
    handler._clinical_notice_chat_id = VICTOR
    AppointmentStore(tmp_path / "appointments.sqlite3")
    return handler, tmp_path / "appointments.sqlite3"


def _outbox(db_path):
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        return list(connection.execute("SELECT chat_key, body FROM outbox_events"))


class TestTheSupplyIncident:
    """A conversa de 31/ago, replayada. Antes da correção: rota da recepção."""

    def test_the_promise_still_matches_because_that_was_never_the_bug(self):
        """A frase casa — o defeito era o destino, não o casador.

        Se este teste começar a falhar, alguém "consertou" reescrevendo o
        prompt para não dizer "equipe", o que emudece a rota inteira em vez de
        corrigi-la: a lead de 24/ago para de chegar junto.
        """
        assert _whatsapp_promises_team_followup(SUPPLY_PROMISE)

    def test_the_thread_never_asks_for_an_appointment(self):
        assert (
            _whatsapp_lead_handoff_route(
                _history(SUPPLY_THREAD[:-1]), SUPPLY_THREAD[-1]
            )
            != "agendamento"
        )

    def test_it_is_read_as_a_document_demand(self):
        """"Me manda o pedido de novo" é papel, e papel é do Victor."""
        assert (
            _whatsapp_lead_handoff_route(
                _history(SUPPLY_THREAD[:-1]), SUPPLY_THREAD[-1]
            )
            == "documento"
        )

    def test_reception_hears_nothing_about_the_nurse(self, tmp_path):
        """O teste que falha sem a correção: zero linhas para a recepção."""
        handler, db_path = _handler(tmp_path)
        handler.note_doctor_escalation(
            event(SUPPLY_THREAD[-1], chat_id=SUPPLIER, user_name="Convênio"),
            "Demanda de documento/relatório — do senhor, não da recepção.",
        )
        destinations = {chat_key for chat_key, _ in _outbox(db_path)}
        assert RECEPTION_DELIVERED not in destinations
        assert RECEPTION not in destinations
        assert destinations == {VICTOR}

    def test_victor_is_told_why_it_was_not_receptions(self, tmp_path):
        handler, db_path = _handler(tmp_path)
        handler.note_doctor_escalation(
            event(SUPPLY_THREAD[-1], chat_id=SUPPLIER, user_name="Convênio"),
            "Demanda de documento/relatório — do senhor, não da recepção.",
        )
        ((_, body),) = _outbox(db_path)
        assert "documento/relatório" in body

    def test_the_old_lock_would_not_have_saved_us(self, tmp_path):
        """O que mudou, provado: a trava de identidade ainda deixa passar.

        ``note_question_escalation`` tem uma segunda trava — "o remetente é
        uma organização?" — escrita em 24/ago exatamente para este caso. Ela
        compara o nome do contato com 16 palavras genéricas (convenio,
        operadora, clinica…) e o contato se chamava só pela marca — nome nu,
        sem nenhuma das 16 dentro —, então não casou nada. Chamada direta,
        ela ENFILEIRA para a recepção: é esta a linha que a rota nova deixou
        de chamar.

        Este teste falha no dia em que alguém achar que a lista de palavras
        resolveu o problema e remover o roteador.
        """
        handler, db_path = _handler(tmp_path)
        handler.note_question_escalation(
            event(SUPPLY_THREAD[-1], chat_id=SUPPLIER, user_name="Vitalis")
        )
        destinations = {chat_key for chat_key, _ in _outbox(db_path)}
        assert destinations == {RECEPTION_DELIVERED}

    def test_the_senders_name_was_never_the_fix(self):
        """A lista de 16 palavras deixou a operadora passar como paciente.

        Nenhuma parte desta correção conhece o nome do contato — é a conversa
        que decide. Uma lista de marcas só adiaria o mesmo defeito para a
        próxima operadora.
        """
        assert (
            _whatsapp_lead_handoff_route(
                _history(SUPPLY_THREAD[:-1]), SUPPLY_THREAD[-1]
            )
            == "documento"
        )


class TestTheLeadOf24AgoStillArrives:
    """A regressão negativa que mais importa: 24/ago não pode voltar.

    "Como funciona a consulta?" não é pedido de vaga — o corroborante que
    ``_whatsapp_has_scheduling_intent`` exige ("quando", "tem", "marcar") não
    está lá. Amarrar a rota nele mataria justamente a lead que ela salva.
    """

    QUESTION = "Bom dia! Como funciona a consulta com o Dr. Victor?"

    def test_it_is_not_a_slot_request(self):
        assert not _whatsapp_has_scheduling_intent(self.QUESTION)

    def test_but_it_is_a_conversation_about_being_seen(self):
        assert _whatsapp_lead_handoff_route([], self.QUESTION) == "confirmar"

    def test_so_the_secretary_asks_before_handing_her_over(self):
        reply = "Bom dia! Vou registrar sua dúvida para a equipe retornar."
        assert "gostaria de agendar" in _whatsapp_with_interest_question(reply)

    def test_and_the_question_is_not_asked_twice(self):
        reply = "Claro! A senhora gostaria de agendar uma consulta?"
        assert _whatsapp_with_interest_question(reply) == reply

    def test_emptying_the_constant_silences_only_the_question(self, monkeypatch):
        """A alavanca escrita no ROLLBACK tem de existir de verdade.

        Esvaziar ``_WHATSAPP_INTEREST_QUESTION`` cala a única mudança de fala
        sem desmontar o roteamento. Sem a guarda, a fala sairia com um ponto
        solto no fim — uma alavanca documentada que não funciona é pior que
        nenhuma, porque só se descobre no dia do incidente.
        """
        import gateway.run as run

        monkeypatch.setattr(run, "_WHATSAPP_INTEREST_QUESTION", "")
        reply = "Bom dia! Vou registrar sua dúvida para a equipe retornar."
        assert run._whatsapp_with_interest_question(reply) == reply

    def test_after_the_yes_reception_gets_her(self):
        """O sim vira o fato medido que faltava."""
        thread = [self.QUESTION, "Sim, quero marcar uma consulta"]
        assert (
            _whatsapp_lead_handoff_route(_history(thread[:-1]), thread[-1])
            == "agendamento"
        )


class TestReceptionKeepsRealBookings:
    """A recepção não pode perder nada que é dela."""

    @pytest.mark.parametrize(
        "text",
        [
            "Quero marcar uma consulta com o Dr. Victor",
            "Bom dia, tem horário para essa semana?",
            "Preciso remarcar minha consulta de quinta",
            "Gostaria de agendar um retorno",
        ],
    )
    def test_an_actual_booking_request_still_goes_to_reception(self, text):
        assert _whatsapp_lead_handoff_route([], text) == "agendamento"

    def test_a_booking_request_is_not_a_document_demand(self):
        """"Pedido de agendamento" é vaga, não papel.

        ``pedido`` está na lista de documentos porque foi a palavra da
        enfermeira. Sem esta exclusão ela sequestraria toda marcação para a
        linha do Victor — o oposto exato do que a recepção existe para fazer.
        """
        assert not _whatsapp_demands_document(
            ["Gostaria de fazer um pedido de agendamento para outubro"]
        )


class TestDocumentsAreVictors:
    """Victor, 31/ago/2026: documento e relatório são dele, não da recepção —
    "mesmo que não seja da lista de parceiros/empresas"."""

    @pytest.mark.parametrize(
        "text",
        [
            "Se o Sr puder me mandar o pedido de novo",
            "Doutor, preciso da receita do mês",
            "Pode assinar o laudo do meu exame?",
            "Estou aguardando o relatório para o convênio",
            "Precisamos da nota fiscal do atendimento",
            "O senhor pode emitir uma declaração de comparecimento?",
        ],
    )
    def test_a_paper_demand_is_recognised(self, text):
        assert _whatsapp_demands_document([text])

    @pytest.mark.parametrize(
        "text",
        [
            "Recebi as receitas, muito agradecido pela atenção",
            "Foi tudo bem no médico, ela me deu a receita",
            "O laudo do exame ficou ótimo",
        ],
    )
    def test_but_talking_about_one_is_not_demanding_one(self, text):
        """Relato não cria trabalho para ninguém.

        Mesma distinção das três rotas de promessa: o verbo de demanda tem de
        estar na oração. É o que separa "me manda a receita" de "ela me deu a
        receita", as duas escritas no mesmo dia.
        """
        assert not _whatsapp_demands_document([text])

    def test_a_patient_asking_for_a_paper_is_not_receptions_either(self):
        """A regra não depende de o remetente ser empresa — foi a lista de
        empresas que falhou em 31/ago."""
        assert (
            _whatsapp_lead_handoff_route([], "Doutor, preciso da receita do mês")
            == "documento"
        )


class TestNothingFallsIntoAHole:
    """Toda rota tem destino. Uma promessa sem ninguém atrás é o defeito que
    esta arquitetura já pagou quatro vezes para aprender."""

    def test_an_unrelated_conversation_still_reaches_victor(self, tmp_path):
        assert (
            _whatsapp_lead_handoff_route(
                [], "Bom dia, sou do banco, é sobre a fatura do cartão"
            )
            == "fora"
        )
        handler, db_path = _handler(tmp_path)
        handler.note_doctor_escalation(
            event("é sobre a fatura", chat_id=CONTACT), "motivo qualquer"
        )
        assert len(_outbox(db_path)) == 1

    def test_two_different_reasons_in_one_hour_are_two_notices(self, tmp_path):
        """O balde por hora não pode engolir o segundo fato."""
        handler, db_path = _handler(tmp_path)
        incoming = event("preciso do laudo", chat_id=CONTACT)
        handler.note_doctor_escalation(incoming, "Demanda de documento.")
        handler.note_doctor_escalation(incoming, "Perguntamos se quer agendar.")
        assert len(_outbox(db_path)) == 2

    def test_the_same_reason_twice_is_still_one_notice(self, tmp_path):
        handler, db_path = _handler(tmp_path)
        incoming = event("preciso do laudo", chat_id=CONTACT)
        handler.note_doctor_escalation(incoming, "Demanda de documento.")
        handler.note_doctor_escalation(incoming, "Demanda de documento.")
        assert len(_outbox(db_path)) == 1


class TestTheThreadIsWhatIsRead:
    """A fala da secretária não julga a intenção da contato — esse foi o bug."""

    def test_only_the_contacts_own_messages_count(self):
        history = [
            {"role": "user", "content": "preciso dos insumos"},
            {
                "role": "assistant",
                "content": "Vou encaminhar para agendar sua consulta",
            },
        ]
        assert _whatsapp_contact_thread_texts(history) == ["preciso dos insumos"]

    def test_the_assistants_words_cannot_create_a_lead(self):
        history = [
            {"role": "user", "content": "Bom dia, é sobre os insumos"},
            {
                "role": "assistant",
                "content": "Posso marcar uma consulta e tem horário na quinta",
            },
        ]
        assert _whatsapp_lead_handoff_route(history, "obrigada") != "agendamento"

    def test_the_current_message_is_read_even_before_history_has_it(self):
        assert _whatsapp_contact_thread_texts([], "quero marcar") == ["quero marcar"]

    def test_and_is_not_counted_twice_when_history_already_has_it(self):
        history = [{"role": "user", "content": "quero marcar"}]
        assert _whatsapp_contact_thread_texts(history, "quero marcar") == [
            "quero marcar"
        ]
