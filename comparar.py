"""Compara as rodadas de uma bateria (uma pasta por rodada: degraus do modo plataforma, ou várias baterias).

Uso: python3 comparar.py resultados/<bateria>
     python3 comparar.py resultados/<bateria> --sem-aws
"""
import sys
from collections import defaultdict
from pathlib import Path

import metricas as m

args = [a for a in sys.argv[1:] if not a.startswith("--")]
if not args:
    sys.exit(__doc__)
base = Path(args[0])
usar_aws = "--sem-aws" not in sys.argv

insts = [(n.replace("triaige-ec2-", ""), iid) for n, iid, _ in m.instancias()] if usar_aws else []
alb = m.alb_dim() if usar_aws else None

linhas, por_endpoint, pipelines = [], {}, {}
for rodada in sorted(p for p in base.iterdir() if (p / "resultados.jtl").exists()):
    por_label, inicio, fim = m.ler_jtl(rodada / "resultados.jtl")
    dur = (fim - inicio) / 1000
    http = m.resumo_http(por_label)
    t = http["t"]
    codigos = defaultdict(int)
    for label, x in por_label.items():
        for c, n in (x["codigos"].items() if m.eh_http(label) else ()):
            codigos[c] += n
    por_endpoint[rodada.name] = {k: (m.pct(v["t"], 50), m.pct(v["t"], 95)) for k, v in por_label.items() if m.eh_http(k)}
    pipe = m.resumo_pipeline(m.ler_pipeline(rodada / "pipeline.csv"))
    if pipe:
        pipelines[rodada.name] = pipe

    cpu, alb_rt, alb_5xx, idade = {}, [], 0, {}
    ini, fim_cw = m.janela(rodada)
    if usar_aws and fim_cw:
        cpu = {nome: max(m.valores(m.serie("AWS/EC2", "CPUUtilization", {"InstanceId": iid}, "Maximum", ini, fim_cw)) or [0])
               for nome, iid in insts}
        alb_rt = m.valores(m.serie("AWS/ApplicationELB", "TargetResponseTime", alb, "Average", ini, fim_cw))
        alb_5xx = sum(m.valores(m.serie("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", alb, "Sum", ini, fim_cw))) + \
            sum(m.valores(m.serie("AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", alb, "Sum", ini, fim_cw)))
        if pipe:
            idade = {n: f["idade_max"] for n, f in m.filas(ini, fim_cw).items() if n.startswith(("Q2", "Q3"))}
            pipelines[rodada.name]["idade"] = idade
    linhas.append((rodada.name, len(t), len(t) / dur, 100 * http["erros"] / max(len(t), 1), dict(codigos),
                   m.pct(t, 50), m.pct(t, 95), m.pct(t, 99), 100 * http["conex_lentas"] / max(len(t), 1),
                   1000 * max(alb_rt or [0]), alb_5xx, cpu))

print(f"{'Rodada':18} {'req':>6} {'req/s':>6} {'erro%':>6} {'p50':>6} {'p95':>6} {'p99':>6} {'conex>3s':>8} "
      f"{'ALB pior min':>12} {'5xx ALB':>7}  CPU pico")
for nome, n, rps, err, cod, p50, p95, p99, lent, alb_ms, e5, cpu in linhas:
    cpu_txt = " ".join(f"{k}={v:.0f}%" for k, v in cpu.items())
    cod_txt = "" if not cod else " " + ",".join(f"{c}x{q}" for c, q in cod.items())
    print(f"{nome[:18]:18} {n:6} {rps:6.1f} {err:6.2f} {p50:6} {p95:6} {p99:6} {lent:7.1f}% {alb_ms:10.0f}ms "
          f"{e5:7.0f}  {cpu_txt}{cod_txt}")

if pipelines:
    print("\nFluxo completo:")
    print(f"{'Rodada':18} {'casos':>6} {'travados':>9} {'média s':>8} {'p95 s':>7} {'p99 s':>7} "
          f"{'chegada/min':>11} {'concl./min':>10}  idade máx Q2/Q3")
    for nome, p in pipelines.items():
        f = lambda v, c=1: "-" if v is None else f"{v:.{c}f}"
        idade = " / ".join(f"{v:.0f}s" if v is not None else "-" for v in p.get("idade", {}).values()) or "-"
        print(f"{nome[:18]:18} {p['n']:6} {p['travados']:4} ({p['pct_travados']:3.0f}%) {f(p['media']):>8} "
              f"{f(p['p95']):>7} {f(p['p99']):>7} {f(p.get('chegada_min'), 2):>11} {f(p.get('concluidos_min'), 2):>10}  {idade}")

print("\np50 / p95 (ms) por endpoint:")
nomes = list(por_endpoint)
labels = sorted({l for d in por_endpoint.values() for l in d})
print(f"{'Endpoint':28} " + " ".join(f"{n[:16]:>16}" for n in nomes))
for l in labels:
    print(f"{l[:28]:28} " + " ".join(
        f"{'%d / %d' % por_endpoint[n][l] if l in por_endpoint[n] else '-':>16}" for n in nomes))
