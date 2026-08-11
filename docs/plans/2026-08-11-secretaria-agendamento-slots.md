# SPEC — Destravar o agendamento da secretária (slots Feegow) e fechar dois guardrails

**Slug PREVC:** `secretaria-agendamento-slots-20260811`
**Versão:** 1.0.0
**Data:** 2026-08-11
**Branch:** `hotfix/whatsapp-secretary-20260806`
**Release viva:** `/home/ubuntu/.hermes/releases/hermes-agent-20260803-71cc13708-tone`
**Base:** `1e8b771df`

---

## 1. Contexto e objetivo

A secretária do WhatsApp está com o agendamento **totalmente inoperante** desde que
os gates do Feegow foram ligados (10/ago). Nenhum agendamento — nem teleconsulta,
nem presencial — chega ao fim. `reservations`, `operations` e `outbox_events` em
`state/appointments.sqlite3` têm **0 linhas**.

Além disso, o token de silêncio `[SILENCIOSO]` está vazando literalmente para
pacientes, e a recepção **nunca é avisada** quando um paciente tenta agendar e o
fluxo morre.

**Objetivo:** fazer o agendamento funcionar de ponta a ponta (prioridade do
usuário), e fechar os dois guardrails que hoje falham em silêncio.

---

## 2. Estado atual investigado (evidências)

### 2.1 Defeito A — parâmetro `tipo` ausente → HTTP 422

`gateway/platforms/feegow_api.py:648` `list_available_slots` monta os params sem o
campo `tipo`, que a API Feegow exige.

Reprodução ao vivo contra a conta real (GET, somente leitura):

```
GET /v1/api/appoints/available-schedule
    ?data_start=11-08-2026&data_end=26-08-2026
    &procedimento_id=3&profissional_id=1&especialidade_id=1&local_id=1
→ 422 {"tipo":["O campo tipo é obrigatório."]}
```

`_safe_call(..., fallback=[])` (`feegow_api.py:329`) engole a exceção e devolve
`[]`. Em `whatsapp_appointments.py:2880`, `if not slots: return self._handoff(...)`
→ o paciente recebe *"Não foi possível concluir este agendamento com segurança."*

Evidência em produção — `errors.log`, no mesmo segundo do handoff da paciente
Iana Gomes:

```
2026-08-11 11:45:45,495 ERROR gateway.platforms.feegow_api:
    Feegow API error in safe_call: Parâmetros inválidos ou faltando:
2026-08-11 01:27:58,292 ERROR ... (mesma falha, teste do operador em 10/ago)
```

A mensagem vem truncada porque o corpo 422 da Feegow usa a chave `tipo`, e
`_request` lê `body.get("message", "")` (`feegow_api.py:204`) — que não existe.
Por isso a falha ficou invisível.

Valores testados: `tipo=P` → **200 com agenda real**; `tipo=A` → 200; `tipo=E` →
200 mas `content: []`; `1`, `2`, `0`, `T`, `procedimento` → 422 inválido.
`P` (procedimento) é o correto para a chamada que passa `procedimento_id`.

### 2.2 Defeito B — payload aninhado que o parser não lê

Com `tipo=P`, a Feegow devolve a agenda **aninhada**, não uma lista de slots:

```json
{"success": true, "total": 1, "content": {"profissional_id": {"1": {
    "local_id": {"1": {
        "2026-08-12": ["14:00:00","14:30:00","15:00:00","15:30:00",
                       "16:00:00","16:30:00","17:00:00"],
        "2026-08-13": ["14:00:00","14:30:00","15:00:00","15:30:00"],
        "2026-08-15": ["09:00:00","09:30:00","10:00:00","10:30:00","11:00:00"],
        "2026-08-19": ["14:30:00","15:00:00","15:30:00"],
        "2026-08-20": ["14:00:00","14:30:00","15:00:00","15:30:00",
                       "16:00:00","16:30:00","17:00:00"],
        "2026-08-26": []}},
    "age_restriction": {"age_from": 1, "age_to": 120}}}}}
```

`_collection_content` (`feegow_api.py:270`) não encontra nenhuma das chaves de
coleção que conhece dentro de `content` e devolve `[{"profissional_id": {...}}]`
— **um item, que não é um slot**. Em seguida `_slot_date_and_time`
(`whatsapp_appointments.py:1801`) procura `data`/`date`/`dia` e
`horario`/`hora`/`time` no topo do dict, não acha, e o `continue` de
`filter_eligible_slots:1861` descarta tudo.

Verificado rodando as funções do próprio release contra o payload real:

```
_collection_content(real)          → 1 item (não-slot)
filter_eligible_slots(…, proc=3)   → []   # com HTTP 200
filter_eligible_slots(…, proc=1)   → []   # com HTTP 200
```

**Ou seja: corrigir só o `tipo` não destrava nada.** Os dois defeitos são
independentes e ambos bloqueantes.

**Por que os testes não pegaram:** as fixtures usam um formato plano inventado —
`{"id": "slot-wed-14", "procedimento_id": 1, "data": "2026-08-05", "horario": "14:00"}`
(`tests/gateway/test_whatsapp_appointments_mutations.py:39`). A Feegow nunca
devolveu esse formato. O parser foi validado contra um contrato que não existe.

**A agenda existe.** Vagas reais na conta hoje: 12/08 e 13/08 (qua/qui,
14:00–17:00), **15/08 sábado 09:00–11:00**, 19/08, 20/08. Inclusive o sábado de
manhã que a política de teleconsulta prioriza. Não falta vaga — falta chegar até ela.

### 2.3 Defeito C — `[ SILENCIOSO ]` vazando para pacientes

O prompt manda o modelo escrever `[SILENCIOSO]` (`gateway/run.py:19863`, `:19865`,
`:19908`, `:20053`). O supressor é uma comparação literal exata:

```python
# gateway/run.py:1100
if cleaned.strip().startswith("[SILENCIOSO]"):
    return None
```

O `gpt-5.6-luna` (ativo desde 07/ago) às vezes escreve `[ SILENCIOSO ]`, com
espaços dentro dos colchetes. O `startswith` falha e o token segue como texto.

Em `state.db`: **83× `[SILENCIOSO]`** (suprimido corretamente) e
**4× `[ SILENCIOSO ]`** — todas em 09, 10 e 11/ago, nenhuma antes do Luna.
As quatro foram entregues:

| Quando | Texto entregue | Tamanho |
|---|---|---|
| 09/08 15:42, 09/08 17:20, 10/08 17:20 | `Aqui é a assistente do Dr. Victor Almeida. [ SILENCIOSO ]` | 57 chars |
| 11/08 14:29 | `[ SILENCIOSO ]` | 14 chars |

A diferença é `_whatsapp_finalize_secretary_response` (`run.py:254`): em sessão
nova ele prefixa a identidade e manda o token junto; quando a identidade já foi
dada antes na conversa (`run.py:297`), ele não reescreve nada e o token sai puro.
Log confirmando a entrega crua:

```
2026-08-11 14:29:56,112 INFO gateway.run: response ready: … response=14 chars
2026-08-11 14:29:56,191 INFO gateway.platforms.base: [Whatsapp] Sending response (14 chars) to 63406736416992@lid
```

### 2.4 Defeito D — recepção nunca é avisada

`_handoff` (`whatsapp_appointments.py:2175`) apenas responde ao **paciente** com o
texto `_RECEPTION` e grava `FlowState.HANDOFF`. Não enfileira nada.

A recepção só é notificada em dois pontos, ambos **depois de um agendamento
concluído**: `enqueue_reception_receipt` (comprovante aceito, `:2408`) e
`enqueue_reception_booking` (`:3862`, dentro de `_complete_authorized_appointment`).

Como nada conclui, `outbox_events` = 0 linhas: **a recepção nunca soube de
nenhuma das tentativas**. Os dois chats presos em `flow_states` com `state=HANDOFF`
(`118347958063114@lid`, `124502813991041@lid`) confirmam.

---

## 3. Escopo

### Entra

1. `list_available_slots` passa a enviar `tipo="P"`.
2. Normalização do payload aninhado de disponibilidade em slots planos,
   respeitando `profissional_id`/`local_id` pedidos.
3. Detalhe de erro de validação da Feegow visível no log (hoje o 422 loga vazio).
4. Supressão do token de silêncio tolerante a espaços, caixa e delimitadores.
5. Aviso à recepção quando o fluxo determinístico cai em handoff.
6. Testes com o payload **real** da Feegow como fixture.

### NÃO entra

- Não alterar `write_enabled`, `payment.enabled` nem qualquer gate do
  `config.yaml`. Ficam exatamente como estão.
- Não alterar a política de horários (presencial qua/qui 14:00–18:00; tele aceita
  a agenda real com sábado de manhã priorizado).
- Não alterar preços nem `_SERVICES`.
- Não trocar o modelo do WhatsApp.
- Não mexer no guard clínico de `1e8b771df`.
- **Não fazer restart, deploy ou promoção de release.** Decisão exclusiva do
  operador, fora do PREVC.
- Não fazer nenhuma escrita real na Feegow durante a validação.

---

## 4. Artefatos previstos

| Arquivo | Ação |
|---|---|
| `gateway/platforms/feegow_api.py` | `tipo="P"`; normalizador do payload aninhado; detalhe do erro 422 |
| `gateway/platforms/whatsapp_appointments.py` | aviso à recepção no handoff |
| `gateway/run.py` | supressão tolerante do token de silêncio |
| `tests/gateway/test_feegow_available_schedule_real_payload.py` | novo — fixture com payload real |
| `tests/gateway/test_whatsapp_silence_token_variants.py` | novo — variantes do token |
| `tests/gateway/test_whatsapp_appointments_handoff_reception.py` | novo — aviso no handoff |
| `docs/plans/2026-08-11-secretaria-agendamento-slots.md` | esta SPEC |

---

## 5. Requisitos

### Funcionais

- **RF1** — `list_available_slots` envia `tipo="P"` e retorna 200 na conta real.
- **RF2** — o payload aninhado vira uma lista de dicts planos, cada um com
  `data` (YYYY-MM-DD), `horario` (HH:MM), `procedimento_id`, `profissional_id`,
  `local_id`.
- **RF3** — apenas o `profissional_id` e o `local_id` pedidos entram no resultado;
  chaves irmãs como `age_restriction` são ignoradas; datas com lista vazia não
  geram slot.
- **RF4** — `filter_eligible_slots` sobre o payload real devolve ≥1 slot para
  procedimento 1 e para procedimento 3, respeitando a política de cada modalidade.
- **RF5** — o formato plano legado continua aceito (compatibilidade com as
  fixtures e testes existentes).
- **RF6** — o supressor de silêncio ignora `[SILENCIOSO]`, `[ SILENCIOSO ]`,
  `{silencioso}`, `(silencioso)`, `silencioso` e variações de caixa/acento,
  isoladas ou como conteúdo único da resposta.
- **RF7** — quando o fluxo cai em `_handoff`, a recepção recebe um aviso pelo
  outbox existente, idempotente por chat, **sem PII** (sem nome, CPF, telefone,
  e-mail ou texto do paciente).
- **RF8** — um erro 422 da Feegow passa a logar qual campo foi rejeitado.

### Não-funcionais

- **RNF1** — fail-closed preservado: qualquer dúvida continua virando handoff.
  Nenhuma correção pode transformar um erro em agendamento otimista.
- **RNF2** — sem PII em log, outbox ou auditoria.
- **RNF3** — nenhuma escrita real na Feegow durante testes; só GET de leitura.
- **RNF4** — falha ao enfileirar o aviso à recepção não pode derrubar a resposta
  ao paciente (mesmo contrato de `:3869`).
- **RNF5** — baseline de testes do gateway não pode regredir (hoje 17 falhas /
  9440 passes).

---

## 6. Design técnico

### 6.1 `tipo` e normalização (`feegow_api.py`)

`list_available_slots` passa a enviar `"tipo": "P"` e, em vez de
`_collection_content`, usa um normalizador dedicado
`_normalize_available_schedule(payload, procedure_id, professional_id, local_id)`:

- aceita a lista plana (retorna como está, filtrando não-dicts) — RF5;
- caso contrário desce `content → profissional_id → <pid> → local_id → <lid>`;
- para cada chave que parseia como data ISO, itera a lista de horários;
- emite `{"data": "YYYY-MM-DD", "horario": "HH:MM", "procedimento_id": …,
  "profissional_id": …, "local_id": …}`;
- ignora chaves não-data (`age_restriction`), listas vazias e horários inválidos;
- nunca levanta: entrada inesperada vira `[]` (fail-closed).

Não é emitida chave `id`: `filter_eligible_slots:1868` já gera um id opaco
determinístico via `_opaque_id("slot", …)`. O id do slot **não** é enviado à
Feegow — a criação usa `data` e `horario` (`whatsapp_appointments.py:4226-4227`) —
então o id opaco é suficiente e evita inventar identificador remoto.

### 6.2 Detalhe do erro 422 (`feegow_api.py:204`)

Quando `body` não tem `message`, serializar o corpo (curto, sem PII) para o log,
de modo que o próximo 422 diga qual campo falhou.

### 6.3 Token de silêncio (`run.py`)

Substituir o `startswith` literal por um teste sobre o texto normalizado:

```python
_SILENCE_TOKEN_RE = re.compile(
    r"^[\[\{\(<]?\s*silencios[oa]\s*[\]\}\)>]?[\s.!]*$",
    re.IGNORECASE,
)
```

aplicado ao texto sem acento e sem espaços invisíveis. Casa quando o token é o
**conteúdo inteiro** da resposta — não se faz remoção parcial no meio de uma frase
legítima, para não abrir caminho a um texto útil ser mutilado.

### 6.4 Aviso à recepção no handoff (`whatsapp_appointments.py`)

`_handoff` passa a enfileirar, quando `self._reception_chat_id` está definido, um
evento no `outbox_events` com corpo fixo e sem PII, idempotente por
`(chat_key, dia)` para não inundar a recepção se o paciente insistir:

```
Um paciente tentou agendar pelo WhatsApp e o atendimento automático não
conseguiu concluir. Verificar na conversa da recepção.
```

Envolvido em `try/except` que só loga (RNF4), igual ao padrão de `:3869`.

---

## 7. Plano por fases

### Fase 1 — Destravar o agendamento (prioridade)

- **Objetivo:** RF1–RF5, RF8. Agendamento presencial e teleconsulta voltam a
  oferecer vagas reais.
- **Arquivos:** `gateway/platforms/feegow_api.py`,
  `tests/gateway/test_feegow_available_schedule_real_payload.py`.
- **Validações:** teste novo com payload real; `filter_eligible_slots` devolve
  ≥1 slot para proc 1 e 3; probe GET ao vivo mostrando 200; suíte de
  `test_feegow_api*` e `test_whatsapp_appointments*` sem regressão.
- **Riscos:** `tipo="P"` incorreto para algum procedimento futuro; mitigado por
  probe real nos três procedimentos em uso (1, 3, 9).

### Fase 2 — Guardrails que falham em silêncio

- **Objetivo:** RF6, RF7.
- **Arquivos:** `gateway/run.py`,
  `gateway/platforms/whatsapp_appointments.py`, dois testes novos.
- **Validações:** testes de variantes do token; teste de que o handoff enfileira
  exatamente um aviso sem PII; suíte de guard clínico (31 testes) sem regressão.
- **Riscos:** supressão ampla demais engolir uma resposta legítima; mitigado por
  casar apenas o conteúdo integral.

---

## 8. Estratégia de validação

1. `pytest tests/gateway/test_feegow_api*.py tests/gateway/test_whatsapp_appointments*.py`
2. `pytest tests/gateway/test_whatsapp_clinical_scope_guard.py` (não regredir)
3. Testes novos das três áreas
4. Probe GET real (somente leitura) nos procedimentos 1, 3 e 9
5. Suíte ampla do gateway comparada ao baseline 17 falhas / 9440 passes
6. `git diff` revisado arquivo a arquivo
7. Judge independente por fase (E e V), conforme PREVC

---

## 9. Riscos e trade-offs

| Risco | Mitigação |
|---|---|
| Editar a árvore da release viva | Python só relê no restart; nenhum restart nesta SPEC |
| Achatamento aceitar lixo e virar agendamento errado | Normalizador estritamente tipado, datas parseadas, fail-closed para `[]` |
| Supressão de silêncio ampla demais | Regex ancorada no texto integral |
| Inundar a recepção com avisos de handoff | Idempotência por `(chat_key, dia)` |
| Vaga real desaparecer entre listar e criar | Já coberto pelo preflight/readback existente; fora de escopo |

---

## 10. Critérios de aceite

- Um paciente que escolhe **3 – Teleconsulta** recebe uma lista de vagas reais,
  não o texto de handoff.
- O mesmo vale para **1 – Consulta presencial**.
- `[ SILENCIOSO ]` e variantes nunca chegam ao paciente.
- Todo handoff gera exatamente um aviso à recepção, sem PII.
- Nenhum gate de produção alterado; nenhum restart feito pelo agente.
