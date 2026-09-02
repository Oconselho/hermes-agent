"""Real Feegow envelope and status-shape contracts for appointment automation."""

from gateway.platforms.feegow_api import FeegowClient
from gateway.platforms.whatsapp_appointments import _extract_status_id


def test_feegow_read_methods_unwrap_success_content_envelopes(monkeypatch):
    client = FeegowClient(token="test-token")
    responses = {
        "patient/search": {
            "success": True,
            "content": [{"paciente_id": 77, "cpf": "52998224725"}],
        },
        "appoints/search": {
            "success": True,
            "content": [
                {
                    "agendamento_id": 91,
                    "paciente_id": 77,
                    "data": "05-08-2026",
                    "horario": "14:00",
                }
            ],
        },
        "appoints/available-schedule": {
            "success": True,
            "content": [
                {"id": "slot-1", "data": "05-08-2026", "horario": "14:00"}
            ],
        },
    }

    def request(method, endpoint, **kwargs):
        return responses[endpoint]

    monkeypatch.setattr(client, "_request", request)

    assert client.find_patient_by_cpf("529.982.247-25") == [
        {"paciente_id": 77, "cpf": "52998224725"}
    ]
    assert client.find_duplicate_appointments(
        paciente_id=77,
        cpf="52998224725",
        data="05-08-2026",
        horario="14:00",
        profissional_id=1,
    ) == [
        {
            "agendamento_id": 91,
            "paciente_id": 77,
            "data": "05-08-2026",
            "horario": "14:00",
        }
    ]
    assert client.list_available_slots(
        procedure_id=1,
        professional_id=1,
        specialty_id=2,
        local_id=3,
        start_date="05-08-2026",
        end_date="06-08-2026",
    ) == [{"id": "slot-1", "data": "05-08-2026", "horario": "14:00"}]


def test_status_parser_accepts_text_and_nested_status_shapes():
    assert _extract_status_id({"status": "Marcado, não confirmado"}) == 1
    assert _extract_status_id({"status": {"nome": "Finalizado"}}) == 3
    assert _extract_status_id({"status": {"descricao": "Confirmado"}}) == 7
    assert _extract_status_id({"status": "Desmarcado"}) == 11
    assert _extract_status_id({"status": {"nome_status": "Remarcado"}}) == 15


def test_list_units_unwraps_matriz_unidades_envelope(monkeypatch):
    """O envelope de 02/set/2026 de ``company/list-unity`` aninha as linhas em
    ``content.matriz`` e ``content.unidades`` (objetos), não mais lista plana.
    Base estruturada ilegível não é lista vazia: os dois grupos viram linhas,
    sem duplicar a matriz quando ela reaparece dentro de ``unidades``."""
    client = FeegowClient(token="test-token")
    matriz_row = {"unidade_id": 0, "nome_fantasia": "Doutor Victor Almeida"}
    responses = {
        "company/list-unity": {
            "success": True,
            "content": {
                "matriz": [matriz_row],
                "unidades": [
                    matriz_row,
                    {"unidade_id": 1, "nome_fantasia": "Filial"},
                ],
            },
            "total": 1,
        }
    }

    def request(method, endpoint, **kwargs):
        return responses[endpoint]

    monkeypatch.setattr(client, "_request", request)
    assert client.list_units() == [
        {"unidade_id": 0, "nome_fantasia": "Doutor Victor Almeida"},
        {"unidade_id": 1, "nome_fantasia": "Filial"},
    ]


def test_list_units_empty_groups_stay_empty_not_fabricated(monkeypatch):
    """``matriz``/``unidades`` presentes porém vazios significam inventário
    vazio real — e não o dicionário inteiro virando uma "linha" falsa."""
    client = FeegowClient(token="test-token")
    responses = {
        "company/list-unity": {"success": True, "content": {"matriz": [], "unidades": []}}
    }

    def request(method, endpoint, **kwargs):
        return responses[endpoint]

    monkeypatch.setattr(client, "_request", request)
    assert client.list_units() == []


def test_search_patients_normalizes_single_object_payload(monkeypatch):
    """O payload de 02/set/2026 de ``patient/search`` é UM objeto (com listas
    ``telefones``/``email`` e CPF aninhado em ``documentos.cpf``). Ele vira
    uma linha canônica com ``paciente_id``/``cpf``/``telefone``/``email``
    escalares — e o portão de identidade por CPF continua casando."""
    client = FeegowClient(token="test-token")
    responses = {
        "patient/search": {
            "success": True,
            "total": 28,
            "content": {
                "id": 1001,
                "nome": "BLOQUEIO - DR VICTOR",
                "nascimento": "01-01-2000",
                "sexo": None,
                "telefones": ["", "71988048263"],
                "celulares": ["71988048263", ""],
                "email": ["victorfalmeida@yahoo.com.br", ""],
                "documentos": {"rg": "", "cpf": "94961522520", "sem_cpf": True},
                "convenios": [],
            },
        }
    }

    def request(method, endpoint, **kwargs):
        return responses[endpoint]

    monkeypatch.setattr(client, "_request", request)
    rows = client.find_patient_by_cpf("949.615.225-20")
    assert rows and len(rows) == 1
    row = rows[0]
    assert row["paciente_id"] == 1001
    assert row["cpf"] == "94961522520"
    assert row["telefone"] == "71988048263"
    assert row["email"] == "victorfalmeida@yahoo.com.br"
