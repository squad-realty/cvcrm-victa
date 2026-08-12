"""
Evento LeadDesqualificado -> Meta Conversions API (CAPI)
Cliente: Victa Engenharia - Vitoria Eusebio
Dataset (pixel): conversoes-eusebio (ID 1431660931167100)

CONTEXTO
--------
Este modulo deve ser chamado a partir do polling do CV CRM, sempre que um
lead do empreendimento Vitoria Eusebio (idempreendimento 43) mudar de
status para desqualificado E o campo "Motivo de Cancelamento" for um dos
motivos-alvo abaixo.

Motivos que disparam o evento (definido com o cliente):
    - Impossivel contatar
    - Nao deseja ser contatada
    - Nao tem perfil Financeiro
    - Engano

Origem do lead determina qual match key usar (em ordem de prioridade):
    1. Lead Ads (Meta)      -> lead_id (leadgen_id) em user_data.lead_id
    2. WhatsApp (CTWA)      -> ctwa_clid em user_data.ctwa_clid
    3. Sempre                -> email, telefone, nome e external_id (idlead)
       como reforco/fallback, mesmo quando lead_id/ctwa_clid existem.

Nao envia fbc/fbp: nao ha script de captura client-side implementado (LP e
canal secundario nesta conta).
"""

import hashlib
import os
import time
import uuid
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Configuracao
# ---------------------------------------------------------------------------

PIXEL_ID = "1431660931167100"  # conversoes-eusebio
GRAPH_API_VERSION = "v20.0"
ACCESS_TOKEN = os.environ["META_CAPI_ACCESS_TOKEN"]  # nunca hardcode o token
if not ACCESS_TOKEN.strip():
    raise RuntimeError(
        "META_CAPI_ACCESS_TOKEN esta vazio. Confira se a secret foi "
        "cadastrada com um valor (nao so o nome) em Settings > Secrets "
        "and variables > Actions."
    )

CAPI_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PIXEL_ID}/events"

# Motivos de cancelamento do CV CRM que devem gerar o evento negativo.
MOTIVOS_DESQUALIFICACAO_ALVO = {
    "Impossível contatar",
    "Não deseja ser contatada",
    "Não tem perfil Financeiro",
    "Engano",
}


# ---------------------------------------------------------------------------
# Helpers de hashing (padrao Meta: sha256 de string normalizada)
# ---------------------------------------------------------------------------

def _normalize(value: str) -> str:
    return value.strip().lower()


def _sha256(value: str) -> str:
    return hashlib.sha256(_normalize(value).encode("utf-8")).hexdigest()


def hash_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    return _sha256(email)


def hash_phone(phone: Optional[str]) -> Optional[str]:
    """Remove qualquer mascara (+, espacos, parenteses, traco) antes do hash."""
    if not phone:
        return None
    digits_only = "".join(ch for ch in phone if ch.isdigit())
    return _sha256(digits_only)


def hash_name(nome_completo: Optional[str]) -> tuple:
    """Divide o nome completo em primeiro nome / sobrenome e hasheia cada um."""
    if not nome_completo or not nome_completo.strip():
        return None, None

    partes = nome_completo.strip().split(maxsplit=1)
    primeiro_nome = partes[0]
    sobrenome = partes[1] if len(partes) > 1 else None

    fn_hash = _sha256(primeiro_nome)
    ln_hash = _sha256(sobrenome) if sobrenome else None
    return fn_hash, ln_hash


# ---------------------------------------------------------------------------
# Construcao do payload
# ---------------------------------------------------------------------------

def build_user_data(lead: dict) -> dict:
    """
    Espera um dict `lead` vindo do polling do CV CRM, com (pelo menos um
    destes preenchido, conforme a origem):
        lead["origem"]        -> "lead_ads" | "whatsapp" | "form_lp"
        lead["leadgen_id"]    -> string, se origem == lead_ads
        lead["ctwa_clid"]     -> string, se origem == whatsapp
        lead["email"]         -> string
        lead["telefone"]      -> string
        lead["idlead"]        -> id interno do lead no CV CRM (external_id)
        lead["nome"]          -> nome completo do lead
    """
    user_data = {}

    origem = lead.get("origem")

    if origem == "lead_ads" and lead.get("leadgen_id"):
        user_data["lead_id"] = lead["leadgen_id"]

    elif origem == "whatsapp" and lead.get("ctwa_clid"):
        user_data["ctwa_clid"] = lead["ctwa_clid"]

    email_hash = hash_email(lead.get("email"))
    phone_hash = hash_phone(lead.get("telefone"))
    if email_hash:
        user_data["em"] = [email_hash]
    if phone_hash:
        user_data["ph"] = [phone_hash]

    if lead.get("idlead"):
        user_data["external_id"] = _sha256(str(lead["idlead"]))

    fn_hash, ln_hash = hash_name(lead.get("nome"))
    if fn_hash:
        user_data["fn"] = [fn_hash]
    if ln_hash:
        user_data["ln"] = [ln_hash]

    return user_data


def build_event_payload(lead: dict) -> dict:
    user_data = build_user_data(lead)

    action_source = "business_messaging" if lead.get("origem") == "whatsapp" else "system_generated"

    event = {
        "event_name": "LeadDesqualificado",
        "event_time": int(time.time()),
        "event_id": str(uuid.uuid4()),
        "action_source": action_source,
        "value": 0,
        "currency": "BRL",
        "user_data": user_data,
    }

    if lead.get("origem") == "whatsapp":
        event["messaging_channel"] = "whatsapp"

    return {"data": [event]}


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------

def enviar_lead_desqualificado(lead: dict) -> dict:
    """
    Chame esta funcao a partir do polling do CV CRM quando:
        lead["motivo_cancelamento"] in MOTIVOS_DESQUALIFICACAO_ALVO
    """
    motivo = lead.get("motivo_cancelamento")
    if motivo not in MOTIVOS_DESQUALIFICACAO_ALVO:
        return {"skipped": True, "motivo": motivo}

    payload = build_event_payload(lead)
    user_data = payload["data"][0]["user_data"]
    if not user_data:
        return {"skipped": True, "motivo": "sem dados de identificacao (sem lead_id/ctwa_clid/email/telefone)"}

    params = {"access_token": ACCESS_TOKEN}

    response = requests.post(CAPI_URL, params=params, json=payload, timeout=10)
    if not response.ok:
        print("Erro do Meta CAPI:", response.status_code, response.text)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Exemplo de uso
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    exemplo_lead = {
        "origem": "whatsapp",
        "ctwa_clid": "AbCdEfGhIjKlMnOp",
        "email": "lead@exemplo.com",
        "telefone": "5585999999999",
        "idlead": 149468,
        "nome": "Luanna",
        "motivo_cancelamento": "Impossível contatar",
    }

    resultado = enviar_lead_desqualificado(exemplo_lead)
    print(resultado)
