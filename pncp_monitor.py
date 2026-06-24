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
TAMANHO_PAGINA = 100  # a API rejeitou 500 com "Tamanho de página inválido"; testando 100
TIMEOUT = 60
RETRY = 5
SLEEP_BETWEEN_PAGES = 0.3  # seja gentil com a API pública
SLEEP_BASE_RETRY = 5  # segundos, multiplicado pela tentativa (backoff)

# Lista global de falhas de conexão da execução atual — usada para que o
# agente NUNCA reporte silenciosamente "nenhuma oportunidade encontrada"
# quando, na verdade, não conseguiu nem consultar a API.
ERROS_CONEXAO: List[str] = []

# ---------------------------------------------------------------------------
# Tabela de domínio oficial "Modalidade da Contratação". A API EXIGE esse
# parâmetro em /contratacoes/publicacao e /contratacoes/proposta — não é
# opcional, então o agente roda em loop por cada modalidade da lista padrão
# e agrega os resultados.
# ---------------------------------------------------------------------------
MODALIDADES = {
    1: "Leilão - Eletrônico",
    2: "Diálogo Competitivo",
    3: "Concurso",
    4: "Concorrência - Eletrônica",
    5: "Concorrência - Presencial",
    6: "Pregão - Eletrônico",
    7: "Pregão - Presencial",
    8: "Dispensa de Licitação",
    9: "Inexigibilidade",
    10: "Manifestação de Interesse",
    11: "Pré-qualificação",
    12: "Credenciamento",
    13: "Leilão - Presencial",
    14: "Inaplicabilidade da Licitação",
}

# Modalidades mais relevantes para compra de robôs/kits educacionais
# (pregão e dispensa cobrem a grande maioria dos casos no setor público).
MODALIDADES_PADRAO = [6, 7, 8, 4, 5]

# ---------------------------------------------------------------------------
# Palavras-chave estratégicas — ajuste conforme seus 3 modelos exclusivos
# e os equipamentos "de prateleira" do catálogo Somai.
# ---------------------------------------------------------------------------
PALAVRAS_CHAVE = [
    # Robótica educacional — termos genéricos
    "robotica educacional",
    "robótica educacional",
    "robo educacional",
    "robô educacional",
    "kit de robotica",
    "kit robotica",
    "laboratorio de robotica",
    "laboratório de robótica",
    "kit maker",
    "impressora 3d educacional",

    # Marcas / produtos específicos distribuídos pela Somai
    "dash robot",
    "robot dash",
    "dash robo",
    "robo dash",
    "wonder workshop",
    "unitree",
    "cyberdog",
    "pepper",
    "nao robo",
    "robo nao",
    "robo humanoide",
    "humanoide robo",
    "humanoide",

    # Aplicações / casos de uso de robôs humanoides e de serviço
    "aspirador robo",
    "robo aspirador",
    "robo limpeza",
    "robo de limpeza",
    "robo atendimento",
    "atendimento robo",
    "concierge robo",
    "robo concierge",

    # STEM / cultura maker / metodologia
    "stem",
    "steam",
    "cultura maker",
    "maker",
    "espaco maker",
    "espaço maker",
    "sala tecnologica educacional",
    "sala tecnológica educacional",
    "pensamento computacional",
    "programacao para criancas",
    "programação para crianças",
    "programacao educacional",
    "programação educacional",
    "scratch",
    "blockly",
    "lego education",
    "kit lego",
    "lego kit",
    "vex robotics",
    "arduino educacional",
    "arduino",
    "kit arduino",

    # IA / formação / plataforma
    "curso ia",
    "ia curso",
    "formacao de professores",
    "formação de professores",
    "material pedagogico",
    "material pedagógico",
    "lms",
    "plataforma de aprendizagem",
]

# Códigos de CLASSE (não o código do item/PDM) usados no parâmetro
# codigoClassificacaoSuperior. Encontre mais em:
# https://catalogo.compras.gov.br/cnbs-web/busca
CODIGOS_CATMAT_PCA = [
    "9999",  # Equipamento / acessório robótico — abrangente, filtrado por palavra-chave abaixo
]


def _get(url: str, params: Dict[str, Any]):
    """GET com retry e backoff. Retorna None em caso de resposta vazia (204).
    Erros 4xx (requisição malformada) NÃO são tentados de novo, pois são
    definitivos — repetir não muda o resultado. Erros 5xx/timeout/conexão
    SÃO tentados de novo (podem ser instabilidade temporária).
    Em erro 4xx, retorna a tupla ("__ERRO_4XX__", corpo_da_resposta_texto)."""
    for attempt in range(1, RETRY + 1):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT)
            if resp.status_code == 204:
                return None  # sem conteúdo (sem resultados) — não é erro

            if 400 <= resp.status_code < 500:
                # Erro definitivo do lado do request — não adianta repetir.
                print(
                    f"[ERRO 4xx - NÃO REPETÍVEL] status={resp.status_code} url={resp.url}\n"
                    f"  corpo da resposta: {resp.text[:500]}",
                    file=sys.stderr,
                )
                return ("__ERRO_4XX__", resp.text)

            resp.raise_for_status()  # levanta para 5xx, tratado abaixo como retryable
            return resp.json()
        except requests.exceptions.RequestException as exc:
            print(f"[aviso] tentativa {attempt}/{RETRY} falhou: {exc}", file=sys.stderr)
            if attempt == RETRY:
                return "__ERRO_CONEXAO__"
            time.sleep(SLEEP_BASE_RETRY * attempt)
    return "__ERRO_CONEXAO__"


def paginar(endpoint: str, params: Dict[str, Any], contexto: str = "") -> Iterator[Dict[str, Any]]:
    """Itera por todas as páginas de um endpoint do PNCP, sempre cedendo
    cada registro individual (item de 'data'). Em caso de falha de conexão
    (não confundir com "sem resultados"), registra em ERROS_CONEXAO.
    Se a API rejeitar o tamanho de página, tenta automaticamente valores
    menores antes de desistir."""
    url = f"{BASE_URL}{endpoint}"
    pagina = 1
    total_paginas = 1
    tamanhos_para_tentar = [TAMANHO_PAGINA, 50, 20, 10]
    tamanho_confirmado = None  # uma vez descoberto, reutiliza nas próximas páginas

    while pagina <= total_paginas:
        body = None
        tamanho_usado = None
        todos_tamanhos_falharam = True

        candidatos = [tamanho_confirmado] if tamanho_confirmado else tamanhos_para_tentar

        for tamanho in candidatos:
            query = dict(params)
            query["pagina"] = pagina
            query["tamanhoPagina"] = tamanho

            resultado = _get(url, query)

            if isinstance(resultado, tuple) and resultado[0] == "__ERRO_4XX__":
                corpo = resultado[1] or ""
                if "tamanho de p" in corpo.lower() or "tamanhopagina" in corpo.lower():
                    print(f"  -> tamanhoPagina={tamanho} rejeitado, tentando valor menor...")
                    continue  # tenta o próximo tamanho da lista
                # Outro tipo de erro 4xx, não relacionado a tamanho de página
                body = "__ERRO_4XX__"
                todos_tamanhos_falharam = False
                break

            # Sucesso (dict) ou erro de conexão (string) — não tem mais a ver
            # com tamanho de página, então não continua tentando tamanhos.
            body = resultado
            tamanho_usado = tamanho
            todos_tamanhos_falharam = False
            break

        if not todos_tamanhos_falharam and isinstance(body, dict):
            tamanho_confirmado = tamanho_usado

        if todos_tamanhos_falharam:
            msg = f"{contexto} (página {pagina}): API rejeitou todos os tamanhos de página testados {tamanhos_para_tentar}"
            print(f"[ERRO 4xx] {msg}", file=sys.stderr)
            ERROS_CONEXAO.append(msg)
            break

        if body == "__ERRO_4XX__":
            msg = f"{contexto} (página {pagina}): requisição rejeitada pela API (400/4xx) — ver corpo da resposta no log acima"
            print(f"[ERRO 4xx] {msg}", file=sys.stderr)
            ERROS_CONEXAO.append(msg)
            break

        if body == "__ERRO_CONEXAO__":
            msg = f"{contexto} (página {pagina}): falha de conexão após {RETRY} tentativas"
            print(f"[ERRO DE CONEXÃO] {msg}", file=sys.stderr)
            ERROS_CONEXAO.append(msg)
            break

        if body is None:
            if pagina == 1:
                print(f"  -> totalRegistros=0 (sem resultados)")
            break

        registros = body.get("data", [])
        total_registros = body.get("totalRegistros", len(registros))
        total_paginas = body.get("totalPaginas", 1)

        if pagina == 1:
            print(f"  -> totalRegistros={total_registros} totalPaginas={total_paginas} (tamanhoPagina={tamanho_usado})")

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
    modalidades: Optional[List[int]] = None,
    codigo_municipio_ibge: Optional[str] = None,
) -> List[Dict[str, Any]]:
    modalidades = modalidades or MODALIDADES_PADRAO
    achados = []
    for codigo_modalidade in modalidades:
        nome_modalidade = MODALIDADES.get(codigo_modalidade, "?")
        print(f"[publicacao] {data_inicial} -> {data_final} (uf={uf}, modalidade={codigo_modalidade} - {nome_modalidade})")
        params: Dict[str, Any] = {
            "dataInicial": data_inicial,
            "dataFinal": data_final,
            "codigoModalidadeContratacao": codigo_modalidade,  # obrigatório na API
        }
        if uf:
            params["uf"] = uf
        if codigo_municipio_ibge:
            params["codigoMunicipioIbge"] = codigo_municipio_ibge

        for item in paginar(
            "/v1/contratacoes/publicacao", params,
            contexto=f"publicacao modalidade={codigo_modalidade} ({nome_modalidade})",
        ):
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
    for item in paginar("/v1/contratacoes/proposta", params, contexto="proposta"):
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
            for item in paginar("/v1/pca/", params, contexto=f"pca codigo={codigo}"):
                texto = json.dumps(item, ensure_ascii=False)
                hits = bate_palavra_chave(texto)
                if hits:
                    item["_palavras_chave_encontradas"] = hits
                    achados.append(item)
    else:
        # Sem filtro de CATMAT: varre tudo e aplica filtro textual local
        params = {"anoPca": ano_pca}
        for item in paginar("/v1/pca/", params, contexto="pca (sem filtro)"):
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
    for item in paginar("/v1/atas", params, contexto="atas"):
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
    if ERROS_CONEXAO:
        print(
            f"\n[AVISO IMPORTANTE] {len(ERROS_CONEXAO)} consulta(s) falharam por "
            f"problema de conexão com a API do PNCP — os resultados abaixo "
            f"podem estar INCOMPLETOS:",
            file=sys.stderr,
        )
        for erro in ERROS_CONEXAO:
            print(f"  - {erro}", file=sys.stderr)

    if not achados:
        if ERROS_CONEXAO:
            print(
                f"[{contexto}] Nenhuma oportunidade encontrada — MAS houve falhas de "
                f"conexão, então isso pode não refletir a realidade. Veja os avisos acima."
            )
        else:
            print(f"[{contexto}] Nenhuma oportunidade encontrada hoje.")
        return

    print(f"\n=== {len(achados)} OPORTUNIDADE(S) ENCONTRADA(S) — {contexto} ===\n")
    for item in achados:
        orgao = item.get("orgaoEntidade", {}).get("razaoSocial", "Órgão não informado")
        objeto = item.get("objetoCompra", item.get("objetoContratacao", ""))
        kws = item.get("_palavras_chave_encontradas", [])
        print(f"- Órgão: {orgao} | Objeto: {objeto[:150]} | Palavras-chave: {', '.join(kws)}")

    assunto = f"[PNCP Monitor] {len(achados)} oportunidade(s) — {contexto}"
    if ERROS_CONEXAO:
        assunto = f"[PNCP Monitor] ⚠ resultados parciais — {len(achados)} oportunidade(s) — {contexto}"
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
    parser.add_argument(
        "--modalidades", type=int, nargs="*",
        help="Lista de codigoModalidadeContratacao (padrão: pregão/dispensa/concorrência)",
    )
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
            modalidades=args.modalidades,
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

    if ERROS_CONEXAO:
        print(
            f"\n[RESUMO] {len(ERROS_CONEXAO)} consulta(s) falharam por problema de "
            f"conexão com pncp.gov.br. Isso pode ser instabilidade temporária do "
            f"servidor do governo. Recomenda-se rodar de novo manualmente "
            f"(aba Actions → Run workflow) para confirmar.",
            file=sys.stderr,
        )
        # Sai com erro para que o GitHub Actions marque a execução como
        # falha (❌) em vez de sucesso (✅) — assim você não tem falsa
        # sensação de que tudo ocorreu bem quando, na verdade, a busca
        # ficou incompleta.
        sys.exit(1)


if __name__ == "__main__":
    main()
