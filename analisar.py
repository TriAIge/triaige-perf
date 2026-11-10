"""Resume uma rodada do JMeter + métricas do CloudWatch no mesmo intervalo.

Uso: python3 analisar.py resultados/<rodada>
"""
import csv
import json
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

rodada = Path(sys.argv[1])
REGIAO = "us-east-1"


def pct(valores, p):
    valores = sorted(valores)
    if not valores:
        return 0
    k = max(0, min(len(valores) - 1, round(p / 100 * len(valores)) - 1))
    return valores[k]


# ---- JMeter ----
por_label = defaultdict(lambda: {"t": [], "erros": 0, "codigos": defaultdict(int)})
inicio = fim = None
for row in csv.DictReader(open(rodada / "resultados.jtl")):
    ts, el = int(row["timeStamp"]), int(row["elapsed"])
    inicio = ts if inicio is None else min(inicio, ts)
    fim = ts + el if fim is None else max(fim, ts + el)
    x = por_label[row["label"]]
    x["t"].append(el)
    if row["success"] != "true":
        x["erros"] += 1
        x["codigos"][row["responseCode"]] += 1

dur_s = (fim - inicio) / 1000
total = sum(len(x["t"]) for x in por_label.values())
erros = sum(x["erros"] for x in por_label.values())
print(f"Duração: {dur_s / 60:.1f} min | requisições: {total} | vazão: {total / dur_s:.1f} req/s | "
      f"erros: {erros} ({100 * erros / total:.2f}%)\n")
print(f"{'Endpoint':40} {'n':>6} {'req/s':>6} {'média':>7} {'p50':>6} {'p95':>6} {'p99':>6} {'máx':>7} {'erros':>6}")
for label, x in sorted(por_label.items(), key=lambda kv: -pct(kv[1]["t"], 95)):
    t = x["t"]
    if label.startswith("Pipeline"):
        continue
    cod = "" if not x["codigos"] else " " + ",".join(f"{c}x{n}" for c, n in x["codigos"].items())
    print(f"{label[:40]:40} {len(t):6} {len(t) / dur_s:6.2f} {statistics.mean(t):7.0f} {pct(t, 50):6} "
          f"{pct(t, 95):6} {pct(t, 99):6} {max(t):7} {x['erros']:6}{cod}")

# ---- Pipeline completo ----
pcsv = rodada / "pipeline.csv"
if pcsv.exists():
    print("\nFluxo completo (finalizar -> status final):")
    for row in csv.DictReader(open(pcsv)):
        ms = int(row["pipelineMs"])
        print(f"  {row['inicio']}  {row['protocolo']}  {row['status']:22} {ms / 1000:6.1f} s")

# ---- CloudWatch ----
ini = (rodada / "inicio.txt").read_text().strip()
fim_txt = (rodada / "fim.txt").read_text().strip() if (rodada / "fim.txt").exists() else None
if not fim_txt:
    sys.exit(0)


def aws(*args):
    out = subprocess.run(["aws", *args, "--region", REGIAO, "--output", "json"], capture_output=True, text=True)
    return json.loads(out.stdout) if out.returncode == 0 and out.stdout else None


def stat(namespace, metric, dims, estat, periodo=60):
    r = aws("cloudwatch", "get-metric-statistics", "--namespace", namespace, "--metric-name", metric,
            "--dimensions", *[f"Name={k},Value={v}" for k, v in dims.items()],
            "--start-time", ini, "--end-time", fim_txt, "--period", str(periodo), "--statistics", estat)
    pts = sorted((p["Timestamp"], p[estat]) for p in (r or {}).get("Datapoints", []))
    return [v for _, v in pts]


print("\nCloudWatch no intervalo do teste:")
insts = aws("ec2", "describe-instances", "--filters", "Name=tag:Name,Values=triaige-*",
            "Name=instance-state-name,Values=running")
for res in (insts or {}).get("Reservations", []):
    for i in res["Instances"]:
        nome = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), i["InstanceId"])
        cpu = stat("AWS/EC2", "CPUUtilization", {"InstanceId": i["InstanceId"]}, "Maximum", 300)
        media = stat("AWS/EC2", "CPUUtilization", {"InstanceId": i["InstanceId"]}, "Average", 300)
        if cpu:
            print(f"  CPU {nome:24} média {statistics.mean(media):5.1f}% | pico {max(cpu):5.1f}%  ({i['InstanceType']})")

lb = aws("elbv2", "describe-load-balancers", "--names", "alb-triaige")
if lb:
    arn = lb["LoadBalancers"][0]["LoadBalancerArn"]
    dim = {"LoadBalancer": arn.split(":loadbalancer/")[1]}
    req = stat("AWS/ApplicationELB", "RequestCount", dim, "Sum")
    rt = stat("AWS/ApplicationELB", "TargetResponseTime", dim, "Average")
    e5 = stat("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", dim, "Sum")
    e5lb = stat("AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", dim, "Sum")
    if req:
        print(f"  ALB requisições: {sum(req):.0f} | pico {max(req) / 60:.1f} req/s | tempo de resposta médio "
              f"{1000 * statistics.mean(rt):.0f} ms (pior minuto {1000 * max(rt):.0f} ms) | 5xx alvo: {sum(e5):.0f} | "
              f"5xx ALB: {sum(e5lb):.0f}")
