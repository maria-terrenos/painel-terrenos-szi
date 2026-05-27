import os
import re
import httpx
import json
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

SLACK_TOKEN = os.getenv("SLACK_TOKEN")
CHANNELS = [
    os.getenv("SLACK_CHANNEL_ANALISES"),  # szni-análises-preliminares
    os.getenv("SLACK_CHANNEL_TERRENOS"),  # szni-análise-terrenos
]
CHANNEL_ESTUDOS = os.getenv("SLACK_CHANNEL_ESTUDOS", "C0854BH9VK6")

client = OpenAI(
    base_url=os.getenv("ANTHROPIC_BASE_URL") + "/v1",
    api_key=os.getenv("ANTHROPIC_API_KEY"),
)
MODEL = "gemini-2.5-flash"

HEADERS = {"Authorization": f"Bearer {SLACK_TOKEN}"}


def slack_get(endpoint, params):
    r = httpx.get(f"https://slack.com/api/{endpoint}", headers=HEADERS, params=params, timeout=15)
    return r.json()


def fetch_channel_threads(channel_id: str, limit: int = 50) -> list[dict]:
    """Busca mensagens principais (threads) de um canal."""
    data = slack_get("conversations.history", {"channel": channel_id, "limit": limit})
    if not data.get("ok"):
        return []
    messages = []
    for msg in data.get("messages", []):
        text = msg.get("text", "")
        # Filtra apenas mensagens que parecem ser de terrenos ([ID-XXXXX])
        if re.search(r'\[ID[\s\-]+\d+\]', text, re.IGNORECASE) or \
           re.search(r'\b(EM|EP|R1|AP|Triagem)\b', text):
            messages.append({
                "ts": msg["ts"],
                "text": text,
                "reply_count": msg.get("reply_count", 0),
                "channel": channel_id,
            })
    return messages


def fetch_thread_replies(channel_id: str, thread_ts: str, limit: int = 20) -> list[str]:
    """Busca replies de uma thread."""
    data = slack_get("conversations.replies", {
        "channel": channel_id,
        "ts": thread_ts,
        "limit": limit,
    })
    if not data.get("ok"):
        return []
    msgs = data.get("messages", [])
    return [m.get("text", "") for m in msgs[1:]]  # pula a mensagem original


def extract_terrain_id(text: str) -> str | None:
    """Extrai o ID do terreno do texto."""
    match = re.search(r'\[ID[\s\-]+(\d+)\]', text, re.IGNORECASE)
    return match.group(1) if match else None


def analyze_thread_with_claude(terrain_id: str, original: str, replies: list[str]) -> dict:
    """Usa Claude para extrair status estruturado de uma thread do Slack."""
    thread_text = f"Mensagem principal:\n{original}\n\nReplies:\n" + \
                  "\n".join(f"- {r}" for r in replies if r.strip())

    prompt = f"""Você é um analista de terrenos. Analise essa thread do Slack sobre o terreno ID-{terrain_id} e extraia as informações abaixo em JSON.

Thread:
{thread_text}

Retorne APENAS um JSON válido com exatamente esses campos:
{{
  "id_terreno": "{terrain_id}",
  "tipo_estudo": "Triagem|AP|EM|EP|R1|outro",
  "status_atual": "em andamento|aprovado|reprovado|aguardando|pausado|revisão",
  "ultima_decisao": "resumo da última decisão tomada na thread (max 100 chars)",
  "proximo_passo": "próxima ação identificada (max 80 chars)",
  "responsavel": "nome ou @ do responsável identificado",
  "sentimento": "positivo|neutro|negativo",
  "dias_sem_update": null
}}

Regras:
- Se não encontrar a info, use null
- tipo_estudo: use o tipo de estudo mencionado (EM, EP, etc.)
- status_atual: "aprovado" se houver "pode seguir", "aprovado", "ok"; "reprovado" se "inviável", "descartado"; "aguardando" se estiver esperando algo
- sentimento: "positivo" se estiver avançando bem, "negativo" se tiver problemas graves, "neutro" caso contrário"""

    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.choices[0].message.content.strip()

    # Remove markdown code block se presente
    clean = re.sub(r'^```(?:json)?\s*', '', raw)
    clean = re.sub(r'\s*```$', '', clean).strip()

    # Tenta parse direto
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # Fallback: extrai bloco JSON via regex
    match = re.search(r'\{[^{}]*\}', clean, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return {
        "id_terreno": terrain_id,
        "tipo_estudo": None,
        "status_atual": "erro ao analisar",
        "ultima_decisao": clean[:100],
        "proximo_passo": None,
        "responsavel": None,
        "sentimento": "neutro",
        "dias_sem_update": None,
    }


def get_user_names() -> dict:
    """Busca mapeamento user_id → nome de exibição (com paginação)."""
    names = {}
    cursor = None
    for _ in range(10):  # máximo 10 páginas
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = slack_get("users.list", params)
        if not data.get("ok"):
            break
        for m in data.get("members", []):
            if m.get("deleted") or m.get("is_bot"):
                continue
            profile = m.get("profile", {})
            name = profile.get("display_name") or profile.get("real_name") or m["id"]
            names[m["id"]] = name
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return names


def _resolve_mentions(text: str, user_names: dict) -> str:
    """Substitui <@UXXXXXX> pelo nome do usuário."""
    def replace(m):
        uid = m.group(1)
        return f"@{user_names.get(uid, uid)}"
    return re.sub(r'<@([A-Z0-9]+)>', replace, text)


def get_terrain_thread(terrain_id: str, channel_id: str = None) -> dict:
    """
    Busca thread do Slack sobre um terreno específico.
    channel_id: canal a pesquisar (padrão: CHANNEL_ESTUDOS)
    Retorna: {found: bool, messages: [...]}
    """
    if channel_id is None:
        channel_id = CHANNEL_ESTUDOS

    # Busca histórico recente do canal (200 mensagens)
    history = slack_get("conversations.history", {"channel": channel_id, "limit": 200})
    if not history.get("ok"):
        return {"found": False, "messages": [], "error": history.get("error", "API error")}

    def _msg_contains_id(msg: dict, tid: str) -> bool:
        """Checa text + attachments (para mensagens que referenciam thread de outro canal)."""
        if tid in msg.get("text", ""):
            return True
        for att in msg.get("attachments", []):
            if tid in att.get("text", "") or tid in att.get("fallback", "") or tid in att.get("pretext", ""):
                return True
        return False

    parent_ts = None
    parent_text = None
    for msg in history.get("messages", []):
        if _msg_contains_id(msg, str(terrain_id)):
            parent_ts = msg["ts"]
            # texto combinado: próprio + attachments para checar "triagem"
            att_texts = " ".join(
                a.get("text", "") + " " + a.get("fallback", "")
                for a in msg.get("attachments", [])
            )
            parent_text = msg.get("text", "") + " " + att_texts
            break

    if not parent_ts:
        return {"found": False, "is_triagem": False, "messages": [], "channel_id": channel_id}

    # Se a mensagem mãe contém "triagem", o estudo de AP ainda não foi postado
    if "triagem" in (parent_text or "").lower():
        return {"found": True, "is_triagem": True, "messages": [], "channel_id": channel_id}

    # Busca o thread completo
    thread_data = slack_get("conversations.replies", {
        "channel": channel_id,
        "ts": parent_ts,
        "limit": 50,
    })
    if not thread_data.get("ok"):
        return {"found": False, "messages": [], "error": thread_data.get("error")}

    user_names = get_user_names()

    # Resolve IDs ausentes com lookup individual
    unknown_ids = {
        msg.get("user", "") for msg in thread_data.get("messages", [])
        if msg.get("user") and msg["user"] not in user_names
    }
    for uid in unknown_ids:
        info = slack_get("users.info", {"user": uid})
        if info.get("ok"):
            p = info["user"].get("profile", {})
            user_names[uid] = p.get("display_name") or p.get("real_name") or uid

    from datetime import datetime
    messages = []
    for msg in thread_data.get("messages", []):
        user_id = msg.get("user", "")
        name = user_names.get(user_id, user_id or "Bot")
        ts_float = float(msg.get("ts", 0))
        dt = datetime.fromtimestamp(ts_float)
        text_clean = _resolve_mentions(msg.get("text", ""), user_names)
        messages.append({
            "ts": msg["ts"],
            "text": text_clean,
            "user_id": user_id,
            "user_name": name,
            "hora": dt.strftime("%d/%m %H:%M"),
            "is_parent": msg["ts"] == parent_ts,
        })

    return {"found": True, "is_triagem": False, "messages": messages, "channel_id": channel_id}


def sync_slack_status(limit_per_channel: int = 30) -> list[dict]:
    """Sincroniza status dos terrenos a partir dos dois canais do Slack."""
    results = []
    seen_ids = set()

    for channel_id in CHANNELS:
        threads = fetch_channel_threads(channel_id, limit=limit_per_channel)
        for thread in threads:
            terrain_id = extract_terrain_id(thread["text"])
            if not terrain_id or terrain_id in seen_ids:
                continue
            seen_ids.add(terrain_id)

            replies = []
            if thread["reply_count"] > 0:
                replies = fetch_thread_replies(channel_id, thread["ts"])

            try:
                analysis = analyze_thread_with_claude(terrain_id, thread["text"], replies)
                analysis["slack_ts"] = thread["ts"]
                analysis["canal"] = "szni-análises-preliminares" if channel_id == CHANNELS[0] else "szni-análise-terrenos"
                results.append(analysis)
            except Exception as e:
                results.append({
                    "id_terreno": terrain_id,
                    "status_atual": "erro",
                    "ultima_decisao": str(e)[:80],
                    "slack_ts": thread["ts"],
                    "canal": channel_id,
                })

    return results
