/**
 * Pure classifier for the bridge's OUTBOUND path: "the owner is handling this
 * chat — should this automatic message still go out?"
 *
 * WHY THIS EXISTS (21/set/2026)
 * =============================
 * The owner cooldown already existed, but only on the way IN: when Dr. Victor
 * answers a chat from his phone, incoming messages in that chat are dropped for
 * 30 minutes. Nothing looked at the way OUT, so a reply already in flight —
 * or a follow-up queued minutes earlier — went out anyway.
 *
 * Measured on 21/set/2026, one chat, three timestamps:
 *
 *     16:47:51.895  the contact writes "Oi"
 *     16:47:52.687  the secretary sends the booking menu — 0,79 s later,
 *                   deterministic path, the model was never called
 *     16:48:23.378  Dr. Victor answers by hand, 31 s after the automation
 *
 * In all three chats he answered by hand that day, the automation had spoken
 * first: 31 s, 4 min and 54 min before him.
 *
 * TWO PROBLEMS, TWO ANSWERS
 * -------------------------
 * 1. Nothing re-checked before sending → the gate below, applied at the single
 *    point every producer (funnel, model, queued follow-up) must cross.
 * 2. The deterministic path answers in 0,79 s, faster than any human can type →
 *    a short grace period before sending, after which the gate is asked again.
 *    The grace is deliberately small: the same owner complained on 02/set that
 *    the secretary was "too slow", and the Jev answered 0,17 to "a 20 s delay
 *    on every automatic reply is a proportionate price". It buys the re-check
 *    a window; it does not try to out-wait a human.
 *
 * WHAT MUST NOT BE HELD
 * ---------------------
 * - `isOwnerReply`: the owner speaking THROUGH Hermes. Holding his own message
 *   because he is active would be absurd.
 * - `isInternalNotice`: notices to reception / to the doctor's own line. They
 *   are not a conversation with the contact, and suppressing them would lose
 *   exactly the information the funnel exists to escalate.
 * - chunk continuations: one reply split into several HTTP calls must not pay
 *   the grace period once per chunk, and must not be cut in half by a cooldown
 *   that started between chunk 1 and chunk 2.
 */

export const ACTION_SEND = 'send';
export const ACTION_HOLD = 'hold_then_recheck';
export const ACTION_DROP = 'drop_owner_active';

/**
 * @param {object} input
 * @param {number} input.now                     current epoch ms
 * @param {number|undefined} input.ownerReplyAt  epoch ms of the owner's last
 *                                               message in this chat (undefined
 *                                               when he never spoke there)
 * @param {boolean} [input.isOwnerReply]         this send IS the owner's message
 * @param {boolean} [input.isInternalNotice]     reception / doctor notice
 * @param {number|undefined} [input.lastAutoSendAt] epoch ms of the last
 *                                               automatic send to this chat
 * @param {boolean} [input.enabled]              master switch (default true)
 * @param {boolean} [input.alreadyHeld]          true on the re-check call
 * @param {number} input.cooldownMs              owner-active window
 * @param {number} input.graceMs                 how long to hold before sending
 * @param {number} input.chunkContinuationMs     window that marks a continuation
 */
export function classifyOutboundOwnerGate({
  now,
  ownerReplyAt,
  isOwnerReply = false,
  isInternalNotice = false,
  lastAutoSendAt,
  enabled = true,
  alreadyHeld = false,
  cooldownMs,
  graceMs,
  chunkContinuationMs,
}) {
  if (!enabled || isOwnerReply || isInternalNotice) {
    return { action: ACTION_SEND, reason: !enabled ? 'disabled' : (isOwnerReply ? 'owner_reply' : 'internal_notice') };
  }

  const ownerTs = Number(ownerReplyAt);
  const ownerActive = Number.isFinite(ownerTs)
    && ownerTs > 0
    && (now - ownerTs) < cooldownMs;

  // A continuation of a reply already being delivered. The first chunk paid the
  // grace and passed the gate; splitting the message is our doing, not the
  // contact's, and half a message is worse than none.
  const lastAuto = Number(lastAutoSendAt);
  const isContinuation = Number.isFinite(lastAuto)
    && lastAuto > 0
    && (now - lastAuto) < chunkContinuationMs;
  if (isContinuation) {
    return { action: ACTION_SEND, reason: 'chunk_continuation' };
  }

  if (ownerActive) {
    return { action: ACTION_DROP, reason: 'owner_active', ownerAgeMs: now - ownerTs };
  }

  if (!alreadyHeld && graceMs > 0) {
    return { action: ACTION_HOLD, reason: 'grace', waitMs: graceMs };
  }

  return { action: ACTION_SEND, reason: alreadyHeld ? 'clear_after_grace' : 'clear' };
}

/**
 * A orquestração: segura, pergunta de novo e devolve `true` quando a mensagem
 * NÃO deve ser entregue. Mora aqui, e não no `bridge.js`, para ser testável
 * sem Baileys nem Express — a espera e a re-checagem são o comportamento que
 * o Victor pediu em 21/set ("reavalie depois de poucos segundos"), e um
 * comportamento que só existe dentro de um endpoint não se prova.
 *
 * Tudo que toca o mundo entra por parâmetro: o relógio, a espera, o estado do
 * dono e o registro.
 */
export async function runOutboundOwnerGate({
  chatId,
  isOwnerReply = false,
  isInternalNotice = false,
  enabled = true,
  cooldownMs,
  graceMs,
  chunkContinuationMs,
  ownerReplyAtFor,
  lastAutoSendAtFor = () => undefined,
  markAutoSend = () => {},
  now = () => Date.now(),
  sleep,
  onSuppressed = () => {},
  prune = () => {},
}) {
  const base = { isOwnerReply, isInternalNotice, enabled, cooldownMs, graceMs, chunkContinuationMs };

  prune();
  let decision = classifyOutboundOwnerGate({
    ...base,
    now: now(),
    ownerReplyAt: ownerReplyAtFor(chatId),
    lastAutoSendAt: lastAutoSendAtFor(chatId),
  });

  if (decision.action === ACTION_HOLD) {
    await sleep(decision.waitMs);
    prune();
    decision = classifyOutboundOwnerGate({
      ...base,
      now: now(),
      ownerReplyAt: ownerReplyAtFor(chatId),
      lastAutoSendAt: lastAutoSendAtFor(chatId),
      alreadyHeld: true,
    });
  }

  if (decision.action === ACTION_DROP) {
    onSuppressed({ chatId, reason: decision.reason, ownerAgeMs: decision.ownerAgeMs, graceMs });
    return true;
  }

  markAutoSend(chatId, now());
  return false;
}
