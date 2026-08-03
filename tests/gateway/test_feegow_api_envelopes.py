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
