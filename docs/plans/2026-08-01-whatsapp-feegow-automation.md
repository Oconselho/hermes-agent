# Plano de implementação: automação Feegow da secretária WhatsApp

Data de referência: 2026-08-01 BRT
Branch: feat/secretary-feegow-automation
Base: 479e4f855b33a954ebae474770d88ee702206294

## Objetivo

Adicionar um fluxo determinístico e transacional de pacientes para consultar, criar, remarcar e desmarcar agendamentos Feegow, cadastrar/editar dados permitidos e controlar reservas provisórias de teleconsulta. Preservar sem alteração o comportamento atual para contatos não pacientes, parceiros e contatos institucionais.

## Gates obrigatórios

1. O novo fluxo só assume a conversa quando houver intenção explícita de agendar, consultar agendamento, remarcar, desmarcar, verificar retorno, editar cadastro ou enviar comprovante de uma reserva já vinculada ao contato.
2. Mensagens institucionais, comerciais, de parceiros, plataformas e terceiros sem intenção explícita de agenda continuam no pipeline atual do modelo.
3. CPF isolado e menções genéricas a consulta/retorno não ativam mutação.
4. Toda mutação exige resumo e autorização textual explícita imediatamente antes da chamada.
5. Antes de criar/remarcar: reconsultar vaga e duplicidade. Após qualquer mutação: readback pelo ID exato.
6. Timeout/erro ambíguo nunca gera retry cego; consultar antes de decidir.
7. A automação nunca aplica status 7. Criações ficam em status 1; somente a recepção confirma.
8. Teleconsulta não cria reserva enquanto dados oficiais de pagamento estiverem ausentes/desabilitados.
9. Procedimento R$800 só mostra quarta/quinta 14h–18h. A configuração Feegow atual oferece apenas sábado nos próximos 15 dias; portanto o fluxo falha fechado e encaminha à recepção até a agenda ser corrigida.
10. Nenhum segredo, CPF, nascimento, e-mail, telefone ou arquivo de comprovante aparece em logs.

## Fase 1 — domínio e roteamento

Arquivos:
- `gateway/platforms/whatsapp_appointments.py` (novo)
- `tests/gateway/test_whatsapp_appointments.py` (novo)

Implementar:
- normalização PT-BR;
- classificação fail-closed de intenção de paciente;
- exclusão explícita de institucional/parceiro;
- armazenamento SQLite 0600, com estados de conversa e auditoria sem dados sensíveis;
- respostas determinísticas e menu inicial;
- TTL e reset de estado.

Testes RED/GREEN:
- Rapidoc/institucional/empresa/fornecedor passam pelo fluxo atual;
- CPF isolado não ativa;
- intenção explícita ativa;
- contato em fluxo ativo continua no fluxo;
- sem dados completos não há chamada Feegow.

## Fase 2 — cliente Feegow e agenda

Arquivos:
- `gateway/platforms/feegow_api.py`
- `tests/gateway/test_feegow_api.py`
- `tests/gateway/test_whatsapp_appointments.py`

Implementar endpoints oficiais:
- `POST appoints/new-appoint`;
- `POST patient/edit`;
- `POST appoints/statusUpdate` (disponível ao módulo, mas status 7 proibido na secretária);
- `POST appoints/cancel-appoint`;
- `POST appoints/reschedule`.

Adicionar validação de payload, exceção de resultado ambíguo, busca por ID e deduplicação. Configurar IDs observados: profissional 1, especialidade 1, local 1, canal 3, procedimentos 1/3/9, motivo 1.

Filtros:
- presencial: quarta/quinta, 14h–18h;
- sábado presencial: nunca automático; caso interior -> recepção;
- teleconsulta: vagas reais; sábado pela manhã prioritário quando existir;
- no máximo 3 opções;
- horário em BRT.

## Fase 3 — identidade, paciente e mutações

Implementar:
- CPF válido;
- nascimento;
- correspondência do telefone WhatsApp com telefone/celular Feegow;
- cadastro novo com nome, CPF, nascimento, sexo cadastral, telefone e e-mail para comunicados;
- resumo e autorização antes de criar paciente;
- edição limitada a telefone/e-mail após identidade e autorização;
- criar presencial R$600 e R$800 no status 1;
- consultar, desmarcar e remarcar com seleção inequívoca, limite de 24h e readback;
- retorno legado/ambíguo sempre para recepção;
- retorno novo somente com consulta-base atendida, procedimento 9, até 60 dias, não usado, sem falta e sem retorno ativo.

## Fase 4 — pagamento de teleconsulta

Estados persistidos:
`RESERVA_CRIADA_STATUS_1`, `AGUARDANDO_COMPROVANTE`, `COMPROVANTE_RECEBIDO`, `AGUARDANDO_VALIDACAO`, `PAGAMENTO_VALIDADO`, `PENDENCIA_NO_COMPROVANTE`, `EXPIRACAO_INICIADA`, `CANCELAMENTO_FEEGOW_PENDENTE`, `RESERVA_CANCELADA`, `CONFIRMADO_STATUS_7`, `EXCECAO_RECEPCAO`.

Implementar:
- configuração fail-closed `enabled`, beneficiário e instruções;
- prazos 12h (>48h), 4h (>24h e <=48h), 1h (>3h e <=24h), sem reserva <=3h;
- vencimento absoluto BRT após readback status 1 e instruções prontas;
- um lembrete: 2h, 1h ou 15min antes;
- associação de imagem/PDF por chat, ID de mensagem, timestamp de infraestrutura e agendamento;
- cópia privada 0600 e hash para idempotência;
- recebimento dentro do prazo interrompe expiração;
- watcher com grace de ingestão e transição SQLite atômica;
- cancelamento do ID exato somente sem comprovante/validação, seguido de readback;
- mensagens outbound apenas depois de readback;
- status 7 apenas observado, nunca escrito.

Enquanto pagamento oficial estiver desabilitado: encaminhar teleconsulta à recepção antes de criar qualquer reserva.

## Fase 5 — integração e rollout

Arquivos:
- `gateway/run.py` para interceptação determinística antes do LLM somente quando `handle()` retornar resposta;
- `tests/gateway/test_whatsapp_secretary_guardrail.py` para regressão institucional;
- configuração do perfil secretary com feature flag inicialmente habilitada para presencial e pagamento desabilitado.

Integração:
- passar texto, origem e metadados de mídia ao handler;
- `None` significa pipeline atual 100% inalterado;
- resposta determinística entra no pipeline normal de entrega/persistência;
- watcher de expiração/lembrete usa o adaptador WhatsApp já conectado;
- falha interna retorna ao pipeline atual apenas antes de ativar estado; em fluxo ativo, falha fechada para recepção.

Validação:
1. pytest focal no venv real;
2. suíte gateway relevante;
3. `py_compile` dos módulos;
4. `node --check` da bridge;
5. simulações offline com API fake cobrindo todos os estados e nenhuma mutação indevida;
6. juiz independente E e V;
7. commit local e release compat local; nunca force-push;
8. restart sem notificação a pacientes;
9. health, conexão WhatsApp, fila, import no venv real e logs frescos;
10. probes read-only Feegow e verificação de que nenhuma consulta de teste foi criada pelo rollout.

## Contrato executável, schema e precedência

Arquivos exatos adicionais:
- `gateway/platforms/whatsapp_appointments.py`: domínio, SQLite, inbox/ledger/outbox, handler e watcher;
- `gateway/platforms/feegow_api.py`: endpoints e validação de payload;
- `gateway/run.py`: uma chamada ao handler e um watcher; `None` mantém o pipeline antigo;
- `tests/gateway/test_whatsapp_appointments.py`, `tests/gateway/test_feegow_api.py`, `tests/gateway/test_whatsapp_secretary_guardrail.py`, `tests/gateway/test_secretary_appointments_config.py`;
- `config/secretary-appointments.staging.yaml`: manifesto versionado, com escrita/pagamento fechados e sem ativação automática; o perfil live permanece sem esse bloco até aprovação;
- `/home/ubuntu/.hermes/profiles/secretary/config.yaml`: bloco `whatsapp.secretary_appointments` somente na etapa de rollout aprovada;
- `/home/ubuntu/.hermes/profiles/secretary/state/appointments.sqlite3` (0600) e `/home/ubuntu/.hermes/profiles/secretary/payment-proofs/` (0700, arquivos 0600).

Configuração:
- `enabled`, `write_enabled` e `payment.enabled` são gates independentes;
- `professional_id: 1`, `specialty_id: 1`, `local_id: 1`, `channel_id: 3`, `cancel_reason_id: 1`;
- procedimentos imutáveis e verificados no preflight: `1=Consulta/R$600`, `3=Teleconsulta/R$300`, `9=Consulta presencial com 1 retorno/R$800`;
- `payment.beneficiary` e `payment.instructions`; pagamento só fica pronto se todos existirem e `payment.enabled` for habilitado em mudança separada, explicitamente aprovada;
- primeiro rollout: `enabled: true`, `write_enabled: false`, `payment.enabled: false`. Nenhuma mutação real no primeiro restart.

Schema aditivo e downgrade-safe:
- `flows(chat_key PRIMARY KEY, state, data_json, updated_at, expires_at)`;
- `inbox(message_id PRIMARY KEY, chat_key, received_at, handled_at)`;
- `operations(id PRIMARY KEY, idempotency_key UNIQUE, kind, target_id, payload_hash, state, remote_id, error_class, created_at, updated_at)`;
- `reservations(appointment_id PRIMARY KEY, chat_key, state, deadline_at, reminder_at, proof_received_at, version)`;
- `proofs(message_id PRIMARY KEY, appointment_id, received_at, sha256 UNIQUE, private_path)`;
- `outbox(id PRIMARY KEY, idempotency_key UNIQUE, chat_key, body, state, created_at, sent_at)`;
- `leases(name PRIMARY KEY, owner, expires_at)`;
- `audit(id PRIMARY KEY, operation_id, event, metadata_json, created_at)` sem PII.
Todas as migrações usam `CREATE TABLE IF NOT EXISTS`/adições compatíveis; release anterior ignora o banco.

Precedência fail-closed antes de ler/escrever estado:
1. grupo, broadcast, status, contato identificado como organização/parceiro/plataforma/fornecedor ou texto institucional explícito => `handle()` retorna `None`, mesmo com “consulta”, “agenda”, CPF ou mídia;
2. estado expirado é removido sem resposta e retorna `None` salvo nova intenção individual inequívoca;
3. CPF isolado, mídia isolada sem reserva ativa e menção genérica => `None`;
4. apenas intenção individual explícita ou reserva individual previamente validada ativa o fluxo.
Se uma organização estiver em estado incorreto, o estado é colocado em quarentena e nenhum watcher/outbox/Feegow é acionado.

Autorização e idempotência:
- toda autorização gera registro single-use ligado a `chat_key + kind + target_id + payload_hash + expires_at`;
- `CONFIRMAR`, `ALTERAR` e `CANCELAR RESERVA` consomem uma autorização uma vez; alteração de qualquer campo a invalida;
- redelivery é deduplicado por `message_id`; operação usa `idempotency_key UNIQUE`; outbound usa outbox UNIQUE;
- timeout/queda entre HTTP e commit entra em `RECONCILE_REQUIRED`; o retry primeiro busca por ID/fingerprint na Feegow;
- watcher usa lease e outbox; dois workers não processam o mesmo vencimento;
- cancelamento por expiração é a única exceção à confirmação imediata: o paciente o consente no resumo da teleconsulta, ligado ao ID e vencimento imutáveis; comprovante com timestamp de infraestrutura `<=deadline` vence a expiração, com grace de ingestão antes da chamada remota;
- `statusUpdate(7)` é rejeitado no próprio cliente usado pela secretária; nenhum chamador pode habilitá-lo.

Privacidade e retenção:
- diretórios pertencem ao usuário do serviço, banco 0600, provas 0600;
- logs/erros usam IDs opacos, nunca payloads; testes sentinela cobrem CPF, nascimento, telefone, e-mail e bytes de mídia;
- fluxos inativos expurgados após 30 dias; comprovantes após 180 dias, salvo `EXCECAO_RECEPCAO`; backups operacionais excluem provas por padrão e preservam apenas banco com permissão 0600.

## Ciclos TDD e critérios de aceite por fase

Cada slice registra RED (teste focal falha pelo comportamento ausente), GREEN, refactor e suíte regressiva durante E. A revisão operacional da execução não usa `delegate_task`; o judge semântico independente ocorre em V. Cada fase cumpre o ciclo independente E→V→C. `prevc-validate.py` roda antes de cada transição.

Fase 1:
- RED: institucional com agenda/CPF/mídia e estado incorreto deve retornar `None`, pipeline antigo exatamente uma vez, metadados intactos, zero estado/Feegow/outbox;
- GREEN: precedência e estado; regressão dos guardrails atuais;
- abortar se qualquer fixture institucional for interceptada.

Fase 2:
- RED por endpoint/payload e bloqueio de status 7; GREEN com fake HTTP; testes de 409, timeout ambíguo e reconciliação;
- testes de agenda: proc9 só sábado => zero opções/zero mutação/recepção; quarta/quinta válidas => até três; mistura exclui sábado; limites 14h/18h/BRT; proc1 independente; reconsulta antes do POST;
- abortar se preflight ID/nome/preço divergir.

Fase 3:
- RED/GREEN para CPF, nascimento, telefone, paciente novo, autorização single-use, replay, concorrência, create/readback, cancel/readback e reschedule/readback;
- retorno antigo, divergente, <24h e sábado excepcional => recepção e zero mutação;
- abortar em qualquer vazamento PII ou mutação sem autorização válida.

Fase 4:
- RED/GREEN para configuração parcial (zero reserva/prova/lembrete/watcher), prazos 12h/4h/1h, <=3h, prova no último segundo, prova tardia, dois workers, crash em cada fronteira, lembrete/outbox idempotente e observação de status 7;
- abortar se pagamento desabilitado criar qualquer efeito.

Fase 5:
- primeiro restart com escrita OFF; simulações offline e smoke do pipeline atual;
- só habilitar escrita presencial em mudança posterior e explícita após judge V/C; pagamento continua OFF;
- aceite: serviço healthy, WhatsApp connected, fila vazia, imports/testes reais verdes, nenhum POST Feegow no rollout e logs sem PII.

## Rollback

Caminhos reais registrados antes da promoção: unit `hermes-secretary.service`; drop-in `/etc/systemd/system/hermes-secretary.service.d/20-official-release-20260719.conf`; ExecStart `/home/ubuntu/.hermes/releases/venv-official-v2026.7.7.2-fce0e197e/bin/python`; WorkingDirectory `/home/ubuntu/.hermes/releases/hermes-agent-official-v2026.7.7.2-fce0e197e-compat`; perfil `/home/ubuntu/.hermes/profiles/secretary`; banco e configuração. Gerar backup 0600 do drop-in, configuração e banco, com SHA-256, antes da troca. Drenar watcher/outbox com timeout; mutações pendentes vão para reconciliação manual. Restaurar o drop-in/release anterior, `systemctl daemon-reload`, reiniciar, aguardar readiness limitada e verificar health/logs pós-rollback. Efeitos Feegow já concluídos nunca são revertidos automaticamente nem repetidos; permanecem no ledger para recepção.

Sequenciamento operacional: concluir E, V e C da release preparada antes de qualquer promoção. A promoção é uma ação separada, com `write_enabled=false` e `payment.enabled=false`; todos os mutadores Feegow verificam `write_enabled` na própria fronteira HTTP e o teste de rollout exige zero POST. Após o restart, executar apenas validação operacional/read-only e, em falha, rollback. Habilitar escrita é uma mudança futura, separada e explicitamente aprovada.

## Limitações operacionais explícitas

- Pagamento automático fica instalado, porém desabilitado até o Dr. Victor fornecer instruções e beneficiário oficiais.
- Pacote R$800 fica instalado, porém não oferecerá as vagas de sábado atualmente retornadas; sem vaga válida de quarta/quinta, o caso vai para recepção.
- Política financeira pós-pagamento (reembolso/crédito/duplicidade) continua sempre com a recepção.
