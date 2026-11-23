"""Resume uma rodada do JMeter + métricas da AWS no mesmo intervalo e confere os critérios de aceite.

Uso: python3 analisar.py resultados/<rodada>          (ex.: resultados/<data>-conservador-pipeline/pipeline)
     python3 analisar.py resultados/<rodada> --sem-aws  (só os arquivos locais)

Seções: endpoints (JTL), fluxo completo (pipeline.csv), filas SQS e DLQ, Textract, Gemini, e-mail,
custo estimado e critérios de aceite (SPEC-cenarios-bc §5).
"""
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import metricas as m

args = [a for a in sys.argv[1:] if not a.startswith("--")]
if not args:
    sys.exit(__doc__)
rodada = Path(args[0])
usar_aws = "--sem-aws" not in sys.argv


def br(v, casas=1):
    return "-" if v is None else f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ---------------------------------------------------------------- JMeter
por_label, inicio, fim = m.ler_jtl(rodada / "resultados.jtl")
dur_s = (fim - inicio) / 1000
http = m.resumo_http(por_label)
carga = m.ler_carga(rodada)
if carga:
    print(f"Cenário: {carga['descricao']} | modo {carga['modo']}")
print(f"Duração: {dur_s / 60:.1f} min | requisições HTTP: {http['total']} | vazão: {http['total'] / dur_s:.1f} req/s | "
      f"erros: {http['erros']} ({100 * http['erros'] / max(http['total'], 1):.2f}%) | 5xx: {http['5xx']}\n")
print(f"{'Endpoint':40} {'n':>6} {'req/s':>6} {'média':>7} {'p50':>6} {'p95':>6} {'p99':>6} {'máx':>7} {'erros':>6}")
for label, x in sorted(por_label.items(), key=lambda kv: -m.pct(kv[1]["t"], 95)):
    if not m.eh_http(label):
        continue
    t = x["t"]
    cod = "" if not x["codigos"] else " " + ",".join(f"{c}x{n}" for c, n in x["codigos"].items())
    print(f"{label[:40]:40} {len(t):6} {len(t) / dur_s:6.2f} {statistics.mean(t):7.0f} {m.pct(t, 50):6} "
          f"{m.pct(t, 95):6} {m.pct(t, 99):6} {max(t):7} {x['erros']:6}{cod}")

# ---------------------------------------------------------------- Pipeline
linhas = m.ler_pipeline(rodada / "pipeline.csv")
pipe = m.resumo_pipeline(linhas)
if pipe:
    print(f"\nFluxo completo (finalizar -> NOTIFIED), {pipe['n']} casos")
    print(f"  NOTIFIED: {pipe['ok']} | travados: {pipe['travados']} ({pipe['pct_travados']:.1f}%) "
          f"| desfechos: {', '.join(f'{k}={v}' for k, v in sorted(pipe['desfechos'].items()))}")
    if pipe["ok"]:
        print(f"  tempo (s): média {br(pipe['media'])} | p50 {br(pipe['p50'])} | p95 {br(pipe['p95'])} | "
              f"p99 {br(pipe['p99'])} | máx {br(pipe['max'])}")
    if "chegada_min" in pipe:
        esperado = carga["derivado"]["docs_min_pico"] if carga else None
        print(f"  chegada medida: {br(pipe['chegada_min'], 2)} docs/min"
              + (f" (planejada {br(esperado, 2)} no regime)" if esperado else "")
              + f" | concluídos: {br(pipe['concluidos_min'], 2)} docs/min | páginas enviadas: {pipe['paginas']}")
    lentos = sorted((r for r in linhas if r["desfecho"] != "OK"), key=lambda r: r["inicio"])[:15]
    for r in lentos:
        print(f"    {r['inicio']}  {r['protocolo']:18} {r['desfecho']:24} status={r['status'] or '-':22} "
              f"{r['pipelineMs'] / 1000:6.1f} s")

if not usar_aws:
    sys.exit(0)
ini, fim_txt = m.janela(rodada)
if not fim_txt:
    sys.exit("\n(rodada sem fim.txt: métricas da AWS omitidas)")
ini_ms, fim_ms = m.ms_iso(ini), m.ms_iso(fim_txt)
if not m.acesso_aws():
    print("\nATENÇÃO: sem credenciais AWS válidas; métricas da AWS ficam sem dado (rode com --sem-aws para omitir).")

# ---------------------------------------------------------------- Infra: CPU e ALB
print("\nCloudWatch no intervalo do teste:")
for nome, iid, tipo in m.instancias():
    cpu = m.valores(m.serie("AWS/EC2", "CPUUtilization", {"InstanceId": iid}, "Maximum", ini, fim_txt, 300))
    media = m.valores(m.serie("AWS/EC2", "CPUUtilization", {"InstanceId": iid}, "Average", ini, fim_txt, 300))
    if cpu:
        print(f"  CPU {nome:24} média {statistics.mean(media):5.1f}% | pico {max(cpu):5.1f}%  ({tipo})")
dim = m.alb_dim()
alb_5xx = None
if dim:
    req = m.valores(m.serie("AWS/ApplicationELB", "RequestCount", dim, "Sum", ini, fim_txt))
    rt = m.valores(m.serie("AWS/ApplicationELB", "TargetResponseTime", dim, "Average", ini, fim_txt))
    e5 = m.valores(m.serie("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", dim, "Sum", ini, fim_txt))
    e5lb = m.valores(m.serie("AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", dim, "Sum", ini, fim_txt))
    if req:
        alb_5xx = (sum(e5) + sum(e5lb), sum(req))
        print(f"  ALB requisições: {sum(req):.0f} | pico {max(req) / 60:.1f} req/s | tempo de resposta médio "
              f"{1000 * statistics.mean(rt):.0f} ms (pior minuto {1000 * max(rt):.0f} ms) | 5xx alvo: {sum(e5):.0f} | "
              f"5xx ALB: {sum(e5lb):.0f}")

# ---------------------------------------------------------------- Filas e DLQ
fim_regime = None
if carga and carga["modo"] != "plataforma":
    min_chegada = sum(t[1] for r in carga["rodadas"] for t in r["trechos"])
    fim_regime = datetime.fromisoformat(ini.replace("Z", "+00:00")) + timedelta(minutes=min_chegada)
filas = m.filas(ini, fim_txt, fim_regime and fim_regime.astimezone(timezone.utc).isoformat())
print("\nFilas SQS (pico no intervalo; tendência da idade = 1o terço -> último terço do regime):")
print(f"  {'Fila':24} {'msgs máx':>8} {'idade máx':>10} {'tendência da idade':>22} {'estável':>8} {'DLQ novas':>9}")
for nome, f in filas.items():
    t = f["tendencia"]
    tend = f"{t[0]:.0f}s -> {t[1]:.0f}s" if t else "-"
    est = m.fila_estavel(t)
    idade_txt = "-" if f["idade_max"] is None else br(f["idade_max"], 0) + "s"
    print(f"  {nome:24} {br(f['vis_max'], 0):>8} {idade_txt:>10} {tend:>22} "
          f"{'-' if est is None else ('sim' if est else 'NÃO'):>8} {br(f['dlq_novas'], 0):>9}")

# ---------------------------------------------------------------- Textract, Gemini, e-mail
tx = m.textract(ini, fim_txt)
ocr = m.metrica_mcp("OcrLatencyMs", ini, fim_txt)
print("\nTextract:")
for op, v in tx.items():
    if v is None:
        print(f"  {op:28} sem dado no CloudWatch")
        continue
    print(f"  {op:28} chamadas {v['chamadas']:6.0f} | throttling {v['throttling']:4.0f} | "
          f"erro servidor {v['erro_servidor']:3.0f} | erro cliente {v['erro_cliente']:3.0f} | resposta {v['resp_ms']:.0f} ms")
if ocr:
    print(f"  tempo por job de OCR (mcp-ai OcrLatencyMs): média {br(ocr['media_ms'] and ocr['media_ms'] / 1000)} s | "
          f"máx {br(ocr['max_ms'] and ocr['max_ms'] / 1000)} s")
else:
    print("  tempo por job de OCR: sem métrica Triaige/MCP (CLOUDWATCH_METRICS_ENABLED desligado no mcp-ai?)")

gm = m.gemini(ini_ms, fim_ms)
an = m.metrica_mcp("AnalysisLatencyMs", ini, fim_txt)
print("\nGemini:")
if gm is None:
    print(f"  sem acesso ao log {m.LOG_MCP}")
else:
    print(f"  chamadas com falha: {gm['falhas']} | 429: {gm['429']} | 5xx: {gm['5xx']} | circuito aberto: "
          f"{gm['circuito_aberto']} | por status: {gm['por_status'] or '-'}")
    print(f"  respostas vazias (nova tentativa): {gm['vazias']} | relatório reprovado -> nova geração: "
          f"{gm['novas_geracoes']} (recuperadas {gm['recuperadas']}) | reformatação JSON: {gm['reformatacoes']}")
retries = m.contador_mcp("GeminiRetryCount", ini, fim_txt)
sucesso = m.contador_mcp("AnalysisSuccessCount", ini, fim_txt)
if an:
    print(f"  análises: {br(an['n'], 0)} | latência da análise (inclui as chamadas ao Gemini): média "
          f"{br(an['media_ms'] and an['media_ms'] / 1000)} s | máx {br(an['max_ms'] and an['max_ms'] / 1000)} s"
          + (f" | retries {retries:.0f}" if retries is not None else ""))
    if sucesso and gm:
        print(f"  taxa de falha por chamada (aprox.): {100 * gm['falhas'] / (sucesso + gm['falhas']):.1f}%")

em = m.email(ini_ms, fim_ms)
print("\nE-mail (log do notification):")
if em is None:
    print(f"  sem acesso ao log {m.LOG_NOTIF}")
else:
    print(f"  enviados {em['enviados']} | falhas de envio {em['falhas']} | limite do Gmail atingido {em['limite_gmail']} | "
          f"falha de autenticação {em['auth']} | esgotou retentativas (DLQ) {em['dlq']}")

# ---------------------------------------------------------------- Custo
if pipe and "paginas" in pipe and carga:
    pr = carga["premissas"]
    textract_usd = pipe["paginas"] * pr["textract_usd_pagina"]
    gemini_usd = pipe["finalizados"] * pr["gemini_usd_doc"]
    est = carga.get("custo_estimado", {}).get("total_usd")
    print(f"\nCusto estimado da rodada: US$ {br(textract_usd + gemini_usd, 2)} "
          f"(Textract {pipe['paginas']} pág. = US$ {br(textract_usd, 2)}; Gemini {pipe['finalizados']} docs x "
          f"US$ {pr['gemini_usd_doc']} = US$ {br(gemini_usd, 2)}, premissa)"
          + (f" | previsto antes da rodada: US$ {br(est, 2)}" if est is not None else ""))

# ---------------------------------------------------------------- Critérios de aceite (§5)
if not pipe:
    print("\nSem fluxo completo nesta rodada: vale só como \"a camada de API suporta o acesso do cenário\".")
    sys.exit(0)

reproc = m.reprocessados(pipe.get("sessoes", set()), ini_ms, fim_ms) if pipe.get("sessoes") else None
crit = []


def criterio(nome, meta, valor, ok):
    crit.append((nome, meta, valor, ok))


criterio("Tempo médio de triagem", "< 6 min", f"{br(pipe['media'])} s",
         pipe["media"] is not None and pipe["media"] < 360)
criterio("p95 do tempo de triagem", "< 6 min", f"{br(pipe['p95'])} s", pipe["p95"] is not None and pipe["p95"] < 360)
if reproc is None:
    criterio("Casos sem reprocessamento", "> 85%", "sem log do mcp-ai", None)
else:
    base = pipe.get("finalizados") or pipe["n"]
    criterio("Casos sem reprocessamento", "> 85%", f"{100 * (base - len(reproc)) / base:.1f}% (aprox.: retry OCR/relatório)",
             (base - len(reproc)) / base > 0.85)
criterio("Casos travados", "0", f"{pipe['travados']} de {pipe['n']}", pipe["travados"] == 0)
fim_envio = pipe["ok"], pipe["desfechos"].get("NOTIFICATION_FAILED", 0)
if sum(fim_envio):
    taxa = 100 * fim_envio[0] / sum(fim_envio)
    criterio("Sucesso de envio", ">= 98,5%", f"{taxa:.1f}% (NOTIFIED / com desfecho de envio)", taxa >= 98.5)
else:
    criterio("Sucesso de envio", ">= 98,5%", "nenhum caso chegou ao envio", False)
taxa_5xx = 100 * http["5xx"] / max(http["total"], 1)
txt_5xx = f"{taxa_5xx:.3f}% JMeter"
if alb_5xx:
    taxa_alb = 100 * alb_5xx[0] / max(alb_5xx[1], 1)
    txt_5xx += f" | {taxa_alb:.3f}% ALB"
    taxa_5xx = max(taxa_5xx, taxa_alb)
criterio("Erros 5xx na API", "< 0,1%", txt_5xx, taxa_5xx < 0.1)
estaveis = {n: m.fila_estavel(f["tendencia"]) for n, f in filas.items()}
instaveis = [n for n, e in estaveis.items() if e is False]
sem_dado = all(e is None for e in estaveis.values())
criterio("Fila em regime", "idade estável", "sem métrica" if sem_dado else ("estável" if not instaveis else
         "crescendo: " + ", ".join(instaveis)), None if sem_dado else not instaveis)
dlqs = [f["dlq_novas"] for f in filas.values() if f["dlq_novas"] is not None]
criterio("DLQ", "0 novas", f"{sum(dlqs):.0f}" if dlqs else "sem métrica", sum(dlqs) == 0 if dlqs else None)
tx_ok = [v for v in tx.values() if v]
modo = carga["modo"] if carga else "pipeline"
if tx_ok and gm is not None:
    thr = sum(v["throttling"] for v in tx_ok) + gm["429"]
    criterio("Throttling Textract/Gemini", "0 em regime" + (" (tolerado na rajada)" if modo == "pico" else ""),
             f"Textract {sum(v['throttling'] for v in tx_ok):.0f} | Gemini 429 {gm['429']}",
             thr == 0 or (modo == "pico" and pipe["travados"] == 0))
else:
    criterio("Throttling Textract/Gemini", "0 em regime", "sem métrica do Textract ou log do mcp-ai", None)

print("\nCritérios de aceite (SPEC §5):")
print(f"  {'Critério':28} {'Meta':26} {'Medido':48} Resultado")
for nome, meta, valor, ok in crit:
    print(f"  {nome:28} {meta:26} {valor:48} {'-' if ok is None else ('OK' if ok else 'FALHOU')}")
todos = all(ok for *_, ok in crit)
print("\n" + ("Rodada COMPROVA o cenário: passou em todos os critérios." if todos else
              "Rodada NÃO comprova o cenário: há critério que falhou ou não foi medido."))
