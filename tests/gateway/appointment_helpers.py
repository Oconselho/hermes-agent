"""Shared offline doubles for the deterministic WhatsApp appointment flow.

Every helper here is synthetic: no network, no real Feegow token, no
production path. ``FakeFeegow`` implements exactly the injected-client
protocol the handler uses, and ``NoCallFeegow`` turns any client call at
all into a test failure so "zero side effects" assertions are literal.
"""

from __future__ import annotations

from types import SimpleNamespace


class NoCallFeegow:
    """Any attribute access that gets called is an immediate failure."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            raise AssertionError(f"Feegow must not be called: {name}")

        return fail


class FakeFeegow:
    """Strict offline fake for the public client protocol used by the flow."""

    def __init__(
        self, *, slots=(), patients=(), appointments=(), duplicates=(), procedures=()
    ):
        self.slots = list(slots)
        self.patients = list(patients)
        self.appointments = list(appointments)
        self.duplicates = list(duplicates)
        self.procedures = list(procedures)
        self.calls = []
        self.created_patients = []
        self.created_appointments = []
        self.cancelled_appointments = []
        self.rescheduled_appointments = []
        self.edited_patients = []
        self.readbacks = {
            int(
                appointment.get(
                    "agendamento_id",
                    appointment.get("appointment_id", appointment.get("id")),
                )
            ): dict(appointment)
            for appointment in self.appointments
        }

    def list_available_slots(self, **filters):
        self.calls.append(("list_available_slots", filters))
        return list(self.slots)

    def find_patient_by_cpf(self, cpf):
        self.calls.append(("find_patient_by_cpf", {"cpf": cpf}))
        return [patient for patient in self.patients if patient.get("cpf") == cpf]

    def search_appointments(self, data_start, data_end):
        self.calls.append(
            (
                "search_appointments",
                {"data_start": data_start, "data_end": data_end},
            )
        )
        return [dict(appointment) for appointment in self.appointments]

    def list_procedures(self, **filters):
        self.calls.append(("list_procedures", filters))
        return list(self.procedures)

    def find_duplicate_appointments(self, **filters):
        self.calls.append(("find_duplicate_appointments", filters))
        return list(self.duplicates)

    def create_patient(self, **payload):
        self.calls.append(("create_patient", payload))
        self.created_patients.append(payload)
        patient = {
            "paciente_id": 501,
            "cpf": payload["cpf"],
            "data_nascimento": payload["data_nascimento"],
            "nome": payload["nome"],
            "telefone": payload["telefone"],
            "email": payload["email"],
            "sexo": payload["sexo"],
        }
        self.patients = [patient]
        return {"success": True, "content": {"paciente_id": 501}}

    def create_appointment(self, **payload):
        self.calls.append(("create_appointment", payload))
        self.created_appointments.append(payload)
        appointment_id = 900 + len(self.created_appointments)
        self.readbacks.setdefault(
            appointment_id,
            {
                "agendamento_id": appointment_id,
                "status_id": 1,
                "paciente_id": payload["paciente_id"],
                "procedimento_id": payload["procedimento_id"],
                "data": payload["data"],
                "horario": payload["horario"],
            },
        )
        return {"success": True, "content": {"agendamento_id": appointment_id}}

    def get_appointment(self, appointment_id):
        self.calls.append(("get_appointment", {"appointment_id": appointment_id}))
        return dict(self.readbacks[appointment_id])

    def cancel_appointment(self, appointment_id, motivo_id):
        self.calls.append(
            (
                "cancel_appointment",
                {"appointment_id": appointment_id, "motivo_id": motivo_id},
            )
        )
        self.cancelled_appointments.append((appointment_id, motivo_id))
        self.readbacks[appointment_id]["status_id"] = 11
        return {"success": True, "content": {"agendamento_id": appointment_id}}

    def reschedule_appointment(self, appointment_id, data, horario):
        payload = {
            "appointment_id": appointment_id,
            "data": data,
            "horario": horario,
        }
        self.calls.append(("reschedule_appointment", payload))
        self.rescheduled_appointments.append(payload)
        self.readbacks[appointment_id].update(
            {"data": data, "horario": horario, "status_id": 15}
        )
        return {"success": True, "content": {"agendamento_id": appointment_id}}

    def edit_patient(self, paciente_id, **changes):
        self.calls.append(("edit_patient", {"paciente_id": paciente_id, **changes}))
        self.edited_patients.append({"paciente_id": paciente_id, **changes})
        for patient in self.patients:
            if patient.get("paciente_id") == paciente_id:
                if "telefone" in changes:
                    patient["celular"] = changes["telefone"]
                if "email" in changes:
                    patient["email"] = changes["email"]
        return {"success": True, "content": {"paciente_id": paciente_id}}


def event(
    text: str,
    *,
    message_id: str = "m-1",
    chat_id: str = "5571999999999@s.whatsapp.net",
    chat_type: str = "dm",
    user_name: str = "Paciente",
    media_urls=(),
    media_types=None,
    timestamp=None,
):
    source = SimpleNamespace(
        platform=SimpleNamespace(value="whatsapp"),
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=chat_id,
        user_name=user_name,
        chat_name=None,
    )
    return SimpleNamespace(
        text=text,
        source=source,
        message_id=message_id,
        media_urls=list(media_urls),
        media_types=(
            list(media_types)
            if media_types is not None
            else ["image/jpeg"] * len(media_urls)
        ),
        timestamp=timestamp,
    )


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def payment_config(**overrides):
    """Fully-open staging gates: enabled + write_enabled + payment ready."""

    config = {
        "enabled": True,
        "write_enabled": True,
        "payment": {
            "enabled": True,
            "beneficiary": "Clínica Exemplo",
            "instructions": "PIX oficial 00000000000",
            "ingestion_grace_seconds": 60,
        },
    }
    config.update(overrides)
    return config


CPF = "52998224725"
CPF_FORMATTED = "529.982.247-25"
BIRTH_DATE = "01/02/1990"
CHAT_KEY = "5571999999999@s.whatsapp.net"


def known_patient(**overrides):
    patient = {
        "paciente_id": 77,
        "cpf": CPF,
        "data_nascimento": BIRTH_DATE,
        "celular": "71999999999",
        "email": "old@example.invalid",
        "nome": "Paciente Exemplo",
        "sexo": "F",
    }
    patient.update(overrides)
    return patient


def authenticate_for_action(handler, action: str, prefix: str) -> str:
    """Drive menu -> action -> CPF -> birth date -> phone confirmation."""

    handler.handle(event("Quero consultar meu agendamento", message_id=f"{prefix}-1"))
    handler.handle(event(action, message_id=f"{prefix}-2"))
    handler.handle(event(CPF_FORMATTED, message_id=f"{prefix}-3"))
    handler.handle(event(BIRTH_DATE, message_id=f"{prefix}-4"))
    return handler.handle(event("SIM", message_id=f"{prefix}-5"))


def existing_patient_flow(handler, prefix: str, *, service: str = "1") -> str:
    """Drive an already-registered patient to the authorization summary."""

    handler.handle(event("Quero agendar uma consulta", message_id=f"{prefix}-1"))
    handler.handle(event("1", message_id=f"{prefix}-2"))
    handler.handle(event(service, message_id=f"{prefix}-3"))
    handler.handle(event("1", message_id=f"{prefix}-4"))
    handler.handle(event(CPF_FORMATTED, message_id=f"{prefix}-5"))
    handler.handle(event(BIRTH_DATE, message_id=f"{prefix}-6"))
    return handler.handle(event("SIM", message_id=f"{prefix}-7"))
