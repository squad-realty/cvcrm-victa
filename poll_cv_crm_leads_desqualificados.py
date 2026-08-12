"""
Polling CV CRM -> Meta Conversions API
Cliente: Victa Engenharia - Vitoria Eusebio (idempreendimento 43)

O QUE ESTE SCRIPT FAZ
---------------------
1. Busca os leads na situacao "Cancelado/Descartado" (id fixo, configurado
   via secret) desde a ultima execucao (controle via state.json).
2. Filtra apenas os leads do empreendimento Vitoria Eusebio (idempreendimento
   43) -- o CV CRM da Victa e uma conta UNICA compartilhada entre todos os
   empreendimentos, entao sem esse filtro leads de outras obras (Vista
   Coqueiral, etc.) vazariam para este pixel.
3. Dentro desses, filtra pelos motivos de cancelamento-alvo combinados com
   o cliente.
4. Envia o evento "LeadDesqualificado" via Meta CAPI para o dataset
   conversoes-eusebio.
5. Atualiza o state.json com o timestamp mais recente processado.

CREDENCIAIS (variaveis de ambiente / secrets do GitHub Actions):
    CV_CRM_SUBDOMINIO             -> "victa"
    CV_CRM_EMAIL
    CV_CRM_TOKEN
    CV_CRM_IDSITUACAO_CANCELADO   -> "3" (mesma conta CV CRM que o Coqueiral)
    META_CAPI_ACCESS_TOKEN        -> token do dataset conversoes-eusebio

PONTOS PRA VALIDAR:
- leadgen_id/ctwa_clid: nao encontrados em campos_adicionais neste lead de
  exemplo (so existiam cf_campanha, TAG_LEAD, Tipo_de_Oportunidade). O
  script tenta mesmo assim por alguns nomes de slug candidatos, mas cai
  para o fallback de email/telefone/nome/external_id na pratica.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import requests

from meta_lead_desqualificado_capi import enviar_lead_desqualificado

# ---------------------------------------------------------------------------
# Configuracao
# ---------------------------------------------------------------------------

CV_CRM_SUBDOMINIO = os.environ["CV_CRM_SUBDOMINIO"]
CV_CRM_EMAIL = os.environ["CV_CRM_EMAIL"]
CV_CRM_TOKEN = os.environ["CV_CRM_TOKEN"]
CV_CRM_IDSITUACAO_CANCELADO = int(os.environ["CV_CRM_IDSITUACAO_CANCELADO"])

CV_CRM_BASE_URL = f"https://{CV_CRM_SUBDOMINIO}.cvcrm.com.br/api"
HEADERS = {"email": CV_CRM_EMAIL, "token": CV_CRM_TOKEN}

STATE_FILE = Path(__file__).parent / "state.json"

# Empreendimento Vitoria Eusebio, confirmado via campo "empreendimentosId"
# no retorno real da API (lead 149468).
CV_CRM_ID_EMPREENDIMENTO = "43"

MOTIVOS_DESQUALIFICACAO_ALVO = {
    "Impossível contatar",
    "Não deseja ser contatada",
    "Não tem perfil Financeiro",
    "Engano",
}

SLUGS_LEADGEN_ID_CANDIDATOS = ["leadgen_id", "cf_leadgen_id", "meta_leadgen_id"]
SLUGS_CTWA_CLID_CANDIDATOS = ["ctwa_clid", "cf_ctwa_clid", "meta_ctwa_clid"]


# ---------------------------------------------------------------------------
# Estado (evita reprocessar os mesmos leads a cada execucao)
# ---------------------------------------------------------------------------

def carregar_estado() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    # Sem state.json ainda: comeca 90 dias atras (eventos mais antigos nao
    # tem uso pratico para a Custom Audience de 180 dias do Meta).
    data_inicial = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    return {"ultima_data_cancelamento_processada": data_inicial}


def salvar_estado(estado: dict) -> None:
    STATE_FILE.write_text(json.dumps(estado, indent=2))


# ---------------------------------------------------------------------------
# Busca de leads cancelados (com retry e teto de seguranca)
# ---------------------------------------------------------------------------

def _get_com_retry(url: str, params: dict, tentativas: int = 3) -> dict:
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        timeout = 30 * tentativa  # 30s, 60s, 90s
        try:
            response = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as erro:
            ultimo_erro = erro
            if tentativa < tentativas:
                print(f"Tentativa {tentativa}/{tentativas} falhou ({erro.__class__.__name__}). Tentando de novo...")
            else:
                print(f"Tentativa {tentativa}/{tentativas} falhou. Desistindo.")
    raise ultimo_erro


def buscar_leads_cancelados(idsituacao_cancelado: int, limit: int = 20, max_paginas: int = 50) -> list:
    leads = []
    offset = 0

    for _ in range(max_paginas):
        params = {
            "idsituacao": idsituacao_cancelado,
            "limit": limit,
            "offset": offset,
        }
        url = f"{CV_CRM_BASE_URL}/v1/comercial/leads"
        data = _get_com_retry(url, params)

        pagina_leads = data.get("leads", [])
        leads.extend(pagina_leads)

        if len(pagina_leads) < limit:
            break
        offset += limit
    else:
        print(f"Atingiu o teto de {max_paginas} paginas -- parando por seguranca.")

    return leads


# ---------------------------------------------------------------------------
# Mapeamento CV CRM -> payload esperado pelo enviar_lead_desqualificado
# ---------------------------------------------------------------------------

def extrair_campo_adicional(campos_adicionais: list, slugs_candidatos: list):
    for campo in campos_adicionais or []:
        if campo.get("slug") in slugs_candidatos:
            return campo.get("valor")
    return None


def identificar_origem(lead: dict) -> str:
    midia = (lead.get("midia_principal") or "").lower()
    midias = [m.lower() for m in (lead.get("midias") or [])]
    todas_midias = [midia] + midias

    if any("whatsapp" in m for m in todas_midias):
        return "whatsapp"
    if any("lead ad" in m or "facebook" in m or "instagram" in m for m in todas_midias):
        return "lead_ads"
    return "form_lp"


def eh_do_empreendimento_alvo(lead_cv: dict) -> bool:
    """
    Filtra para o Vitoria Eusebio usando o campo flat "empreendimentosId"
    (confirmado no retorno real da API), com fallback para o array
    "empreendimento" caso o flat nao venha preenchido.
    """
    empreendimentos_id = lead_cv.get("empreendimentosId")
    if empreendimentos_id is not None:
        return str(empreendimentos_id) == CV_CRM_ID_EMPREENDIMENTO

    for emp in lead_cv.get("empreendimento") or []:
        if str(emp.get("id")) == CV_CRM_ID_EMPREENDIMENTO:
            return True
    return False


def mapear_lead(lead_cv: dict) -> dict:
    campos_adicionais = lead_cv.get("campos_adicionais", [])

    return {
        "origem": identificar_origem(lead_cv),
        "leadgen_id": extrair_campo_adicional(campos_adicionais, SLUGS_LEADGEN_ID_CANDIDATOS),
        "ctwa_clid": extrair_campo_adicional(campos_adicionais, SLUGS_CTWA_CLID_CANDIDATOS),
        "email": lead_cv.get("email"),
        "telefone": lead_cv.get("telefone"),
        "idlead": lead_cv.get("idlead"),
        "nome": lead_cv.get("nome"),
        "motivo_cancelamento": (lead_cv.get("motivo_cancelamento") or {}).get("nome"),
    }


# ---------------------------------------------------------------------------
# Execucao principal
# ---------------------------------------------------------------------------

def main() -> None:
    estado = carregar_estado()
    ultima_data_processada = estado["ultima_data_cancelamento_processada"]

    leads = buscar_leads_cancelados(CV_CRM_IDSITUACAO_CANCELADO)

    maior_data_cancelamento = ultima_data_processada
    enviados = 0
    ignorados_motivo = 0
    ignorados_empreendimento = 0
    falhas = 0

    for lead_cv in leads:
        data_cancelamento = lead_cv.get("data_cancelamento", "")

        if data_cancelamento <= ultima_data_processada:
            continue

        if not eh_do_empreendimento_alvo(lead_cv):
            ignorados_empreendimento += 1
            continue

        motivo = (lead_cv.get("motivo_cancelamento") or {}).get("nome")
        if motivo not in MOTIVOS_DESQUALIFICACAO_ALVO:
            ignorados_motivo += 1
            continue

        lead_mapeado = mapear_lead(lead_cv)

        try:
            resultado = enviar_lead_desqualificado(lead_mapeado)
            print(f"Lead {lead_cv.get('idlead')} -> {resultado}")
            enviados += 1
        except Exception as erro:
            print(f"Lead {lead_cv.get('idlead')} FALHOU -> {erro.__class__.__name__}: {erro}")
            falhas += 1

        if data_cancelamento > maior_data_cancelamento:
            maior_data_cancelamento = data_cancelamento

    estado["ultima_data_cancelamento_processada"] = maior_data_cancelamento
    salvar_estado(estado)

    print(
        f"Concluido. Enviados: {enviados}. "
        f"Ignorados (outro empreendimento): {ignorados_empreendimento}. "
        f"Ignorados (motivo fora do alvo): {ignorados_motivo}. "
        f"Falhas: {falhas}."
    )


if __name__ == "__main__":
    main()
