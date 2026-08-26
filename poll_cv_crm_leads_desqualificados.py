"""
Polling CV CRM -> Meta Conversions API
Cliente: Victa Engenharia - Linha Vitória (Eusébio, Jasmim, Íris, Maracanaú)

O QUE ESTE SCRIPT FAZ
---------------------
1. Descobre automaticamente o ID da situacao "Cancelado" no workflow de Leads
   do CV CRM (via GET /workflows/{funcionalidade}), para nao depender de um
   numero fixo que pode mudar entre ambientes.
2. Busca os leads cancelados (GET /v1/comercial/leads?idsituacao=...) desde a
   ultima execucao (controle feito por um arquivo local state.json). A
   situacao "Cancelado" e COMPARTILHADA por toda a conta CV CRM da Victa --
   nao e exclusiva dos 4 empreendimentos da linha Vitoria -- entao essa busca
   sozinha traz leads de outros produtos tambem.
3. Filtra pelos 4 empreendimentos da linha Vitoria (EMPREENDIMENTOS_ALVO,
   usando o campo "idempreendimento" do lead) E pelo "motivo_cancelamento.nome"
   estar na lista de motivos-alvo combinada com o cliente. As duas condicoes
   sao obrigatorias -- sem o filtro de empreendimento, leads desqualificados
   de qualquer outro produto da Victa tambem disparariam evento no Meta.
4. Para cada lead filtrado, monta o payload e envia o evento
   "LeadDesqualificado" via Meta CAPI pras DUAS contas (eusebio e iris),
   reaproveitando a logica de meta_lead_desqualificado_capi.py -- nao ha
   roteamento por empreendimento, todo lead desqualificado vai pras duas.
5. Atualiza o state.json com o timestamp mais recente processado, para a
   proxima execucao nao reprocessar os mesmos leads.

CREDENCIAIS (variaveis de ambiente, nunca hardcode):
    CV_CRM_SUBDOMINIO             -> subdominio do CV CRM (victa)
    CV_CRM_EMAIL                  -> e-mail do usuario administrativo com token gerado
    CV_CRM_TOKEN                  -> token gerado no painel do gestor
    CV_CRM_IDSITUACAO_CANCELADO   -> id da situacao "Cancelado", compartilhado pelos 4 empreendimentos
    META_CAPI_ACCESS_TOKEN_EUSEBIO -> token de acesso do dataset conversões-eusébio
    META_CAPI_ACCESS_TOKEN_IRIS    -> token de acesso do dataset da conta íris

ATENCAO / PONTOS PRA VALIDAR:
- O nome da "funcionalidade" usado em /workflows/{funcionalidade} para Leads
  foi assumido como "leads". Se a chamada retornar vazio/erro, verifique o
  nome exato com o suporte do CV CRM ou no proprio painel (Configuracoes >
  Workflows).
- leadgen_id/ctwa_clid: o schema padrao de retorno do lead NAO tem esses
  campos nativamente. O script tenta achar em "campos_adicionais" (lista de
  slug/valor customizados) usando alguns nomes candidatos comuns. Se o CV CRM
  de voces nao tiver esse campo configurado, o evento cai automaticamente
  para o fallback de email/telefone hasheados (ainda funciona, so com EMQ
  um pouco mais baixo).
"""

import json
import os
import time
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

# ID da situacao "Cancelado" no workflow de Leads. Descoberto manualmente
# (via curl em um lead ja cancelado) porque o endpoint de descoberta
# automatica de workflow nao esta documentado publicamente para a v1.
CV_CRM_IDSITUACAO_CANCELADO = int(os.environ["CV_CRM_IDSITUACAO_CANCELADO"])

CV_CRM_BASE_URL = f"https://{CV_CRM_SUBDOMINIO}.cvcrm.com.br/api"
HEADERS = {"email": CV_CRM_EMAIL, "token": CV_CRM_TOKEN}

STATE_FILE = Path(__file__).parent / "state.json"

MOTIVOS_DESQUALIFICACAO_ALVO = {
    "Impossível contatar",
    "Não deseja ser contatada",
    "Não tem perfil Financeiro",
    "Engano",
}

# idempreendimento (retornado pelo CV CRM em cada lead) dos 4 empreendimentos
# da linha Vitoria -- IDs conferidos direto na tela de Empreendimentos do
# painel. So leads com idempreendimento nessa lista disparam evento no Meta;
# qualquer outro produto da Victa que compartilhe a mesma situacao
# "Cancelado" e ignorado.
EMPREENDIMENTOS_ALVO = {
    43: "Vitória Eusébio",
    35: "Vitória Jasmim",
    40: "Vitória Íris",
    42: "Vitória Maracanaú",
}

# Nomes de slug candidatos em campos_adicionais para os IDs de clique da Meta.
# Ajuste conforme o nome real configurado no CV CRM, se existir.
SLUGS_LEADGEN_ID_CANDIDATOS = ["leadgen_id", "cf_leadgen_id", "meta_leadgen_id"]
SLUGS_CTWA_CLID_CANDIDATOS = ["ctwa_clid", "cf_ctwa_clid", "meta_ctwa_clid"]


# ---------------------------------------------------------------------------
# Estado (evita reprocessar os mesmos leads a cada execucao)
# ---------------------------------------------------------------------------

def carregar_estado() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    # Sem state.json ainda: comeca 90 dias atras, nao em 1970.
    # Eventos mais antigos que isso nao tem uso pratico (a Custom Audience
    # do Meta so olha ate 180 dias, e nao vale a pena reprocessar anos de
    # historico logo na primeira execucao).
    data_inicial = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    return {"ultima_data_cancelamento_processada": data_inicial}


def salvar_estado(estado: dict) -> None:
    STATE_FILE.write_text(json.dumps(estado, indent=2))


# ---------------------------------------------------------------------------
# Busca de leads cancelados
# ---------------------------------------------------------------------------

def buscar_leads_cancelados(idsituacao_cancelado: int, limit: int = 20, max_paginas: int = 50) -> list:
    """
    Pagina pelo endpoint de leads filtrando por idsituacao. Retorna todos os
    leads encontrados (sem filtrar motivo ainda -- isso e feito depois).
    Teto de max_paginas (50 x 20 = 1000 leads) como seguranca extra, alem
    do timeout-minutes do proprio workflow.
    """
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
        print(f"Atingiu o teto de {max_paginas} paginas -- parando por seguranca. "
              f"Se isso acontecer com frequencia, pode ser preciso paginar em lotes menores.")

    return leads


def _get_com_retry(url: str, params: dict, tentativas: int = 5) -> dict:
    """
    A API do CV CRM as vezes demora para responder em consultas mais
    pesadas, ou devolve erro 503/429 quando esta sobrecarregada (comum em
    rodadas de backfill que fazem milhares de chamadas seguidas). Tenta ate
    5 vezes, aumentando timeout E esperando entre as tentativas, antes de
    desistir de fato.
    """
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        timeout = 30 * tentativa  # 30s, 60s, 90s, 120s, 150s
        try:
            response = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as erro:
            ultimo_erro = erro
            motivo = erro.__class__.__name__
        except requests.exceptions.HTTPError as erro:
            status = erro.response.status_code if erro.response is not None else None
            corpo = erro.response.text[:500] if erro.response is not None else ""
            print(f"Erro HTTP {status} em {params} -- corpo da resposta: {corpo}")
            # 5xx (erro do servidor) e 429 (rate limit) sao transitorios --
            # vale tentar de novo. Outros 4xx (400, 401, 403...) normalmente
            # sao erro de credencial/parametro que retry nao resolve.
            if status is not None and (status >= 500 or status == 429):
                ultimo_erro = erro
                motivo = f"HTTP {status}"
            else:
                raise

        espera = 5 * tentativa  # 5s, 10s, 15s, 20s, 25s -- da folego pro servidor
        if tentativa < tentativas:
            print(f"Tentativa {tentativa}/{tentativas} falhou ({motivo}). "
                  f"Esperando {espera}s antes de tentar de novo...")
            time.sleep(espera)
        else:
            print(f"Tentativa {tentativa}/{tentativas} falhou ({motivo}). Desistindo.")
    raise ultimo_erro


# ---------------------------------------------------------------------------
# Mapeamento CV CRM -> payload esperado pelo enviar_lead_desqualificado
# ---------------------------------------------------------------------------

def extrair_campo_adicional(campos_adicionais: list, slugs_candidatos: list) -> str | None:
    for campo in campos_adicionais or []:
        if campo.get("slug") in slugs_candidatos:
            return campo.get("valor")
    return None


def extrair_ids_empreendimento(lead_cv: dict) -> set:
    """
    O campo "idempreendimento" NAO existe no lead retornado pela API (vem
    sempre None) -- descoberto rodando descobrir_idempreendimento.py contra
    leads reais. O campo de verdade e "empreendimento", uma LISTA de dicts
    tipo [{"id": 43, "nome": "VITORIA EUSEBIO"}] (um lead pode, em teoria,
    estar associado a mais de um). Ha tambem um campo auxiliar
    "empreendimentosId" (string, ex: "43", possivelmente separado por
    virgula se houver mais de um) -- usado aqui so como fallback, caso a
    lista "empreendimento" venha vazia por algum motivo.

    Retorna um set de ids (int) -- pode ter mais de um, ou nenhum.
    """
    ids = set()
    for item in lead_cv.get("empreendimento") or []:
        if isinstance(item, dict) and item.get("id") is not None:
            try:
                ids.add(int(item["id"]))
            except (TypeError, ValueError):
                pass

    if not ids:
        bruto = lead_cv.get("empreendimentosId")
        if bruto:
            for pedaco in str(bruto).split(","):
                pedaco = pedaco.strip()
                if pedaco.isdigit():
                    ids.add(int(pedaco))

    return ids


def identificar_origem(lead: dict) -> str:
    """
    Usa midia_principal/midias para inferir a origem (lead_ads, whatsapp,
    form_lp). Ajuste os termos de busca conforme o que aparece de fato nas
    midias cadastradas para a linha Vitória.
    """
    midia = (lead.get("midia_principal") or "").lower()
    midias = [m.lower() for m in (lead.get("midias") or [])]
    todas_midias = [midia] + midias

    if any("whatsapp" in m for m in todas_midias):
        return "whatsapp"
    if any("lead ad" in m or "facebook" in m or "instagram" in m for m in todas_midias):
        return "lead_ads"
    return "form_lp"


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
        "ids_empreendimento": sorted(extrair_ids_empreendimento(lead_cv)),
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
    enviados = 0          # sucesso nas DUAS contas
    parciais = 0          # sucesso em uma conta, falha na outra
    ignorados_motivo = 0
    ignorados_empreendimento = 0
    ignorados_sem_dados = 0
    falhas = 0            # falha nas DUAS contas (ou exception nao tratada)

    for lead_cv in leads:
        # .get(..., "") so cobre chave AUSENTE -- se a chave existir com
        # valor null, o .get() retorna None mesmo assim e a comparacao com
        # string quebra (TypeError). O "or ''" cobre os dois casos.
        data_cancelamento = lead_cv.get("data_cancelamento") or ""

        # So processa leads cancelados depois da ultima execucao
        if not data_cancelamento or data_cancelamento <= ultima_data_processada:
            continue

        # So processa os 4 empreendimentos da linha Vitoria -- a situacao
        # "Cancelado" e compartilhada com outros produtos da Victa, entao
        # esse filtro e obrigatorio, nao defensivo.
        ids_empreendimento = extrair_ids_empreendimento(lead_cv)
        if not ids_empreendimento & EMPREENDIMENTOS_ALVO.keys():
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

            if isinstance(resultado, dict) and resultado.get("skipped"):
                # Motivo fora do alvo (ja filtrado acima, defensivo) ou sem
                # nenhum dado de identificacao pra casar no Meta.
                ignorados_sem_dados += 1
            else:
                # resultado e {"eusebio": {...}, "iris": {...}} -- cada valor
                # e a resposta da Meta (sucesso) ou {"erro": "..."} (falha
                # so daquela conta). Conta como falha de fato so se TODAS as
                # contas falharam; parcial se so uma falhou.
                contas_com_erro = sum(
                    1 for v in resultado.values() if isinstance(v, dict) and "erro" in v
                )
                if contas_com_erro == 0:
                    enviados += 1
                elif contas_com_erro == len(resultado):
                    falhas += 1
                    print(f"Lead {lead_cv.get('idlead')} FALHOU nas duas contas -- ver erro acima.")
                else:
                    parciais += 1
                    print(f"Lead {lead_cv.get('idlead')} falhou em parte das contas -- ver erro acima.")
        except Exception as erro:
            print(f"Lead {lead_cv.get('idlead')} FALHOU -> {erro.__class__.__name__}: {erro}")
            falhas += 1

        # Avanca o cursor de data mesmo em caso de falha, para nao tentar
        # reprocessar um lead permanentemente problematico a cada execucao.
        if data_cancelamento > maior_data_cancelamento:
            maior_data_cancelamento = data_cancelamento

    estado["ultima_data_cancelamento_processada"] = maior_data_cancelamento
    salvar_estado(estado)

    print(
        f"Concluido. Enviados (2 contas ok): {enviados}. "
        f"Parciais (1 conta falhou): {parciais}. "
        f"Ignorados (fora dos 4 empreendimentos): {ignorados_empreendimento}. "
        f"Ignorados (motivo fora do alvo): {ignorados_motivo}. "
        f"Ignorados (sem dado de identificacao): {ignorados_sem_dados}. "
        f"Falhas (2 contas falharam): {falhas}."
    )


if __name__ == "__main__":
    main()
