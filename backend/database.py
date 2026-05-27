import os
import psycopg2
import psycopg2.extras
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))


def _business_days_between(start: date, end: date) -> int:
    """Dias úteis (seg–sex) de start (inclusive) até end (exclusive)."""
    if not start or end <= start:
        return 0
    days, cur = 0, start
    while cur < end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def _add_business_days(start: date, n: int) -> date:
    """Retorna a data após n dias úteis a partir de start."""
    cur, added = start, 0
    while added < n:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


def _farol(entry: date, meta_du: int, hoje: date) -> dict:
    """Retorna dict com dias_uteis, prazo, dias_atraso e farol para um terreno."""
    if not entry:
        return {"dias_uteis": None, "prazo": None, "dias_atraso": 0, "farol": "cinza"}
    du      = _business_days_between(entry, hoje)
    prazo   = _add_business_days(entry, meta_du)
    atraso  = max(0, _business_days_between(prazo, hoje))
    if hoje < prazo:
        cor = "verde"
    elif hoje == prazo:
        cor = "amarelo"
    else:
        cor = "vermelho"
    return {"dias_uteis": du, "prazo": prazo.strftime("%d/%m"), "dias_atraso": atraso, "farol": cor}

def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS"),
    )

PARSE_DATE = """
    CASE
        WHEN {col} ~ '^[0-9]{{2}}/[0-9]{{2}}/[0-9]{{4}}' THEN TO_DATE({col}, 'DD/MM/YYYY')
        WHEN {col} ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' THEN TO_DATE(LEFT({col},10), 'YYYY-MM-DD')
        ELSE NULL
    END
"""

def pd(col):
    return PARSE_DATE.format(col=col)


def get_funil_leadtime():
    """
    Leadtime do funil de terrenos ABERTOS (excluindo EP Finalizado, Perdido, Excluídos).
    Retorna: média atual, tendência mensal (ciclos concluídos) e top 5 ofensores.
    """
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Busca ativos válidos com data de saída do Perdido (se houver)
    cur.execute("""
        SELECT
            t.ids, t.etapa, t.cidade, t.bairro,
            t.data_de_entrada_triagem,
            t.data_de_entrada_em_analise_preliminar,
            t.data_de_entrada_em_fila_de_em,
            t.data_de_entrada_em_em_iniciado,
            t.data_de_entrada_em_em_analise_de_roi,
            t.data_de_entrada_em_em_finalizado,
            t.data_de_entrada_em_fila_de_ep,
            t.data_de_entrada_em_ep_iniciado,
            t.data_de_entrada_em_ep_revisao_de_engenharia,
            t.data_de_entrada_em_ep_orcamento_previo,
            t.data_de_entrada_em_revisao_de_roi,
            t.data_de_entrada_em_proposta,
            t.data_de_entrada_em_primeira_proposta,
            t.data_de_entrada_em_negociacao,
            t.data_de_entrada_em_aceito_pelo_vendedor,
            p.perdidopropostalasttimeout
        FROM pipefy_szi_all_cards_transformada_bd_terreno t
        LEFT JOIN pipefy_szi_all_cards_304548446_colunas_expandidas p
            ON p.id_do_terreno = t.ids
        WHERE t.status = 'Aberto'
          AND t.etapa NOT IN ('EP Finalizado', 'Excluídos', 'Excluído')
          AND t.etapa NOT ILIKE '%perdido%'
    """)
    rows = [dict(r) for r in cur.fetchall()]

    # Tendência: média mensal de ciclos concluídos (triagem → EP Finalizado) — últimos 12 meses
    cur.execute(f"""
        WITH parsed AS (
            SELECT
                {pd('data_de_entrada_triagem')} AS dt_entrada,
                {pd('data_de_entrada_em_ep_finalizado')} AS dt_ep_fim
            FROM pipefy_szi_all_cards_transformada_bd_terreno
            WHERE status = 'Aberto'
              AND etapa = 'EP Finalizado'
              AND data_de_entrada_triagem IS NOT NULL
              AND data_de_entrada_em_ep_finalizado IS NOT NULL
        )
        SELECT
            TO_CHAR(dt_ep_fim, 'YYYY-MM') AS mes,
            COUNT(*) AS qtd,
            ROUND(AVG(dt_ep_fim - dt_entrada)) AS media_dias
        FROM parsed
        WHERE dt_ep_fim >= CURRENT_DATE - INTERVAL '12 months'
          AND dt_ep_fim - dt_entrada BETWEEN 0 AND 365
        GROUP BY mes
        ORDER BY mes
    """)
    tendencia_raw = [dict(r) for r in cur.fetchall()]
    conn.close()

    from datetime import date, datetime
    import re

    FASE_COLS = [
        'data_de_entrada_triagem','data_de_entrada_em_analise_preliminar',
        'data_de_entrada_em_fila_de_em','data_de_entrada_em_em_iniciado',
        'data_de_entrada_em_em_analise_de_roi','data_de_entrada_em_em_finalizado',
        'data_de_entrada_em_fila_de_ep','data_de_entrada_em_ep_iniciado',
        'data_de_entrada_em_ep_revisao_de_engenharia','data_de_entrada_em_ep_orcamento_previo',
        'data_de_entrada_em_revisao_de_roi',
        'data_de_entrada_em_proposta','data_de_entrada_em_primeira_proposta',
        'data_de_entrada_em_negociacao','data_de_entrada_em_aceito_pelo_vendedor',
    ]

    def parse_date(val):
        if not val: return None
        val = str(val).strip()
        try:
            if re.match(r'^\d{2}/\d{2}/\d{4}', val):
                d, m, y = val[:10].split('/')
                return date(int(y), int(m), int(d))
            if re.match(r'^\d{4}-\d{2}-\d{2}', val):
                return datetime.strptime(val[:10], '%Y-%m-%d').date()
        except: pass
        return None

    hoje = date.today()
    ativos = []

    for r in rows:
        datas = sorted([dt for dt in [parse_date(r.get(c)) for c in FASE_COLS] if dt])
        if not datas: continue

        entrada_original = datas[0]
        dias_desde_criacao = (hoje - entrada_original).days

        reopen = parse_date(r.get('perdidopropostalasttimeout'))
        if reopen and dias_desde_criacao > 30:
            data_inicio = reopen
        elif dias_desde_criacao <= 30:
            data_inicio = entrada_original
        else:
            gaps = [(datas[i] - datas[i-1]).days for i in range(1, len(datas))]
            max_gap = max(gaps) if gaps else 0
            if max_gap > 20:
                idx = gaps.index(max_gap)
                data_inicio = datas[idx + 1]
            else:
                data_inicio = entrada_original

        dias_reais = max((hoje - data_inicio).days, 0)
        ativos.append({
            "id": r['ids'],
            "etapa": r['etapa'],
            "localizacao": f"{r['cidade']}, {r['bairro']}".replace(r['cidade'] + ', ' + r['cidade'], r['cidade']),
            "dias": dias_reais,
            "data_inicio": str(data_inicio),
        })

    ativos.sort(key=lambda x: x['dias'], reverse=True)
    media_atual = round(sum(a['dias'] for a in ativos) / len(ativos)) if ativos else 0

    # Calcula variação da tendência (último mês vs penúltimo)
    variacao = None
    if len(tendencia_raw) >= 2:
        ultimo = tendencia_raw[-1]['media_dias'] or 0
        penultimo = tendencia_raw[-2]['media_dias'] or 0
        if penultimo > 0:
            variacao = round(((ultimo - penultimo) / penultimo) * 100, 1)

    return {
        "media_atual": media_atual,
        "total_ativos": len(ativos),
        "variacao_pct": variacao,
        "tendencia": [{"mes": t['mes'], "media_dias": int(t['media_dias'] or 0), "qtd": int(t['qtd'])} for t in tendencia_raw],
        "ofensores": ativos[:5],
    }


def get_etapas_breakdown():
    """
    Retorna terrenos agrupados por etapa com leadtime (dias na etapa atual) e listagem.
    """
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT
            t.ids, t.etapa, t.cidade, t.bairro,
            t.data_de_entrada_triagem,
            t.data_de_entrada_em_analise_preliminar,
            t.data_de_entrada_em_fila_de_em,
            t.data_de_entrada_em_em_iniciado,
            t.data_de_entrada_em_em_analise_de_roi,
            t.data_de_entrada_em_em_finalizado,
            t.data_de_entrada_em_fila_de_ep,
            t.data_de_entrada_em_ep_iniciado,
            t.data_de_entrada_em_ep_revisao_de_engenharia,
            t.data_de_entrada_em_ep_orcamento_previo,
            t.data_de_entrada_em_revisao_de_roi,
            t.data_de_entrada_em_proposta,
            t.data_de_entrada_em_primeira_proposta,
            t.data_de_entrada_em_negociacao,
            t.data_de_entrada_em_aceito_pelo_vendedor
        FROM pipefy_szi_all_cards_transformada_bd_terreno t
        WHERE t.status = 'Aberto'
          AND t.etapa NOT IN ('EP Finalizado', 'Excluídos', 'Excluído')
          AND t.etapa NOT ILIKE '%perdido%'
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    from datetime import datetime
    import re

    FASE_COLS = [
        'data_de_entrada_triagem', 'data_de_entrada_em_analise_preliminar',
        'data_de_entrada_em_fila_de_em', 'data_de_entrada_em_em_iniciado',
        'data_de_entrada_em_em_analise_de_roi', 'data_de_entrada_em_em_finalizado',
        'data_de_entrada_em_fila_de_ep', 'data_de_entrada_em_ep_iniciado',
        'data_de_entrada_em_ep_revisao_de_engenharia', 'data_de_entrada_em_ep_orcamento_previo',
        'data_de_entrada_em_revisao_de_roi',
        'data_de_entrada_em_proposta', 'data_de_entrada_em_primeira_proposta',
        'data_de_entrada_em_negociacao', 'data_de_entrada_em_aceito_pelo_vendedor',
    ]

    def parse_date(val):
        if not val: return None
        val = str(val).strip()
        try:
            if re.match(r'^\d{2}/\d{2}/\d{4}', val):
                d, m, y = val[:10].split('/')
                return date(int(y), int(m), int(d))
            if re.match(r'^\d{4}-\d{2}-\d{2}', val):
                return datetime.strptime(val[:10], '%Y-%m-%d').date()
        except: pass
        return None

    # Mapeamento etapa → coluna de data de entrada
    ETAPA_COL = {
        'triagem': 'data_de_entrada_triagem',
        'análise preliminar': 'data_de_entrada_em_analise_preliminar',
        'analise preliminar': 'data_de_entrada_em_analise_preliminar',
        'fila de em': 'data_de_entrada_em_fila_de_em',
        'em iniciado': 'data_de_entrada_em_em_iniciado',
        'em análise de roi': 'data_de_entrada_em_em_analise_de_roi',
        'em analise de roi': 'data_de_entrada_em_em_analise_de_roi',
        'em finalizado': 'data_de_entrada_em_em_finalizado',
        'fila de ep': 'data_de_entrada_em_fila_de_ep',
        'ep iniciado': 'data_de_entrada_em_ep_iniciado',
        'ep revisão de engenharia': 'data_de_entrada_em_ep_revisao_de_engenharia',
        'ep revisao de engenharia': 'data_de_entrada_em_ep_revisao_de_engenharia',
        'ep orçamento prévio': 'data_de_entrada_em_ep_orcamento_previo',
        'ep orcamento previo': 'data_de_entrada_em_ep_orcamento_previo',
        'revisão de roi': 'data_de_entrada_em_revisao_de_roi',
        'revisao de roi': 'data_de_entrada_em_revisao_de_roi',
        'proposta': 'data_de_entrada_em_proposta',
        'primeira proposta': 'data_de_entrada_em_primeira_proposta',
        'negociação': 'data_de_entrada_em_negociacao',
        'negociacao': 'data_de_entrada_em_negociacao',
        'aceito pelo vendedor': 'data_de_entrada_em_aceito_pelo_vendedor',
    }

    # Meta de dias úteis por etapa (None = sem farol)
    META_UTEIS = {'em iniciado': 1, 'ep iniciado': 2}

    FUNNEL_ORDER = [
        'Triagem', 'Análise Preliminar', 'Fila de EM', 'EM Iniciado',
        'EM Análise de ROI', 'EM Finalizado', 'Fila de EP', 'EP Iniciado',
        'EP Revisão de Engenharia', 'EP Orçamento Prévio', 'Revisão de ROI',
        'Proposta', 'Primeira Proposta', 'Negociação', 'Aceito pelo Vendedor',
    ]

    hoje = date.today()
    etapas = {}

    for r in rows:
        etapa = r['etapa']
        col = ETAPA_COL.get(etapa.lower().strip())

        if col and r.get(col):
            dt = parse_date(r[col])
            dias = (hoje - dt).days if dt else None
        else:
            # Fallback: usa a data mais recente dentre todas as colunas
            datas = sorted([dt for dt in [parse_date(r.get(c)) for c in FASE_COLS] if dt])
            dias = (hoje - datas[-1]).days if datas else None

        cidade = r.get('cidade') or ''
        bairro = r.get('bairro') or ''
        local = f"{cidade}, {bairro}".strip(', ')

        if etapa not in etapas:
            etapas[etapa] = {"etapa": etapa, "count": 0, "total_dias": 0, "terrenos": []}

        etapas[etapa]["count"] += 1
        if dias is not None:
            etapas[etapa]["total_dias"] += dias

        # Farol para EM/EP Iniciado
        meta = META_UTEIS.get(etapa.lower().strip())
        farol_data = _farol(dt, meta, hoje) if (meta and dt) else {}

        terreno_dict = {
            "id": r['ids'],
            "localizacao": local,
            "dias_na_etapa": dias if dias is not None else 0,
        }
        terreno_dict.update(farol_data)
        etapas[etapa]["terrenos"].append(terreno_dict)

    result = []
    seen = set()
    for etapa_name in FUNNEL_ORDER:
        if etapa_name in etapas:
            e = etapas[etapa_name]
            avg = round(e["total_dias"] / e["count"]) if e["count"] > 0 else 0
            terrenos = sorted(e["terrenos"], key=lambda x: x["dias_na_etapa"], reverse=True)
            result.append({"etapa": etapa_name, "count": e["count"], "avg_dias": avg, "terrenos": terrenos})
            seen.add(etapa_name)

    # Etapas que não estão no FUNNEL_ORDER (nomes desconhecidos)
    for etapa_name, e in etapas.items():
        if etapa_name not in seen:
            avg = round(e["total_dias"] / e["count"]) if e["count"] > 0 else 0
            terrenos = sorted(e["terrenos"], key=lambda x: x["dias_na_etapa"], reverse=True)
            result.append({"etapa": etapa_name, "count": e["count"], "avg_dias": avg, "terrenos": terrenos})

    return result


def get_proposta_relatorio():
    """
    Terrenos em 'EM Relatório de Proposta' com tags, dias na etapa e links.
    """
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT
            t.ids, t.cidade, t.bairro,
            t.link_da_pasta_do_terreno,
            t.data_de_entrada_em_relatorio_de_proposta,
            e.labels,
            e.url          AS pipefy_url,
            e.link_planilha_geral
        FROM pipefy_szi_all_cards_transformada_bd_terreno t
        LEFT JOIN pipefy_szi_all_cards_304548446_colunas_expandidas e
            ON e.title = t.ids
        WHERE t.etapa = 'EM Relatório de Proposta'
          AND t.status = 'Aberto'
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    from datetime import date, datetime
    import re

    def parse_date(val):
        if not val: return None
        val = str(val).strip()
        try:
            if re.match(r'^\d{2}/\d{2}/\d{4}', val):
                d, m, y = val[:10].split('/')
                return date(int(y), int(m), int(d))
            if re.match(r'^\d{4}-\d{2}-\d{2}', val):
                return datetime.strptime(val[:10], '%Y-%m-%d').date()
        except: pass
        return None

    hoje = date.today()
    terrenos = []
    validar_count = 0

    for r in rows:
        # Labels já vêm como lista de dicts pelo psycopg2
        labels_raw = r.get('labels') or []
        label_names = [l.get('name', '') for l in labels_raw if isinstance(l, dict)]
        has_validar = any('validar em reuni' in n.lower() for n in label_names)

        if has_validar:
            validar_count += 1

        dt = parse_date(r.get('data_de_entrada_em_relatorio_de_proposta'))
        dias = (hoje - dt).days if dt else 0

        cidade = r.get('cidade') or ''
        bairro = r.get('bairro') or ''
        local = f"{cidade}, {bairro}".strip(', ')

        link_pasta = r.get('link_da_pasta_do_terreno') or None

        link_estudo   = None
        link_planilha = r.get('link_planilha_geral') or None

        if link_pasta:
            try:
                from drive_helper import find_study_pdf, find_planilha_geral, planilha_com_aba
                # PDF do estudo de massa
                link_estudo = find_study_pdf(link_pasta)
                # Planilha geral: banco primeiro, Drive como fallback
                if not link_planilha:
                    link_planilha = find_planilha_geral(link_pasta)
                # Aponta para a aba correta (EM ou AP) dentro da planilha
                if link_planilha:
                    link_planilha = planilha_com_aba(link_planilha, tem_estudo=bool(link_estudo))
            except Exception as ex:
                print(f"[DB] Erro Drive para terreno {r.get('ids')}: {ex}")

        terrenos.append({
            "id": r['ids'],
            "localizacao": local,
            "dias_na_etapa": dias,
            "tags": label_names,
            "has_validar": has_validar,
            "link_pasta": link_pasta,
            "link_planilha": link_planilha,
            "link_estudo": link_estudo,
        })

    terrenos.sort(key=lambda x: x['dias_na_etapa'], reverse=True)

    return {
        "total": len(terrenos),
        "validar_reuniao": validar_count,
        "sem_tag": len(terrenos) - validar_count,
        "terrenos": terrenos,
    }


def get_final_funil():
    """
    Terrenos nas etapas finais do funil: EP Orçamento Prévio, EP Revisão de ROI.
    """
    ETAPAS = [
        ('EP Orçamento Prévio', 'data_de_entrada_em_ep_orcamento_previo', 'ep_orc'),
        ('EP Revisão de ROI',   'data_de_entrada_em_revisao_de_roi',      'ep_roi'),
    ]
    etapa_nomes = [e[0] for e in ETAPAS]

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""
        SELECT
            t.ids, t.etapa, t.cidade, t.bairro,
            t.link_da_pasta_do_terreno,
            t.data_de_entrada_em_ep_orcamento_previo,
            t.data_de_entrada_em_revisao_de_roi,
            e.link_planilha_geral
        FROM pipefy_szi_all_cards_transformada_bd_terreno t
        LEFT JOIN pipefy_szi_all_cards_304548446_colunas_expandidas e
            ON e.title = t.ids
        WHERE t.status = 'Aberto'
          AND t.etapa = ANY(%s)
    """, (etapa_nomes,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    from datetime import datetime
    import re

    def parse_date(val):
        if not val: return None
        val = str(val).strip()
        try:
            if re.match(r'^\d{2}/\d{2}/\d{4}', val):
                d, m, y = val[:10].split('/')
                return date(int(y), int(m), int(d))
            if re.match(r'^\d{4}-\d{2}-\d{2}', val):
                return datetime.strptime(val[:10], '%Y-%m-%d').date()
        except: pass
        return None

    hoje = date.today()
    grupos = {e[0]: {"etapa": e[0], "key": e[2], "terrenos": []} for e in ETAPAS}

    for r in rows:
        etapa = r['etapa']
        col   = next((e[1] for e in ETAPAS if e[0] == etapa), None)
        dt    = parse_date(r.get(col)) if col else None
        dias  = (hoje - dt).days if dt else 0

        cidade = r.get('cidade') or ''
        bairro = r.get('bairro') or ''
        local  = f"{cidade}, {bairro}".strip(', ')
        link_pasta = r.get('link_da_pasta_do_terreno') or None

        link_estudo   = None
        link_planilha = r.get('link_planilha_geral') or None

        link_projeto     = None
        link_quadro_areas = None

        if link_pasta:
            try:
                from drive_helper import find_ep_estudos, find_planilha_geral, planilha_com_aba
                ep = find_ep_estudos(link_pasta)
                link_projeto      = ep["projeto"]
                link_quadro_areas = ep["quadro_areas"]
                if not link_planilha:
                    link_planilha = find_planilha_geral(link_pasta)
                if link_planilha:
                    link_planilha = planilha_com_aba(link_planilha, tem_estudo=True)
            except Exception as ex:
                print(f"[DB] Erro Drive para Final Funil {r.get('ids')}: {ex}")

        if etapa in grupos:
            grupos[etapa]["terrenos"].append({
                "id": r['ids'],
                "localizacao": local,
                "dias_na_etapa": dias,
                "link_pasta": link_pasta,
                "link_planilha": link_planilha,
                "link_projeto": link_projeto,
                "link_quadro_areas": link_quadro_areas,
            })

    # Ordena cada grupo por dias desc
    result = []
    for etapa, key, _ in ETAPAS:
        g = grupos[etapa]
        g["terrenos"].sort(key=lambda x: x["dias_na_etapa"], reverse=True)
        g["count"] = len(g["terrenos"])
        if g["count"] > 0:
            result.append(g)

    total = sum(g["count"] for g in result)
    return {"total": total, "grupos": result}


def get_pre_proposta():
    """
    Terrenos em 'Pré Proposta' com dias na etapa, links e histórico para tendência.
    """
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT
            t.ids, t.cidade, t.bairro,
            t.link_da_pasta_do_terreno,
            t.data_de_entrada_em_pre_proposta,
            e.link_planilha_geral
        FROM pipefy_szi_all_cards_transformada_bd_terreno t
        LEFT JOIN pipefy_szi_all_cards_304548446_colunas_expandidas e
            ON e.title = t.ids
        WHERE t.etapa = 'Pré Proposta'
          AND t.status = 'Aberto'
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    from datetime import datetime
    import re

    def parse_date(val):
        if not val: return None
        val = str(val).strip()
        try:
            if re.match(r'^\d{2}/\d{2}/\d{4}', val):
                d, m, y = val[:10].split('/')
                return date(int(y), int(m), int(d))
            if re.match(r'^\d{4}-\d{2}-\d{2}', val):
                return datetime.strptime(val[:10], '%Y-%m-%d').date()
        except: pass
        return None

    META_DIAS_UTEIS = 2
    hoje = date.today()
    terrenos = []

    for r in rows:
        dt = parse_date(r.get('data_de_entrada_em_pre_proposta'))
        dias_corridos = (hoje - dt).days if dt else 0
        f = _farol(dt, META_DIAS_UTEIS, hoje)

        cidade = r.get('cidade') or ''
        bairro = r.get('bairro') or ''
        local = f"{cidade}, {bairro}".strip(', ')
        link_pasta = r.get('link_da_pasta_do_terreno') or None

        link_estudo   = None
        link_planilha = r.get('link_planilha_geral') or None

        if link_pasta:
            try:
                from drive_helper import find_study_pdf, find_planilha_geral, planilha_com_aba
                link_estudo = find_study_pdf(link_pasta)
                if not link_planilha:
                    link_planilha = find_planilha_geral(link_pasta)
                if link_planilha:
                    link_planilha = planilha_com_aba(link_planilha, tem_estudo=bool(link_estudo))
            except Exception as ex:
                print(f"[DB] Erro Drive para Pré Proposta {r.get('ids')}: {ex}")

        terrenos.append({
            "id": r['ids'],
            "localizacao": local,
            "dias_na_etapa": dias_corridos,
            "dias_uteis": f["dias_uteis"],
            "prazo": f["prazo"],
            "dias_atraso": f["dias_atraso"],
            "farol": f["farol"],
            "link_pasta": link_pasta,
            "link_planilha": link_planilha,
            "link_estudo": link_estudo,
        })

    terrenos.sort(key=lambda x: (x['dias_uteis'] or 0), reverse=True)
    avg_uteis = round(sum((t['dias_uteis'] or 0) for t in terrenos) / len(terrenos), 1) if terrenos else 0

    return {
        "total": len(terrenos),
        "avg_uteis": avg_uteis,
        "terrenos": terrenos,
    }


def get_leadtime():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""
        WITH parsed AS (
            SELECT
                {pd('data_de_entrada_triagem')} AS dt_triagem,
                {pd('data_de_entrada_em_analise_preliminar')} AS dt_ap,
                {pd('data_de_entrada_em_fila_de_em')} AS dt_fila_em,
                {pd('data_de_entrada_em_em_iniciado')} AS dt_em_inicio,
                {pd('data_de_entrada_em_em_finalizado')} AS dt_em_fim,
                {pd('data_de_entrada_em_fila_de_ep')} AS dt_fila_ep,
                {pd('data_de_entrada_em_ep_iniciado')} AS dt_ep_inicio,
                {pd('data_de_entrada_em_ep_finalizado')} AS dt_ep_fim,
                {pd('data_de_entrada_em_proposta')} AS dt_proposta,
                {pd('data_de_entrada_em_negociacao')} AS dt_negociacao,
                {pd('data_de_entrada_em_aceito_pelo_vendedor')} AS dt_aceito
            FROM bd_terrenos_nekt_tratado
        )
        SELECT
            ROUND(AVG(dt_ap - dt_triagem))           AS triagem_ap,
            ROUND(AVG(dt_fila_em - dt_ap))           AS ap_fila_em,
            ROUND(AVG(dt_em_inicio - dt_fila_em))    AS fila_em_em,
            ROUND(AVG(dt_fila_ep - dt_em_fim))       AS em_fila_ep,
            ROUND(AVG(dt_ep_inicio - dt_fila_ep))    AS fila_ep_ep,
            ROUND(AVG(dt_proposta - dt_ep_fim))      AS ep_proposta,
            ROUND(AVG(dt_negociacao - dt_proposta))  AS proposta_negociacao,
            ROUND(AVG(dt_aceito - dt_negociacao))    AS negociacao_aceito
        FROM parsed
    """)
    row = dict(cur.fetchone())
    conn.close()
    fases = [
        ("Triagem → AP",          row["triagem_ap"]),
        ("AP → Fila EM",          row["ap_fila_em"]),
        ("Fila EM → EM",          row["fila_em_em"]),
        ("EM → Fila EP",          row["em_fila_ep"]),
        ("Fila EP → EP",          row["fila_ep_ep"]),
        ("EP → Proposta",         row["ep_proposta"]),
        ("Proposta → Negociação", row["proposta_negociacao"]),
        ("Negociação → Aceito",   row["negociacao_aceito"]),
    ]
    return [{"fase": f, "dias": int(d) if d else 0} for f, d in fases]


def get_conversao():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE data_de_entrada_triagem IS NOT NULL)                   AS triagem,
            COUNT(*) FILTER (WHERE data_de_entrada_em_analise_preliminar IS NOT NULL)     AS ap,
            COUNT(*) FILTER (WHERE data_de_entrada_em_fila_de_em IS NOT NULL)             AS fila_em,
            COUNT(*) FILTER (WHERE data_de_entrada_em_em_iniciado IS NOT NULL)            AS em,
            COUNT(*) FILTER (WHERE data_de_entrada_em_fila_de_ep IS NOT NULL)             AS fila_ep,
            COUNT(*) FILTER (WHERE data_de_entrada_em_ep_iniciado IS NOT NULL)            AS ep,
            COUNT(*) FILTER (WHERE data_de_entrada_em_proposta IS NOT NULL)               AS proposta,
            COUNT(*) FILTER (WHERE data_de_entrada_em_negociacao IS NOT NULL)             AS negociacao,
            COUNT(*) FILTER (WHERE data_de_entrada_em_aceito_pelo_vendedor IS NOT NULL)   AS aceito
        FROM bd_terrenos_nekt_tratado
    """)
    row = dict(cur.fetchone())
    conn.close()
    fases = ["triagem", "ap", "fila_em", "em", "fila_ep", "ep", "proposta", "negociacao", "aceito"]
    labels = ["Triagem", "AP", "Fila EM", "EM", "Fila EP", "EP", "Proposta", "Negociação", "Aceito"]
    result = []
    prev = None
    for key, label in zip(fases, labels):
        val = row[key]
        pct = round(val / prev * 100, 1) if prev and prev > 0 else 100.0
        result.append({"fase": label, "total": val, "conversao_pct": pct})
        prev = val
    return result


def get_ativos():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT
            ids,
            etapa,
            cidade,
            estado,
            bairro,
            score_do_terreno,
            temperatura_da_negociacao,
            responsavel_arquitetura_em,
            responsavel_arquitetura_ep,
            atualizado_em,
            data_de_entrada_triagem,
            data_de_entrada_em_analise_preliminar,
            data_de_entrada_em_fila_de_em,
            data_de_entrada_em_em_iniciado,
            data_de_entrada_em_fila_de_ep,
            data_de_entrada_em_ep_iniciado,
            data_de_entrada_em_proposta,
            data_de_entrada_em_negociacao,
            vgv_roi_em,
            vgv_roi_ep,
            _roi_em,
            _roi_ep
        FROM bd_terrenos_nekt_tratado
        WHERE status = 'Aberto'
        ORDER BY atualizado_em DESC NULLS LAST
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_kpis():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'Aberto')  AS ativos,
            COUNT(*) FILTER (WHERE status = 'Ganho')   AS ganhos,
            COUNT(*) FILTER (WHERE status = 'Perdido') AS perdidos,
            COUNT(*) FILTER (WHERE status = 'Aberto' AND etapa = 'Análise Preliminar') AS em_ap,
            COUNT(*) FILTER (WHERE status = 'Aberto' AND etapa ILIKE '%EM%')           AS em_em,
            COUNT(*) FILTER (WHERE status = 'Aberto' AND etapa ILIKE '%EP%')           AS em_ep,
            COUNT(*) FILTER (WHERE status = 'Aberto' AND etapa ILIKE '%Proposta%')     AS em_proposta,
            COUNT(*) FILTER (WHERE status = 'Aberto' AND etapa ILIKE '%Negociação%')   AS em_negociacao
        FROM bd_terrenos_nekt_tratado
    """)
    row = dict(cur.fetchone())
    conn.close()
    return row
