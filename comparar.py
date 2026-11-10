"""Compara as rodadas de uma bateria em degraus (uma pasta por nível).

Uso: python3 comparar.py resultados/<bateria>-degraus
"""
import csv
import json
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REGIAO = "us-east-1"
base = Path(sys.argv[1])


def pct(v, p):
    v = sorted(v)
    return v[max(0, min(len(v) - 1, round(p / 100 * len(v)) - 1))] if v else 0


def aws(*args):
    r = subprocess.run(["aws", *args, "--region", REGIAO, "--output", "json"], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout else None


def serie(ns, metric, dims, estat, ini, fim, periodo=60):
    r = aws("cloudwatch", "get-metric-statistics", "--namespace", ns, "--metric-name", metric,
            "--dimensions", *[f"Name={k},Value={v}" for k, v in dims.items()],
            "--start-time", ini, "--end-time", fim, "--period", str(periodo), "--statistics", estat)
    return [p[estat] for p in (r or {}).get("Datapoints", [])]


insts = []
for res in (aws("ec2", "describe-instances", "--filters", "Name=tag:Name,Values=triaige-*",
                "Name=instance-state-name,Values=running") or {}).get("Reservations", []):
    for i in res["Instances"]:
        nome = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), i["InstanceId"])
        insts.append((nome.replace("triaige-ec2-", ""), i["InstanceId"]))
lb = aws("elbv2", "describe-load-balancers", "--names", "alb-triaige")
alb_dim = {"LoadBalancer": lb["LoadBalancers"][0]["LoadBalancerArn"].split(":loadbalancer/")[1]} if lb else None

linhas, por_endpoint = [], {}
for rodada in sorted(p for p in base.iterdir() if (p / "resultados.jtl").exists()):
    t, erros, lentas, inicio, fim = [], 0, 0, None, None
    ep = defaultdict(list)
    codigos = defaultdict(int)
    for r in csv.DictReader(open(rodada / "resultados.jtl")):
        ts, el = int(r["timeStamp"]), int(r["elapsed"])
        inicio = ts if inicio is None else min(inicio, ts)
        fim = ts + el if fim is None else max(fim, ts + el)
        t.append(el)
        ep[r["label"]].append(el)
        if int(r["Connect"] or 0) >= 3000:
            lentas += 1
        if r["success"] != "true":
            erros += 1
            codigos[r["responseCode"]] += 1
    dur = (fim - inicio) / 1000
    por_endpoint[rodada.name] = {k: (pct(v, 50), pct(v, 95)) for k, v in ep.items()}

    ini, fim_cw = (rodada / "inicio.txt").read_text().strip(), (rodada / "fim.txt").read_text().strip()
    cpu = {nome: max(serie("AWS/EC2", "CPUUtilization", {"InstanceId": iid}, "Maximum", ini, fim_cw) or [0])
           for nome, iid in insts}
    alb_rt = serie("AWS/ApplicationELB", "TargetResponseTime", alb_dim, "Average", ini, fim_cw) if alb_dim else []
    alb_5xx = sum(serie("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", alb_dim, "Sum", ini, fim_cw)) if alb_dim else 0
    alb_elb5xx = sum(serie("AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", alb_dim, "Sum", ini, fim_cw)) if alb_dim else 0
    linhas.append((rodada.name, len(t), len(t) / dur, 100 * erros / len(t), dict(codigos), pct(t, 50), pct(t, 95),
                   pct(t, 99), 100 * lentas / len(t), 1000 * max(alb_rt or [0]), alb_5xx + alb_elb5xx, cpu))

print(f"{'Rodada':18} {'req':>6} {'req/s':>6} {'erro%':>6} {'p50':>6} {'p95':>6} {'p99':>6} {'conex>3s':>8} "
      f"{'ALB pior min':>12} {'5xx ALB':>7}  CPU pico")
for nome, n, rps, err, cod, p50, p95, p99, lent, alb, e5, cpu in linhas:
    cpu_txt = " ".join(f"{k}={v:.0f}%" for k, v in cpu.items())
    cod_txt = "" if not cod else " " + ",".join(f"{c}x{q}" for c, q in cod.items())
    print(f"{nome:18} {n:6} {rps:6.1f} {err:6.2f} {p50:6} {p95:6} {p99:6} {lent:7.1f}% {alb:10.0f}ms {e5:7.0f}  {cpu_txt}{cod_txt}")

print("\np50 / p95 (ms) por endpoint:")
nomes = list(por_endpoint)
labels = sorted({l for d in por_endpoint.values() for l in d})
print(f"{'Endpoint':28} " + " ".join(f"{n[:16]:>16}" for n in nomes))
for l in labels:
    print(f"{l[:28]:28} " + " ".join(
        f"{'%d / %d' % por_endpoint[n][l] if l in por_endpoint[n] else '-':>16}" for n in nomes))
