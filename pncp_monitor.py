#!/usr/bin/env python3
"""
Agente de Monitoramento PNCP — Robótica Educacional (Somai EdTech)
====================================================================

Monitora a API pública de Consulta do PNCP (Portal Nacional de
Contratações Públicas) à procura de editais, dispensas, PCAs e atas
relacionados a robótica educacional / Dash robot / equipamentos maker.

Não exige autenticação. Endpoint base: https://pncp.gov.br/api/consulta

Uso típico (cron diário ao fim da tarde):
    python3 pncp_monitor.py --modo publicacao --dias 1
    python3 pncp_monitor.py --modo pca --ano 2027
    python3 pncp_monitor.py --modo atas --dias 30
"""

import argparse
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, Iterator, List, Optional

import requests

# ---------------------------------------------------------------------------
# Configuração de e-mail (via variáveis de ambiente / GitHub Secrets)
# ---------------------------------------------------------------------------
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_REMETENTE = os.environ.get("EMAIL_REMETENTE", SMTP_USER)
EMAIL_DESTINATARIOS = [
    e.strip() for e in os.environ.get("EMAIL_DESTINATARIOS", "").split(",") if e.strip()
]

BASE_URL = "https://pncp.gov.br/api/consulta"
TAMANHO_PAGINA = 500  # máximo permitido pela API
TIMEOUT = 30
RETRY = 3
SLEEP_BETWEEN_PAGES = 0.3  # seja gentil com a API pública

# ---------------------------------------------------------------------------
# Palavras-chave estratégicas — ajuste conforme seus 3 modelos exclusivos
# e os equipamentos "de prateleira" do catálogo Somai.
# ---------------------------------------------------------------------------
PALAVRAS_CHAVE = [
    "robotica educacional",
    "robótica educacional",
    "robo educacional",
    "robô educacional",
    "kit de robotica",
    "kit robotica",
    "dash robot",
    "robot dash",
    "wonder workshop",
    "stem",
    "programacao para criancas",
    "programação para crianças",
    "pensamento computacional",
    "cultura maker",
    "espaco maker",
    "espaço maker",
    "laboratorio de robotica",
    "laboratório de robótica",
    "impressora 3d educacional",
    "kit maker",
    "scratch",
    "blockly",
    "lego education",
    "vex robotics",
    "arduino educacional",
]

# Códigos de CLASSE (não o código do item/PDM) usados no parâmetro
# codigoClassificacaoSuperior. Encontre mais em:
# https://catalogo.compras.gov.br/cnbs-web/busca
CODIGOS_CATMAT_PCA = [
    "9999",  # Equipamento / acessório robótico — abrangente, filtrado por palavra-chave abaixo
]


def _get(url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """GET com retry simples e tratamento de erro."""
    for attempt in range(1, RETRY + 1):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT)
            if resp.status_code == 204:
                return None  # sem conteúdo (sem resultados)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            print(f"[aviso] tentativa {attempt}/{RETRY} falhou: {exc}", file=sys.stderr)
            if attempt == RETRY:
                return None
            time.sleep(2 * attempt)
    return None


def paginar(endpoint: str, params: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Itera por todas as páginas de um endpoint do PNCP, sempre cedendo
    cada registro individual (item de 'data')."""
    url = f"{BASE_URL}{endpoint}"
    pagina = 1
    total_paginas = 1

    while pagina <= total_paginas:
        query = dict(params)
        query["pagina"] = pagina
        query["tamanhoPagina"] = TAMANHO_PAGINA

        body = _get(url, query)
        if body is None:
            break

        registros = body.get("data", [])
        total_registros = body.get("totalRegistros", len(registros))
        total_paginas = body.get("totalPaginas", 1)

        if pagina == 1:
            print(f"  -> totalRegistros={total_registros} totalPaginas={total_paginas}")

        for item in registros:
            yield item

        pagina += 1
        if pagina <= total_paginas:
            time.sleep(SLEEP_BETWEEN_PAGES)


def bate_palavra_chave(texto: str) -> List[str]:
    """Retorna a lista de palavras-chave encontradas em um texto (case-insensitive)."""
    if not texto:
        return []
    texto_lower = texto.lower()
    return [kw for kw in PALAVRAS_CHAVE if kw in texto_lower]


# ---------------------------------------------------------------------------
# Modo 1: Editais/avisos diários — /v1/contratacoes/publicacao
# ---------------------------------------------------------------------------
def monitorar_publicacao(
    data_inicial: str,
    data_final: str,
    uf: Optional[str] = None,
    codigo_modalidade: Optional[int] = None,
    codigo_municipio_ibge: Optional[str] = None,
) -> List[Dict[str, Any]]:
    print(f"[publicacao] {data_inicial} -> {data_final} (uf={uf}, modalidade={codigo_modalidade})")
    params: Dict[str, Any] = {
        "dataInicial": data_inicial,
        "dataFinal": data_final,
    }
    if uf:
        params["uf"] = uf
    if codigo_modalidade:
        params["codigoModalidadeContratacao"] = codigo_modalidade
    if codigo_municipio_ibge:
        params["codigoMunicipioIbge"] = codigo_municipio_ibge

    achados = []
    for item in paginar("/v1/contratacoes/publicacao", params):
        objeto = item.get("objetoCompra", "") or ""
        hits = bate_palavra_chave(objeto)
        if hits:
            item["_palavras_chave_encontradas"] = hits
            achados.append(item)
    return achados


# ---------------------------------------------------------------------------
# Modo 2: Oportunidades em aberto (prazo de proposta) — /v1/contratacoes/proposta
# ---------------------------------------------------------------------------
def monitorar_proposta(
    data_inicial: str,
    data_final: str,
    uf: Optional[str] = None,
) -> List[Dict[str, Any]]:
    print(f"[proposta] {data_inicial} -> {data_final} (uf={uf})")
    params: Dict[str, Any] = {
        "dataInicial": data_inicial,
        "dataFinal": data_final,
    }
    if uf:
        params["uf"] = uf

    achados = []
    for item in paginar("/v1/contratacoes/proposta", params):
        objeto = item.get("objetoCompra", "") or ""
        hits = bate_palavra_chave(objeto)
        if hits:
            item["_palavras_chave_encontradas"] = hits
            achados.append(item)
    return achados


# ---------------------------------------------------------------------------
# Modo 3: Planos de Contratações Anuais — /v1/pca/
# ---------------------------------------------------------------------------
def monitorar_pca(ano_pca: int, codigos_catmat: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    codigos = codigos_catmat or CODIGOS_CATMAT_PCA
    print(f"[pca] anoPca={ano_pca} codigos={codigos or '(nenhum — varrendo tudo por palavra-chave)'}")
    achados = []

    if codigos:
        # O código de classe (ex: 9999 "Equipamento / acessório robótico")
        # reduz o universo de itens que a API devolve, mas ainda é
        # abrangente — então a palavra-chave continua decidindo o que é
        # realmente relevante (Dash, robótica educacional, STEM etc.).
        for codigo in codigos:
            params = {"anoPca": ano_pca, "codigoClassificacaoSuperior": codigo}
            for item in paginar("/v1/pca/", params):
                texto = json.dumps(item, ensure_ascii=False)
                hits = bate_palavra_chave(texto)
                if hits:
                    item["_palavras_chave_encontradas"] = hits
                    achados.append(item)
    else:
        # Sem filtro de CATMAT: varre tudo e aplica filtro textual local
        params = {"anoPca": ano_pca}
        for item in paginar("/v1/pca/", params):
            texto = json.dumps(item, ensure_ascii=False)
            hits = bate_palavra_chave(texto)
            if hits:
                item["_palavras_chave_encontradas"] = hits
                achados.append(item)
    return achados


# ---------------------------------------------------------------------------
# Modo 4: Atas de Registro de Preços — /v1/atas
# ---------------------------------------------------------------------------
def monitorar_atas(
    data_inicial: str,
    data_final: str,
    cnpj_orgao: Optional[str] = None,
) -> List[Dict[str, Any]]:
    print(f"[atas] {data_inicial} -> {data_final} (cnpjOrgao={cnpj_orgao})")
    params: Dict[str, Any] = {
        "dataInicial": data_inicial,
        "dataFinal": data_final,
    }
    if cnpj_orgao:
        params["cnpjOrgao"] = cnpj_orgao

    achados = []
    for item in paginar("/v1/atas", params):
        texto = json.dumps(item, ensure_ascii=False)
        hits = bate_palavra_chave(texto)
        if hits:
            item["_palavras_chave_encontradas"] = hits
            achados.append(item)
    return achados


# ---------------------------------------------------------------------------
# Alerta (placeholder) — plugue aqui Slack/Email/WhatsApp/Webhook
# ---------------------------------------------------------------------------
def montar_html(achados: List[Dict[str, Any]], contexto: str) -> str:
    linhas = []
    for item in achados:
        orgao = item.get("orgaoEntidade", {}).get("razaoSocial", "Órgão não informado")
        objeto = item.get("objetoCompra", item.get("objetoContratacao", ""))
        numero_controle = item.get("numeroControlePNCP", "")
        link = item.get("linkSistemaOrigem", "")
        kws = ", ".join(item.get("_palavras_chave_encontradas", []))
        uf = item.get("unidadeOrgao", {}).get("ufSigla", "")
        data_pub = item.get("dataPublicacaoPncp", "")

        linhas.append(f"""
        <tr style="border-bottom:1px solid #e2e2e2;">
          <td style="padding:10px;"><strong>{orgao}</strong> ({uf})</td>
          <td style="padding:10px;">{objeto[:300]}</td>
          <td style="padding:10px;">{kws}</td>
          <td style="padding:10px;">{data_pub}</td>
          <td style="padding:10px;">{f'<a href="{link}">Ver edital</a>' if link else numero_controle}</td>
        </tr>
        """)

    return f"""
    <html><body style="font-family:Arial,sans-serif;">
      <h2>PNCP Monitor — {contexto}</h2>
      <p>{len(achados)} oportunidade(s) encontrada(s) em {datetime.now().strftime('%d/%m/%Y %H:%M')}.</p>
      <table style="width:100%; border-collapse:collapse; font-size:13px;">
        <tr style="background:#f0f0f0; text-align:left;">
          <th style="padding:10px;">Órgão</th>
          <th style="padding:10px;">Objeto</th>
          <th style="padding:10px;">Palavras-chave</th>
          <th style="padding:10px;">Publicação</th>
          <th style="padding:10px;">Link</th>
        </tr>
        {''.join(linhas)}
      </table>
    </body></html>
    """


def enviar_email(assunto: str, corpo_html: str) -> None:
    if not (SMTP_USER and SMTP_PASS and EMAIL_DESTINATARIOS):
        print("[aviso] Credenciais de e-mail ou destinatários não configurados — pulando envio.")
        print("        Configure SMTP_USER, SMTP_PASS, EMAIL_DESTINATARIOS (env vars).")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"] = EMAIL_REMETENTE
    msg["To"] = ", ".join(EMAIL_DESTINATARIOS)
    msg.attach(MIMEText(corpo_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(EMAIL_REMETENTE, EMAIL_DESTINATARIOS, msg.as_string())
        print(f"[ok] e-mail enviado para {', '.join(EMAIL_DESTINATARIOS)}")
    except Exception as exc:
        print(f"[erro] falha ao enviar e-mail: {exc}", file=sys.stderr)


def enviar_alerta(achados: List[Dict[str, Any]], contexto: str) -> None:
    if not achados:
        print(f"[{contexto}] Nenhuma oportunidade encontrada hoje.")
        return

    print(f"\n=== {len(achados)} OPORTUNIDADE(S) ENCONTRADA(S) — {contexto} ===\n")
    for item in achados:
        orgao = item.get("orgaoEntidade", {}).get("razaoSocial", "Órgão não informado")
        objeto = item.get("objetoCompra", item.get("objetoContratacao", ""))
        kws = item.get("_palavras_chave_encontradas", [])
        print(f"- Órgão: {orgao} | Objeto: {objeto[:150]} | Palavras-chave: {', '.join(kws)}")

    assunto = f"[PNCP Monitor] {len(achados)} oportunidade(s) — {contexto}"
    enviar_email(assunto, montar_html(achados, contexto))


def salvar_json(achados: List[Dict[str, Any]], caminho: str) -> None:
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(achados, f, ensure_ascii=False, indent=2)
    print(f"[ok] resultados salvos em {caminho}")


def main():
    parser = argparse.ArgumentParser(description="Agente de monitoramento PNCP")
    parser.add_argument(
        "--modo",
        choices=["publicacao", "proposta", "pca", "atas"],
        required=True,
        help="Qual endpoint monitorar",
    )
    parser.add_argument("--dias", type=int, default=1, help="Janela de dias retroativos (publicacao/proposta/atas)")
    parser.add_argument("--data-inicial", help="AAAAMMDD (sobrescreve --dias)")
    parser.add_argument("--data-final", help="AAAAMMDD (sobrescreve --dias)")
    parser.add_argument("--uf", help="Sigla do estado, ex: SP")
    parser.add_argument("--municipio-ibge", help="Código IBGE do município")
    parser.add_argument("--modalidade", type=int, help="codigoModalidadeContratacao")
    parser.add_argument("--cnpj-orgao", help="CNPJ do órgão (modo atas)")
    parser.add_argument("--ano", type=int, help="anoPca (modo pca)")
    parser.add_argument("--catmat", nargs="*", help="Códigos CATMAT (modo pca)")
    parser.add_argument("--saida", help="Caminho para salvar JSON com os achados")

    args = parser.parse_args()

    hoje = datetime.now()
    data_final = args.data_final or hoje.strftime("%Y%m%d")
    data_inicial = args.data_inicial or (hoje - timedelta(days=args.dias)).strftime("%Y%m%d")

    if args.modo == "publicacao":
        achados = monitorar_publicacao(
            data_inicial, data_final, uf=args.uf,
            codigo_modalidade=args.modalidade,
            codigo_municipio_ibge=args.municipio_ibge,
        )
        enviar_alerta(achados, "Editais/Avisos Publicados")

    elif args.modo == "proposta":
        achados = monitorar_proposta(data_inicial, data_final, uf=args.uf)
        enviar_alerta(achados, "Em Prazo de Proposta")

    elif args.modo == "pca":
        if not args.ano:
            parser.error("--ano é obrigatório no modo pca")
        achados = monitorar_pca(args.ano, codigos_catmat=args.catmat)
        enviar_alerta(achados, f"PCA {args.ano}")

    elif args.modo == "atas":
        achados = monitorar_atas(data_inicial, data_final, cnpj_orgao=args.cnpj_orgao)
        enviar_alerta(achados, "Atas de Registro de Preços")

    if args.saida:
        salvar_json(achados, args.saida)


if __name__ == "__main__":
    main()
