# PNCP Monitor — Robótica Educacional

Agente que roda diariamente via GitHub Actions, consulta a API pública do
PNCP procurando editais relacionados a robótica educacional (Dash, STEM,
cultura maker, etc.) e envia um e-mail para a equipe de vendas.

## Como colocar no ar (15 minutos)

### 1. Criar o repositório no GitHub
- Crie um repositório novo (pode ser **privado** — gratuito no GitHub).
- Suba estes arquivos mantendo a estrutura:
  ```
  pncp_monitor.py
  .github/workflows/pncp_monitor.yml
  ```

### 2. Gerar uma senha de app para o e-mail
Se for usar Gmail (recomendado, já configurado em `SMTP_HOST=smtp.gmail.com`):
1. Ative a verificação em duas etapas na conta Google que vai enviar os e-mails.
2. Acesse https://myaccount.google.com/apppasswords e gere uma **senha de app**.
3. Guarde essa senha — é diferente da senha normal da conta.

Se usar outro provedor (Outlook, Zoho, servidor próprio), ajuste `SMTP_HOST`
e `SMTP_PORT` nos secrets abaixo.

### 3. Configurar os Secrets no GitHub
No repositório: **Settings → Secrets and variables → Actions → New repository secret**

| Nome                  | Valor                                          |
|------------------------|------------------------------------------------|
| `SMTP_HOST`            | `smtp.gmail.com` (ou outro)                    |
| `SMTP_PORT`            | `587`                                          |
| `SMTP_USER`            | seu e-mail de envio, ex: `alertas@somai.com.br`|
| `SMTP_PASS`            | a senha de app gerada no passo 2               |
| `EMAIL_DESTINATARIOS`  | e-mails da equipe de vendas, separados por vírgula, ex: `vendas@somai.com.br,joao@somai.com.br` |

### 4. Testar manualmente
Vá na aba **Actions** do repositório → selecione o workflow **"PNCP Monitor -
Robótica Educacional"** → botão **"Run workflow"**. Isso dispara uma execução
imediata sem esperar o agendamento.

### 5. Agendamento automático
Já está configurado para rodar **todos os dias às 17h (Brasília)**, via cron
no arquivo `.github/workflows/pncp_monitor.yml`. Para mudar o horário, edite
a linha `cron: "0 20 * * *"` (formato UTC — Brasília = UTC-3).

## Ajustes finos

- **Palavras-chave**: edite a lista `PALAVRAS_CHAVE` em `pncp_monitor.py`
  com os nomes exatos dos seus 3 modelos exclusivos.
- **Códigos CATMAT (para o módulo de PCA)**: quando você tiver os códigos
  (veja instruções que te passei), adicione em `CODIGOS_CATMAT_PCA`.
- **Filtrar por estado**: para focar numa região, adicione `--uf SP` (ou
  outra sigla) no comando dentro do workflow `.yml`.
- **Adicionar outros modos** (proposta, pca, atas): basta duplicar o passo
  "Rodar monitor" no `.yml` trocando `--modo` e os parâmetros.

## Teste local (opcional, antes de subir pro GitHub)

```bash
export SMTP_USER="seuemail@gmail.com"
export SMTP_PASS="sua_senha_de_app"
export EMAIL_DESTINATARIOS="vendas@somai.com.br"

python3 pncp_monitor.py --modo publicacao --dias 1
```
