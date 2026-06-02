#!/usr/bin/env python3
"""
Verificador de Pendências WhatsApp - Dr. Victor Almeida

Gera:
1. JSON com estado atual (para o painel)
2. HTML com cards interativos (links clicáveis acionam comandos via Telegram)
3. Alerta formatado para o cron

Estado do painel é salvo em painel/state.json e persiste entre execuções.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Config
HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
STATE_DB = HERMES_HOME / "state.db"
OUTPUT_DIR = HERMES_HOME / "painel"
OUTPUT_HTML = OUTPUT_DIR / "index.html"
OUTPUT_JSON = OUTPUT_DIR / "pendencies.json"
STATE_FILE = OUTPUT_DIR / "state.json"

URGENT_HOURS = 4
WARNING_HOURS = 1

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---- Categorias de irrelevância (propaganda, venda, serviços) ----
IRRELEVANT_KEYWORDS = [
    "loteamento", "imóvel", "imóveis", "terreno", "apartamento", "venda",
    "corretor", "corretora", "empreendimento", "lançamento imobiliário",
    "reserva apoema", "condomínio", "incorporadora",
    "propaganda", "divulgação", "parceria comercial",
    "instagram reel", "reels", "segue lá", "segue o perfil",
    "consultoria", "serviço de", "oferecer serviços",
    "doctoralia", "perfil doctoralia", "atualização de perfil",
]

# Palavras que indicam que a secretária pediu ação do Dr. Victor
NEEDS_DECISION_KEYWORDS = [
    # A secretária anotou e passou a bola
    "deixo o recado", "deixar um recado", "vou anotar", "anotado",
    "recado completo", "deixei registrado", "aguardando retorno",
    "dr. victor decide", "deixo para ele decidir",
    # Paciente pedindo algo que requer decisão médica
    "aumento de medicação", "aumentar", "implante", "reajuste",
    "mudar receita", "nova receita", "preciso de receita",
    "agendar consulta", "marcar consulta", "quero agendar",
    "retorno", "urgente", "emergência", "dor", "sintomas",
    # Mensagem que o paciente deixou e precisa ser lida
    "deixou um recado", "recado para o dr", "recado para você",
]


def load_previous_state():
    """Carrega estado anterior (itens resolvidos/ignorados manualmente)."""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"resolved": [], "ignored": [], "session_labels": {}}


def save_state(state):
    """Salva estado."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_whatsapp_sessions():
    """Busca sessões recentes do WhatsApp no banco."""
    if not STATE_DB.exists():
        return []
    conn = sqlite3.connect(str(STATE_DB))
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("""
        SELECT s.id as session_id, s.source, s.user_id, s.title,
               s.started_at, s.message_count, s.ended_at
        FROM sessions s
        WHERE s.source = 'whatsapp'
        ORDER BY s.started_at DESC
        LIMIT 50
    """)
    sessions = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return sessions


def get_last_messages(session_id, limit=5):
    """Últimas mensagens de uma sessão."""
    conn = sqlite3.connect(str(STATE_DB))
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("""
        SELECT role, content, timestamp
        FROM messages
        WHERE session_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (session_id, limit))
    messages = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return list(reversed(messages))


def is_irrelevant(title, messages):
    """Verifica se a conversa é propaganda/venda/serviço."""
    text = title.lower()
    for msg in messages:
        text += " " + (msg.get("content") or "").lower()
    return any(kw in text for kw in IRRELEVANT_KEYWORDS)


def has_needs_decision(title, messages):
    """Verifica se a conversa tem algo que precisa da decisão do Dr. Victor."""
    text = title.lower()
    for msg in messages:
        text += " " + (msg.get("content") or "").lower()
    return any(kw in text for kw in NEEDS_DECISION_KEYWORDS)


def is_farewell(messages):
    """Verifica se a secretária já encerrou a conversa."""
    farewell_keywords = [
        "tenha um ótimo dia", "tenha um bom dia", "fica à vontade",
        "se precisar de mais alguma coisa", "se precisar de algo",
        "sem problema", "por nada", "fico feliz em ajudar",
        "melhoras", "boa recuperação", "obrigada",
        "prontinho", "tudo certo", "ok! tenha um ótimo dia",
    ]
    last_assistant = None
    for m in reversed(messages):
        if m["role"] == "assistant":
            last_assistant = (m.get("content") or "").lower()
            break
    if last_assistant and messages and messages[-1]["role"] == "assistant":
        return any(kw in last_assistant for kw in farewell_keywords)
    return False


def load_owner_reply_cooldowns():
    """Carrega o mapa de chats onde o dono respondeu manualmente (bridge fromMe).
    
    Retorna dict: {chat_id: timestamp} dos chats com handoff ativo.
    """
    cooldown_file = HERMES_HOME / "whatsapp" / "owner-reply-cooldowns.json"
    if not cooldown_file.exists():
        return {}
    try:
        with open(cooldown_file) as f:
            raw = json.load(f)
        # Formato: {"chat_id@lid": timestamp_iso, ...}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def has_owner_replied(session, owner_reply_map):
    """Verifica se o dono (Dr. Victor) já respondeu manualmente neste chat.
    
    Confere se o chat_id da sessão aparece no owner-reply-cooldowns.json
    com timestamp recente (< 24h).
    """
    if not owner_reply_map:
        return False
    chat_id = session.get("user_id", "")
    if not chat_id:
        return False
    # Tenta match exato e substring (LID pode vir em formatos diferentes)
    now = datetime.now(timezone.utc)
    for cid, ts_str in owner_reply_map.items():
        if chat_id in cid or cid in chat_id:
            try:
                ts = datetime.fromisoformat(ts_str)
                hours = (now - ts).total_seconds() / 3600
                if hours < 24:
                    return True
            except Exception:
                continue
    return False


def analyze_pendencies(sessions, state):
    """Analisa sessões e classifica cada uma."""
    now = datetime.now(timezone.utc)
    pendencies = []
    resolved = []
    ignored = []
    learnt = []
    
    resolved_ids = set(state.get("resolved", []))
    ignored_ids = set(state.get("ignored", []))
    labels = state.get("session_labels", {})
    
    # Carrega mapa de respostas manuais do dono (fromMe bridge)
    owner_reply_map = load_owner_reply_cooldowns()
    owner_handled_ids = set()  # sessões que o dono já respondeu manualmente
    
    for session in sessions:
        started = session.get("started_at")
        if not started:
            continue
        
        session_id = session["session_id"]
        started_dt = datetime.fromtimestamp(started, tz=timezone.utc)
        hours_ago = (now - started_dt).total_seconds() / 3600
        title = (session.get("title") or "").strip()
        messages = get_last_messages(session_id)
        ended_at = session.get("ended_at")
        
        # Tenta extrair o nome do contato apenas se houver padrão claro
        # Tipo: "Aqui é Suzana" ou "Sou Michele" ou "Me chamo João"
        contact_name = title  # fallback: usa o título da sessão
        for m in messages:
            if m["role"] == "user" and m.get("content"):
                first_msg = (m.get("content") or "").strip()
                import re as _nr
                match = _nr.search(r'(?:sou|aqui é|é o|é a|me chamo|meu nome é)\s+([A-ZÀ-Úa-zà-ú]+)', first_msg, _nr.IGNORECASE)
                if match:
                    contact_name = match.group(1).capitalize()
                    break
        
        # Pula sessões sem título ou de teste
        if not title or title.lower() in ("none", "null", ""):
            continue
        test_kw = ["teste", "nudge funciona", "erro", "cuidado para não testar"]
        if any(kw in title.lower() for kw in test_kw):
            continue
        
        # Pula se já foi resolvido/ignorado manualmente
        if session_id in resolved_ids:
            continue
        if session_id in ignored_ids:
            continue
        
        # Pula se o dono já respondeu manualmente neste chat (fromMe)
        if has_owner_replied(session, owner_reply_map):
            owner_handled_ids.add(session_id)
            continue
        
        # Filtra irrelevantes (propaganda, venda, imóveis)
        if is_irrelevant(title, messages):
            ignored.append({
                "session_id": session_id,
                "title": title,
                "reason": "irrelevante",
            })
            continue
        
        # Sessão ativa (aberta nas últimas 2h ou sem fim)
        is_open = ended_at is None or (
            (now - datetime.fromtimestamp(ended_at, tz=timezone.utc)).total_seconds() < 7200
        )
        
        if hours_ago > 12 or not is_open or session.get("message_count", 0) < 2:
            continue
        
        # Verifica se precisa de decisão do Dr. Victor
        needs_decision = has_needs_decision(title, messages)
        closed = is_farewell(messages)
        
        if closed and not needs_decision:
            # Secretária já resolveu, sem pendência real
            resolved.append({
                "session_id": session_id,
                "title": title,
                "hours_ago": round(hours_ago, 1),
                "message_count": session.get("message_count", 0),
                "last_activity": started_dt.strftime("%d/%m %H:%M"),
            })
            continue
        
        # Classifica urgência
        if needs_decision:
            urgency = "decisao"
        elif hours_ago > URGENT_HOURS:
            urgency = "urgente"
        elif hours_ago > WARNING_HOURS:
            urgency = "atencao"
        else:
            urgency = "normal"
        
        # Preview das últimas mensagens
        preview_lines = []
        for m in messages[-3:]:
            role = "Paciente" if m["role"] == "user" else "Secretaria"
            content = (m.get("content") or "")[:150]
            if content:
                preview_lines.append(f"[{role}] {content}")
        
        item = {
            "session_id": session_id,
            "user_id": session.get("user_id", ""),
            "title": title,
            "contact_name": contact_name,
            "hours_ago": round(hours_ago, 1),
            "urgency": urgency,
            "needs_decision": needs_decision,
            "message_count": session.get("message_count", 0),
            "preview": "\n".join(preview_lines),
            "last_activity": started_dt.strftime("%d/%m %H:%M"),
            "label": labels.get(session_id, ""),
        }
        pendencies.append(item)
    
    return pendencies, resolved, ignored


def render_output(pendencies, resolved, ignored):
    """Gera JSON e HTML nos dois diretórios."""
    import shutil
    import subprocess
    
    now = datetime.now(timezone.utc)
    NGINX_DIR = Path("/var/www/html/painel")
    
    # Ordena: decisão > urgente > atenção
    urgency_order = {"decisao": 0, "urgente": 1, "atencao": 2, "normal": 3}
    pendencies.sort(key=lambda p: (urgency_order.get(p["urgency"], 9), -p["hours_ago"]))
    
    data = {
        "generated_at": now.isoformat(),
        "generated_at_br": now.strftime("%d/%m/%Y %H:%M"),
        "total_pendencias": len(pendencies),
        "decisao_count": sum(1 for p in pendencies if p["urgency"] == "decisao"),
        "urgente_count": sum(1 for p in pendencies if p["urgency"] == "urgente"),
        "atencao_count": sum(1 for p in pendencies if p["urgency"] == "atencao"),
        "pendencies": pendencies,
        "resolved": resolved,
        "ignored": ignored,
    }
    
    # JSON — diretório local
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    # HTML — diretório local
    html = build_html(data)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    
    # Copia para o diretório do nginx (sudo pois é owned by root)
    os.makedirs(NGINX_DIR, exist_ok=True)
    subprocess.run(
        ["sudo", "cp", str(OUTPUT_JSON), str(OUTPUT_HTML), str(STATE_FILE), str(NGINX_DIR) + "/"],
        check=False, timeout=10
    )
    
    return data


def build_html(data):
    """Gera HTML com tema claro, pt-BR e links de ação."""
    decisoes = [p for p in data["pendencies"] if p["urgency"] == "decisao"]
    urgentes = [p for p in data["pendencies"] if p["urgency"] == "urgente"]
    atencao = [p for p in data["pendencies"] if p["urgency"] == "atencao"]
    normais = [p for p in data["pendencies"] if p["urgency"] == "normal"]
    ignorados = data.get("ignored", [])
    
    def card_html(p, badge_cls, badge_text, border_cls):
        actions = ""
        preview = p["preview"].replace("\n", "<br>")
        contact_display = p.get("contact_name", "") or p["title"]
        return f"""
        <div class="card {border_cls}">
            <div class="card-header">
                <span class="badge {badge_cls}">{badge_text}</span>
                <span class="time">{p['hours_ago']}h atrás</span>
            </div>
            <div class="card-body">
                <h3>{contact_display}</h3>
                <p class="card-subtitle">{p['title']}</p>
                <p class="last-activity">Última: {p['last_activity']} · {p['message_count']} msg</p>
                <div class="preview">{preview}</div>
            </div>
        </div>"""
    
    cards = []
    for p in decisoes:
        cards.append(card_html(p, "badge-decisao", "🔴 Precisa de decisão", "decisao"))
    for p in urgentes:
        cards.append(card_html(p, "badge-urgente", "🔴 Urgente", "urgente"))
    for p in atencao:
        cards.append(card_html(p, "badge-atencao", "🟡 Atenção", "atencao"))
    for p in normais:
        cards.append(card_html(p, "badge-normal", "✅ Normal", "normal"))
    
    cards_html = "".join(cards) if cards else """
        <div class="empty-state">
            <div class="empty-icon">✅</div>
            <h2>Tudo em dia!</h2>
            <p>Nenhuma pendência no momento.</p>
        </div>"""
    
    ignored_count = len(ignorados)
    ignored_section = ""
    if ignorados:
        ignored_list = "".join(
            f'<li>{i["title"]}</li>' for i in ignorados
        )
        ignored_section = f"""
        <details style="margin: 20px 0;">
            <summary style="color: var(--text-muted); font-size: 13px; cursor: pointer;">
                🚫 {ignored_count} ignorados (propaganda/venda/serviço)
            </summary>
            <ul style="color: var(--text-muted); font-size: 12px; padding: 10px 20px;">
                {ignored_list}
            </ul>
        </details>"""
    
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel Dr. Victor</title>
    <style>
        :root {{
            --bg: #f5f5fa;
            --card-bg: #ffffff;
            --card-border: #dde1ea;
            --text: #1a1a2e;
            --text-muted: #6b7280;
            --text-secondary: #4b5563;
            --decisao: #dc2626;
            --urgente: #ea580c;
            --atencao: #d97706;
            --normal: #16a34a;
            --accent: #4f46e5;
            --section-bg: #eef0f6;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            padding: 16px;
            max-width: 800px;
            margin: 0 auto;
            -webkit-font-smoothing: antialiased;
        }}
        .header {{ text-align: center; padding: 24px 0 20px; margin-bottom: 20px; }}
        .header h1 {{ font-size: 22px; margin-bottom: 2px; }}
        .header p {{ color: var(--text-muted); font-size: 13px; }}
        .stats {{ display: flex; gap: 10px; justify-content: center; margin-bottom: 20px; }}
        .stat {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 14px 20px;
            text-align: center;
            min-width: 90px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06);
        }}
        .stat .number {{ font-size: 26px; font-weight: 700; }}
        .stat .label {{ font-size: 11px; color: var(--text-muted); margin-top: 3px; font-weight: 500; }}
        .stat.decisao .number {{ color: var(--decisao); }}
        .stat.urgente .number {{ color: var(--urgente); }}
        .stat.atencao .number {{ color: var(--atencao); }}
        .stat.total .number {{ color: var(--accent); }}
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            margin-bottom: 10px;
            overflow: hidden;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06);
        }}
        .card.decisao {{ border-left: 4px solid var(--decisao); }}
        .card.urgente {{ border-left: 4px solid var(--urgente); }}
        .card.atencao {{ border-left: 4px solid var(--atencao); }}
        .card.normal {{ border-left: 4px solid var(--normal); opacity: 0.75; }}
        .card-header {{
            display: flex; justify-content: space-between; align-items: center;
            padding: 10px 14px; border-bottom: 1px solid var(--card-border);
        }}
        .badge {{ font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 20px; }}
        .badge-decisao {{ background: #fef2f2; color: var(--decisao); border: 1px solid #fecaca; }}
        .badge-urgente {{ background: #fff7ed; color: var(--urgente); border: 1px solid #fed7aa; }}
        .badge-atencao {{ background: #fffbeb; color: var(--atencao); border: 1px solid #fde68a; }}
        .badge-normal {{ background: #f0fdf4; color: var(--normal); border: 1px solid #bbf7d0; }}
        .time {{ font-size: 12px; color: var(--text-muted); }}
        .card-body {{ padding: 10px 14px; }}
        .card-body h3 {{ font-size: 15px; margin-bottom: 2px; font-weight: 600; }}
        .card-subtitle {{ font-size: 12px; color: var(--text-muted); margin-bottom: 6px; }}
        .last-activity {{ font-size: 12px; color: var(--text-muted); margin-bottom: 8px; }}
        .preview {{
            font-size: 13px; color: var(--text-secondary);
            background: var(--section-bg);
            padding: 8px 10px; border-radius: 10px;
            line-height: 1.5; margin-bottom: 10px;
        }}
        .actions {{ display: flex; gap: 6px; flex-wrap: wrap; }}
        .btn {{
            display: inline-block;
            font-size: 12px; font-weight: 600;
            padding: 6px 12px; border-radius: 8px;
            text-decoration: none;
            transition: opacity 0.15s;
        }}
        .btn:hover {{ opacity: 0.8; }}
        .btn-responder {{ background: #eef2ff; color: var(--accent); border: 1px solid #c7d2fe; }}
        .btn-resolver {{ background: #f0fdf4; color: var(--normal); border: 1px solid #bbf7d0; }}
        .btn-ignorar {{ background: #f8f8f8; color: var(--text-muted); border: 1px solid #e5e7eb; }}
        .empty-state {{ text-align: center; padding: 60px 20px; }}
        .empty-icon {{ font-size: 48px; margin-bottom: 12px; }}
        .empty-state h2 {{ font-size: 20px; margin-bottom: 6px; }}
        .empty-state p {{ color: var(--text-muted); }}
        .section-title {{ font-size: 15px; margin: 20px 0 10px; color: var(--text-secondary); font-weight: 600; }}
        .legenda {{ font-size: 12px; color: var(--text-muted); margin-bottom: 16px; line-height: 1.6; }}
        @media (max-width: 600px) {{
            .stats {{ flex-wrap: wrap; }}
            .stat {{ min-width: 80px; padding: 10px 14px; }}
            .actions {{ flex-direction: column; }}
            .btn {{ text-align: center; }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🏥 Dr. Victor Almeida</h1>
        <p>Painel de Pendências · Atualizado {data['generated_at_br']}</p>
    </div>

    <div class="stats">
        <div class="stat decisao">
            <div class="number">{data['decisao_count']}</div>
            <div class="label">🔴 Decisão</div>
        </div>
        <div class="stat urgente">
            <div class="number">{data['urgente_count']}</div>
            <div class="label">🔴 Urgentes</div>
        </div>
        <div class="stat atencao">
            <div class="number">{data['atencao_count']}</div>
            <div class="label">🟡 Atenção</div>
        </div>
        <div class="stat total">
            <div class="number">{data['total_pendencias']}</div>
            <div class="label">Total</div>
        </div>
    </div>

    <p class="legenda">
        🔴 <strong>Precisa de decisão</strong> · 🔴 <strong>Urgente</strong> (&gt;4h) · 🟡 <strong>Atenção</strong> (&gt;1h)<br>
        📱 Me diga aqui no Telegram: <em>"resolver [nome]"</em>, <em>"ignorar [nome]"</em> ou <em>"responder [nome] mensagem"</em>
    </p>

    <h2 class="section-title">📋 Pendências</h2>
    {cards_html}

    {ignored_section}

    <div style="text-align:center;padding:20px 0;color:var(--text-muted);font-size:11px;">
        Atualização automática a cada 1h.
    </div>
</body>
</html>"""
    
    return html


def format_alert(data):
    """Alerta conciso para Telegram. Retorna None se nada a reportar."""
    parts = []
    decisoes = [p for p in data["pendencies"] if p["urgency"] == "decisao"]
    urgentes = [p for p in data["pendencies"] if p["urgency"] == "urgente"]
    atencao = [p for p in data["pendencies"] if p["urgency"] == "atencao"]
    
    if not decisoes and not urgentes and not atencao:
        return None
    
    if decisoes:
        parts.append(f"🔴 *{len(decisoes)} precisa(m) de decisão:*")
        for p in decisoes:
            contact = p.get("contact_name", p["title"])
            parts.append(f"  • {contact} ({p['hours_ago']}h)")
    
    if urgentes:
        parts.append(f"\n🔴 *{len(urgentes)} urgente(s):*")
        for p in urgentes:
            contact = p.get("contact_name", p["title"])
            parts.append(f"  • {contact} ({p['hours_ago']}h)")
    
    if atencao:
        parts.append(f"\n🟡 *{len(atencao)} para atenção:*")
        for p in atencao:
            contact = p.get("contact_name", p["title"])
            parts.append(f"  • {contact} ({p['hours_ago']}h)")
    
    parts.append(f"\n📊 Painel: http://163.176.225.183/painel/")
    parts.append(f"💬 Me diga o comando: *responder [nome]* ou *resolver [nome]*")
    
    return "\n".join(parts)


def main():
    state = load_previous_state()
    sessions = get_whatsapp_sessions()
    
    if not sessions:
        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_at_br": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M"),
            "total_pendencias": 0,
            "decisao_count": 0,
            "urgente_count": 0,
            "atencao_count": 0,
            "pendencies": [],
            "resolved": [],
            "ignored": [],
        }
        render_output([], [], [])
        print("OK: Nenhuma sessão encontrada.")
        return
    
    pendencies, resolved, ignored = analyze_pendencies(sessions, state)
    data = render_output(pendencies, resolved, ignored)
    
    # Auto-resolve sessões onde o dono já respondeu manualmente
    owner_reply_map = load_owner_reply_cooldowns()
    if owner_reply_map:
        for session in sessions:
            sid = session.get("session_id", "")
            if sid and sid not in state.get("resolved", []) and sid not in state.get("ignored", []):
                if has_owner_replied(session, owner_reply_map):
                    state.setdefault("resolved", []).append(sid)
                    state.setdefault("session_labels", {})[sid] = "respondido pelo dono"
        save_state(state)
    
    # Atualiza estado com itens ignorados automaticamente
    auto_ignored_ids = [i["session_id"] for i in ignored]
    if auto_ignored_ids:
        state["ignored"] = list(set(state.get("ignored", []) + auto_ignored_ids))
        save_state(state)
    
    alert = format_alert(data)
    if alert:
        print("ALERT:")
        print(alert)
    else:
        print("OK: Nenhuma pendência no momento.")


if __name__ == "__main__":
    main()
