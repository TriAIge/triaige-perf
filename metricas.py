"""Leitura dos resultados (JTL, pipeline.csv, carga.json) e das métricas da AWS.

Usado por analisar.py e comparar.py. Só usa a biblioteca padrão e o AWS CLI.
"""
import csv
import json
import re
import statistics
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REGIAO = "us-east-1"
FILAS = {  # nome curto -> fila (triaige-infra/terraform/modules/messaging/sqs)
    "Q1 docs-received": "triaige-docs-received",
    "Q2 docs-preprocessing": "triaige-docs-preprocessing",
    "Q3 results-ready": "triaige-results-ready",
    "retry notification": "triaige-notification-retry",
    "retry ocr": "triaige-mcp-ocr-retry",
}
LOG_MCP = "/triaige/triaige-srv-mcp-ai"
LOG_NOTIF = "/triaige/triaige-srv-notification"
NS_MCP = "Triaige/MCP"  # micrometer-registry-cloudwatch2 do mcp-ai (CLOUDWATCH_METRICS_ENABLED=true)


def pct(valores, p):
    valores = sorted(valores)
    if not valores:
        return 0
    return valores[max(0, min(len(valores) - 1, round(p / 100 * len(valores)) - 1))]


def ms_iso(texto):
    return int(datetime.fromisoformat(texto.strip().replace("Z", "+00:00")).timestamp() * 1000)


def janela(rodada):
    """(início, fim) em ISO UTC a partir de inicio.txt/fim.txt; fim None se a rodada não terminou."""
    rodada = Path(rodada)
    fim = rodada / "fim.txt"
    return (rodada / "inicio.txt").read_text().strip(), fim.read_text().strip() if fim.exists() else None


# ---------------------------------------------------------------- arquivos da rodada

def ler_jtl(caminho):
    por_label = defaultdict(lambda: {"t": [], "erros": 0, "codigos": defaultdict(int), "conex_lentas": 0})
    inicio = fim = None
    for row in csv.DictReader(open(caminho, encoding="utf-8")):
        ts, el = int(row["timeStamp"]), int(row["elapsed"])
        inicio = ts if inicio is None else min(inicio, ts)
        fim = ts + el if fim is None else max(fim, ts + el)
        x = por_label[row["label"]]
        x["t"].append(el)
        if int(row.get("Connect") or 0) >= 3000:
            x["conex_lentas"] += 1
        if row["success"] != "true":
            x["erros"] += 1
            x["codigos"][row["responseCode"]] += 1
    return por_label, inicio, fim


def eh_http(label):
    return not label.startswith("Pipeline")


def resumo_http(por_label):
    http = {k: v for k, v in por_label.items() if eh_http(k)}
    total = sum(len(x["t"]) for x in http.values())
    cinco = sum(n for x in http.values() for c, n in x["codigos"].items() if c.startswith("5"))
    erros = sum(x["erros"] for x in http.values())
    return {"total": total, "erros": erros, "5xx": cinco,
            "t": [t for x in http.values() for t in x["t"]],
            "conex_lentas": sum(x["conex_lentas"] for x in http.values())}


def ler_pipeline(caminho):
    """Linhas do pipeline.csv. Aceita o formato antigo (sem 'desfecho')."""
    caminho = Path(caminho)
    if not caminho.exists():
        return []
    linhas = []
    for r in csv.DictReader(open(caminho, encoding="utf-8")):
        if not r.get("desfecho"):
            r["desfecho"] = "OK" if r["status"] == "NOTIFIED" else "TIMEOUT"
        r["pipelineMs"] = int(r["pipelineMs"])
        linhas.append(r)
    return linhas


def ler_carga(rodada):
    """carga.json gerado pelo calc_carga.py (na rodada ou na pasta da bateria)."""
    for p in (Path(rodada) / "carga.json", Path(rodada).parent / "carga.json"):
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


def resumo_pipeline(linhas):
    if not linhas:
        return None
    ok = [r["pipelineMs"] / 1000 for r in linhas if r["desfecho"] == "OK"]
    desfechos = defaultdict(int)
    for r in linhas:
        desfechos[r["desfecho"] if r["desfecho"] != "FALHA" else r["status"]] += 1
    res = {"n": len(linhas), "ok": len(ok), "travados": len(linhas) - len(ok),
           "pct_travados": 100 * (len(linhas) - len(ok)) / len(linhas), "desfechos": dict(desfechos),
           "media": statistics.mean(ok) if ok else None, "p50": pct(ok, 50) if ok else None,
           "p95": pct(ok, 95) if ok else None, "p99": pct(ok, 99) if ok else None, "max": max(ok) if ok else None}
    if "chegadaMs" in linhas[0]:
        chegadas = sorted(int(r["chegadaMs"]) for r in linhas)
        fins = sorted(int(r["fimMs"]) for r in linhas if r["desfecho"] == "OK")
        janela_min = max((chegadas[-1] - chegadas[0]) / 60000, 1 / 60)
        res["chegada_min"] = len(chegadas) / janela_min if len(chegadas) > 1 else None
        # vazão concluída: NOTIFIED por minuto na janela de chegadas (ou entre términos, se maior); com poucos
        # concluídos, a janela entre o 1o e o último término sozinha inflaria a taxa
        res["concluidos_min"] = (len(fins) / max((fins[-1] - fins[0]) / 60000, janela_min)
                                 if len(fins) > 1 else 0.0)
        res["paginas"] = sum(int(r["paginas"] or 0) for r in linhas if int(r.get("finalizarMs") or 0) > 0)
        res["finalizados"] = sum(1 for r in linhas if int(r.get("finalizarMs") or 0) > 0)
        res["sessoes"] = {r["sessionId"] for r in linhas}
    return res


# ---------------------------------------------------------------- AWS

def aws(*args):
    r = subprocess.run(["aws", *args, "--region", REGIAO, "--output", "json"], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def serie(ns, metric, dims, estat, ini, fim, periodo=60):
    """Pontos (timestamp, valor) ordenados. Lista vazia se a métrica não existir ou faltar permissão."""
    if dims is None:
        return []
    r = aws("cloudwatch", "get-metric-statistics", "--namespace", ns, "--metric-name", metric,
            "--dimensions", *[f"Name={k},Value={v}" for k, v in dims.items()],
            "--start-time", ini, "--end-time", fim, "--period", str(periodo), "--statistics", estat)
    return sorted((p["Timestamp"], p[estat]) for p in (r or {}).get("Datapoints", []))


def valores(pts):
    return [v for _, v in pts]


def serie_custom(ns, metric, estat, ini, fim):
    """Soma das séries de uma métrica em todas as combinações de dimensão (tags do micrometer)."""
    r = aws("cloudwatch", "list-metrics", "--namespace", ns, "--metric-name", metric)
    total = defaultdict(float)
    for m in (r or {}).get("Metrics", []):
        dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
        cmd = ["cloudwatch", "get-metric-statistics", "--namespace", ns, "--metric-name", metric,
               "--start-time", ini, "--end-time", fim, "--period", "60", "--statistics", estat]
        if dims:
            cmd += ["--dimensions", *[f"Name={k},Value={v}" for k, v in dims.items()]]
        for p in (aws(*cmd) or {}).get("Datapoints", []):
            total[p["Timestamp"]] = max(total[p["Timestamp"]], p[estat]) if estat == "Maximum" else total[p["Timestamp"]] + p[estat]
    return sorted(total.items())


def instancias():
    out = []
    for res in (aws("ec2", "describe-instances", "--filters", "Name=tag:Name,Values=triaige-*",
                    "Name=instance-state-name,Values=running") or {}).get("Reservations", []):
        for i in res["Instances"]:
            nome = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), i["InstanceId"])
            out.append((nome, i["InstanceId"], i["InstanceType"]))
    return out


def alb_dim():
    lb = aws("elbv2", "describe-load-balancers", "--names", "alb-triaige")
    return {"LoadBalancer": lb["LoadBalancers"][0]["LoadBalancerArn"].split(":loadbalancer/")[1]} if lb else None


def tendencia(pts):
    """Compara o último terço com o primeiro: (média 1o terço, média último terço)."""
    v = valores(pts)
    if len(v) < 3:
        return None
    k = len(v) // 3
    return statistics.mean(v[:k]), statistics.mean(v[-k:])


def filas(ini, fim, fim_regime=None):
    """Por fila: pico de mensagens e de idade, tendência da idade no regime e mensagens novas na DLQ."""
    out = {}
    for nome, fila in FILAS.items():
        vis = serie("AWS/SQS", "ApproximateNumberOfMessagesVisible", {"QueueName": fila}, "Maximum", ini, fim)
        idade = serie("AWS/SQS", "ApproximateAgeOfOldestMessage", {"QueueName": fila}, "Maximum", ini, fim)
        dlq = valores(serie("AWS/SQS", "ApproximateNumberOfMessagesVisible", {"QueueName": fila + "-dlq"},
                            "Maximum", ini, fim))
        if not dlq:
            # O SQS não publica métricas de fila vazia e inativa: sem série, vale a contagem atual.
            url = aws("sqs", "get-queue-url", "--queue-name", fila + "-dlq")
            atr = url and aws("sqs", "get-queue-attributes", "--queue-url", url["QueueUrl"],
                              "--attribute-names", "ApproximateNumberOfMessages")
            if atr and atr["Attributes"]["ApproximateNumberOfMessages"] == "0":
                dlq = [0]
        regime = [(t, v) for t, v in idade if fim_regime is None or t <= fim_regime]
        out[nome] = {"vis_max": max(valores(vis), default=None), "idade_max": max(valores(idade), default=None),
                     "tendencia": tendencia(regime), "dlq_novas": (max(dlq) - dlq[0]) if dlq else None,
                     "dlq_total": dlq[-1] if dlq else None}
    return out


def fila_estavel(t):
    """Idade estável: o último terço do regime não passa de 1,5x o primeiro (com folga de 60 s)."""
    if not t:
        return None
    primeiro, ultimo = t
    return ultimo <= max(1.5 * primeiro, primeiro + 60)


def textract(ini, fim):
    out = {}
    for op in ("StartDocumentTextDetection", "GetDocumentTextDetection"):
        d = {"Operation": op}
        if not m_existe("AWS/Textract", "SuccessfulRequestCount", d, ini, fim):
            out[op] = None  # sem dado: sem chamadas no intervalo ou sem acesso ao CloudWatch
            continue
        out[op] = {
            "chamadas": sum(valores(serie("AWS/Textract", "SuccessfulRequestCount", d, "Sum", ini, fim))),
            "throttling": sum(valores(serie("AWS/Textract", "ThrottledCount", d, "Sum", ini, fim))),
            "erro_servidor": sum(valores(serie("AWS/Textract", "ServerErrorCount", d, "Sum", ini, fim))),
            "erro_cliente": sum(valores(serie("AWS/Textract", "UserErrorCount", d, "Sum", ini, fim))),
            "resp_ms": statistics.mean(valores(serie("AWS/Textract", "ResponseTime", d, "Average", ini, fim)) or [0]),
        }
    return out


def m_existe(ns, metric, dims, ini, fim):
    return bool(serie(ns, metric, dims, "SampleCount", ini, fim))


def acesso_aws():
    return aws("sts", "get-caller-identity") is not None


def metrica_mcp(nome, ini, fim):
    """Timer do micrometer: tenta <nome>.avg/.max/.count; volta None se o mcp-ai não publica métricas."""
    avg = valores(serie_custom(NS_MCP, nome + ".avg", "Average", ini, fim))
    mx = valores(serie_custom(NS_MCP, nome + ".max", "Maximum", ini, fim))
    cnt = valores(serie_custom(NS_MCP, nome + ".count", "Sum", ini, fim))
    if not (avg or mx or cnt):
        return None
    return {"media_ms": statistics.mean(avg) if avg else None, "max_ms": max(mx) if mx else None,
            "n": sum(cnt) if cnt else None}


def contador_mcp(nome, ini, fim):
    v = valores(serie_custom(NS_MCP, nome + ".count", "Sum", ini, fim)) or valores(serie_custom(NS_MCP, nome, "Sum", ini, fim))
    return sum(v) if v else None


def logs(grupo, padrao, ini_ms, fim_ms):
    """Mensagens do CloudWatch Logs que casam com o padrão; None se o grupo não puder ser lido."""
    r = aws("logs", "filter-log-events", "--log-group-name", grupo, "--start-time", str(ini_ms),
            "--end-time", str(fim_ms), "--filter-pattern", padrao, "--query", "events[].message")
    return r


def gemini(ini_ms, fim_ms):
    falhas = logs(LOG_MCP, '"Gemini call failed"', ini_ms, fim_ms)
    if falhas is None:
        return None
    status = defaultdict(int)
    for m in falhas:
        s = re.search(r"status=(\d{3})", m)
        status[s.group(1) if s else "rede/timeout"] += 1
    circuito = logs(LOG_MCP, '"Circuito aberto"', ini_ms, fim_ms) or []
    vazias = logs(LOG_MCP, '"Gemini returned an empty response"', ini_ms, fim_ms) or []
    novas = logs(LOG_MCP, '"tentando 1 nova geração"', ini_ms, fim_ms) or []
    recuperadas = logs(LOG_MCP, '"recuperado na nova geração"', ini_ms, fim_ms) or []
    reformat = logs(LOG_MCP, '"pedindo reformatação em modo JSON"', ini_ms, fim_ms) or []
    return {"falhas": len(falhas), "por_status": dict(status), "vazias": len(vazias),
            "novas_geracoes": len(novas), "recuperadas": len(recuperadas), "reformatacoes": len(reformat),
            "429": status.get("429", 0), "5xx": sum(n for s, n in status.items() if s.startswith("5")),
            "circuito_aberto": len(circuito)}


def email(ini_ms, fim_ms):
    enviados = logs(LOG_NOTIF, '"E-mail enviado com sucesso"', ini_ms, fim_ms)
    if enviados is None:
        return None
    return {"enviados": len(enviados),
            "falhas": len(logs(LOG_NOTIF, '"Falha ao enviar e-mail"', ini_ms, fim_ms) or []),
            "limite_gmail": len(logs(LOG_NOTIF, '"Limite de envio do Gmail atingido"', ini_ms, fim_ms) or []),
            "auth": len(logs(LOG_NOTIF, '"Falha de autenticacao SMTP"', ini_ms, fim_ms) or []),
            "dlq": len(logs(LOG_NOTIF, '"Tentativas de retry esgotadas"', ini_ms, fim_ms) or [])}


def reprocessados(sessoes, ini_ms, fim_ms):
    """Sessões do teste que tiveram retentativa de OCR ou de geração do relatório (log do mcp-ai)."""
    achadas = set()
    # Mensagens antes e depois da correção do mcp-ai (SPEC-correcoes-pipeline B1, B2, B5).
    for padrao in ('"OCR retry enqueued"', '"fora do formato esperado, tentando 1 retry"',
                   '"tentando 1 nova geração"', '"pedindo reformatação em modo JSON"',
                   '"Gemini returned an empty response"'):
        msgs = logs(LOG_MCP, padrao, ini_ms, fim_ms)
        if msgs is None:
            return None
        for m in msgs:
            s = re.search(r"sessionId=([0-9a-f-]{36})", m)
            if s and s.group(1) in sessoes:
                achadas.add(s.group(1))
    return achadas
