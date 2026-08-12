#!/usr/bin/env python3
"""Read-only CRM funnel report for the WhatsApp secretary.

Prints where every lead currently sits and which ones went quiet mid-flow.
Strictly read-only: it opens the appointments database in SQLite read-only
mode and sends nothing to anyone — reception follow-up stays a human
decision, not an automated outbound message.

Usage:
    python scripts/secretary_funnel_report.py [--db PATH] [--stale-hours N]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BRT = ZoneInfo("America/Bahia")

DEFAULT_DB = os.path.join(
    os.path.expanduser("~/.hermes/profiles/secretary"), "state", "appointments.sqlite3"
)

# Funnel order, first contact to outcome. Anything not listed is printed after.
STAGE_ORDER = (
    "NOVO",
    "QUALIFICANDO",
    "ESCOLHENDO_SERVICO",
    "ESCOLHENDO_HORARIO",
    "IDENTIFICANDO",
    "AGUARDANDO_AUTORIZACAO",
    "AGUARDANDO_PAGAMENTO",
    "AGENDADO",
    "ATENDIMENTO_HUMANO",
    "PERDIDO",
)

# Stages where a quiet lead is a lost booking rather than a finished one.
ACTIONABLE_STAGES = (
    "NOVO",
    "QUALIFICANDO",
    "ESCOLHENDO_SERVICO",
    "ESCOLHENDO_HORARIO",
    "IDENTIFICANDO",
    "AGUARDANDO_AUTORIZACAO",
    "AGUARDANDO_PAGAMENTO",
)


def _connect(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise SystemExit(f"funnel database not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _age(stamp: str, now: datetime) -> str:
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return "?"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRT)
    seconds = max(0, int((now - parsed).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}min"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help="appointments database path")
    parser.add_argument(
        "--stale-hours",
        type=int,
        default=24,
        help="how long without activity counts as a stalled lead (default: 24)",
    )
    args = parser.parse_args()

    now = datetime.now(BRT)
    connection = _connect(args.db)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "leads" not in tables:
            print("No funnel yet: this database predates the CRM tables.")
            return 0

        counts = {
            str(stage): int(count)
            for stage, count in connection.execute(
                "SELECT stage, COUNT(*) FROM leads GROUP BY stage"
            )
        }
        total = sum(counts.values())

        print(f"FUNIL DA SECRETÁRIA — {now:%d/%m/%Y %H:%M} (America/Bahia)")
        print(f"Total de contatos no funil: {total}\n")

        ordered = list(STAGE_ORDER) + sorted(set(counts) - set(STAGE_ORDER))
        width = max((len(stage) for stage in ordered), default=0)
        for stage in ordered:
            count = counts.get(stage, 0)
            if count == 0 and stage not in ACTIONABLE_STAGES:
                continue
            bar = "#" * min(count, 40)
            print(f"  {stage:<{width}}  {count:>4}  {bar}")

        cutoff = (now - timedelta(hours=args.stale_hours)).isoformat()
        placeholders = ",".join("?" for _ in ACTIONABLE_STAGES)
        stale = connection.execute(
            "SELECT chat_key, stage, service_label, updated_at, touches FROM leads"
            f" WHERE updated_at < ? AND stage IN ({placeholders})"
            " ORDER BY updated_at",
            (cutoff, *ACTIONABLE_STAGES),
        ).fetchall()

        print(
            f"\nLEADS PARADOS há mais de {args.stale_hours}h "
            f"({len(stale)}) — candidatos a retomada pela recepção:"
        )
        if not stale:
            print("  (nenhum)")
        for chat_key, stage, service_label, updated_at, touches in stale:
            label = f" · {service_label}" if service_label else ""
            print(
                f"  {str(chat_key):<28} {str(stage):<24}"
                f" parado há {_age(str(updated_at), now):>5}"
                f" · {int(touches or 0)} msgs{label}"
            )
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
