import os
import json
import time
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from apscheduler.schedulers.background import BackgroundScheduler

from database import get_leadtime, get_conversao, get_ativos, get_kpis, get_funil_leadtime, get_etapas_breakdown, get_proposta_relatorio, get_pre_proposta, get_final_funil
from slack_analyzer import sync_slack_status, get_terrain_thread

app = FastAPI(title="Painel de Terrenos SZI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cache em memória — usa /app/data/cache.json se existir (Docker), senão raiz do projeto (local)
_cache_dir = Path(os.getenv("CACHE_DIR", str(Path(__file__).parent.parent)))
CACHE_FILE = _cache_dir / "cache.json"

def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}

def save_cache(data: dict):
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, default=str))

cache: dict = load_cache()


def run_sync():
    """Sincroniza todos os dados e salva no cache."""
    global cache
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando sincronização...")
    try:
        cache["kpis"]           = get_kpis()
        cache["funil_leadtime"] = get_funil_leadtime()
        cache["etapas"]         = get_etapas_breakdown()
        cache["proposta"]       = get_proposta_relatorio()
        cache["pre_proposta"]   = get_pre_proposta()
        cache["final_funil"]    = get_final_funil()
        cache["leadtime"]       = get_leadtime()
        cache["conversao"]      = get_conversao()
        cache["ativos"]         = get_ativos()

        # Histórico por etapa para tendência
        # Histórico Pré Proposta
        pp = cache.get("pre_proposta", {})
        if pp.get("total", 0) > 0:
            hist_pp = cache.get("pre_proposta_historico", [])
            hist_pp.append({"ts": datetime.now().isoformat(), "count": pp["total"], "avg_dias": pp.get("avg_uteis", 0)})
            cache["pre_proposta_historico"] = hist_pp[-60:]

        for etapa_key, cache_key in [
            (lambda e: 'falta' in e.lower(), "falta_info_historico"),
            (lambda e: 'análise preliminar' in e.lower() or 'analise preliminar' in e.lower(), "ap_historico"),
            (lambda e: e.lower().strip() == 'em iniciado', "em_historico"),
            (lambda e: e.lower().strip() == 'ep iniciado', "ep_historico"),
        ]:
            match = next((e for e in cache["etapas"] if etapa_key(e.get('etapa',''))), None)
            if match:
                hist = cache.get(cache_key, [])
                hist.append({"ts": datetime.now().isoformat(), "count": match["count"], "avg_dias": match["avg_dias"]})
                cache[cache_key] = hist[-60:]

        # Salva dados do DB antes do Slack (que pode demorar)
        cache["updated_at"] = datetime.now().isoformat()
        save_cache(cache)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Dados do banco salvos, iniciando sync Slack...")

        # Slack com timeout de 120s via thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(sync_slack_status, 40)
            try:
                cache["slack"] = future.result(timeout=120)
                cache["updated_at"] = datetime.now().isoformat()
                save_cache(cache)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Sincronização concluída")
            except concurrent.futures.TimeoutError:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Slack timeout — dados do banco já salvos")
    except Exception as e:
        print(f"Erro na sincronização: {e}")
        cache["sync_error"] = str(e)


# Scheduler — 3 vezes ao dia
sync_hours = os.getenv("SYNC_HOURS", "8,13,18").split(",")
scheduler = BackgroundScheduler()
for h in sync_hours:
    scheduler.add_job(run_sync, "cron", hour=int(h.strip()), minute=0)
scheduler.start()

# Se cache vazio, sincroniza ao iniciar
if not cache.get("updated_at"):
    import threading
    threading.Thread(target=run_sync, daemon=True).start()


# ── Endpoints ──────────────────────────────────────────────

@app.get("/api/status")
def status():
    return {
        "updated_at": cache.get("updated_at"),
        "slack_count": len(cache.get("slack", [])),
        "ativos_count": len(cache.get("ativos", [])),
        "sync_hours": sync_hours,
    }

@app.get("/api/kpis")
def kpis():
    return cache.get("kpis", {})

@app.get("/api/funil-leadtime")
def funil_leadtime():
    return cache.get("funil_leadtime", {})

@app.get("/api/leadtime")
def leadtime():
    return cache.get("leadtime", [])

@app.get("/api/conversao")
def conversao():
    return cache.get("conversao", [])

@app.get("/api/ativos")
def ativos():
    return cache.get("ativos", [])

@app.get("/api/etapas")
def etapas():
    return cache.get("etapas", [])

@app.get("/api/falta-info")
def falta_info():
    """Dados da etapa 'Falta Informação': média atual, tendência e lista de terrenos."""
    etapas_data = cache.get("etapas", [])
    fi = next((e for e in etapas_data if 'falta' in e.get('etapa','').lower()), None)
    if not fi:
        return {"count": 0, "avg_atual": 0, "variacao_pct": None, "terrenos": [], "historico": []}

    hist = cache.get("falta_info_historico", [])
    variacao = None
    if len(hist) >= 2:
        prev = hist[-2]["avg_dias"] or 0
        atual = hist[-1]["avg_dias"] or 0
        if prev > 0:
            variacao = round(((atual - prev) / prev) * 100, 1)

    return {
        "count": fi["count"],
        "avg_atual": fi["avg_dias"],
        "variacao_pct": variacao,
        "terrenos": fi["terrenos"],     # já ordenado por dias_na_etapa desc
        "historico": hist[-12:],        # últimos 12 pontos para sparkline
    }

@app.get("/api/slack")
def slack():
    return cache.get("slack", [])

@app.get("/api/ap")
def ap_data():
    """Dados da etapa 'Análise Preliminar': média atual, tendência e lista de terrenos."""
    etapas_data = cache.get("etapas", [])
    ap = next((e for e in etapas_data if 'análise preliminar' in e.get('etapa','').lower()
               or 'analise preliminar' in e.get('etapa','').lower()), None)
    if not ap:
        return {"count": 0, "avg_atual": 0, "variacao_pct": None, "terrenos": [], "historico": []}

    hist = cache.get("ap_historico", [])
    variacao = None
    if len(hist) >= 2:
        prev = hist[-2]["avg_dias"] or 0
        atual = hist[-1]["avg_dias"] or 0
        if prev > 0:
            variacao = round(((atual - prev) / prev) * 100, 1)

    return {
        "count": ap["count"],
        "avg_atual": ap["avg_dias"],
        "variacao_pct": variacao,
        "terrenos": ap["terrenos"],
        "historico": hist[-12:],
    }

@app.get("/api/slack/terreno/{terrain_id}")
def slack_terreno(terrain_id: str):
    """Busca thread do Slack para um terreno — canal de estudos (C0854BH9VK6)."""
    return get_terrain_thread(terrain_id)

@app.get("/api/estudos")
def estudos():
    """Terrenos em EM Iniciado e EP Iniciado com médias e tendências."""
    etapas_data = cache.get("etapas", [])

    def etapa_block(name_check, hist_key):
        match = next((e for e in etapas_data if name_check(e.get('etapa','').lower().strip())), None)
        hist = cache.get(hist_key, [])
        variacao = None
        if len(hist) >= 2:
            prev = hist[-2]["avg_dias"] or 0
            atual = hist[-1]["avg_dias"] or 0
            if prev > 0:
                variacao = round(((atual - prev) / prev) * 100, 1)
        if not match:
            return {"count": 0, "avg_atual": 0, "variacao_pct": None, "terrenos": [], "historico": hist[-12:]}
        return {
            "count": match["count"],
            "avg_atual": match["avg_dias"],
            "variacao_pct": variacao,
            "terrenos": match["terrenos"],
            "historico": hist[-12:],
        }

    return {
        "em": etapa_block(lambda e: e == 'em iniciado', "em_historico"),
        "ep": etapa_block(lambda e: e == 'ep iniciado', "ep_historico"),
    }

@app.get("/api/proposta")
def proposta():
    return cache.get("proposta", {"total": 0, "terrenos": []})

@app.get("/api/final-funil")
def final_funil():
    return cache.get("final_funil", {"total": 0, "grupos": []})

@app.get("/api/pre-proposta")
def pre_proposta():
    d = cache.get("pre_proposta", {"total": 0, "avg_uteis": 0, "terrenos": []})
    hist = cache.get("pre_proposta_historico", [])
    variacao = None
    if len(hist) >= 2:
        prev  = hist[-2]["avg_dias"] or 0
        atual = hist[-1]["avg_dias"] or 0
        if prev > 0:
            variacao = round(((atual - prev) / prev) * 100, 1)
    return {**d, "variacao_pct": variacao, "historico": hist[-12:]}

@app.get("/api/slack/estudos/{terrain_id}")
def slack_estudos(terrain_id: str):
    """Busca thread do Slack — canal análise de terrenos (C072CQWFS2W)."""
    return get_terrain_thread(terrain_id, os.getenv("SLACK_CHANNEL_TERRENOS"))

@app.get("/api/slack/ap/{terrain_id}")
def slack_ap(terrain_id: str):
    """Busca thread do Slack para um terreno — canal análises preliminares (C08U5E0EN13)."""
    return get_terrain_thread(terrain_id, os.getenv("SLACK_CHANNEL_ANALISES"))

@app.post("/api/sync")
def sync_manual():
    """Dispara sincronização manual."""
    import threading
    threading.Thread(target=run_sync, daemon=True).start()
    return {"message": "Sincronização iniciada", "started_at": datetime.now().isoformat()}


# Serve o frontend
frontend_dir = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

@app.get("/")
def index():
    return FileResponse(str(frontend_dir / "index.html"))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("APP_PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
