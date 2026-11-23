"""Calcula a carga de um cenário do Business Case (SPEC-cenarios-bc §2).

Uso:
  python3 calc_carga.py cenarios/conservador.properties                  # tabela do cenário
  python3 calc_carga.py cenarios/conservador.properties --modo pipeline  # + plano da rodada e custo
  python3 calc_carga.py CENARIO --modo M --gerar DIR   # grava DIR/carga.json e DIR/<rodada>/carga.properties
                                                       # (lido pelo JMeter com -q) e lista as rodadas
  python3 calc_carga.py CENARIO --jmeter               # só os -J do cenário (pico de uso, sem schedule)

Modos: plataforma (degraus até o portal simultâneo, pipeline desligado),
       pipeline (chegada constante, pipeline ligado), pico (rajada de 3x).
"""
import argparse
import csv
import json
import math
import sys
from pathlib import Path

RAIZ = Path(__file__).parent

# Premissas e parâmetros padrão. Um cenário pode sobrescrever qualquer um deles.
# origem: BC = Business Case | PREMISSA = hipótese do grupo (a banca vai perguntar) | TESTE = parâmetro do teste
PADROES = {
    "n_ess": (0, "BC", "escritórios no plano Essencial"),
    "n_pro": (0, "BC", "escritórios no plano Profissional"),
    "n_corp": (0, "BC", "escritórios no plano Corporativo"),
    "usuarios_ess": (2, "BC slide 13", "usuários por escritório Essencial"),
    "usuarios_pro": (5, "BC slide 13", "usuários por escritório Profissional"),
    "usuarios_corp": (15, "BC slide 13", "usuários por escritório Corporativo"),
    "franquia_ess": (100, "BC slide 13", "unidades/mês do Essencial"),
    "franquia_pro": (500, "BC slide 13", "unidades/mês do Profissional"),
    "franquia_corp": (1000, "BC slide 13", "unidades/mês do Corporativo"),
    "uso": (1.0, "PREMISSA", "uso da franquia (1,0 = pior caso)"),
    "dias": (22, "PREMISSA", "dias úteis por mês"),
    "horas": (9, "PREMISSA", "horas úteis por dia"),
    "fpico": (2.0, "PREMISSA", "fator da hora de pico sobre a hora média"),
    "conc": (0.15, "PREMISSA", "concorrência de usuários humanos no portal"),
    "t_pipe_min": (5.0, "MEDIDO/conservador", "tempo médio de pipeline em min (hoje ~0,5; meta < 6)"),
    "poll_seg": (5, "JMX", "intervalo de polling de status em s"),
    "admin_users": (3, "PREMISSA", "usuários do painel admin (uso interno, fixo)"),
    "api_conc": (0.02, "PREMISSA", "fração dos escritórios com integração fazendo consultas avulsas ao mesmo tempo"),
    "limite_email_dia": (500, "PREMISSA", "limite diário do Gmail SMTP (500 conta pessoal, 2.000 Workspace; confirmar)"),
    "docs_min_piso": (0.0, "TESTE", "piso da taxa de chegada (só para smoke)"),
    "multiplicador": (1.0, "TESTE", "multiplica a quantidade de escritórios (estresse)"),
    # Modo plataforma
    "degraus": ("25,50,75,100", "TESTE", "degraus em % do portal simultâneo"),
    "degrau_seg": (180, "TESTE", "duração de cada degrau em s"),
    "ramp_seg": (30, "TESTE", "rampa de cada degrau em s"),
    "pico_plataforma_seg": (120, "TESTE", "duração do pico da plataforma (100% em 10 s)"),
    # Modos pipeline e pico
    "regime_min": (30, "TESTE", "minutos de chegada em regime (modo pipeline)"),
    "drenagem_min": (10, "TESTE", "minutos sem chegada no fim, para drenar os casos em voo"),
    "pico_base_min": (5, "TESTE", "modo pico: minutos na taxa normal antes da rajada"),
    "pico_rajada_min": (5, "TESTE", "modo pico: minutos de rajada"),
    "pico_volta_min": (10, "TESTE", "modo pico: minutos na taxa normal depois da rajada"),
    "pico_fator": (3.0, "TESTE", "modo pico: multiplicador da taxa na rajada"),
    "pipeline_timeout_seg": (600, "TESTE", "tempo máximo de espera por NOTIFIED antes de contar como travado"),
    # Custo
    "textract_usd_pagina": (0.0015, "AWS (us-east-1)", "Textract DetectDocumentText, faixa até 1M páginas/mês"),
    "gemini_usd_doc": (0.01, "PREMISSA a calibrar", "custo do Gemini por documento (conferir no billing)"),
    "pdfs_csv": ("dados/pdfs.csv", "TESTE", "PDFs sorteados no fluxo completo"),
}


def ler_properties(caminho):
    valores = {}
    for linha in Path(caminho).read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith(("#", "!")) or "=" not in linha:
            continue
        k, v = linha.split("=", 1)
        valores[k.strip()] = v.strip()
    return valores


def carregar_cenario(caminho):
    caminho = Path(caminho)
    brutos = ler_properties(caminho)
    if "herda" in brutos:
        base = carregar_cenario(caminho.parent / brutos.pop("herda"))
        base_brutos = base["_brutos"]
        mult = float(brutos.get("multiplicador", 1))
        brutos = {**base_brutos, **brutos, "multiplicador": str(mult * float(base_brutos.get("multiplicador", 1)))}
    pendentes = [k for k, v in brutos.items() if v.upper() == "A_PREENCHER"]
    if pendentes:
        sys.exit(f"{caminho}: preencha {', '.join(pendentes)} antes de usar este cenário.")
    desconhecidas = set(brutos) - set(PADROES) - {"descricao"}
    if desconhecidas:
        sys.exit(f"{caminho}: chave(s) desconhecida(s): {', '.join(sorted(desconhecidas))}")

    c = {"_brutos": brutos, "_origem": {}, "nome": caminho.stem, "descricao": brutos.get("descricao", caminho.stem)}
    for k, (padrao, origem, _) in PADROES.items():
        if k in brutos:
            v = brutos[k]
            c[k] = type(padrao)(float(v)) if isinstance(padrao, int) else type(padrao)(v)
            c["_origem"][k] = f"{origem} (cenário)" if origem.startswith("PREMISSA") else origem
        else:
            c[k] = padrao
            c["_origem"][k] = origem
    return c


def pdfs(c):
    caminho = RAIZ / c["pdfs_csv"]
    linhas = [l for l in caminho.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    itens = [(r["caminho"], int(r["paginas"]), float(r["peso"])) for r in csv.DictReader(linhas)]
    total = sum(p for _, _, p in itens)
    return itens, sum(pag * peso for _, pag, peso in itens) / total


def derivar(c):
    m = c["multiplicador"]
    n_ess, n_pro, n_corp = c["n_ess"] * m, c["n_pro"] * m, c["n_corp"] * m
    d = {"escritorios": n_ess + n_pro + n_corp}
    d["usuarios_nomeados"] = c["usuarios_ess"] * n_ess + c["usuarios_pro"] * n_pro + c["usuarios_corp"] * n_corp
    d["unidades_mes"] = (c["franquia_ess"] * n_ess + c["franquia_pro"] * n_pro + c["franquia_corp"] * n_corp) * c["uso"]
    d["docs_dia"] = d["unidades_mes"] / c["dias"]
    d["docs_hora_media"] = d["docs_dia"] / c["horas"]
    d["docs_min_pico_bc"] = d["docs_hora_media"] * c["fpico"] / 60
    d["docs_min_pico"] = max(d["docs_min_pico_bc"], c["docs_min_piso"])
    d["docs_em_voo"] = d["docs_min_pico"] * c["t_pipe_min"]  # Little: L = λ·W
    d["reqs_polling"] = d["docs_em_voo"] / c["poll_seg"]
    d["portal_simult"] = d["usuarios_nomeados"] * c["conc"]
    d["api_simult"] = d["escritorios"] * c["api_conc"]
    d["emails_dia"] = d["docs_dia"]
    _, d["paginas_media"] = pdfs(c)
    return d


def fmt_rate(por_min):
    return f"{por_min:.4f}".rstrip("0").rstrip(".") or "0"


def plano(c, d, modo):
    """Lista de rodadas: (nome, props do JMeter, minutos de chegada por taxa)."""
    portal, api, voo = d["portal_simult"], d["api_simult"], d["docs_em_voo"]
    base = {"adminUsers": c["admin_users"], "pollSec": c["poll_seg"],
            "pipelineTimeoutSec": c["pipeline_timeout_seg"], "pdfsCsv": c["pdfs_csv"]}
    sem_docs = {"docsPorMinuto": 0, "docsSchedule": "pause(1 s)"}
    r = d["docs_min_pico"]

    if modo == "plataforma":
        rodadas = []
        for i, pct in enumerate(int(x) for x in str(c["degraus"]).split(",")):
            f = pct / 100
            rodadas.append((f"{i + 1}-{pct:03d}pct", {
                **base, **sem_docs, "portalUsers": math.ceil(portal * f), "apiUsers": math.ceil(api * f),
                "pollingUsers": math.ceil(voo * f), "duration": c["degrau_seg"], "ramp": c["ramp_seg"]}, []))
        rodadas.append((f"{len(rodadas) + 1}-pico", {
            **base, **sem_docs, "portalUsers": math.ceil(portal), "apiUsers": math.ceil(api),
            "pollingUsers": math.ceil(voo), "duration": c["pico_plataforma_seg"], "ramp": 10}, []))
        return rodadas

    humanos = {"portalUsers": math.ceil(portal), "apiUsers": math.ceil(api), "pollingUsers": 0, "ramp": c["ramp_seg"]}
    dren = c["drenagem_min"]
    if modo == "pipeline":
        reg = c["regime_min"]
        sched = f"rate({fmt_rate(r)}/min) random_arrivals({reg} min) pause({dren} min)"
        return [("pipeline", {**base, **humanos, "docsPorMinuto": fmt_rate(r), "docsSchedule": sched,
                              "duration": int(reg * 60)}, [(r, reg)])]
    if modo == "pico":
        b, raj, volta, rp = c["pico_base_min"], c["pico_rajada_min"], c["pico_volta_min"], r * c["pico_fator"]
        R, RP = fmt_rate(r), fmt_rate(rp)
        # rate(x) seguido de rate(y) = degrau instantâneo; rate(x) ... rate(x) = taxa constante no trecho
        sched = (f"rate({R}/min) random_arrivals({b} min) rate({R}/min) "
                 f"rate({RP}/min) random_arrivals({raj} min) rate({RP}/min) "
                 f"rate({R}/min) random_arrivals({volta} min) pause({dren} min)")
        return [("pico", {**base, **humanos, "docsPorMinuto": R, "docsSchedule": sched,
                          "duration": int((b + raj + volta) * 60)}, [(r, b), (rp, raj), (r, volta)])]
    raise ValueError(modo)


def custo(c, d, rodadas):
    docs = sum(taxa * minutos for _, _, trechos in rodadas for taxa, minutos in trechos)
    paginas = docs * d["paginas_media"]
    textract = paginas * c["textract_usd_pagina"]
    gemini = docs * c["gemini_usd_doc"]
    return {"docs": docs, "paginas": paginas, "textract_usd": textract, "gemini_usd": gemini,
            "total_usd": textract + gemini, "emails": docs}


def duracao_min(c, modo, rodadas):
    if modo == "plataforma":
        return sum(p["duration"] + 30 for _, p, _ in rodadas) / 60
    return sum(m for _, _, t in rodadas for _, m in t) + c["drenagem_min"]


def br(v, casas=0):
    s = f"{v:,.{casas}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def imprimir(c, d, modo, rodadas):
    print(f"Cenário: {c['descricao']}  ({c['nome']})\n")
    print("Premissas e parâmetros")
    usados = ["n_ess", "n_pro", "n_corp", "multiplicador", "uso", "dias", "horas", "fpico", "conc", "t_pipe_min",
              "poll_seg", "admin_users", "api_conc", "limite_email_dia", "docs_min_piso"]
    for k in usados:
        if k == "multiplicador" and c[k] == 1 or k == "docs_min_piso" and not c[k]:
            continue
        print(f"  {k:18} {str(c[k]):>10}   {c['_origem'][k]:24} {PADROES[k][2]}")

    print("\nCarga derivada (§2.2)")
    linhas = [
        ("Escritórios", br(d["escritorios"])),
        ("Usuários nomeados", br(d["usuarios_nomeados"])),
        ("Usuários de portal simultâneos", br(d["portal_simult"], 1)),
        ("Integrações com consulta avulsa simultânea", br(d["api_simult"], 1)),
        ("Documentos/mês (máx.)", br(d["unidades_mes"])),
        ("Documentos/dia útil", br(d["docs_dia"])),
        ("Chegada no pico", f"{br(d['docs_min_pico_bc'], 2)} docs/min ({br(d['docs_min_pico_bc'] / 60, 3)}/s)"),
        ("Documentos em processamento simultâneo", br(d["docs_em_voo"], 1)),
        ("Polling de status", f"{br(d['reqs_polling'], 1)} req/s"),
        ("E-mails/dia", br(d["emails_dia"])),
        ("Páginas por documento (média do sorteio)", br(d["paginas_media"], 1)),
    ]
    if c["docs_min_piso"] > d["docs_min_pico_bc"]:
        linhas.insert(7, ("Chegada usada no teste (piso do smoke)", f"{br(d['docs_min_pico'], 2)} docs/min"))
    for nome, v in linhas:
        print(f"  {nome:44} {v}")

    if d["emails_dia"] > c["limite_email_dia"]:
        print(f"\n  ATENÇÃO: {br(d['emails_dia'])} e-mails/dia passam do limite de envio considerado "
              f"({br(c['limite_email_dia'])}/dia). A entrega não fecha este cenário com o Gmail SMTP.")

    if not modo:
        return
    if modo != "plataforma" and c["drenagem_min"] * 60 < c["pipeline_timeout_seg"]:
        print(f"\n  ATENÇÃO: drenagem ({c['drenagem_min']} min) menor que pipeline_timeout_seg "
              f"({c['pipeline_timeout_seg']} s): casos que chegarem no fim podem ser cortados sem desfecho.")
    print(f"\nModo {modo}: {len(rodadas)} rodada(s), ~{br(duracao_min(c, modo, rodadas))} min")
    print(f"  {'Rodada':12} {'portal':>6} {'admin':>5} {'api':>4} {'polling':>7} {'dur(s)':>6} {'ramp':>4}  chegada de documentos")
    for nome, p, _ in rodadas:
        print(f"  {nome:12} {p['portalUsers']:6} {p['adminUsers']:5} {p['apiUsers']:4} {p['pollingUsers']:7} "
              f"{p['duration']:6} {p['ramp']:4}  {p['docsSchedule'] if p['docsPorMinuto'] else '-'}")
    if modo != "plataforma":
        k = custo(c, d, rodadas)
        print(f"\nEstimativa de custo da rodada (pipeline ligado)")
        print(f"  Documentos esperados  {br(k['docs'])}  |  páginas {br(k['paginas'])}  |  e-mails {br(k['emails'])}")
        print(f"  Textract  US$ {br(k['textract_usd'], 2)}  ({br(k['paginas'])} pág. x US$ {c['textract_usd_pagina']})")
        print(f"  Gemini    US$ {br(k['gemini_usd'], 2)}  ({br(k['docs'])} docs x US$ {c['gemini_usd_doc']}, premissa a calibrar)")
        print(f"  Total     US$ {br(k['total_usd'], 2)}  (sem EC2/ALB/SQS/S3, que já estão ligados)")


def gerar(c, d, modo, rodadas, destino):
    destino = Path(destino)
    destino.mkdir(parents=True, exist_ok=True)
    resumo = {"cenario": c["nome"], "descricao": c["descricao"], "modo": modo,
              "premissas": {k: c[k] for k in PADROES}, "origem": c["_origem"], "derivado": d,
              "rodadas": [{"nome": n, "props": p, "trechos": t} for n, p, t in rodadas]}
    if modo != "plataforma":
        resumo["custo_estimado"] = custo(c, d, rodadas)
    (destino / "carga.json").write_text(json.dumps(resumo, indent=2, ensure_ascii=False), encoding="utf-8")
    for nome, props, _ in rodadas:
        (destino / nome).mkdir(exist_ok=True)
        (destino / nome / "carga.properties").write_text(
            f"# gerado por calc_carga.py: {c['nome']} / {modo} / {nome}\n"
            + "".join(f"{k}={v}\n" for k, v in props.items()), encoding="utf-8")
        print(nome)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cenario")
    ap.add_argument("--modo", choices=["plataforma", "pipeline", "pico"])
    ap.add_argument("--gerar", metavar="DIR", help="grava carga.json e um carga.properties por rodada")
    ap.add_argument("--jmeter", action="store_true", help="imprime os -J do cenário no pico de uso")
    a = ap.parse_args()

    c = carregar_cenario(a.cenario)
    d = derivar(c)
    if a.jmeter:
        p = plano(c, d, "pipeline")[0][1]
        print(" ".join(f"-J{k}={v}" for k, v in p.items() if k != "docsSchedule"))
        return
    rodadas = plano(c, d, a.modo) if a.modo else []
    if a.gerar:
        if not a.modo:
            sys.exit("--gerar exige --modo")
        gerar(c, d, a.modo, rodadas, a.gerar)
    else:
        imprimir(c, d, a.modo, rodadas)


if __name__ == "__main__":
    main()
