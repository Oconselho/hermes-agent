/**
 * A automação não fala por cima do dono — testes da guarda de SAÍDA.
 *
 * O caso medido em 21/set/2026, num chat só:
 *   16:47:51.895  a contato escreve "Oi"
 *   16:47:52.687  a secretária manda o menu — 0,79 s depois, sem chamar o modelo
 *   16:48:23.378  o Dr. Victor responde à mão, 31 s depois da automação
 *
 * A checagem do dono existia só na ENTRADA, então nada olhou para a resposta
 * que já estava a caminho. Este arquivo protege a checagem na saída — e o que
 * ela NÃO pode segurar: a mensagem do próprio dono, os avisos internos e a
 * continuação de uma resposta partida em pedaços.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  classifyOutboundOwnerGate,
  runOutboundOwnerGate,
  ACTION_SEND,
  ACTION_HOLD,
  ACTION_DROP,
} from './outbound_owner_gate.js';

const MINUTO = 60 * 1000;
const PADRAO = {
  cooldownMs: 30 * MINUTO,
  graceMs: 12000,
  chunkContinuationMs: 8000,
};
const AGORA = 1790020103378;

function decide(extra = {}) {
  return classifyOutboundOwnerGate({ now: AGORA, ...PADRAO, ...extra });
}

// --------------------------------------------------------------------------
// 1. O caso medido
// --------------------------------------------------------------------------

test('o dono acabou de falar no chat: a automação não envia', () => {
  const decision = decide({ ownerReplyAt: AGORA - 31000, alreadyHeld: true });
  assert.equal(decision.action, ACTION_DROP);
  assert.equal(decision.reason, 'owner_active');
  assert.equal(decision.ownerAgeMs, 31000);
});

test('resposta nova espera a janela de cortesia antes de sair', () => {
  const decision = decide({ ownerReplyAt: undefined });
  assert.equal(decision.action, ACTION_HOLD);
  assert.equal(decision.waitMs, 12000);
});

test('o dono que responde DURANTE a espera cancela o envio', () => {
  // Primeira passada: nada do dono, segura.
  assert.equal(decide({ ownerReplyAt: undefined }).action, ACTION_HOLD);
  // Ele escreveu enquanto a mensagem esperava; a re-checagem enxerga.
  const recheck = decide({ ownerReplyAt: AGORA - 2000, alreadyHeld: true });
  assert.equal(recheck.action, ACTION_DROP);
});

test('sem sinal do dono, a mensagem sai depois da espera', () => {
  const decision = decide({ ownerReplyAt: undefined, alreadyHeld: true });
  assert.equal(decision.action, ACTION_SEND);
  assert.equal(decision.reason, 'clear_after_grace');
});

test('intervenção antiga (fora da janela) não segura nada', () => {
  const decision = decide({ ownerReplyAt: AGORA - 31 * MINUTO, alreadyHeld: true });
  assert.equal(decision.action, ACTION_SEND);
});

// --------------------------------------------------------------------------
// 2. O que a guarda NÃO pode segurar
// --------------------------------------------------------------------------

test('a mensagem do PRÓPRIO dono nunca é segurada', () => {
  const decision = decide({ ownerReplyAt: AGORA - 1000, isOwnerReply: true });
  assert.equal(decision.action, ACTION_SEND);
  assert.equal(decision.reason, 'owner_reply');
});

test('aviso interno (recepção / linha do doutor) sempre sai', () => {
  // Se ele respondeu à mão no chat da recepção, o aviso do paciente seguinte
  // não pode sumir: é exatamente a informação que o funil existe para escalar.
  const decision = decide({ ownerReplyAt: AGORA - 1000, isInternalNotice: true });
  assert.equal(decision.action, ACTION_SEND);
  assert.equal(decision.reason, 'internal_notice');
});

test('continuação de uma resposta partida em pedaços não espera nem é cortada', () => {
  const decision = decide({ ownerReplyAt: AGORA - 1000, lastAutoSendAt: AGORA - 900 });
  assert.equal(decision.action, ACTION_SEND);
  assert.equal(decision.reason, 'chunk_continuation');
});

test('envio anterior VELHO não conta como continuação', () => {
  const decision = decide({ ownerReplyAt: AGORA - 1000, lastAutoSendAt: AGORA - 60000 });
  assert.equal(decision.action, ACTION_DROP);
});

// --------------------------------------------------------------------------
// 3. Desligar, e os valores de borda
// --------------------------------------------------------------------------

test('desligada, a guarda não muda nada', () => {
  const decision = decide({ ownerReplyAt: AGORA - 1000, enabled: false });
  assert.equal(decision.action, ACTION_SEND);
  assert.equal(decision.reason, 'disabled');
});

test('espera zero significa enviar direto, sem deixar de checar o dono', () => {
  assert.equal(decide({ ownerReplyAt: undefined, graceMs: 0 }).action, ACTION_SEND);
  assert.equal(decide({ ownerReplyAt: AGORA - 1000, graceMs: 0 }).action, ACTION_DROP);
});

test('carimbo corrompido é tratado como ausência de sinal', () => {
  for (const lixo of [NaN, 0, null, undefined, 'ontem']) {
    const decision = decide({ ownerReplyAt: lixo, alreadyHeld: true });
    assert.equal(decision.action, ACTION_SEND, `carimbo ${String(lixo)} segurou a mensagem`);
  }
});

test('o limite da janela é exclusivo nos dois lados', () => {
  const noLimite = decide({ ownerReplyAt: AGORA - 30 * MINUTO, alreadyHeld: true });
  assert.equal(noLimite.action, ACTION_SEND, 'exatamente 30 min já não é "ativo"');
  const dentro = decide({ ownerReplyAt: AGORA - 30 * MINUTO + 1, alreadyHeld: true });
  assert.equal(dentro.action, ACTION_DROP);
});

// --------------------------------------------------------------------------
// 4. A orquestração: segurar, perguntar de novo, registrar
// --------------------------------------------------------------------------
//
// É aqui que mora o "reavalie depois de poucos segundos" pedido em 21/set.
// Nada de relógio real: o tempo e a espera entram por parâmetro.

function cenario({ ownerReplyAt, avancaDuranteEspera = 0, ...resto } = {}) {
  let agora = AGORA;
  const esperas = [];
  const suprimidas = [];
  const marcados = [];
  const estado = { ownerReplyAt };
  return {
    esperas,
    suprimidas,
    marcados,
    estado,
    run: () => runOutboundOwnerGate({
      chatId: 'contato@lid',
      ...PADRAO,
      ...resto,
      ownerReplyAtFor: () => estado.ownerReplyAt,
      lastAutoSendAtFor: () => estado.lastAutoSendAt,
      markAutoSend: (id, ts) => marcados.push([id, ts]),
      now: () => agora,
      sleep: async (ms) => {
        esperas.push(ms);
        agora += ms;
        if (avancaDuranteEspera) {
          // o dono digitou enquanto a mensagem esperava
          estado.ownerReplyAt = agora - avancaDuranteEspera;
        }
      },
      onSuppressed: (info) => suprimidas.push(info),
    }),
  };
}

test('sem sinal do dono: espera a cortesia, envia e marca o envio', async () => {
  const c = cenario({ ownerReplyAt: undefined });
  const suprimiu = await c.run();
  assert.equal(suprimiu, false);
  assert.deepEqual(c.esperas, [12000]);
  assert.equal(c.marcados.length, 1);
  assert.equal(c.suprimidas.length, 0);
});

test('o dono responde DURANTE a espera: a mensagem não é entregue', async () => {
  const c = cenario({ ownerReplyAt: undefined, avancaDuranteEspera: 1000 });
  const suprimiu = await c.run();
  assert.equal(suprimiu, true);
  assert.deepEqual(c.esperas, [12000], 'esperou uma vez só');
  assert.equal(c.marcados.length, 0, 'não pode contar como envio');
  assert.equal(c.suprimidas[0].reason, 'owner_active');
});

test('dono ativo antes de tudo: nem espera, já descarta', async () => {
  const c = cenario({ ownerReplyAt: AGORA - 5000 });
  assert.equal(await c.run(), true);
  assert.deepEqual(c.esperas, [], 'não faz sentido esperar para confirmar o óbvio');
  assert.equal(c.suprimidas[0].ownerAgeMs, 5000);
});

test('aviso interno não espera nem é descartado', async () => {
  const c = cenario({ ownerReplyAt: AGORA - 5000, isInternalNotice: true });
  assert.equal(await c.run(), false);
  assert.deepEqual(c.esperas, []);
});

test('toda supressão é registrada — silêncio parece sucesso', async () => {
  const c = cenario({ ownerReplyAt: AGORA - 5000 });
  await c.run();
  assert.equal(c.suprimidas.length, 1);
  assert.equal(c.suprimidas[0].chatId, 'contato@lid');
  assert.equal(c.suprimidas[0].graceMs, 12000);
});

test('sem espera configurada, ainda assim checa o dono', async () => {
  const c = cenario({ ownerReplyAt: AGORA - 5000, graceMs: 0 });
  assert.equal(await c.run(), true);
  assert.deepEqual(c.esperas, []);
});
