# triaige-perf

Testes de carga do TriAIge (JMeter) dimensionados pelos cenários do Business Case.
A pergunta que eles respondem, com dado medido: **a plataforma aguenta o volume de 500 escritórios (e o do Ano 5)?**

## Modelo de carga

A carga não é um número fixo de usuários: ela sai do cenário de mercado (`cenarios/*.properties`)
pelas fórmulas abaixo, implementadas em `calc_carga.py`.

```
usuarios_nomeados = 2·n_ess + 5·n_pro + 15·n_corp
unidades_mes      = (100·n_ess + 500·n_pro + 1000·n_corp) · uso
docs_hora_media   = unidades_mes / dias / horas
docs_min_pico     = docs_hora_media · fpico / 60          # taxa de chegada (modelo aberto)
docs_em_voo       = docs_min_pico · t_pipe                 # Little: L = λ·W
reqs_polling      = docs_em_voo / poll                     # req/s só de consulta de status
portal_simult     = usuarios_nomeados · conc
emails_dia        = unidades_mes / dias
```

### Premissas

| Parâmetro | Valor padrão | Origem |
|---|---|---|
| Usuários por plano (Essencial/Profissional/Corporativo) | 2 / 5 / 15 | BC, slide 13 |
| Franquia em unidades/mês | 100 / 500 / 1.000 | BC, slide 13 |
| `uso` (uso da franquia) | 1,0 (pior caso) | **Premissa** |
| `dias` úteis/mês, `horas` úteis/dia | 22 e 9 | Premissa |
| `fpico` (hora de pico sobre a média) | 2,0 | **Premissa** |
| `conc` (portal simultâneo) | 15% | **Premissa** |
| `t_pipe_min` (tempo de pipeline) | 5 min | Conservador (medido hoje: ~30 s; meta < 6 min) |
| `poll_seg` | 5 s | JMX |
| `admin_users` | 3, fixo | **Premissa**: o painel admin é uso interno e não cresce com os clientes |
| `api_conc` (integrações com consulta avulsa simultânea) | 2% dos escritórios | **Premissa** |
| `limite_email_dia` | 500 | **Premissa**: Gmail SMTP de conta pessoal (Workspace: 2.000); confirmar |
| `gemini_usd_doc` | US$ 0,01 | **Premissa a calibrar** com o billing |
| `textract_usd_pagina` | US$ 0,0015 | Preço público do Textract (us-east-1) |

Qualquer premissa pode ser sobrescrita no arquivo do cenário; a tabela impressa marca a origem de cada uma.

### Cenário conservador (500 × Essencial)

```
$ python3 calc_carga.py cenarios/conservador.properties
  Usuários nomeados                            1.000
  Usuários de portal simultâneos               150,0
  Documentos/mês (máx.)                        50.000
  Documentos/dia útil                          2.273
  Chegada no pico                              8,42 docs/min (0,140/s)
  Documentos em processamento simultâneo       42,1
  Polling de status                            8,4 req/s
  E-mails/dia                                  2.273
  ATENÇÃO: 2.273 e-mails/dia passam do limite de envio considerado (500/dia) ...
```

Cerca de 2.270 e-mails por dia passam do limite diário do Gmail SMTP: a entrega não fecha o
cenário conservador sem migrar para o Amazon SES ou equivalente. O modo `pipeline` deixa isso visível.

### Cenários

| Arquivo | Descrição |
|---|---|
| `cenarios/smoke.properties` | 5 escritórios e rodadas curtas, para validar o script com custo desprezível |
| `cenarios/conservador.properties` | 500 Essencial (slide 14) |
| `cenarios/bc-ano5.properties` | Mix do Ano 5 da planilha `BC_grupo_10_mercado_atualizado.xlsm`; **a preencher pelo grupo** (o script recusa enquanto houver `A_PREENCHER`) |
| `cenarios/estresse.properties` | 2× o Ano 5 (herda `bc-ano5` com `multiplicador=2`), para achar o ponto de ruptura |

## Como rodar

Pré-requisitos: JMeter ≥ 5.5 (testado no 5.6.3), Python 3 e AWS CLI com acesso de leitura ao CloudWatch e ao CloudWatch Logs.

```sh
cp .env.exemplo .env      # preencha TRIAIGE_HOST, TRIAIGE_API_TOKEN e TRIAIGE_SENHA
./rodar-cenario.sh cenarios/smoke.properties plataforma
./rodar-cenario.sh cenarios/conservador.properties plataforma
./rodar-cenario.sh cenarios/conservador.properties pipeline
./rodar-cenario.sh cenarios/conservador.properties pico
```

| Modo | O que roda | Pipeline (OCR, Gemini, e-mail) | Duração |
|---|---|---|---|
| `plataforma` | Degraus de 25/50/75/100% do portal simultâneo, mais um pico de 100% em 10 s; inclui o polling equivalente aos documentos em voo | Desligado | ~16 min |
| `pipeline` | Chegada constante (Poisson) de `docs_min_pico`, com portal, admin e API em paralelo | Ligado | 30 min em regime + 10 min de drenagem |
| `pico` | 5 min normais, 5 min de rajada a 3× e 10 min normais (ex.: segunda-feira de manhã) | Ligado | 20 min + 10 min de drenagem |

Nos modos com pipeline, o script imprime a **estimativa de custo** e o checklist de pré-requisitos e pede
confirmação (`-y` pula a pergunta). Antes de rodar, confira:

- [ ] Service Quotas do Textract assíncrono (TPS de `StartDocumentTextDetection` e jobs simultâneos) contra `docs_min_pico`
- [ ] Limite de requisições por minuto e por dia do Gemini no tier da chave usada
- [ ] Destino de e-mail de teste (caixa coletora ou SES sandbox), para não enviar e-mail real
- [ ] AWS Budgets com alerta para a janela do teste
- [ ] Os casos levam `[LOAD TEST]` no título: limpe-os depois e exclua-os das métricas de negócio

Cada rodada fica em `resultados/<data>-<cenário>-<modo>/<rodada>/` com `carga.properties`, `resultados.jtl`,
`pipeline.csv`, o relatório HTML do JMeter e `inicio.txt`/`fim.txt`. O `carga.json` da bateria guarda as
premissas e a carga calculada.

### Análise

```sh
python3 analisar.py resultados/<bateria>/pipeline   # uma rodada: endpoints, pipeline, filas, custo e critérios
python3 comparar.py resultados/<bateria>            # degraus lado a lado (modo plataforma)
```

Espere uns 5 minutos depois do fim antes de analisar, para o CloudWatch consolidar. `--sem-aws` usa só os arquivos locais.

O `analisar.py` mostra:

- **Pipeline:** n, média, p50, p95 e p99 de finalizar até `NOTIFIED`; casos travados (status final diferente de
  `NOTIFIED` ou timeout) por desfecho; chegada medida contra a planejada e casos concluídos por minuto.
- **Filas SQS** (Q1, Q2, Q3 e as duas de retentativa): pico de mensagens e de idade, e tendência da idade
  (primeiro contra último terço do regime). Uma fila que cresce no regime indica capacidade menor que a chegada.
- **DLQ:** mensagens novas no intervalo.
- **Textract:** chamadas, throttling e erros por operação (`AWS/Textract`), além do tempo por job (`OcrLatencyMs`).
- **Gemini:** falhas por status (429, 5xx) a partir do log do mcp-ai e latência da análise (`AnalysisLatencyMs`).
- **E-mail:** enviados, falhas, limite do Gmail e DLQ, a partir do log do notification.
- **Custo estimado** da rodada (páginas enviadas × Textract + documentos × Gemini).
- **Critérios de aceite** com OK, FALHOU ou "-" (não medido).

As métricas `Triaige/MCP` exigem `CLOUDWATCH_METRICS_ENABLED=true` no mcp-ai; sem isso, essas linhas ficam sem dado.

## Critérios de aceite

| Critério | Meta | Fonte |
|---|---|---|
| Tempo médio de triagem | < 6 min (e p95 < 6 min) | `pipeline.csv` |
| Casos sem reprocessamento | > 85% | Log do mcp-ai (retentativa de OCR ou de geração do relatório); aproximado |
| Casos travados | 0 | `pipeline.csv` |
| Sucesso de envio | ≥ 98,5% | `NOTIFIED` / (`NOTIFIED` + `NOTIFICATION_FAILED`) |
| Erros 5xx na API | < 0,1% | JMeter e ALB |
| Fila em regime | idade da mensagem mais antiga estável | SQS (último terço ≤ 1,5× o primeiro, com folga de 60 s) |
| DLQ | 0 novas em regime normal | SQS |
| Throttling Textract/Gemini | 0 em regime; tolerado na rajada se não houver caso travado | CloudWatch e logs |

Uma rodada só **comprova o cenário** se passar em todos os critérios no modo `pipeline`. O modo `plataforma`
sozinho só sustenta a frase "a camada de API suporta o acesso do cenário".

## Arquivos

| Arquivo | Papel |
|---|---|
| `triaige-carga-mista.jmx` | Plano do JMeter: API escritório (consultas avulsas), painel admin, portal, polling simulado e fluxo completo em modelo aberto |
| `calc_carga.py` | Cenário → carga, plano por modo, custo e `carga.properties` de cada rodada |
| `rodar-cenario.sh` | Roda um cenário num modo |
| `analisar.py`, `comparar.py`, `metricas.py` | Análise de uma rodada, comparação entre rodadas e leitura comum (JTL, CSV, AWS) |
| `gerar_pdfs.py`, `dados/pdfs.csv` | PDFs sintéticos de 1, 5 e 10 páginas (sem dado pessoal) e o peso de cada um no sorteio (50/30/20%) |
| `rodar-degraus.sh` | Bateria antiga (20 a 150 usuários fixos), mantida só para comparação |

O JMX não tem credenciais: `host`, `apiToken` e `senha` vêm de `-J` ou das variáveis `TRIAIGE_*`
(o `rodar-cenario.sh` carrega o `.env`, e o token não vai parar no `jmeter.log`).
