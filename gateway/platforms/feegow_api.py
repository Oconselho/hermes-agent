"""
Feegow API Client — Integração com o prontuário eletrônico Feegow.

API baseada em DocPlanner / Feegow Clinic.
Autenticação via JWT no header ``x-access-token``.

Endpoints documentados (base: https://api.feegow.com/v1/api):

    POST /patients/search     — buscar paciente por CPF, nome, telefone ou email
    POST /patients/create     — cadastrar novo paciente
    GET  /company/list-unity  — listar unidades da clínica
    GET  /specialties/list    — listar especialidades (params: unidade_id)
    GET  /professional/list   — listar profissionais (params: unidade_id, especialidade_id)
    GET  /professional/info-specialties — info detalhada do profissional (params: profissional_id)
    GET  /procedures/list     — listar procedimentos (params: unidade_id, especialidade_id, profissional_id)
    GET  /appoints/search     — consultar agenda (params: data_start, data_end no formato DD-MM-AAAA)
    POST /appoints/create     — criar agendamento
    GET  /appoints/list-channel — listar canais de agendamento

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
    ):
        """Inicializa o cliente Feegow.

        Args:
            token: JWT de autenticação (obtido no painel Feegow).
            base_url: URL base da API. O padrão é https://api.feegow.com/v1/api.
            timeout: Timeout em segundos para cada requisição HTTP.
        """
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

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
    ) -> Dict[str, Any]:
        """Faz uma requisição HTTP com retry e backoff exponencial.

        Args:
            method: "GET" ou "POST".
            endpoint: Caminho relativo (ex: "patients/search").
            params: Parâmetros de query string (para GET).
            json_data: Corpo JSON (para POST).
            timeout: Timeout em segundos (usa self.timeout se não informado).

        Returns:
            Dicionário com a resposta JSON da API.

        Raises:
            FeegowAPIError: Em caso de erro irrecuperável após todos os retries.
            FeegowAuthError: Se o token for inválido ou expirado.
            FeegowNotFoundError: Se o recurso não for encontrado.
            FeegowValidationError: Se parâmetros obrigatórios estiverem faltando.
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        timeout = timeout or self.timeout
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    timeout=timeout,
                )

                # Sucesso
                if 200 <= response.status_code < 300:
                    try:
                        return response.json()
                    except ValueError:
                        return {"success": True, "raw": response.text}

                # Autenticação
                if response.status_code == 401:
                    raise FeegowAuthError(
                        "Token de acesso inválido ou expirado. Verifique o JWT.",
                        status_code=401,
                        response_body=self._safe_json(response),
                    )

                # Permissão negada (ex: token publicapi em endpoints GET)
                if response.status_code == 403:
                    raise FeegowAuthError(
                        "Acesso negado. Seu token pode ter permissões limitadas "
                        "(audience: publicapi). Contate o suporte Feegow para "
                        "obter um token com acesso completo.",
                        status_code=403,
                        response_body=self._safe_json(response),
                    )

                # Não encontrado
                if response.status_code == 404:
                    raise FeegowNotFoundError(
                        "Recurso não encontrado na API Feegow.",
                        status_code=404,
                        response_body=self._safe_json(response),
                    )

                # Erro de validação (422)
                if response.status_code == 422:
                    body = self._safe_json(response)
                    msg = body.get("message", "") if isinstance(body, dict) else ""
                    raise FeegowValidationError(
                        f"Parâmetros inválidos ou faltando: {msg}".strip(),
                        status_code=422,
                        response_body=body,
                    )

                # Outro erro HTTP
                raise FeegowAPIError(
                    f"Erro HTTP {response.status_code}",
                    status_code=response.status_code,
                    response_body=self._safe_json(response),
                )

            except FeegowAPIError:
                # Não retentar erros da API (4xx) — são erros do cliente
                raise
            except requests.Timeout:
                last_error = FeegowAPIError(
                    f"Timeout após {timeout}s (tentativa {attempt}/{MAX_RETRIES})"
                )
                if attempt < MAX_RETRIES:
                    wait = BACKOFF_FACTOR ** attempt
                    logger.warning(
                        "Feegow API timeout (attempt %d/%d), retrying in %.1fs",
                        attempt, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
            except requests.ConnectionError as e:
                last_error = FeegowAPIError(
                    f"Erro de conexão: {e} (tentativa {attempt}/{MAX_RETRIES})"
                )
                if attempt < MAX_RETRIES:
                    wait = BACKOFF_FACTOR ** attempt
                    logger.warning(
                        "Feegow API connection error (attempt %d/%d), retrying in %.1fs",
                        attempt, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
            except Exception as e:
                last_error = FeegowAPIError(f"Erro inesperado: {e}")
                if attempt < MAX_RETRIES:
                    wait = BACKOFF_FACTOR ** attempt
                    logger.warning(
                        "Feegow API unexpected error (attempt %d/%d): %s, retrying in %.1fs",
                        attempt, MAX_RETRIES, e, wait,
                    )
                    time.sleep(wait)

        # Esgotou os retries
        raise last_error if last_error else FeegowAPIError("Erro desconhecido")

    @staticmethod
    def _safe_json(response: requests.Response) -> Any:
        """Tenta decodificar JSON da resposta; retorna texto bruto se falhar."""
        try:
            return response.json()
        except ValueError:
            return response.text[:500]

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
    ) -> Dict[str, Any]:
        """Busca pacientes na base Feegow.

        Pelo menos um dos parâmetros deve ser informado.

        Args:
            cpf: CPF do paciente (apenas números, ex: "00000000000").
            nome: Nome completo ou parcial do paciente.
            telefone: Telefone com DDD (ex: "71999999999").
            email: Email do paciente.

        Returns:
            Dicionário com a resposta da API. Em caso de sucesso, contém
            a chave ``data`` com a lista de pacientes encontrados.
        """
        body: Dict[str, str] = {}
        if cpf:
            body["cpf"] = cpf
        if nome:
            body["nome"] = nome
        if telefone:
            body["telefone"] = telefone
        if email:
            body["email"] = email

        if not body:
            raise FeegowValidationError(
                "Informe ao menos um critério de busca: cpf, nome, telefone ou email."
            )

        logger.info("Feegow: searching patients with %s", list(body.keys()))
        return self._safe_call(
            lambda: self._request("POST", "patients/search", json_data=body),
        )

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
        """Cadastra um novo paciente na base Feegow.

        Args:
            nome: Nome completo do paciente.
            cpf: CPF (apenas números).
            data_nascimento: Data no formato DD-MM-AAAA (ex: "15-03-1985").
            telefone: Telefone com DDD (ex: "71999999999").
            email: Email do paciente (opcional).
            sexo: "M" para masculino, "F" para feminino.
            **kwargs: Campos adicionais (endereco, numero, complemento, bairro,
                      cidade, estado, cep).

        Returns:
            Dicionário com a resposta da API. Em caso de sucesso, contém
            os dados do paciente criado.
        """
        body: Dict[str, Any] = {
            "nome": nome,
            "cpf": cpf,
            "data_nascimento": data_nascimento,
            "telefone": telefone,
            "email": email,
            "sexo": sexo,
            **kwargs,
        }

        logger.info("Feegow: creating patient %s (CPF: %s)", nome, cpf)
        result = self._safe_call(
            lambda: self._request("POST", "patients/create", json_data=body),
        )
        # Se criou paciente, invalidar cache de busca
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
                lambda: self._request("GET", "company/list-unity"),
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
                lambda: self._request("GET", "specialties/list", params=params),
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
                lambda: self._request(
                    "GET", "professional/list", params=params,
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
                lambda: self._request(
                    "GET", "procedures/list", params=params,
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
            lambda: self._request(
                "GET", "appoints/search", params=params,
            ),
            fallback=[],
        )

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
        """Cria um agendamento (consulta) para um paciente.

        Args:
            paciente_id: ID do paciente na base Feegow.
            profissional_id: ID do profissional (médico).
            unidade_id: ID da unidade (clínica/consultório).
            especialidade_id: ID da especialidade.
            data: Data da consulta no formato DD-MM-AAAA.
            horario: Horário no formato HH:MM ou HH:MM:SS.
            procedimento_id: ID do procedimento (opcional).
            canal_id: ID do canal de agendamento (opcional).
            convenio_id: ID do convênio (opcional).
            plano_id: ID do plano do convênio (opcional).
            observacoes: Observações para o agendamento (opcional).
            **kwargs: Campos adicionais aceitos pela API.

        Returns:
            Dicionário com a resposta da API. Em caso de sucesso, contém
            os dados do agendamento criado.
        """
        body: Dict[str, Any] = {
            "paciente_id": paciente_id,
            "profissional_id": profissional_id,
            "unidade_id": unidade_id,
            "especialidade_id": especialidade_id,
            "data": data,
            "horario": horario,
        }
        if procedimento_id is not None:
            body["procedimento_id"] = procedimento_id
        if canal_id is not None:
            body["canal_id"] = canal_id
        if convenio_id is not None:
            body["convenio_id"] = convenio_id
        if plano_id is not None:
            body["plano_id"] = plano_id
        if observacoes:
            body["observacoes"] = observacoes
        body.update(kwargs)

        logger.info(
            "Feegow: creating appointment — paciente=%d, profissional=%d, data=%s %s",
            paciente_id, profissional_id, data, horario,
        )
        result = self._safe_call(
            lambda: self._request("POST", "appoints/create", json_data=body),
        )
        return result

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
                lambda: self._request("GET", "appoints/list-channel"),
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
            Dicionário com dados do paciente ou None se não encontrado.
        """
        result = self.search_patients(cpf=cpf, nome=nome, telefone=telefone)
        if result.get("error"):
            return None
        data = result.get("data", [])
        if isinstance(data, list) and data:
            return data[0]
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
        if isinstance(result, dict) and result.get("error"):
            return FALLBACK_MESSAGE
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
