#!/bin/bash
# gateway-guardian-safe.sh — staging-safe guardian for hermes-gateway
# Intended to replace the live guardian only after review and rollout approval.

set -u

LOG_FILE="${HERMES_GUARDIAN_LOG:-$HOME/.hermes/logs/gateway-watchdog.log}"
LOCK_FILE="${HERMES_GUARDIAN_LOCK:-/tmp/hermes-guardian.lock}"
MAINTENANCE_LOCK="${HERMES_MAINTENANCE_LOCK:-$HOME/.hermes/update-maintenance.lock}"
GATEWAY_SERVICE="${HERMES_GATEWAY_SERVICE:-hermes-gateway}"
BRIDGE_PORT="${HERMES_BRIDGE_PORT:-3000}"
GATEWAY_LOG="${HERMES_GATEWAY_LOG:-$HOME/.hermes/logs/gateway.log}"

exec 200>"$LOCK_FILE"
flock -n 200 || exit 0

log() {
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >> "$LOG_FILE"
}

# A planned update/rollout owns the service lifecycle. Never race it.
if [ -e "$MAINTENANCE_LOCK" ]; then
    log "maintenance lock present — no automatic gateway action"
    exit 0
fi

status=$(sudo systemctl is-active "$GATEWAY_SERVICE" 2>/dev/null || echo "inactive")

case "$status" in
    active) ;;
    activating|deactivating)
        log "gateway em transição ($status) — aguardando"
        exit 0
        ;;
    failed|inactive|dead)
        log "gateway $status — iniciando via systemd"
        sudo systemctl reset-failed "$GATEWAY_SERVICE" 2>/dev/null || true
        sudo systemctl start "$GATEWAY_SERVICE" 2>/dev/null || true
        log "gateway start solicitado via systemd (estava $status)"
        exit 0
        ;;
esac

bridge_ok=false
if curl -sf --max-time 5 "http://127.0.0.1:${BRIDGE_PORT}/health" >/dev/null 2>&1; then
    bridge_ok=true
fi

log_age=0
if [ -f "$GATEWAY_LOG" ]; then
    last_line_ts=$(tail -1 "$GATEWAY_LOG" 2>/dev/null | grep -oP '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}' | head -1)
    if [ -n "$last_line_ts" ]; then
        last_epoch=$(date -d "$last_line_ts" +%s 2>/dev/null || echo 0)
        now_epoch=$(date +%s)
        log_age=$(( now_epoch - last_epoch ))
    fi
fi

polling_conflicts=0
if [ -f "$GATEWAY_LOG" ]; then
    polling_conflicts=$(tail -120 "$GATEWAY_LOG" 2>/dev/null | grep -c "polling conflict" || echo 0)
fi

should_restart=false
reason=""

# A/B: only act when the bridge is actually offline.
if ! $bridge_ok && [ "$log_age" -gt 600 ] 2>/dev/null; then
    should_restart=true
    reason="bridge_offline+log_stale(${log_age}s)"
elif ! $bridge_ok && [ "$log_age" -le 600 ] 2>/dev/null && [ "$log_age" -gt 30 ]; then
    should_restart=true
    reason="bridge_offline+log_active(${log_age}s)"
fi

# C: stale logs are informational when the bridge is healthy; idle gateways
# naturally stop appending to gateway.log.
if $bridge_ok && [ "$log_age" -gt 3600 ] 2>/dev/null; then
    log "gateway idle (bridge=true log=${log_age}s) — no restart"
fi

# D: retain conflict detection, but use graceful restart and maintenance lock.
if [ "$polling_conflicts" -ge 3 ] 2>/dev/null; then
    should_restart=true
    reason="telegram_polling_conflict(${polling_conflicts}x)"
fi

if $should_restart; then
    log "gateway unhealthy ($reason) — requesting graceful restart"
    if sudo systemctl restart "$GATEWAY_SERVICE" 2>/dev/null; then
        log "gateway graceful restart requested ($reason)"
    else
        log "gateway restart request failed ($reason); no forced-kill fallback"
    fi
else
    _min=$(date +%M | sed 's/^0//')
    _min=${_min:-0}
    if [ $(( _min / 30 )) -eq 0 ] 2>/dev/null; then
        log "gateway OK (bridge=$bridge_ok log=${log_age}s conflicts=$polling_conflicts)"
    fi
fi

exit 0
