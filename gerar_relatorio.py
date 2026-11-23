"""Relatório consolidado dos testes de carga (HTML + PDF + figuras SVG + tabelas CSV), para o artigo do TCC.

Uso:
  python3 gerar_relatorio.py --saida relatorio resultados/<bateria> [resultados/<bateria> ...]
  python3 gerar_relatorio.py --saida relatorio --base resultados/<bateria-antes> resultados/<bateria> ...

Etapas:
  1. coleta: para cada rodada (subpasta com resultados.jtl), grava <rodada>/dados.json com JMeter, pipeline.csv
     e métricas da AWS. Rodadas já coletadas não vão de novo à AWS (--recoletar força).
  2. geração: <saida>/relatorio.html, <saida>/relatorio.pdf (Chrome headless), <saida>/figuras/*.svg e
     <saida>/tabelas/*.csv.

--base marca baterias de referência (antes das correções), mostradas só na comparação antes x depois.
Um arquivo <saida>/discussao.html, se existir, entra como seção de discussão.
"""
import csv
import html
import json
import math
import re
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import metricas as m

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CORES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
CORES_ESCURO = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]


def br(v, casas=1):
    return "-" if v is None else f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def esc(t):
    return html.escape(str(t))


# ================================================================ coleta

def coletar(rodada, recoletar=False):
    destino = rodada / "dados.json"
    if destino.exists() and not recoletar:
        return json.loads(destino.read_text(encoding="utf-8"))

    carga = m.ler_carga(rodada) or {}
    por_label, inicio, fim = m.ler_jtl(rodada / "resultados.jtl")
    dur_s = (fim - inicio) / 1000
    http = m.resumo_http(por_label)
    t = http["t"]
    d = {
        "bateria": rodada.parent.name, "rodada": rodada.name,
        "cenario": carga.get("cenario"), "descricao": carga.get("descricao"), "modo": carga.get("modo"),
        "multiplicador": (carga.get("premissas") or {}).get("multiplicador", 1.0),
        "derivado": carga.get("derivado"), "custo_previsto": carga.get("custo_estimado"),
        "props": next((r["props"] for r in carga.get("rodadas", []) if r["nome"] == rodada.name), None),
        "trechos": next((r.get("trechos") for r in carga.get("rodadas", []) if r["nome"] == rodada.name), None),
        "http": {"total": http["total"], "erros": http["erros"], "5xx": http["5xx"], "dur_s": dur_s,
                 "rps": http["total"] / dur_s, "p50": m.pct(t, 50), "p95": m.pct(t, 95), "p99": m.pct(t, 99),
                 "conex_lentas_pct": 100 * http["conex_lentas"] / max(len(t), 1)},
        "endpoints": {k: {"n": len(v["t"]), "p50": m.pct(v["t"], 50), "p95": m.pct(v["t"], 95), "erros": v["erros"]}
                      for k, v in por_label.items() if m.eh_http(k)},
    }

    linhas = m.ler_pipeline(rodada / "pipeline.csv")
    pipe = m.resumo_pipeline(linhas)
    if pipe:
        pipe.pop("sessoes", None)
        ini_round = m.ms_iso((rodada / "inicio.txt").read_text())
        pipe["casos"] = [{"chegada_s": (int(r["chegadaMs"]) - ini_round) / 1000 if r.get("chegadaMs") else None,
                          "fim_s": (int(r["fimMs"]) - ini_round) / 1000 if r.get("fimMs") and int(r["fimMs"]) else None,
                          "desfecho": r["desfecho"], "status": r["status"], "t_s": r["pipelineMs"] / 1000,
                          "paginas": r.get("paginas")} for r in linhas]
        d["pipeline"] = pipe

    crit = rodada / "analise.txt"
    if crit.exists():
        d["criterios"] = ler_criterios(crit.read_text(encoding="utf-8"))

    ini, fim_txt = m.janela(rodada)
    if fim_txt and m.acesso_aws():
        d["aws"] = coletar_aws(rodada, d, ini, fim_txt, linhas)
    if fim_txt and "aws" in d:  # só guarda em cache rodada encerrada e com dado da AWS
        destino.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def coletar_aws(rodada, d, ini, fim_txt, linhas):
    ini_ms, fim_ms = m.ms_iso(ini), m.ms_iso(fim_txt)
    a = {"cpu": {}}
    for nome, iid, tipo in m.instancias():
        pico = m.valores(m.serie("AWS/EC2", "CPUUtilization", {"InstanceId": iid}, "Maximum", ini, fim_txt, 300))
        media = m.valores(m.serie("AWS/EC2", "CPUUtilization", {"InstanceId": iid}, "Average", ini, fim_txt, 300))
        if pico:
            a["cpu"][nome.replace("triaige-ec2-", "")] = {"pico": max(pico), "media": statistics.mean(media), "tipo": tipo}
    dim = m.alb_dim()
    if dim:
        req = m.valores(m.serie("AWS/ApplicationELB", "RequestCount", dim, "Sum", ini, fim_txt))
        rt = m.valores(m.serie("AWS/ApplicationELB", "TargetResponseTime", dim, "Average", ini, fim_txt))
        e5 = m.valores(m.serie("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", dim, "Sum", ini, fim_txt))
        e5lb = m.valores(m.serie("AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", dim, "Sum", ini, fim_txt))
        if req:
            a["alb"] = {"req": sum(req), "rps_pico": max(req) / 60, "rt_ms": 1000 * statistics.mean(rt),
                        "rt_pior_ms": 1000 * max(rt), "5xx": sum(e5) + sum(e5lb)}
    if d.get("pipeline"):
        fim_regime = None
        if d.get("trechos"):
            minutos = sum(tr[1] for tr in d["trechos"])
            fim_regime = (datetime.fromisoformat(ini.replace("Z", "+00:00")) + timedelta(minutes=minutos)).isoformat()
        filas = m.filas(ini, fim_txt, fim_regime)
        a["filas"] = {k: {**v, "estavel": m.fila_estavel(v["tendencia"])} for k, v in filas.items()}
        q2 = next(f for k, f in m.FILAS.items() if k.startswith("Q2"))
        t0 = datetime.fromisoformat(ini.replace("Z", "+00:00"))
        a["q2_idade"] = [((datetime.fromisoformat(ts.replace("Z", "+00:00")) - t0).total_seconds() / 60, v)
                         for ts, v in m.serie("AWS/SQS", "ApproximateAgeOfOldestMessage", {"QueueName": q2},
                                              "Maximum", ini, fim_txt)]
        a["textract"] = m.textract(ini, fim_txt)
        a["gemini"] = m.gemini(ini_ms, fim_ms)
        a["email"] = m.email(ini_ms, fim_ms)
        a["analise_ms"] = m.metrica_mcp("AnalysisLatencyMs", ini, fim_txt)
        a["ocr_ms"] = m.metrica_mcp("OcrLatencyMs", ini, fim_txt)
        nulos = m.logs(m.LOG_MCP, '"NullPointerException"', ini_ms, fim_ms)
        a["npe"] = None if nulos is None else len(nulos)
        dup_q2 = m.logs(m.LOG_MCP, '"Duplicate Q2 message discarded"', ini_ms, fim_ms)
        a["q2_descartadas"] = None if dup_q2 is None else len(dup_q2)
        adiantadas = m.logs(m.LOG_MCP, '"ahead of finalize commit"', ini_ms, fim_ms)
        a["q2_adiantadas"] = None if adiantadas is None else len(adiantadas)
        dup_cache = m.logs(m.LOG_MCP, '"uk_jurisprudence_cache_query_hash"', ini_ms, fim_ms)
        a["cache_duplicado"] = None if dup_cache is None else len(
            [x for x in dup_cache if "Duplicate entry" in x and "jurisprudence_query falhou" not in x])
        invalid = m.logs(m.LOG_MCP, '"INVALID_REPORT_FORMAT"', ini_ms, fim_ms)
        a["invalid_report"] = None if invalid is None else len(invalid)
    return a


def ler_criterios(texto):
    bloco = texto.split("Critérios de aceite", 1)
    if len(bloco) < 2:
        return None
    out = []
    for linha in bloco[1].splitlines()[2:]:
        if not linha.strip() or linha.startswith("Rodada"):
            break
        cols = re.split(r"\s{2,}", linha.strip())
        if len(cols) >= 4:
            out.append({"criterio": cols[0], "meta": cols[1], "medido": " ".join(cols[2:-1]), "resultado": cols[-1]})
    return out


# ================================================================ SVG

class Grafico:
    """Gráfico SVG com eixos, grade recessiva e marcas. Cores por var(--sN, fallback) para funcionar
    inline no HTML (com tema escuro) e como arquivo .svg avulso."""

    def __init__(self, largura=640, altura=320, esq=56, dir=16, topo=16, base=44):
        self.w, self.h, self.e, self.d, self.t, self.b = largura, altura, esq, dir, topo, base
        self.partes = []

    @property
    def pw(self):
        return self.w - self.e - self.d

    @property
    def ph(self):
        return self.h - self.t - self.b

    def escala(self, x0, x1, y0, y1):
        self.x0, self.x1, self.y0, self.y1 = x0, x1, y0, y1

    def X(self, v):
        return self.e + (v - self.x0) / ((self.x1 - self.x0) or 1) * self.pw

    def Y(self, v):
        return self.t + self.ph - (v - self.y0) / ((self.y1 - self.y0) or 1) * self.ph

    def eixo_y(self, ticks, fmt=lambda v: br(v, 0), titulo=None):
        for v in ticks:
            y = self.Y(v)
            self.partes.append(f'<line x1="{self.e}" x2="{self.w - self.d}" y1="{y:.1f}" y2="{y:.1f}" '
                               f'stroke="var(--grade,#e4e3df)" stroke-width="1"/>')
            self.partes.append(f'<text x="{self.e - 8}" y="{y + 4:.1f}" text-anchor="end" class="tick" '
                               f'fill="var(--txt2,#52514e)" font-size="11">{esc(fmt(v))}</text>')
        if titulo:
            self.partes.append(f'<text x="14" y="{self.t + self.ph / 2:.0f}" transform="rotate(-90 14 {self.t + self.ph / 2:.0f})" '
                               f'text-anchor="middle" fill="var(--txt2,#52514e)" font-size="11">{esc(titulo)}</text>')

    def eixo_x(self, ticks, fmt=lambda v: br(v, 0), titulo=None):
        yb = self.t + self.ph
        self.partes.append(f'<line x1="{self.e}" x2="{self.w - self.d}" y1="{yb}" y2="{yb}" stroke="var(--eixo,#a9a8a2)"/>')
        for v in ticks:
            x = self.X(v) if not isinstance(v, tuple) else v[0]
            rot = fmt(v) if not isinstance(v, tuple) else v[1]
            self.partes.append(f'<text x="{x:.1f}" y="{yb + 16}" text-anchor="middle" fill="var(--txt2,#52514e)" '
                               f'font-size="11">{esc(rot)}</text>')
        if titulo:
            self.partes.append(f'<text x="{self.e + self.pw / 2:.0f}" y="{self.h - 6}" text-anchor="middle" '
                               f'fill="var(--txt2,#52514e)" font-size="11">{esc(titulo)}</text>')

    def linha(self, pts, i, tracejada=False, marcadores=True, dica=None, rotulo=None):
        if not pts:
            return
        d = " ".join(f"{'M' if k == 0 else 'L'}{self.X(x):.1f},{self.Y(y):.1f}" for k, (x, y) in enumerate(pts))
        cor = cor_var(i)
        self.partes.append(f'<path d="{d}" fill="none" stroke="{cor}" stroke-width="2" stroke-linejoin="round"'
                           + (' stroke-dasharray="6 4"' if tracejada else "") + "/>")
        if marcadores:
            for k, (x, y) in enumerate(pts):
                tip = dica(k, x, y) if dica else ""
                self.partes.append(f'<circle cx="{self.X(x):.1f}" cy="{self.Y(y):.1f}" r="4.5" fill="{cor}" '
                                   f'stroke="var(--fundo,#fcfcfb)" stroke-width="2"><title>{esc(tip)}</title></circle>')
        if rotulo:
            x, y = pts[-1]
            self.partes.append(f'<text x="{self.X(x) + 8:.1f}" y="{self.Y(y) + 4:.1f}" font-size="11" '
                               f'fill="var(--txt1,#0b0b0b)">{esc(rotulo)}</text>')

    def referencia(self, pts, rotulo):
        d = " ".join(f"{'M' if k == 0 else 'L'}{self.X(x):.1f},{self.Y(y):.1f}" for k, (x, y) in enumerate(pts))
        self.partes.append(f'<path d="{d}" fill="none" stroke="var(--txt3,#8a8984)" stroke-width="1.5" stroke-dasharray="3 4"/>')
        x, y = pts[-1]
        self.partes.append(f'<text x="{self.X(x) - 4:.1f}" y="{self.Y(y) - 6:.1f}" text-anchor="end" font-size="11" '
                           f'fill="var(--txt2,#52514e)">{esc(rotulo)}</text>')

    def barra(self, x, largura, valor, i, dica="", rotulo=None):
        y0, y1 = self.Y(max(self.y0, 0)), self.Y(valor)
        h = max(y0 - y1, 0)
        r = min(4, largura / 2, h)
        cor = cor_var(i)
        if h > 0:
            self.partes.append(
                f'<path d="M{x:.1f},{y0:.1f} V{y1 + r:.1f} Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f} H{x + largura - r:.1f} '
                f'Q{x + largura:.1f},{y1:.1f} {x + largura:.1f},{y1 + r:.1f} V{y0:.1f} Z" fill="{cor}">'
                f'<title>{esc(dica)}</title></path>')
        if rotulo is not None:
            self.partes.append(f'<text x="{x + largura / 2:.1f}" y="{y1 - 5:.1f}" text-anchor="middle" font-size="10.5" '
                               f'fill="var(--txt1,#0b0b0b)">{esc(rotulo)}</text>')

    def svg(self, titulo):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" role="img" '
                f'aria-label="{esc(titulo)}" font-family="Helvetica, Arial, sans-serif" class="graf" style="max-width:{self.w}px">'
                f'<rect width="{self.w}" height="{self.h}" fill="var(--fundo,#fcfcfb)"/>' + "".join(self.partes) + "</svg>")


def cor_var(i):
    return f"var(--s{i + 1},{CORES[i]})"


def ticks(maximo, n=5):
    if maximo <= 0:
        return [0, 1]
    bruto = maximo / n
    mag = 10 ** math.floor(math.log10(bruto))
    passo = next(p * mag for p in (1, 2, 2.5, 5, 10) if p * mag >= bruto)
    out, v = [], 0.0
    while v <= maximo * 1.0001 + passo:
        out.append(round(v, 6))
        if v >= maximo:
            break
        v += passo
    return out


def legenda(series):
    itens = "".join(f'<span class="leg-item"><span class="leg-cor" style="background:{cor_var(i)}"></span>{esc(n)}</span>'
                    for i, n in enumerate(series))
    return f'<div class="legenda">{itens}</div>'


def barras_agrupadas(grupos, series, valores, titulo_y, fmt=lambda v: br(v, 0), rotular=True, largura=640, altura=300):
    """grupos: rótulos no eixo x; series: nomes; valores[g][s] (None = sem dado)."""
    g = Grafico(largura, altura)
    vmax = max((v for linha in valores for v in linha if v is not None), default=1)
    tk = ticks(vmax * 1.12)
    g.escala(0, 1, 0, tk[-1])
    g.eixo_y(tk, fmt=lambda v: br(v, 0) if v >= 10 or v == 0 else br(v, 1), titulo=titulo_y)
    passo = g.pw / len(grupos)
    larg = min(28, (passo * 0.72 - 2 * (len(series) - 1)) / len(series))
    xt = []
    for gi, nome in enumerate(grupos):
        bloco = len(series) * larg + 2 * (len(series) - 1)
        x = g.e + gi * passo + (passo - bloco) / 2
        for si, s in enumerate(series):
            v = valores[gi][si]
            if v is not None:
                g.barra(x, larg, v, si, dica=f"{nome} · {s}: {fmt(v)}", rotulo=fmt(v) if rotular else None)
            x += larg + 2
        xt.append((g.e + gi * passo + passo / 2, nome))
    g.eixo_x(xt)
    return g


# ================================================================ relatório

def nome_degrau(r):
    r = r.split("-", 1)[-1]
    return "pico" if r == "pico" else f"{int(r.replace('pct', ''))}%"


def rotulo(d):
    mult = d.get("multiplicador") or 1
    base = "Conservador" if mult == 1 else f"Estresse {br(mult, 0)}×"
    if d.get("cenario") == "smoke":
        base = "Smoke"
    return f"{base} · {d['modo'] if d['rodada'] in ('pipeline', 'pico') else d['rodada']}"


def gerar(saida, rodadas, base):
    figs, tabs = saida / "figuras", saida / "tabelas"
    figs.mkdir(parents=True, exist_ok=True)
    tabs.mkdir(parents=True, exist_ok=True)
    secoes = []
    figuras = []

    def figura(nome, titulo, svg, nota, leg=None):
        (figs / f"{nome}.svg").write_text(svg, encoding="utf-8")
        n = len(figuras) + 1
        figuras.append(nome)
        return (f'<figure id="{nome}"><figcaption><b>Figura {n}</b> — {esc(titulo)}</figcaption>'
                f'{leg or ""}{svg}<p class="fonte">{nota}</p></figure>')

    ntab = [0]

    def tabela(nome, titulo, cab, linhas, nota=""):
        with open(tabs / f"{nome}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cab)
            w.writerows([[re.sub("<[^>]+>", "", str(c)) for c in l] for l in linhas])
        ntab[0] += 1
        corpo = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in l) + "</tr>" for l in linhas)
        return (f'<table id="{nome}"><caption><b>Tabela {ntab[0]}</b> — {esc(titulo)}</caption><thead><tr>'
                + "".join(f"<th>{esc(c)}</th>" for c in cab) + f"</tr></thead><tbody>{corpo}</tbody></table>"
                + (f'<p class="fonte">{nota}</p>' if nota else ""))

    plat = [d for d in rodadas if d["modo"] == "plataforma" and d.get("cenario") != "smoke"]
    pipes = [d for d in rodadas if d["modo"] in ("pipeline", "pico") and d.get("pipeline") and d.get("cenario") != "smoke"]
    escada = sorted([d for d in pipes if d["modo"] == "pipeline"], key=lambda d: d["multiplicador"])
    base_pipes = [d for d in base if d.get("pipeline")]
    base_plat = [d for d in base if d["modo"] == "plataforma"]

    # ---------- Tabela: carga derivada
    cen = {}
    for d in rodadas:
        if d.get("derivado") and d.get("cenario") != "smoke":
            cen.setdefault(d["multiplicador"], d)
    linhas = []
    for mult, d in sorted(cen.items()):
        dv = d["derivado"]
        linhas.append([("Conservador (500 × Essencial)" if mult == 1 else f"{br(mult, 0)} × conservador"),
                       br(dv["escritorios"], 0), br(dv["usuarios_nomeados"], 0), br(dv["portal_simult"], 0),
                       br(dv["docs_dia"], 0), br(dv["docs_min_pico"], 2), br(dv["docs_em_voo"], 1),
                       br(dv["reqs_polling"], 1)])
    secoes.append(("Carga derivada do Business Case", tabela(
        "t1_carga", "Carga derivada de cada cenário (modelo do Business Case)",
        ["Cenário", "Escritórios", "Usuários nomeados", "Portal simultâneo", "Docs/dia útil", "Chegada no pico (docs/min)",
         "Docs em processamento (L = λ·W)", "Polling (req/s)"], linhas,
        "Premissas: uso de 100% da franquia, 22 dias úteis, 9 h/dia, fator de pico 2,0, 15% dos usuários simultâneos, "
        "tempo de pipeline de referência 5 min e polling a cada 5 s (calc_carga.py).")))

    # ---------- Plataforma
    if plat:
        degraus = sorted(plat, key=lambda d: d["rodada"])
        nomes = [nome_degrau(d["rodada"]) for d in degraus]
        ant = {d["rodada"]: d for d in base_plat}
        series = (["16/11 (antes)", "18/11 (depois)"] if ant else ["18/11"])
        def par(campo_fn):
            return [[campo_fn(ant.get(d["rodada"])) if ant else None, campo_fn(d)] if ant else [campo_fn(d)] for d in degraus]
        v_rps = par(lambda d: d and d["http"]["rps"])
        v_alb = par(lambda d: d and (d.get("aws") or {}).get("alb", {}).get("rt_ms"))
        html_p = figura("f1_plataforma_vazao", "Vazão por degrau de carga da camada de API (pipeline desligado)",
                        barras_agrupadas(nomes, series, v_rps, "requisições/s", fmt=lambda v: br(v, 1)).svg("Vazão por degrau"),
                        "Fonte: JMeter. Cada degrau corresponde a uma fração do portal simultâneo do cenário conservador "
                        "(150 usuários), mais o polling equivalente aos documentos em processamento.", legenda(series))
        html_p += figura("f2_plataforma_alb", "Tempo médio de resposta no balanceador (ALB) por degrau",
                         barras_agrupadas(nomes, series, v_alb, "ms", fmt=lambda v: br(v, 0)).svg("Tempo no ALB"),
                         "Fonte: CloudWatch AWS/ApplicationELB TargetResponseTime (média por minuto). Mede o servidor, "
                         "sem a latência de rede entre o gerador de carga e a AWS.", legenda(series))
        linhas = []
        for d in degraus:
            a = d.get("aws") or {}
            cpu = a.get("cpu") or {}
            linhas.append([nome_degrau(d["rodada"]), br(d["http"]["total"], 0), br(d["http"]["rps"], 1),
                           f"{100 * d['http']['erros'] / max(d['http']['total'], 1):.2f}%".replace(".", ","),
                           br(d["http"]["p50"], 0), br(d["http"]["p95"], 0),
                           br(a.get("alb", {}).get("rt_ms"), 0), br(a.get("alb", {}).get("rt_pior_ms"), 0),
                           br(max((c["pico"] for c in cpu.values()), default=None), 0) + "%"])
        html_p += tabela("t2_plataforma", "Modo plataforma do cenário conservador, por degrau",
                         ["Degrau", "Requisições", "req/s", "Erros", "p50 cliente (ms)", "p95 cliente (ms)",
                          "ALB médio (ms)", "ALB pior minuto (ms)", "CPU pico"], linhas,
                         "p50/p95 do cliente incluem a rede entre o gerador de carga e a região us-east-1.")
        secoes.append(("Camada de API (modo plataforma)", html_p))

    # ---------- Escalabilidade do pipeline
    if escada:
        g = Grafico(640, 340, dir=90)
        xs = [d["pipeline"].get("chegada_min") or d["derivado"]["docs_min_pico"] for d in escada]
        ymax = max([d["pipeline"]["p95"] or 0 for d in escada] + [360])
        tk = ticks(ymax * 1.1)
        xt = ticks(max(xs) * 1.1)
        g.escala(0, xt[-1], 0, tk[-1])
        g.eixo_y(tk, titulo="tempo de triagem (s)")
        g.eixo_x(xt, titulo="taxa de chegada medida (documentos/min)")
        g.referencia([(0, 360), (xt[-1], 360)], "meta: 6 min")
        for i, (campo, nome) in enumerate([("p50", "p50"), ("p95", "p95")]):
            pts = [(x, d["pipeline"][campo]) for x, d in zip(xs, escada)
                   if d["pipeline"][campo] is not None and d["pipeline"]["ok"] >= d["pipeline"]["n"] / 2]
            g.linha(pts, i, dica=lambda k, x, y, e=escada, n=nome: f"{rotulo(e[k])}: {n} = {br(y)} s a {br(x, 1)} docs/min",
                    rotulo=nome)
        for x, d in zip(xs, escada):
            g.partes.append(f'<text x="{g.X(x):.1f}" y="{g.t + g.ph - 6:.1f}" text-anchor="middle" font-size="10" '
                            f'fill="var(--txt3,#8a8984)">{br(d["multiplicador"], 0)}×</text>')
        html_e = figura("f3_escalabilidade_triagem", "Tempo de triagem (finalizar → e-mail enviado) em função da taxa de chegada",
                        g.svg("Tempo de triagem x chegada"),
                        "Fonte: pipeline.csv (JMeter). Cada ponto é uma rodada do modo pipeline (1× = cenário conservador). "
                        "Só casos concluídos (NOTIFIED) e só rodadas em que ao menos metade dos casos foi notificada: a partir de 2× "
                        "o limite diário do e-mail impediu a notificação, e o percentil de poucos casos não é representativo.",
                        legenda(["p50", "p95"]))

        g = Grafico(640, 340, dir=90)
        conc = [d["pipeline"]["ok"] / (d["http"]["dur_s"] / 60) for d in escada]
        anal = [((d.get("aws") or {}).get("analise_ms") or {}).get("n", 0) / (d["http"]["dur_s"] / 60) for d in escada]
        lim = ticks(max(xs + conc + anal) * 1.1)
        g.escala(0, lim[-1], 0, lim[-1])
        g.eixo_y(lim, titulo="concluídos (documentos/min)")
        g.eixo_x(lim, titulo="taxa de chegada medida (documentos/min)")
        g.referencia([(0, 0), (lim[-1], lim[-1])], "concluídos = chegada")
        g.linha(list(zip(xs, anal)), 0, rotulo="análises",
                dica=lambda k, x, y, e=escada: f"{rotulo(e[k])}: chegada {br(x, 1)} → análises {br(y, 1)}/min")
        g.linha(list(zip(xs, conc)), 1, rotulo="notificados",
                dica=lambda k, x, y, e=escada: f"{rotulo(e[k])}: chegada {br(x, 1)} → notificados {br(y, 1)}/min")
        html_e += figura("f4_vazao_concluida", "Vazão do pipeline: análises concluídas e casos notificados por minuto contra a taxa de chegada",
                         g.svg("Vazão concluída x chegada"),
                         "Análises: contagem de análises do mcp-ai (Micrometer/CloudWatch) dividida pela duração da rodada. "
                         "Notificados: casos que chegaram a NOTIFIED (JMeter). Pontos abaixo da diagonal indicam acúmulo; "
                         "a distância entre as duas curvas a 2× é o limite diário do e-mail.",
                         legenda(["análises concluídas", "notificados (NOTIFIED)"]))

        # chegadas x conclusões acumuladas (small multiples)
        mult_svgs = []
        for d in sorted(pipes, key=lambda d: (d["modo"] != "pipeline", d["multiplicador"])):
            casos = d["pipeline"]["casos"]
            cheg = sorted(c["chegada_s"] / 60 for c in casos if c["chegada_s"] is not None)
            fins = sorted(c["fim_s"] / 60 for c in casos if c["fim_s"] is not None and c["desfecho"] == "OK")
            if not cheg:
                continue
            g = Grafico(320, 220, esq=44, base=36)
            tmax = max(cheg + fins)
            xt, yt = ticks(tmax), ticks(len(cheg))
            g.escala(0, xt[-1], 0, yt[-1])
            g.eixo_y(yt)
            g.eixo_x(xt, titulo="minutos")
            g.linha([(x, k + 1) for k, x in enumerate(cheg)], 0, marcadores=False)
            g.linha([(x, k + 1) for k, x in enumerate(fins)], 1, marcadores=False)
            mult_svgs.append(f'<div class="mini"><div class="mini-tit">{esc(rotulo(d))}</div>{g.svg(rotulo(d))}</div>')
        svg_grade = f'<div class="grade-mini">{"".join(mult_svgs)}</div>'
        (figs / "f5_acumulado.html").write_text(svg_grade, encoding="utf-8")
        figuras.append("f5")
        html_e += (f'<figure id="f5"><figcaption><b>Figura {len(figuras)}</b> — Documentos que chegaram e documentos '
                   f'concluídos, acumulados ao longo de cada rodada</figcaption>{legenda(["chegadas", "concluídos (NOTIFIED)"])}'
                   f'{svg_grade}<p class="fonte">A distância vertical entre as curvas é o número de documentos em '
                   f'processamento; no fim da rodada as curvas devem se encontrar (drenagem completa).</p></figure>')

        # idade da mensagem mais antiga na Q2
        series_q2 = [(rotulo(d), d["aws"]["q2_idade"]) for d in sorted(pipes, key=lambda d: (d["modo"] != "pipeline", d["multiplicador"]))
                     if (d.get("aws") or {}).get("q2_idade")]
        if series_q2:
            g = Grafico(640, 320)
            tmax = max(x for _, s in series_q2 for x, _ in s)
            vmax = max(v for _, s in series_q2 for _, v in s)
            xt, yt = ticks(tmax), ticks(max(vmax, 10) * 1.1)
            g.escala(0, xt[-1], 0, yt[-1])
            g.eixo_y(yt, titulo="idade máx. (s)")
            g.eixo_x(xt, titulo="minutos desde o início da rodada")
            for i, (nome, s) in enumerate(series_q2[:8]):
                g.linha(s, i, marcadores=False)
            html_e += figura("f6_fila_q2", "Idade da mensagem mais antiga na fila de pré-processamento (Q2)",
                             g.svg("Idade Q2"),
                             "Fonte: CloudWatch AWS/SQS ApproximateAgeOfOldestMessage (máximo por minuto). Idade crescente "
                             "durante o regime indica que o consumo não acompanha a chegada.",
                             legenda([n for n, _ in series_q2[:8]]))

        # CPU por instância
        com_cpu = [d for d in sorted(pipes, key=lambda d: (d["modo"] != "pipeline", d["multiplicador"])) if (d.get("aws") or {}).get("cpu")]
        if com_cpu:
            inst = sorted({k for d in com_cpu for k in d["aws"]["cpu"]})
            vals = [[d["aws"]["cpu"].get(k, {}).get("pico") for k in inst] for d in com_cpu]
            html_e += figura("f7_cpu", "Pico de CPU por instância em cada rodada com pipeline",
                             barras_agrupadas([rotulo(d).replace(" · ", "\n").split("\n")[0] + ("" if d["modo"] == "pipeline" else " (pico)")
                                               for d in com_cpu], inst, vals, "CPU (%)", fmt=lambda v: br(v, 0)).svg("CPU"),
                             "Fonte: CloudWatch AWS/EC2 CPUUtilization (máximo em janelas de 5 min).", legenda(inst))

        # tabela resumo (slide 18)
        linhas = []
        for d in sorted(pipes, key=lambda d: (d["modo"] != "pipeline", d["multiplicador"])):
            p, a = d["pipeline"], d.get("aws") or {}
            cpu = a.get("cpu") or {}
            custo = None
            if d.get("custo_previsto"):
                custo = p.get("paginas", 0) * 0.0015 + p.get("finalizados", 0) * 0.01
            linhas.append([rotulo(d), br(p.get("chegada_min"), 1), br(p["n"], 0),
                           f"{p['travados']} ({br(p['pct_travados'], 1)}%)",
                           br(p["media"]), br(p["p95"]), br(p["p99"]), br(p["ok"] / (d["http"]["dur_s"] / 60), 1),
                           br((a.get("filas") or {}).get(next((k for k in (a.get("filas") or {}) if k.startswith("Q2")), ""), {}).get("idade_max"), 0),
                           br(max((c["pico"] for c in cpu.values()), default=None), 0) + "%",
                           br(d["http"]["total"], 0), f"{100 * d['http']['5xx'] / max(d['http']['total'], 1):.3f}%".replace(".", ","),
                           "US$ " + br(custo, 2)])
        html_e += tabela("t3_resumo_pipeline", "Resumo das rodadas com pipeline ligado (OCR, LLM e e-mail)",
                         ["Rodada", "Chegada (docs/min)", "Casos", "Travados", "Triagem média (s)", "p95 (s)", "p99 (s)",
                          "Notificados/min", "Idade máx. Q2 (s)", "CPU pico", "Req. HTTP", "5xx", "Custo est."], linhas,
                         "Custo estimado: páginas × US$ 0,0015 (Textract) + documentos × US$ 0,01 (Gemini, premissa a calibrar). "
                         "Não inclui EC2, ALB, SQS e S3, que ficam ligados independentemente do teste.")
        secoes.append(("Pipeline de triagem: cenário conservador e escada de estresse", html_e))

    # ---------- critérios
    blocos = []
    for d in sorted(pipes, key=lambda d: (d["modo"] != "pipeline", d["multiplicador"])):
        if not d.get("criterios"):
            continue
        linhas = [[c["criterio"], c["meta"], esc(c["medido"]),
                   f'<span class="st st-{"ok" if c["resultado"] == "OK" else ("na" if c["resultado"] == "-" else "falha")}">'
                   f'{"✓ OK" if c["resultado"] == "OK" else ("–" if c["resultado"] == "-" else "✗ " + c["resultado"])}</span>']
                  for c in d["criterios"]]
        blocos.append(tabela(f"t4_criterios_{d['bateria']}", f"Critérios de aceite — {rotulo(d)}",
                             ["Critério", "Meta", "Medido", "Resultado"], linhas))
    if blocos:
        secoes.append(("Critérios de aceite por rodada", "".join(blocos)))

    # ---------- antes x depois
    if base_pipes:
        antes = base_pipes[0]
        depois = next((d for d in escada if d["multiplicador"] == 1), None)
        if depois:
            def falhas(d):
                a = d.get("aws") or {}
                g = a.get("gemini") or {}
                des = d["pipeline"]["desfechos"]
                return [br(d["pipeline"]["n"], 0), f"{d['pipeline']['travados']} ({br(d['pipeline']['pct_travados'], 1)}%)",
                        br(des.get("ANALYSIS_FAILED", 0), 0), br(des.get("TIMEOUT", 0), 0),
                        br(a.get("npe"), 0), br(a.get("q2_descartadas"), 0), br(a.get("invalid_report"), 0),
                        br(a.get("cache_duplicado"), 0), br(g.get("vazias"), 0), br(g.get("novas_geracoes"), 0),
                        br(d["pipeline"]["media"]), br(d["pipeline"]["p95"])]
            cab = ["Casos", "Travados", "ANALYSIS_FAILED", "Timeout (preso na fila)", "NullPointerException",
                   "Q2 descartada como duplicata", "INVALID_REPORT_FORMAT", "Duplicata no cache",
                   "Respostas vazias do LLM (recuperadas)", "Novas gerações do relatório", "Triagem média (s)", "p95 (s)"]
            fa, fd = falhas(antes), falhas(depois)
            linhas = [[c, a_, d_] for c, a_, d_ in zip(cab, fa, fd)]
            html_ad = tabela("t5_antes_depois", "Cenário conservador (modo pipeline) antes e depois das correções",
                             ["Indicador", "16/11 (antes)", "18/11 (depois)"], linhas,
                             "Antes: respostas vazias do LLM não eram registradas (contagem obtida no log de depuração: 8). "
                             "Correções descritas em SPEC-correcoes-pipeline.md (B1 a B5).")
            v = [[antes["pipeline"]["pct_travados"], depois["pipeline"]["pct_travados"]]]
            html_ad += figura("f8_travados_antes_depois", "Casos travados no cenário conservador antes e depois das correções",
                              barras_agrupadas(["Conservador · pipeline"], ["16/11 (antes)", "18/11 (depois)"], v, "% de casos",
                                               fmt=lambda x: br(x, 1), largura=420, altura=260).svg("Travados"),
                              "Caso travado: status final diferente de NOTIFIED ou sem desfecho até o timeout do teste.",
                              legenda(["16/11 (antes)", "18/11 (depois)"]))
            secoes.append(("Efeito das correções", html_ad))

    # ---------- robustez do LLM
    linhas = []
    for d in sorted(pipes, key=lambda d: (d["modo"] != "pipeline", d["multiplicador"])):
        a = d.get("aws") or {}
        g = a.get("gemini") or {}
        an = a.get("analise_ms") or {}
        oc = a.get("ocr_ms") or {}
        tx = a.get("textract") or {}
        thr = sum((v or {}).get("throttling", 0) for v in tx.values() if v) if tx else None
        e = a.get("email") or {}
        linhas.append([rotulo(d), br(an.get("n"), 0), br(an.get("media_ms") and an["media_ms"] / 1000),
                       br(oc.get("media_ms") and oc["media_ms"] / 1000), br(g.get("vazias"), 0),
                       f"{br(g.get('novas_geracoes'), 0)} ({br(g.get('recuperadas'), 0)})", br(g.get("reformatacoes"), 0),
                       br(g.get("429"), 0), br(thr, 0), f"{br(e.get('enviados'), 0)} / {br(e.get('falhas'), 0)}"])
    if linhas:
        secoes.append(("Serviços externos: OCR, LLM e e-mail", tabela(
            "t6_servicos_externos", "OCR (Textract), LLM (Gemini) e e-mail por rodada",
            ["Rodada", "Análises", "Análise média (s)", "OCR por job (s)", "Respostas vazias LLM", "Novas gerações (recuperadas)",
             "Reformatações JSON", "Gemini 429", "Throttling Textract", "E-mails enviados / falhas"], linhas,
            "Fontes: métricas Triaige/MCP (micrometer), AWS/Textract e logs do mcp-ai e do notification no CloudWatch Logs.")))

    discussao = saida / "discussao.html"
    if discussao.exists():
        secoes.append(("Discussão", discussao.read_text(encoding="utf-8")))

    secoes.append(("Método", METODO))
    pagina = montar_html(secoes, rodadas)
    (saida / "relatorio.html").write_text(pagina, encoding="utf-8")
    pdf(saida)


METODO = """
<p>A carga não é um número fixo de usuários: ela deriva do cenário de mercado do Business Case. A taxa de chegada de
documentos no pico é <i>docs_min = unidades_mês / dias / horas × f<sub>pico</sub> / 60</i>, e o número de documentos em
processamento simultâneo segue a lei de Little (<i>L = λ·W</i>). O gerador (Apache JMeter 5.6.3) usa um modelo aberto
(<i>OpenModelThreadGroup</i>) com chegadas de Poisson, de modo que a taxa de chegada não diminui quando o sistema fica lento.</p>
<p>Cada documento percorre o fluxo real: criação da sessão, URL pré-assinada, envio do PDF ao S3, finalização e consulta
de status a cada 5 s até o estado terminal. Os PDFs são sintéticos (1, 5 e 10 páginas, com pesos de 50/30/20%) e não
contêm dados pessoais. Em paralelo rodam o portal do escritório, o painel administrativo e consultas avulsas da API.</p>
<p>Modos: <b>plataforma</b> (degraus de 25 a 100% do portal simultâneo, pipeline desligado); <b>pipeline</b> (chegada
constante na taxa do cenário, com OCR, LLM e e-mail ligados, seguida de drenagem); <b>pico</b> (rajada de 3× a taxa por
5 min). A escada de estresse multiplica o número de escritórios do cenário conservador por 2, 4 e 8.</p>
<p><b>Ambiente:</b> AWS us-east-1, três instâncias EC2 (bff-front t3.large, mcp t3.large, mysql t3.medium) atrás de um
Application Load Balancer; filas SQS entre os serviços; Amazon Textract assíncrono para OCR; Google Gemini 2.5 Flash para
a análise; e-mail via SMTP. O gerador de carga roda fora da AWS.</p>
<p><b>Ameaças à validade:</b> (i) a latência medida no cliente inclui a rede entre o gerador e a AWS; por isso os tempos do
servidor são tomados do ALB; (ii) os PDFs sintéticos repetem o mesmo conteúdo, o que favorece acertos no cache de
jurisprudência; (iii) o custo do LLM por documento é uma premissa a calibrar com o faturamento; (iv) cada rodada foi
executada uma vez, sem repetições para intervalo de confiança; (v) o e-mail usou uma conta Gmail comum, cujo limite diário (~500 destinatários) se esgotou no início da rodada 2×, de modo que nas rodadas 2×, 4× e 8× o fim do fluxo é medido pela análise concluída e não pela notificação; (vi) nas rodadas 4× e 8×, parte dos erros do cliente são tempos limite de conexão do próprio gerador, que acumula requisições pendentes quando o servidor fica lento.</p>
"""


def montar_html(secoes, rodadas):
    cores_css = "".join(f"--s{i + 1}:{c};" for i, c in enumerate(CORES))
    cores_esc = "".join(f"--s{i + 1}:{c};" for i, c in enumerate(CORES_ESCURO))
    escuro = (f"color-scheme:dark;--fundo:#1a1a19;--pagina:#141413;--txt1:#f0efec;--txt2:#c3c2b7;--txt3:#8f8e88;"
              f"--grade:#2e2e2b;--eixo:#5a5954;--borda:#33332f;--ok:#0ca30c;--falha:#e66767;{cores_esc}")
    datas = sorted({d["bateria"][:8] for d in rodadas})
    periodo = " a ".join(dict.fromkeys(f"{x[6:8]}/{x[4:6]}/{x[:4]}" for x in (datas[0], datas[-1]))) if datas else ""
    sumario = "".join(f'<li><a href="#s{i}">{esc(t)}</a></li>' for i, (t, _) in enumerate(secoes))
    corpo = "".join(f'<section id="s{i}"><h2>{i + 1}. {esc(t)}</h2>{h}</section>' for i, (t, h) in enumerate(secoes))
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Testes de carga TriAIge</title>
<style>
:root{{color-scheme:light;--fundo:#fcfcfb;--pagina:#ffffff;--txt1:#0b0b0b;--txt2:#52514e;--txt3:#8a8984;--grade:#e4e3df;
--eixo:#a9a8a2;--borda:#dcdbd6;--ok:#006300;--falha:#b42318;{cores_css}}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{{escuro}}}}}
:root[data-theme="dark"]{{{escuro}}}
body{{margin:0;background:var(--pagina);color:var(--txt1);font:15px/1.55 Georgia,"Times New Roman",serif}}
main{{max-width:880px;margin:0 auto;padding:32px 16px 64px}}
h1{{font:600 26px/1.25 Helvetica,Arial,sans-serif;margin:0 0 4px}}
h2{{font:600 19px/1.3 Helvetica,Arial,sans-serif;margin:40px 0 12px;padding-top:8px;border-top:1px solid var(--borda)}}
.sub{{color:var(--txt2);font-family:Helvetica,Arial,sans-serif;font-size:13px;margin:0 0 20px}}
nav ol{{font-family:Helvetica,Arial,sans-serif;font-size:13.5px;color:var(--txt2);padding-left:20px}}
nav a{{color:inherit}}
figure{{margin:24px 0;break-inside:avoid}}
figcaption,caption{{font:13.5px/1.4 Helvetica,Arial,sans-serif;text-align:left;margin-bottom:8px;color:var(--txt1)}}
svg.graf{{width:100%;height:auto;display:block}}
.fonte{{font:12px/1.45 Helvetica,Arial,sans-serif;color:var(--txt2);margin:6px 0 0}}
.legenda{{display:flex;flex-wrap:wrap;gap:6px 16px;font:12.5px Helvetica,Arial,sans-serif;color:var(--txt2);margin:0 0 6px}}
.leg-item{{display:inline-flex;align-items:center;gap:6px}}
.leg-cor{{width:12px;height:12px;border-radius:3px;display:inline-block}}
.grade-mini{{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}}
.mini-tit{{font:12.5px Helvetica,Arial,sans-serif;color:var(--txt2);margin-bottom:2px}}
.tabwrap{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;margin:24px 0 4px;font:12.5px/1.4 Helvetica,Arial,sans-serif;break-inside:avoid;
display:block;overflow-x:auto}}
th,td{{border-bottom:1px solid var(--borda);padding:5px 8px;text-align:right;vertical-align:top}}
th{{color:var(--txt2);font-weight:600;border-bottom:1.5px solid var(--eixo)}}
td:first-child,th:first-child{{text-align:left}}
td{{font-variant-numeric:tabular-nums}}
.st{{white-space:nowrap;font-weight:600}}.st-ok{{color:var(--ok)}}.st-falha{{color:var(--falha)}}.st-na{{color:var(--txt3)}}
@media print{{body{{font-size:11pt;background:#fff}}main{{max-width:none;padding:0}}nav{{display:none}}
table{{display:table;font-size:8.5pt}}th,td{{padding:3px 4px}}
h2{{break-after:avoid}}@page{{size:A4;margin:18mm 16mm}}}}
</style></head><body><main>
<h1>Testes de carga da plataforma TriAIge</h1>
<p class="sub">Cenário conservador do Business Case (500 escritórios no plano Essencial) e escada de estresse · {esc(periodo)} ·
gerado em {datetime.now().strftime("%d/%m/%Y %H:%M")}</p>
<nav><ol>{sumario}</ol></nav>
{corpo}
</main></body></html>"""


def pdf(saida):
    if not Path(CHROME).exists():
        print("Chrome não encontrado: PDF não gerado (abra o HTML e imprima em PDF).")
        return
    r = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        "--force-device-scale-factor=1", "--print-to-pdf=" + str((saida / "relatorio.pdf").resolve()),
                        (saida / "relatorio.html").resolve().as_uri()], capture_output=True, text=True, timeout=120)
    print("PDF:", saida / "relatorio.pdf" if r.returncode == 0 else f"falhou ({r.stderr[-300:]})")


def rodadas_de(bateria, recoletar):
    return [coletar(p, recoletar) for p in sorted(bateria.iterdir()) if (p / "resultados.jtl").exists()]


def main():
    args = sys.argv[1:]
    if "--saida" not in args:
        sys.exit(__doc__)
    saida = Path(args[args.index("--saida") + 1])
    recoletar = "--recoletar" in args
    base, alvo, k = [], [], 0
    resto = [a for a in args if a not in ("--recoletar",)]
    while k < len(resto):
        a = resto[k]
        if a == "--saida":
            k += 2
            continue
        if a == "--base":
            base.append(Path(resto[k + 1]))
            k += 2
            continue
        alvo.append(Path(a))
        k += 1
    rod_base = [d for b in base for d in rodadas_de(b, recoletar)]
    rod = [d for b in alvo for d in rodadas_de(b, recoletar)]
    gerar(saida, rod, rod_base)
    print("HTML:", saida / "relatorio.html")


if __name__ == "__main__":
    main()
