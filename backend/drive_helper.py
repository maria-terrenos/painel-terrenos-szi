"""
Google Drive helper — encontra o PDF de estudo dentro da pasta do terreno.

Usa Service Account (JSON key) — funciona para pastas compartilhadas com
o e-mail da service account.

Configuração (dois modos):
  Modo arquivo (local):
    1. Salve o JSON da service account como service_account.json na raiz do projeto
    2. Compartilhe as pastas dos terrenos com o e-mail da service account

  Modo env var (produção / Coolify):
    1. Set GOOGLE_SA_JSON com o conteúdo completo do JSON (string)
    2. Compartilhe as pastas dos terrenos com o e-mail da service account
"""

import os
import re
import json
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / '.env')

SA_PATH     = os.getenv("GOOGLE_SA_PATH", str(Path(__file__).parent.parent / "service_account.json"))
DRIVE_API   = "https://www.googleapis.com/drive/v3/files"
SHEETS_API  = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES      = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]


def _get_access_token() -> str | None:
    """
    Obtém access token via service account.
    Prioridade:
      1. GOOGLE_SA_JSON (env var com conteúdo JSON) — usado em produção/Coolify
      2. Arquivo service_account.json no disco — usado em desenvolvimento local
    """
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request

        sa_json_str = os.getenv("GOOGLE_SA_JSON")
        if sa_json_str:
            # Suporta tanto JSON puro quanto base64 (necessário no Coolify)
            try:
                import base64
                decoded = base64.b64decode(sa_json_str).decode("utf-8")
                sa_info = json.loads(decoded)
            except Exception:
                sa_info = json.loads(sa_json_str)
            creds = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
        elif Path(SA_PATH).exists():
            # Modo local: carrega do arquivo
            creds = service_account.Credentials.from_service_account_file(SA_PATH, scopes=SCOPES)
        else:
            print("[Drive] Nenhuma credencial encontrada (GOOGLE_SA_JSON ou service_account.json)")
            return None

        creds.refresh(Request())
        return creds.token
    except Exception as e:
        print(f"[Drive] Erro ao obter token: {e}")
        return None


def extract_folder_id(drive_url: str) -> str | None:
    """Extrai o ID da pasta a partir de uma URL do Google Drive."""
    if not drive_url:
        return None
    m = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_url)
    return m.group(1) if m else None


def _list_files(parent_id: str, token: str, mime_filter: str | None = None, name_contains: str | None = None) -> list[dict]:
    """Consulta a Drive API e retorna lista de arquivos/pastas filhos."""
    q_parts = [f"'{parent_id}' in parents", "trashed = false"]
    if mime_filter:
        q_parts.append(f"mimeType = '{mime_filter}'")
    if name_contains:
        q_parts.append(f"name contains '{name_contains}'")

    params = {
        "q": " and ".join(q_parts),
        "fields": "files(id, name, webViewLink)",
        "pageSize": 10,
    }
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(DRIVE_API, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("files", [])
    except Exception as e:
        print(f"[Drive] Erro na API: {e}")
        return []


def find_planilha_geral(folder_url: str) -> str | None:
    """
    Dado o link da pasta do terreno, encontra a Planilha Geral na pasta raiz.
    Ignora arquivos com 'OLD' no nome. Retorna webViewLink ou None.
    """
    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        return None

    token = _get_access_token()
    if not token:
        return None

    try:
        sheets = _list_files(
            folder_id, token,
            mime_filter="application/vnd.google-apps.spreadsheet",
        )
        # Filtra OLD e retorna o primeiro válido
        valid = [s for s in sheets if 'old' not in s.get('name', '').lower()]
        return valid[0]["webViewLink"] if valid else None

    except Exception as e:
        print(f"[Drive] Erro ao buscar planilha: {e}")
        return None


def _extract_spreadsheet_id(url: str) -> str | None:
    """Extrai o ID do spreadsheet de uma URL do Google Sheets."""
    m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', url)
    return m.group(1) if m else None


def planilha_com_aba(planilha_url: str, tem_estudo: bool) -> str:
    """
    Retorna a URL da planilha apontando diretamente para a aba correta:
      - tem_estudo=True  → aba '[SZN-EM] Potencial Construtivo'
      - tem_estudo=False → aba que contém '[SZN-AP] CPC' no nome

    Se não encontrar a aba, retorna a URL original sem #gid.
    """
    if not planilha_url:
        return planilha_url

    spreadsheet_id = _extract_spreadsheet_id(planilha_url)
    if not spreadsheet_id:
        return planilha_url

    token = _get_access_token()
    if not token:
        return planilha_url

    try:
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(
            f"{SHEETS_API}/{spreadsheet_id}",
            params={"fields": "sheets.properties(sheetId,title)"},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        sheets = resp.json().get("sheets", [])

        target_gid = None
        if tem_estudo:
            # Aba exata: [SZN-EM] Potencial Construtivo
            for s in sheets:
                if s["properties"]["title"].strip() == "[SZN-EM] Potencial Construtivo":
                    target_gid = s["properties"]["sheetId"]
                    break
        else:
            # Aba que contém [SZN-AP] CPC no nome
            for s in sheets:
                if "[SZN-AP] CPC" in s["properties"]["title"]:
                    target_gid = s["properties"]["sheetId"]
                    break

        if target_gid is not None:
            # Monta URL limpa + âncora de aba
            base = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={target_gid}"
            return base

        return planilha_url  # fallback: URL original

    except Exception as e:
        print(f"[Sheets] Erro ao buscar aba: {e}")
        return planilha_url


def find_study_pdf(folder_url: str) -> str | None:
    """
    Dado o link da pasta do terreno, encontra o PDF dentro de '01.Estudo de Massa'.
    Retorna a URL de visualização do PDF ou None se não encontrado.
    """
    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        return None

    token = _get_access_token()
    if not token:
        return None

    try:
        # 1. Busca a subpasta "01.Estudo de Massa"
        subfolders = _list_files(
            folder_id, token,
            mime_filter="application/vnd.google-apps.folder",
            name_contains="Estudo de Massa",
        )
        if not subfolders:
            return None

        subfolder_id = subfolders[0]["id"]

        # 2. Busca o PDF dentro da subpasta
        pdfs = _list_files(subfolder_id, token, mime_filter="application/pdf")
        return pdfs[0]["webViewLink"] if pdfs else None

    except Exception as e:
        print(f"[Drive] Erro ao buscar PDF: {e}")
        return None


def find_ep_estudos(folder_url: str) -> dict:
    """
    Dado o link da pasta do terreno, busca os dois PDFs dentro de '03.Estudo Preliminar':
      - 'projeto':      PDF cujo nome contém 'FOLHAS' (case-insensitive)
      - 'quadro_areas': o outro PDF (sem 'FOLHAS' no nome)

    Retorna {"projeto": url|None, "quadro_areas": url|None}
    """
    resultado = {"projeto": None, "quadro_areas": None}

    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        return resultado

    token = _get_access_token()
    if not token:
        return resultado

    try:
        # 1. Busca a subpasta "03.Estudo Preliminar"
        subfolders = _list_files(
            folder_id, token,
            mime_filter="application/vnd.google-apps.folder",
            name_contains="Estudo Preliminar",
        )
        if not subfolders:
            return resultado

        subfolder_id = subfolders[0]["id"]

        # 2. Lista todos os PDFs da subpasta
        pdfs = _list_files(subfolder_id, token, mime_filter="application/pdf")

        for pdf in pdfs:
            nome = pdf.get("name", "")
            if "folhas" in nome.lower():
                resultado["projeto"] = pdf["webViewLink"]
            else:
                resultado["quadro_areas"] = pdf["webViewLink"]

        return resultado

    except Exception as e:
        print(f"[Drive] Erro ao buscar EP estudos: {e}")
        return resultado
