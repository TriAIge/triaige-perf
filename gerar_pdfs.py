"""Gera os PDFs sintéticos usados no fluxo completo (1, 5 e 10 páginas).

Todo o conteúdo é inventado: nomes, CPF e CNPJ são fictícios e não pertencem a ninguém.
O texto imita um holerite com horas extras, um cartão de ponto e trechos de contrato,
para que o OCR e a análise de IA tenham o que processar.

Python puro, sem dependências. Uso: python3 gerar_pdfs.py  (grava em dados/pdfs/)
"""
from pathlib import Path

SAIDA = Path(__file__).parent / "dados" / "pdfs"

EMPREGADO = "Fulano Sintetico da Silva"
EMPRESA = "Empresa Ficticia de Testes Ltda"
CPF = "000.000.000-00"
CNPJ = "00.000.000/0000-00"

MESES = ["Janeiro", "Fevereiro", "Marco", "Abril", "Maio", "Junho",
         "Julho", "Agosto", "Setembro", "Outubro"]


def holerite(i):
    mes = MESES[i % len(MESES)]
    he = 20 + 3 * i
    return [
        "DOCUMENTO SINTETICO PARA TESTE DE CARGA - SEM DADOS PESSOAIS REAIS",
        "",
        f"RECIBO DE PAGAMENTO DE SALARIO - {mes.upper()}/2026",
        f"Empregador: {EMPRESA}   CNPJ: {CNPJ}",
        f"Empregado: {EMPREGADO}   CPF: {CPF}",
        "Cargo: Auxiliar Administrativo   Admissao: 01/03/2022",
        "",
        "Cod  Descricao                         Ref.        Vencimentos   Descontos",
        "001  Salario base                      220 h         3.000,00",
        f"050  Horas extras 50% (informadas)     {he} h            0,00",
        "101  INSS                              9%                           270,00",
        "102  IRRF                                                            45,00",
        "",
        "Total de vencimentos: 3.000,00   Total de descontos: 315,00",
        "Liquido a receber: 2.685,00",
        "",
        f"Observacao: o cartao de ponto registra {he} horas extras no mes,",
        "mas o valor correspondente nao foi pago neste recibo.",
    ]


def cartao_ponto(i):
    linhas = [
        "DOCUMENTO SINTETICO PARA TESTE DE CARGA - SEM DADOS PESSOAIS REAIS",
        "",
        f"CARTAO DE PONTO - {MESES[i % len(MESES)].upper()}/2026 - {EMPREGADO}",
        "",
        "Dia   Entrada  Saida almoco  Retorno  Saida   Horas   Extras",
    ]
    for d in range(1, 23):
        extra = 1 + (d + i) % 3
        linhas.append(f"{d:02d}    08:00    12:00         13:00    {17 + extra}:00   {8 + extra}h     {extra}h")
    linhas += ["", "Assinatura do empregado: ________________________"]
    return linhas


def contrato(i):
    return [
        "DOCUMENTO SINTETICO PARA TESTE DE CARGA - SEM DADOS PESSOAIS REAIS",
        "",
        f"CONTRATO INDIVIDUAL DE TRABALHO - PAGINA {i + 1}",
        "",
        f"Clausula {i + 1}a. O EMPREGADO cumprira jornada de 44 horas semanais,",
        "de segunda a sexta-feira, das 08h00 as 17h00, com uma hora de intervalo.",
        "Paragrafo unico. As horas excedentes a jornada contratual serao pagas",
        "com adicional de 50% sobre o valor da hora normal, conforme o art. 59 da CLT,",
        "ou compensadas em banco de horas mediante acordo escrito.",
        "",
        f"Partes: {EMPRESA} (EMPREGADORA) e {EMPREGADO} (EMPREGADO).",
    ]


def paginas(n):
    """Mistura de tipos: a 1a página é sempre o holerite."""
    geradores = [holerite, cartao_ponto, contrato]
    return [holerite(0)] + [geradores[k % 3](k) for k in range(1, n)]


def esc(texto):
    return texto.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf(paginas_texto):
    objs = []  # conteúdo de cada objeto, na ordem dos números (1-based)

    def add(conteudo):
        objs.append(conteudo)
        return len(objs)

    catalogo = add(None)
    raiz_paginas = add(None)
    fonte = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
    kids = []
    for linhas in paginas_texto:
        corpo = "BT /F1 10 Tf 13 TL 40 800 Td\n" + "".join(f"({esc(l)}) '\n" for l in linhas) + "ET\n"
        stream = corpo.encode("cp1252")
        conteudo = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] "
                        b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                        % (raiz_paginas, fonte, conteudo)))
    objs[catalogo - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % raiz_paginas
    objs[raiz_paginas - 1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
                              + b"] /Count %d >>" % len(kids))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, conteudo in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + conteudo + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, catalogo, xref)
    return bytes(out)


if __name__ == "__main__":
    SAIDA.mkdir(parents=True, exist_ok=True)
    for n in (1, 5, 10):
        arq = SAIDA / f"sintetico-{n:02d}p.pdf"
        arq.write_bytes(pdf(paginas(n)))
        print(f"{arq}  {n} página(s)  {arq.stat().st_size} bytes")
