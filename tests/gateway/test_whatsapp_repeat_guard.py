"""A secretária não diz duas vezes a mesma coisa para o mesmo contato.

Incidente 26/ago/2026. A Val (71 8774-9408, chat ``170707921182854@lid``)
escreveu pedindo que um relatório da filha fosse adaptado para dar entrada na
Secretaria de Educação de Salvador, e mandou o pedido em texto seguido de três
PDFs e uma imagem — cinco mensagens em onze minutos. Recebeu a MESMA recusa
clínica de 223 caracteres cinco vezes, uma para cada mensagem.

Duas ausências produziram isso, e cada uma responde por uma metade:

  * A REGRA #4 do prompt se declarava vencedora sobre "qualquer outra
    instrução deste prompt". As regras anti-repetição moram nesse mesmo
    prompt, então ela vencia elas também. A coluna ``reasoning`` do 2º turno
    guardou o modelo cogitando o silêncio ("Deciding on silent clinical
    response") antes de a REGRA #4 passar por cima.
  * A estrada do modelo não tinha guarda de repetição nenhuma. O funil de
    agendamento tem (``_is_immediate_repeat``) e no MESMO log, 25 minutos
    antes, ele calou um chat repetido — mas para a Val o funil nem engatou
    (``scheduling=False``), e do outro lado não havia rede.

Os textos abaixo são os que a Val realmente recebeu, não invenções.
"""

import pytest

from gateway.response_filters import says_the_same_as, visible_message_signature


# ── O que a Val recebeu cinco vezes, 26/ago/2026 11:36–11:47 BRT ────────────
RECUSA = (
    "Bom dia! Sou a assistente do Dr. Victor Almeida. Este canal não presta "
    "orientação clínica. Vou encaminhar sua mensagem para a equipe do Dr. "
    "Victor. Em caso de urgência, procure atendimento médico imediatamente ou "
    "ligue 192."
)

# A mesma recusa numa reentrada: sem saudação e sem identificação, porque a
# REGRA #1 só manda identificar na primeira resposta da sessão. Byte a byte
# não bate com a de cima; para quem lê o WhatsApp é a mesma mensagem.
RECUSA_REENTRADA = (
    "Este canal não presta orientação clínica. Vou encaminhar sua mensagem "
    "para a equipe do Dr. Victor. Em caso de urgência, procure atendimento "
    "médico imediatamente ou ligue 192."
)

# A fala nova, de 26/ago: nomeia o pedido administrativo e mantém a frase que
# dispara o aviso para o Victor. Não é repetição da recusa seca.
RECONHECIMENTO = (
    "Bom dia! Sou a assistente do Dr. Victor Almeida. Recebi seu pedido de "
    "adequação do relatório para a Secretaria de Educação e os anexos. Este "
    "canal não presta orientação clínica, mas vou encaminhar tudo para a "
    "equipe do Dr. Victor avaliar e retornar. Em caso de urgência, procure "
    "atendimento médico imediatamente ou ligue 192."
)


def test_a_recusa_repetida_e_reconhecida_como_repeticao():
    """As mensagens 2 a 5 da Val eram a mensagem 1 outra vez."""
    assert says_the_same_as(RECUSA, RECUSA) is True


def test_a_saudacao_nao_disfarca_a_repeticao():
    """Onze minutos depois vira "Boa tarde", e continua sendo a mesma coisa."""
    tarde = RECUSA.replace("Bom dia!", "Boa tarde!")
    assert says_the_same_as(RECUSA, tarde) is True


def test_a_identificacao_ausente_nao_disfarca_a_repeticao():
    """Abertura fria e reentrada carregam o mesmo corpo — e o corpo é a mensagem."""
    assert says_the_same_as(RECUSA, RECUSA_REENTRADA) is True
    assert says_the_same_as(RECUSA_REENTRADA, RECUSA) is True


def test_o_reconhecimento_administrativo_nao_e_repeticao_da_recusa():
    """Nomear o pedido diz algo novo; calar isso seria o erro oposto."""
    assert says_the_same_as(RECUSA, RECONHECIMENTO) is False


@pytest.mark.parametrize(
    "outra",
    [
        "Bom dia! Sou a assistente do Dr. Victor Almeida. Recebi o comprovante.",
        "O Dr. Victor vai avaliar e retornará assim que possível.",
        "Este canal não atende urgência. Procure emergência imediatamente ou ligue 192.",
    ],
)
def test_respostas_diferentes_nunca_sao_confundidas(outra):
    """Falso positivo aqui é paciente sem resposta — o erro caro dos dois."""
    assert says_the_same_as(RECUSA, outra) is False


def test_resposta_vazia_nunca_conta_como_repeticao():
    """Vazio é o caminho de silêncio, e não é decisão desta função."""
    assert says_the_same_as(RECUSA, "") is False
    assert says_the_same_as(RECUSA, None) is False
    assert says_the_same_as("", "") is False


def test_as_duas_estradas_concordam_sobre_o_que_e_a_mesma_mensagem():
    """O funil e o modelo têm regras diferentes; o reconhecedor é um só.

    Hoje são duas cópias — ``_response_signature`` no funil e
    ``visible_message_signature`` aqui — porque
    ``gateway/platforms/whatsapp_appointments.py`` carrega trabalho não
    commitado que não pode entrar neste commit. Este teste é o que impede as
    duas de divergirem enquanto a unificação não acontece: se alguém mexer em
    uma e esquecer a outra, quebra aqui.
    """
    from gateway.platforms.whatsapp_appointments import _response_signature

    for texto in (RECUSA, RECUSA_REENTRADA, RECONHECIMENTO, "", "  ", "CPF inválido."):
        assert _response_signature(texto) == visible_message_signature(texto), texto
