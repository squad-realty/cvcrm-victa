"""
Evento LeadDesqualificado -> Meta Conversions API (CAPI)
Cliente: Victa Engenharia - Linha Vitória (Eusébio, Jasmim, Íris, Maracanaú)

CONTEXTO
--------
Os 4 empreendimentos da linha Vitória compartilham o mesmo funil de
desqualificação no CV CRM (mesma situação "Cancelado"). Todo lead
desqualificado, de qualquer um dos 4, deve gerar o evento
"LeadDesqualificado" nas DUAS contas de Meta Ads da Victa -- não há
roteamento por empreendimento, o evento sempre vai pras duas contas
listadas em CONTAS_META.

Datasets (pixels):
    eusebio -> conversões-eusébio (ID 1431660931167100)
    iris    -> ID 1018544220543248

Motivos que disparam o evento (definido com o cliente):
    - Impossivel contatar
    - Nao deseja ser contatada
    - Nao tem perfil Financeiro
    - Engano

Origem do lead determina qual match key usar (em ordem de prioridade):
    1. Lead Ads (Meta)      -> lead_id (leadgen_id) em user_data.lead_id
    2. WhatsApp (CTWA)      -> ctwa_clid em user_data.ctwa_clid
    3. Formulario de LP     -> email + telefone hasheados (fallback)

Nao envia fbc/fbp: canal principal (Lead Ads + WhatsApp) nao depende deles.
LP e secundaria, entao nao ha script de captura client-side implementado.
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

GRAPH_API_VERSION = "v20.0"

# Uma conta = um pixel + um access token proprio. O evento e enviado pra
# TODAS as contas listadas aqui, pra cada lead desqualificado.
CONTAS_META = [
    {
        "nome": "eusebio",
        "pixel_id": "1431660931167100",  # conversões-eusébio
        "access_token": os.environ["META_CAPI_ACCESS_TOKEN_EUSEBIO"],
    },
    {
        "nome": "iris",
        "pixel_id": "1018544220543248",
        "access_token": os.environ["META_CAPI_ACCESS_TOKEN_IRIS"],
    },
]

for _conta in CONTAS_META:
    if not _conta["access_token"].strip():
        raise RuntimeError(
            f"O access token da conta '{_conta['nome']}' esta vazio. Confira se "
            f"a secret foi cadastrada com um valor (nao so o nome) em Settings > "
            f"Secrets and variables > Actions."
        )


# Motivos de cancelamento do CV CRM que devem gerar o evento negativo.
# Ajustar aqui caso o cliente inclua/remova motivos no futuro.
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
    """Espera telefone em formato E.164 (ex: 5585999999999), sem '+' ou espacos."""
    if not phone:
        return None
    digits_only = "".join(ch for ch in phone if ch.isdigit())
    return _sha256(digits_only)


def hash_name(nome_completo: Optional[str]) -> tuple:
    """
    Divide o nome completo em primeiro nome / sobrenome e retorna os hashes
    de cada um. Ex: "Joao Da Silva" -> ("joao", "da silva") hasheados.
    """
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
    Espera um dict `lead` vindo do seu pipeline de polling do CV CRM, com
    (pelo menos um destes preenchido, conforme a origem):
        lead["origem"]        -> "lead_ads" | "whatsapp" | "form_lp"
        lead["leadgen_id"]    -> string, se origem == lead_ads
        lead["ctwa_clid"]     -> string, se origem == whatsapp
        lead["email"]         -> string, se origem == form_lp
        lead["telefone"]      -> string, se origem == form_lp
        lead["idlead"]        -> id interno do lead no CV CRM (external_id)
        lead["nome"]          -> nome completo do lead
    Ajuste os nomes de chave para bater com o dict real do seu pipeline.
    """
    user_data = {}

    origem = lead.get("origem")

    if origem == "lead_ads" and lead.get("leadgen_id"):
        user_data["lead_id"] = lead["leadgen_id"]

    elif origem == "whatsapp" and lead.get("ctwa_clid"):
        user_data["ctwa_clid"] = lead["ctwa_clid"]

    # Email/telefone sempre que disponiveis, mesmo como reforco adicional
    # (nao atrapalha o match, so melhora).
    email_hash = hash_email(lead.get("email"))
    phone_hash = hash_phone(lead.get("telefone"))
    if email_hash:
        user_data["em"] = [email_hash]
    if phone_hash:
        user_data["ph"] = [phone_hash]

    # external_id: nao precisa ser hasheado pela Meta, mas hasheamos por
    # consistencia/seguranca (evita expor o idlead interno em claro).
    if lead.get("idlead"):
        user_data["external_id"] = _sha256(str(lead["idlead"]))

    # Nome/sobrenome: divide o nome completo no primeiro espaco.
    fn_hash, ln_hash = hash_name(lead.get("nome"))
    if fn_hash:
        user_data["fn"] = [fn_hash]
    if ln_hash:
        user_data["ln"] = [ln_hash]

    return user_data


def build_event_payload(lead: dict) -> dict:
    user_data = build_user_data(lead)

    # IMPORTANTE: action_source e sempre "system_generated", mesmo para leads
    # de origem WhatsApp. A Meta so aceita "business_messaging" quando o
    # event_name esta numa lista fixa de eventos padrao (Purchase,
    # LeadSubmitted, QualifiedLead, etc.) -- um nome customizado como
    # "LeadDesqualificado" e rejeitado com HTTP 400 / error_subcode 2804066
    # nessa fonte. O ctwa_clid continua indo em user_data normalmente como
    # chave de correspondencia, isso nao depende do action_source.
    event = {
        "event_name": "LeadDesqualificado",
        "event_time": int(time.time()),
        # Mesmo event_id nas duas contas -- e o mesmo acontecimento sendo
        # espelhado, e a deduplicacao do Meta e por dataset (pixel), entao
        # nao ha conflito entre eusebio e iris.
        "event_id": str(uuid.uuid4()),
        "action_source": "system_generated",
        "value": 0,
        "currency": "BRL",
        "user_data": user_data,
    }

    return {"data": [event]}


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------

def enviar_lead_desqualificado(lead: dict) -> dict:
    """
    Chame esta funcao a partir do seu loop de polling do CV CRM quando:
        lead["motivo_cancelamento"] in MOTIVOS_DESQUALIFICACAO_ALVO

    Envia o mesmo evento pra CADA conta em CONTAS_META (hoje: eusebio e
    iris). Uma conta falhar nao impede o envio pra outra -- o resultado de
    cada uma vem separado no dict de retorno, com a chave sendo o "nome" da
    conta.
    """
    motivo = lead.get("motivo_cancelamento")
    if motivo not in MOTIVOS_DESQUALIFICACAO_ALVO:
        return {"skipped": True, "motivo": motivo}

    payload = build_event_payload(lead)
    user_data = payload["data"][0]["user_data"]
    if not user_data:
        return {"skipped": True, "motivo": "sem dados de identificacao (sem lead_id/ctwa_clid/email/telefone)"}

    resultados = {}
    for conta in CONTAS_META:
        capi_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{conta['pixel_id']}/events"
        params = {"access_token": conta["access_token"]}
        try:
            response = requests.post(capi_url, params=params, json=payload, timeout=10)
            if not response.ok:
                # Mostra o corpo do erro do Meta (geralmente tem a causa exata
                # em response["error"]["message"]), nao so o codigo HTTP.
                print(f"Erro do Meta CAPI ({conta['nome']}):", response.status_code, response.text)
            response.raise_for_status()
            resultados[conta["nome"]] = response.json()
        except Exception as erro:
            resultados[conta["nome"]] = {"erro": f"{erro.__class__.__name__}: {erro}"}

    return resultados


# ---------------------------------------------------------------------------
# Exemplo de uso (dentro do seu polling existente)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    exemplo_lead = {
        "origem": "whatsapp",
        "ctwa_clid": "AbCdEfGhIjKlMnOp",
        "email": "lead@exemplo.com",
        "telefone": "5585999999999",
        "motivo_cancelamento": "Impossível contatar",
    }

    resultado = enviar_lead_desqualificado(exemplo_lead)
    print(resultado)
