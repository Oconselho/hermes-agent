"""
Feegow API Client — Integração com o prontuário eletrônico Feegow.

API baseada em DocPlanner / Feegow Clinic.
Autenticação via JWT no header ``x-access-token``.

Endpoints documentados utilizados por esta integração (base: ``https://api.feegow.com/v1/api``):

    GET  /patient/search             — buscar paciente por CPF, nome, telefone ou e-mail
    POST /patient/edit               — cadastrar/editar paciente
    GET  /company/list-unity         — listar unidades da clínica
    GET  /specialties/list            — listar especialidades (params: unidade_id)
    GET  /professional/list           — listar profissionais
    GET  /professional/info-specialties — detalhes do profissional
    GET  /procedures/list             — listar procedimentos
    GET  /appoints/search             — consultar agendamentos
    GET  /appoints/available-schedule — consultar vagas filtradas
    POST /appoints/new-appoint        — criar agendamento em status 1
    POST /appoints/statusUpdate       — atualizar status
    POST /appoints/cancel-appoint     — desmarcar consulta
    POST /appoints/reschedule         — remarcar consulta
    GET  /appoints/list-channel       — listar canais de agendamento

Limitação conhecida (2026-06-22):
    Tokens com audience "publicapi" recebem HTTP 403 em endpoints GET.
    Isso é uma restrição do WAF/CloudFront da Feegow. Endpoints POST
    funcionam normalmente. Para acesso completo, é necessário um token
    com permissões mais amplas (contate o suporte Feegow).

Formato de datas: DD-MM-AAAA (ex: "22-06-2026").
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.feegow.com/v1/api"
DEFAULT_TIMEOUT = 30  # segundos
MAX_RETRIES = 3
CACHE_TTL = 300  # 5 minutos para listas (unidades, especialidades, profissionais)
BACKOFF_FACTOR = 1.5  # multiplicador do backoff exponencial

# Fallback amigável quando a API está indisponível
FALLBACK_MESSAGE = (
    "Houve um problema técnico ao acessar a agenda. "
    "Por favor, entre em contato com a recepção pelo WhatsApp 71996691002. "
    "Peço desculpas pelo inconveniente."
)


class FeegowAPIError(Exception):
    """Exceção base para erros da API Feegow."""

    def __init__(self, message: str, status_code: int = 0, response_body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class FeegowAuthError(FeegowAPIError):
    """Erro de autenticação (token inválido, expirado ou sem permissão)."""
    pass


class FeegowNotFoundError(FeegowAPIError):
    """Recurso não encontrado (paciente, profissional, etc.)."""
    pass


class FeegowValidationError(FeegowAPIError):
    """Erro de validação — parâmetros obrigatórios faltando ou inválidos."""
    pass


class FeegowConflictError(FeegowAPIError):
    """Conflito explícito (HTTP 409), que exige reconciliação por leitura."""


class FeegowWriteDisabledError(FeegowAPIError):
    """Uma mutação foi recusada porque o cliente está em modo somente leitura."""


class FeegowAmbiguousResultError(FeegowAPIError):
    """O transporte falhou depois do envio e o resultado deve ser reconciliado."""


class FeegowClient:
    """Cliente para a API pública do prontuário eletrônico Feegow.

    Uso:
        client = FeegowClient(token="eyJ...")
        pacientes = client.search_patients(cpf="00000000000")
        agenda = client.create_appointment(
            paciente_id=123,
            profissional_id=1,
            unidade_id=1,
            especialidade_id=1,
            data="22-06-2026",
            horario="14:00",
        )
    """

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
        *,
        write_enabled: bool = False,
    ):
        """Inicializa o cliente Feegow em modo somente leitura por padrão.

        ``write_enabled`` precisa ser habilitado explicitamente; todos os
        mutadores também validam esse gate imediatamente antes do POST.
        """
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.write_enabled = bool(write_enabled)

        # Cache interno: {cache_key: (timestamp, data)}
        self._cache: Dict[str, Tuple[float, Any]] = {}

        # Sessão HTTP reutilizável
        self.session = requests.Session()
        self.session.headers.update({
            "x-access-token": self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        timeout: Optional[int] = None,
        *,
        max_attempts: int = MAX_RETRIES,
        ambiguous_on_transport: bool = False,
    ) -> Dict[str, Any]:
        """Perform an HTTP request.

        Reads may retry transient transport failures. Mutators call this with a
        single attempt and ``ambiguous_on_transport=True`` because replaying a
        timed-out POST can create duplicate appointments.
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        timeout = timeout or self.timeout
        attempts = max(1, int(max_attempts))
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    timeout=timeout,
                )
                if 200 <= response.status_code < 300:
                    try:
                        return response.json()
                    except ValueError:
                        return {"success": True, "raw": response.text}
                body = self._safe_json(response)
                if response.status_code in {401, 403}:
                    raise FeegowAuthError(
                        "Token inválido, expirado ou sem permissão.",
                        status_code=response.status_code,
                        response_body=body,
                    )
                if response.status_code == 404:
                    raise FeegowNotFoundError(
                        "Recurso não encontrado na API Feegow.",
                        status_code=404,
                        response_body=body,
                    )
                if response.status_code == 409:
                    raise FeegowConflictError(
                        "Conflito informado pela API Feegow.",
                        status_code=409,
                        response_body=body,
                    )
                if response.status_code == 422:
                    # Feegow reports field errors as {"<field>": ["reason"]}
                    # and sends no "message" key at all. Reading only
                    # "message" logged a bare "Parâmetros inválidos ou
                    # faltando:" with nothing after the colon, which is how a
                    # missing mandatory field stayed invisible for days. Fall
                    # back to naming the rejected fields. The 422 body of this
                    # API carries validation text, never patient data.
                    message = body.get("message", "") if isinstance(body, dict) else ""
                    if not message and isinstance(body, dict):
                        message = "; ".join(
                            f"{field}: {'; '.join(str(item) for item in reasons)}"
                            if isinstance(reasons, (list, tuple))
                            else f"{field}: {reasons}"
                            for field, reasons in list(body.items())[:5]
                        )
                    if not message:
                        message = str(body)[:200]
                    raise FeegowValidationError(
                        f"Parâmetros inválidos ou faltando: {message}".strip(),
                        status_code=422,
                        response_body=body,
                    )
                raise FeegowAPIError(
                    f"Erro HTTP {response.status_code}",
                    status_code=response.status_code,
                    response_body=body,
                )
            except FeegowAPIError:
                raise
            except (requests.Timeout, requests.ConnectionError) as exc:
                if ambiguous_on_transport:
                    raise FeegowAmbiguousResultError(
                        "Resultado ambíguo após falha de transporte; reconcile por leitura."
                    ) from exc
                last_error = FeegowAPIError(
                    f"Falha de transporte (tentativa {attempt}/{attempts})"
                )
            except Exception as exc:
                if ambiguous_on_transport:
                    raise FeegowAmbiguousResultError(
                        "Resultado ambíguo após falha de transporte; reconcile por leitura."
                    ) from exc
                last_error = FeegowAPIError(f"Erro inesperado: {exc}")

            if attempt < attempts:
                wait = BACKOFF_FACTOR ** attempt
                logger.warning(
                    "Feegow API transient failure (attempt %d/%d), retrying in %.1fs",
                    attempt,
                    attempts,
                    wait,
                )
                time.sleep(wait)

        raise last_error if last_error else FeegowAPIError("Erro desconhecido")

    def _require_write_enabled(self) -> None:
        if not self.write_enabled:
            raise FeegowWriteDisabledError(
                "Escritas Feegow estão desabilitadas; habilite write_enabled explicitamente."
            )

    def _mutating_post(self, endpoint: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Execute exactly one mutation attempt and surface ambiguous outcomes."""
        self._require_write_enabled()
        return self._request(
            "POST",
            endpoint,
            json_data=body,
            max_attempts=1,
            ambiguous_on_transport=True,
        )

    @staticmethod
    def _safe_json(response: requests.Response) -> Any:
        """Tenta decodificar JSON da resposta; retorna texto bruto se falhar."""
        try:
            return response.json()
        except ValueError:
            return response.text[:500]

    @staticmethod
    def _grouped_rows(value: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Rows of a ``{grupo: [linhas]}`` envelope, or ``None`` if not grouped.

        The 02/set/2026 ``company/list-unity`` answer nests its rows in
        ``content.matriz`` and ``content.unidades`` (dicts of lists) instead
        of a flat list. A grouped envelope has EVERY value as a list: return
        the concatenation of the groups, keeping only dict rows and dropping
        exact duplicates (the matriz row may also appear as a unit). Return
        ``[]`` when the groups exist but are empty — a real empty inventory,
        not an unreadable shape — and ``None`` when any value is a scalar,
        which is a single-object row (the new ``patient/search`` payload has
        ``telefones``/``email`` lists next to scalar fields) and must reach
        the caller as one row, never collapse to nothing.
        """
        if not value or not all(
            isinstance(item, (list, tuple)) for item in value.values()
        ):
            return None
        rows: List[Dict[str, Any]] = []
        for group in value.values():
            for item in group:
                if isinstance(item, dict) and item not in rows:
                    rows.append(item)
        return rows

    @staticmethod
    def _collection_content(result: Any) -> List[Dict[str, Any]]:
        """Unwrap Feegow's ``success/content`` read envelope."""
        if isinstance(result, (list, tuple)):
            return [item for item in result if isinstance(item, dict)]
        if not isinstance(result, dict) or result.get("success") is False or result.get("error"):
            return []
        for key in (
            "content", "data", "results", "items", "slots", "agendamentos",
            "appointments", "pacientes", "horarios",
        ):
            if key not in result:
                continue
            value = result[key]
            if isinstance(value, (list, tuple)):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                grouped = FeegowClient._grouped_rows(value)
                if grouped is not None:
                    return grouped
                nested = FeegowClient._collection_content(value)
                return nested or [value]
        return [result] if result else []

    @staticmethod
    def _normalize_available_schedule(
        payload: Any,
        *,
        procedure_id: int,
        professional_id: int,
        local_id: int,
    ) -> List[Dict[str, Any]]:
        """Flatten ``appoints/available-schedule`` into one dict per free slot.

        Feegow does not answer this endpoint with a list of slots. It answers
        with a tree keyed by professional and location, whose leaves are the
        free times of one calendar day::

            content:
              profissional_id:
                "1":
                  local_id:
                    "1":
                      "2026-08-12": ["14:00:00", "14:30:00"]
                      "2026-08-26": []
                  age_restriction: {"age_from": 1, "age_to": 120}

        ``_collection_content`` cannot express that shape — it finds no
        collection key under ``content`` and returns the branch itself as a
        single "row", which then carries no date or time and is dropped by
        ``filter_eligible_slots``. Every booking therefore died with an empty
        agenda even on HTTP 200.

        Only the professional and location that were actually requested are
        read, so a future multi-professional answer cannot offer someone
        else's agenda. Sibling keys that are not dates (``age_restriction``)
        and days whose list is empty produce nothing.

        No ``id`` is emitted: the slot id is an internal handle only —
        ``create_appointment`` sends ``data``/``horario`` — and
        ``filter_eligible_slots`` already derives a deterministic opaque id.
        Inventing a remote-looking identifier here would be worse than none.

        A flat list is passed through untouched, so a plain answer (and every
        existing caller and fixture) keeps working. Anything unparseable
        yields ``[]``: an empty agenda is handed off to reception, which is
        the fail-closed outcome.
        """

        if isinstance(payload, (list, tuple)):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        if payload.get("success") is False or payload.get("error"):
            return []

        content = payload.get("content", payload)
        if isinstance(content, (list, tuple)):
            return [item for item in content if isinstance(item, dict)]
        if not isinstance(content, dict):
            return []

        by_professional = content.get("profissional_id")
        if not isinstance(by_professional, dict):
            return []
        professional_branch = by_professional.get(str(professional_id))
        if not isinstance(professional_branch, dict):
            return []
        by_local = professional_branch.get("local_id")
        if not isinstance(by_local, dict):
            return []
        local_branch = by_local.get(str(local_id))
        if not isinstance(local_branch, dict):
            return []

        slots: List[Dict[str, Any]] = []
        for raw_day, raw_times in local_branch.items():
            try:
                day = datetime.strptime(str(raw_day), "%Y-%m-%d").date()
            except (TypeError, ValueError):
                continue  # not a date key (e.g. age_restriction)
            if not isinstance(raw_times, (list, tuple)):
                continue
            for raw_time in raw_times:
                text = str(raw_time).strip()
                for fmt in ("%H:%M:%S", "%H:%M"):
                    try:
                        moment = datetime.strptime(text, fmt).time()
                        break
                    except ValueError:
                        moment = None
                if moment is None:
                    continue
                slots.append(
                    {
                        "data": day.isoformat(),
                        "horario": moment.strftime("%H:%M"),
                        "procedimento_id": int(procedure_id),
                        "profissional_id": int(professional_id),
                        "local_id": int(local_id),
                    }
                )
        slots.sort(key=lambda slot: (slot["data"], slot["horario"]))
        return slots

    def _cached(
        self,
        cache_key: str,
        fetcher: Callable[[], Any],
        ttl: int = CACHE_TTL,
    ) -> Any:
        """Retorna dados do cache ou busca via ``fetcher``.

        Args:
            cache_key: Chave única para o cache.
            fetcher: Função sem argumentos que busca os dados.
            ttl: Tempo de vida do cache em segundos (padrão: 300s = 5min).

        Returns:
            Os dados (do cache ou frescos).
        """
        now = time.time()
        if cache_key in self._cache:
            ts, data = self._cache[cache_key]
            if now - ts < ttl:
                logger.debug("Feegow cache hit: %s (%.0fs old)", cache_key, now - ts)
                return data
            logger.debug("Feegow cache expired: %s", cache_key)

        data = fetcher()
        self._cache[cache_key] = (now, data)
        return data

    def _clear_cache(self, prefix: str = "") -> None:
        """Limpa o cache inteiro ou apenas entradas com determinado prefixo."""
        if not prefix:
            self._cache.clear()
            logger.debug("Feegow cache fully cleared")
        else:
            keys = [k for k in self._cache if k.startswith(prefix)]
            for k in keys:
                del self._cache[k]
            logger.debug("Feegow cache cleared for prefix=%r (%d entries)", prefix, len(keys))

    def _safe_call(self, func: Callable[[], Any], fallback: Any = None) -> Any:
        """Executa ``func`` com graceful degradation.

        Se a função levantar FeegowAPIError, faz log do erro e retorna
        ``fallback`` (ou um dict com a mensagem amigável).

        Args:
            func: Função sem argumentos que faz a chamada à API.
            fallback: Valor a retornar em caso de erro. Se None, usa dict de erro.

        Returns:
            Resultado da função ou fallback em caso de erro.
        """
        try:
            return func()
        except FeegowAPIError as e:
            logger.error("Feegow API error in safe_call: %s", e)
            if fallback is not None:
                return fallback
            return {"error": True, "message": FALLBACK_MESSAGE, "detail": str(e)}
        except Exception as e:
            logger.error("Unexpected error in safe_call: %s", e)
            if fallback is not None:
                return fallback
            return {"error": True, "message": FALLBACK_MESSAGE}

    # ==================================================================
    # PACIENTES
    # ==================================================================

    def search_patients(
        self,
        cpf: Optional[str] = None,
        nome: Optional[str] = None,
        telefone: Optional[str] = None,
        email: Optional[str] = None,
        *,
        strict: bool = False,
    ) -> List[Dict[str, Any]]:
        """Busca pacientes na base Feegow e normaliza o envelope em uma lista.

        Usa o endpoint ``patient/search`` (singular, GET).
        Parâmetros em português: ``paciente_cpf``, ``paciente_nome``.

        ``strict=True`` desliga a degradação graciosa e propaga o erro. Quem
        só enriquece contexto para o modelo prefere lista vazia a exceção; o
        portão de identidade do agendamento, não: para ele lista vazia
        significa "este CPF não existe na base", e a decisão seguinte é criar
        um paciente novo. Em 13/ago/2026 um 409 da Feegow virou exatamente
        isso — base ilegível lida como base vazia. Base ilegível não é base
        vazia.
        """
        params: Dict[str, str] = {}
        if cpf:
            params["paciente_cpf"] = cpf
        if nome:
            params["paciente_nome"] = nome
        if telefone:
            params["telefone"] = telefone
        if email:
            params["email"] = email

        if not params:
            raise FeegowValidationError(
                "Informe ao menos um critério de busca: cpf, nome, telefone ou email."
            )

        logger.info("Feegow: searching patients with %s", list(params.keys()))

        def read() -> List[Dict[str, Any]]:
            return self._normalize_patient_rows(
                self._request("GET", "patient/search", params=params)
            )

        if strict:
            return read()
        return self._safe_call(read, fallback=[])

    @staticmethod
    def _normalize_patient_rows(result: Any) -> List[Dict[str, Any]]:
        """Normalize every ``patient/search`` shape into canonical rows.

        The 02/set/2026 API answers this endpoint with ONE object — scalar
        fields plus list-valued ``telefones``/``celulares``/``email`` and a
        nested ``documentos`` dict — where legacy answers carried a flat
        list of rows with top-level ``paciente_id``/``cpf``/``telefone``.
        Legacy rows pass through unchanged; the new object is rewritten to
        the canonical keys the gateway and the empty-turn supervisor read
        (``paciente_id``, ``cpf``, ``telefone``, ``email``), preserving the
        first non-empty value of each list. Nested ``documentos.cpf`` also
        feeds the canonical ``cpf`` so identity gates keep matching.
        """
        rows = FeegowClient._collection_content(result)
        normalized: List[Dict[str, Any]] = []
        for row in rows:
            if "paciente_id" in row or "patient_id" in row:
                normalized.append(row)
                continue
            if "id" not in row:
                normalized.append(row)
                continue
            item = dict(row)

            def first(value: Any) -> Any:
                if isinstance(value, (list, tuple)):
                    for entry in value:
                        if entry not in (None, ""):
                            return entry
                    return None
                return value

            documents = item.get("documentos")
            cpf = first(item.get("cpf"))
            if cpf in (None, "") and isinstance(documents, dict):
                cpf = first(documents.get("cpf"))
            item["cpf"] = cpf
            item["telefone"] = first(item.get("telefone")) or first(
                item.get("celulares")
            )
            item["email"] = first(item.get("email"))
            item["paciente_id"] = item.get("id")
            normalized.append(item)
        return normalized

    def create_patient(
        self,
        nome: str,
        cpf: str,
        data_nascimento: str,
        telefone: str,
        email: str = "",
        sexo: str = "M",
        **kwargs,
    ) -> Dict[str, Any]:
        """Create a patient through the current ``patient/edit`` endpoint."""
        body: Dict[str, Any] = {
            "nome_completo": nome,
            "nome_paciente": nome,
            "cpf": cpf,
            "data_nascimento": data_nascimento,
            "telefone": telefone,
            "email": email,
            "sexo": sexo,
            **kwargs,
        }
        result = self._mutating_post("patient/edit", body)
        if result.get("success"):
            self._clear_cache("patients:")
        return result

    def edit_patient(self, paciente_id: int, **changes: Any) -> Dict[str, Any]:
        """Edit explicitly supplied patient fields; never infer or overwrite blanks."""
        if not paciente_id:
            raise FeegowValidationError("paciente_id é obrigatório")
        allowed = {
            "nome_completo", "nome_paciente", "cpf", "data_nascimento",
            "telefone", "email", "sexo", "endereco", "numero",
            "complemento", "bairro", "cidade", "estado", "cep",
        }
        body = {key: value for key, value in changes.items() if key in allowed}
        if not body:
            raise FeegowValidationError("Informe ao menos um campo permitido para editar")
        body["paciente_id"] = int(paciente_id)
        result = self._mutating_post("patient/edit", body)
        if result.get("success"):
            self._clear_cache("patients:")
        return result

    # ==================================================================
    # UNIDADES
    # ==================================================================

    def list_units(self) -> List[Dict[str, Any]]:
        """Lista as unidades (clínicas/consultórios) disponíveis.

        Returns:
            Lista de dicionários com dados das unidades.
            Em caso de erro (ex: token publicapi sem permissão GET),
            retorna lista vazia.

        Note:
            Este endpoint usa GET. Tokens com audience "publicapi"
            podem receber HTTP 403. Nesse caso, retorna [].
        """
        logger.info("Feegow: listing units")
        return self._safe_call(
            lambda: self._cached(
                "units:all",
                lambda: self._collection_content(
                    self._request("GET", "company/list-unity")
                ),
            ),
            fallback=[],
        )

    # ==================================================================
    # ESPECIALIDADES
    # ==================================================================

    def list_specialties(
        self,
        unidade_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Lista as especialidades médicas disponíveis.

        Args:
            unidade_id: ID da unidade para filtrar (opcional).

        Returns:
            Lista de dicionários com dados das especialidades.
            Em caso de erro, retorna lista vazia.
        """
        params = {}
        if unidade_id:
            params["unidade_id"] = unidade_id

        cache_key = f"specialties:{unidade_id or 'all'}"
        logger.info("Feegow: listing specialties (unidade_id=%s)", unidade_id)
        return self._safe_call(
            lambda: self._cached(
                cache_key,
                lambda: self._collection_content(
                    self._request("GET", "specialties/list", params=params)
                ),
            ),
            fallback=[],
        )

    # ==================================================================
    # PROFISSIONAIS
    # ==================================================================

    def list_professionals(
        self,
        unidade_id: Optional[int] = None,
        especialidade_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Lista os profissionais (médicos) disponíveis.

        Args:
            unidade_id: ID da unidade para filtrar (opcional).
            especialidade_id: ID da especialidade para filtrar (opcional).

        Returns:
            Lista de dicionários com dados dos profissionais.
            Em caso de erro, retorna lista vazia.
        """
        params: Dict[str, Any] = {}
        if unidade_id:
            params["unidade_id"] = unidade_id
        if especialidade_id:
            params["especialidade_id"] = especialidade_id

        cache_key = f"professionals:{unidade_id or 0}:{especialidade_id or 0}"
        logger.info(
            "Feegow: listing professionals (unidade=%s, especialidade=%s)",
            unidade_id, especialidade_id,
        )
        return self._safe_call(
            lambda: self._cached(
                cache_key,
                lambda: self._collection_content(
                    self._request(
                        "GET", "professional/list", params=params
                    )
                ),
            ),
            fallback=[],
        )

    def get_professional_info(
        self,
        profissional_id: int,
    ) -> Dict[str, Any]:
        """Obtém informações detalhadas de um profissional, incluindo
        suas especialidades.

        Args:
            profissional_id: ID do profissional na base Feegow.

        Returns:
            Dicionário com dados detalhados do profissional.
            Em caso de erro, retorna dict com chave "error".
        """
        logger.info("Feegow: getting info for professional %d", profissional_id)
        cache_key = f"professional_info:{profissional_id}"
        return self._safe_call(
            lambda: self._cached(
                cache_key,
                lambda: self._request(
                    "GET", "professional/info-specialties",
                    params={"profissional_id": profissional_id},
                ),
            ),
        )

    # ==================================================================
    # PROCEDIMENTOS
    # ==================================================================

    def list_procedures(
        self,
        unidade_id: Optional[int] = None,
        especialidade_id: Optional[int] = None,
        profissional_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Lista os procedimentos (tipos de consulta/exame) disponíveis.

        Args:
            unidade_id: ID da unidade (opcional).
            especialidade_id: ID da especialidade (opcional).
            profissional_id: ID do profissional (opcional).

        Returns:
            Lista de dicionários com dados dos procedimentos.
            Em caso de erro, retorna lista vazia.
        """
        params: Dict[str, Any] = {}
        if unidade_id:
            params["unidade_id"] = unidade_id
        if especialidade_id:
            params["especialidade_id"] = especialidade_id
        if profissional_id:
            params["profissional_id"] = profissional_id

        logger.info("Feegow: listing procedures")
        cache_key = f"procedures:{unidade_id or 0}:{especialidade_id or 0}:{profissional_id or 0}"
        return self._safe_call(
            lambda: self._cached(
                cache_key,
                lambda: self._collection_content(
                    self._request(
                        "GET", "procedures/list", params=params
                    )
                ),
            ),
            fallback=[],
        )

    # ==================================================================
    # AGENDA / CONSULTAS
    # ==================================================================

    def search_appointments(
        self,
        data_start: str,
        data_end: str,
    ) -> List[Dict[str, Any]]:
        """Consulta horários disponíveis na agenda.

        Args:
            data_start: Data inicial no formato DD-MM-AAAA (ex: "22-06-2026").
            data_end: Data final no formato DD-MM-AAAA (ex: "29-06-2026").

        Returns:
            Lista de slots/consultas no período.
            Em caso de erro (ex: token publicapi sem permissão GET),
            retorna lista vazia.

        Note:
            Este endpoint usa GET. Tokens com audience "publicapi"
            podem receber HTTP 403.
        """
        params = {
            "data_start": data_start,
            "data_end": data_end,
        }
        logger.info(
            "Feegow: searching appointments from %s to %s",
            data_start, data_end,
        )
        return self._safe_call(
            lambda: self._collection_content(
                self._request("GET", "appoints/search", params=params)
            ),
            fallback=[],
        )

    def list_available_slots(
        self,
        *,
        procedure_id: int,
        professional_id: int,
        specialty_id: int,
        local_id: int,
        start_date: str,
        end_date: str,
    ) -> List[Dict[str, Any]]:
        """Read real agenda slots with all identifiers required by the flow.

        ``tipo`` is mandatory on this endpoint and its absence is not a soft
        failure: Feegow answers HTTP 422 ``{"tipo": ["O campo tipo é
        obrigatório."]}``, which ``_safe_call`` turns into an empty agenda,
        which the booking flow reads as "no vacancy" and hands off to
        reception. Every appointment — teleconsultation and in-person alike —
        failed that way. ``"P"`` selects the by-procedure agenda, matching the
        ``procedimento_id`` sent alongside it.
        """
        params = {
            "data_start": start_date,
            "data_end": end_date,
            "procedimento_id": int(procedure_id),
            "profissional_id": int(professional_id),
            "especialidade_id": int(specialty_id),
            "local_id": int(local_id),
            "tipo": "P",
        }
        return self._safe_call(
            lambda: self._normalize_available_schedule(
                self._request("GET", "appoints/available-schedule", params=params),
                procedure_id=int(procedure_id),
                professional_id=int(professional_id),
                local_id=int(local_id),
            ),
            fallback=[],
        )

    def find_patient_by_cpf(self, cpf: str) -> List[Dict[str, Any]]:
        """Return exact CPF candidates for the deterministic identity gate.

        Reads strictly: this is the only caller whose empty result is a
        decision ("no such patient → create one"), so an unreadable base must
        raise here and let the flow fail closed to reception.
        """
        digits = "".join(ch for ch in str(cpf) if ch.isdigit())
        return [
            patient
            for patient in self.search_patients(cpf=digits, strict=True)
            if "".join(ch for ch in str(patient.get("cpf", "")) if ch.isdigit()) == digits
        ]

    def find_duplicate_appointments(
        self,
        *,
        paciente_id: Optional[int],
        cpf: str,
        data: str,
        horario: str,
        profissional_id: int,
    ) -> List[Dict[str, Any]]:
        """Read before create/remarcar; ambiguity fails closed in the caller."""
        day = datetime.strptime(
            data,
            "%Y-%m-%d" if len(str(data).split("-", 1)[0]) == 4 else "%d-%m-%Y",
        ).strftime("%d-%m-%Y")
        rows = self.search_appointments(day, day)
        duplicates: List[Dict[str, Any]] = []
        for row in rows:
            same_patient = paciente_id is not None and str(
                row.get("paciente_id", row.get("patient_id", ""))
            ) == str(paciente_id)
            same_cpf = "".join(
                ch for ch in str(row.get("cpf", "")) if ch.isdigit()
            ) == "".join(ch for ch in str(cpf) if ch.isdigit())
            same_time = str(row.get("horario", row.get("time", "")))[:5] == str(horario)[:5]
            raw_professional = row.get(
                "profissional_id", row.get("professional_id")
            )
            same_professional = raw_professional in (None, "") or str(
                raw_professional
            ) == str(profissional_id)
            if (same_patient or same_cpf) and same_time and same_professional:
                duplicates.append(row)
        return duplicates

    def create_appointment(
        self,
        paciente_id: int,
        profissional_id: int,
        unidade_id: int,
        especialidade_id: int,
        data: str,
        horario: str,
        procedimento_id: Optional[int] = None,
        canal_id: Optional[int] = None,
        convenio_id: Optional[int] = None,
        plano_id: Optional[int] = None,
        observacoes: str = "",
        **kwargs,
    ) -> Dict[str, Any]:
        """Create an appointment in status 1 using the current endpoint."""
        body: Dict[str, Any] = {
            "paciente_id": int(paciente_id),
            "profissional_id": int(profissional_id),
            "local_id": int(kwargs.pop("local_id", unidade_id)),
            "especialidade_id": int(especialidade_id),
            "data": data,
            "horario": horario,
            "status_id": 1,
        }
        optional = {
            "procedimento_id": procedimento_id,
            "canal_id": canal_id,
            "convenio_id": convenio_id,
            "plano_id": plano_id,
            "notas": observacoes or kwargs.pop("notas", None),
            "valor": kwargs.pop("valor", None),
            "enviar_confirmacao": kwargs.pop("enviar_confirmacao", None),
            "encaixe": kwargs.pop("encaixe", None),
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        # A caller cannot smuggle a second status through kwargs.
        body.update({key: value for key, value in kwargs.items() if key != "status_id"})
        return self._mutating_post("appoints/new-appoint", body)

    def update_appointment_status(
        self, appointment_id: int, status_id: int
    ) -> Dict[str, Any]:
        """Update status, except status 7 which is forbidden by policy."""
        if int(status_id) == 7:
            raise FeegowValidationError("status 7 é proibido por política de segurança")
        return self._mutating_post(
            "appoints/statusUpdate",
            {"agendamento_id": int(appointment_id), "status_id": int(status_id)},
        )

    def cancel_appointment(
        self, appointment_id: int, motivo_id: int
    ) -> Dict[str, Any]:
        if not motivo_id:
            raise FeegowValidationError("motivo_id é obrigatório para cancelamento")
        return self._mutating_post(
            "appoints/cancel-appoint",
            {"agendamento_id": int(appointment_id), "motivo_id": int(motivo_id)},
        )

    def reschedule_appointment(
        self, appointment_id: int, data: str, horario: str
    ) -> Dict[str, Any]:
        return self._mutating_post(
            "appoints/reschedule",
            {"agendamento_id": int(appointment_id), "data": data, "horario": horario},
        )

    def get_appointment(self, appointment_id: int) -> Dict[str, Any]:
        """Read one exact appointment for mandatory post-write reconciliation."""
        return self._request(
            "GET", "appoints/search", params={"agendamento_id": int(appointment_id)}
        )

    def list_channels(self) -> List[Dict[str, Any]]:
        """Lista os canais de agendamento disponíveis.

        Returns:
            Lista de canais (ex: WhatsApp, telefone, presencial).
            Em caso de erro, retorna lista vazia.
        """
        logger.info("Feegow: listing appointment channels")
        return self._safe_call(
            lambda: self._cached(
                "channels:all",
                lambda: self._collection_content(
                    self._request("GET", "appoints/list-channel")
                ),
            ),
            fallback=[],
        )

    # ==================================================================
    # Métodos de conveniência para a secretária WhatsApp
    # ==================================================================

    def find_patient_for_secretary(
        self,
        cpf: Optional[str] = None,
        nome: Optional[str] = None,
        telefone: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Busca paciente e retorna o primeiro resultado de forma amigável.

        Método de conveniência para a secretária do WhatsApp.
        Faz a busca e extrai o primeiro paciente encontrado.

        Args:
            cpf: CPF do paciente.
            nome: Nome completo ou parcial.
            telefone: Telefone com DDD.

        Returns:
            Dicionário com dados do paciente ou None se não encontrado/erro.
        """
        try:
            result = self.search_patients(cpf=cpf, nome=nome, telefone=telefone)
        except Exception:
            logger.warning("Feegow find_patient_for_secretary: search failed")
            return None
        if isinstance(result, list) and result:
            return result[0] if isinstance(result[0], dict) else None
        return None

    def get_available_slots_text(
        self,
        data_start: str,
        data_end: str,
    ) -> str:
        """Retorna uma descrição em português dos horários disponíveis.

        Método de conveniência para injeção no contexto da secretária.

        Args:
            data_start: Data inicial (DD-MM-AAAA).
            data_end: Data final (DD-MM-AAAA).

        Returns:
            Texto formatado com os horários ou mensagem de erro amigável.
        """
        result = self.search_appointments(data_start, data_end)
        if isinstance(result, dict):
            if result.get("error") or result.get("success") is False:
                return FALLBACK_MESSAGE
            # Feegow returns successful collection endpoints in an envelope:
            # {"success": true, "total": N, "content": [...]}.  Older
            # responses may use ``data`` instead of ``content``.
            result = result.get("content", result.get("data", []))
        if not result:
            return (
                f"Não foram encontrados horários disponíveis entre "
                f"{data_start} e {data_end}. "
                f"Sugira ao paciente entrar em contato com a recepção "
                f"pelo WhatsApp 71996691002."
            )
        # Se a API retornar dados, formatar
        if isinstance(result, list):
            if len(result) == 0:
                return (
                    f"Não há horários disponíveis entre {data_start} e {data_end}. "
                    f"Sugira ao paciente contatar a recepção."
                )
            lines = ["Horários disponíveis na agenda do Dr. Victor:"]
            for slot in result[:10]:  # máximo 10 horários
                data = slot.get("data", "?")
                hora = slot.get("horario", slot.get("hora", "?"))
                lines.append(f"  • {data} às {hora}")
            return "\n".join(lines)
        return FALLBACK_MESSAGE

    def health_check(self) -> bool:
        """Verifica se a API Feegow está acessível com o token atual.

        Tenta acessar o endpoint raiz (que retorna "OK") e verifica
        se o token é aceito.

        Returns:
            True se a API está acessível e o token é válido.
        """
        try:
            # GET / retorna "OK" mesmo sem token
            r = self.session.get(
                self.base_url.rstrip("/v1/api"),
                timeout=10,
            )
            return r.status_code in (200, 404, 422)
        except Exception:
            return False
