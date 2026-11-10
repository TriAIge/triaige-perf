#!/bin/sh
# Carga em degraus (20/40/80/120 usuários, 3 min cada) + pico (150 usuários em 10 s, 2 min).
# Só API e front: sem finalizar sessões (não chama OCR, Gemini nem e-mail) e
# 1 sessão por usuário da API (-JnewSessionEachIter=false) para não encher o banco.
# Divisão: 50% API do escritório, 25% painel admin, 25% portal.
set -e
cd "$(dirname "$0")"
BASE="resultados/$(date +%Y%m%d-%H%M)-degraus"
mkdir -p "$BASE"
echo "$BASE" > .ultimo-degraus

rodar() { # nome usuarios duracao ramp
  nome=$1; n=$2; dur=$3; ramp=$4
  api=$((n / 2)); admin=$((n / 4)); portal=$((n - api - admin))
  dir="$BASE/$nome"; mkdir -p "$dir"
  echo "$(date +%H:%M:%S) $nome: $n usuários (api=$api admin=$admin portal=$portal) por ${dur}s"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$dir/inicio.txt"
  jmeter -n -t triaige-carga-mista.jmx -l "$dir/resultados.jtl" -j "$dir/jmeter.log" \
    -Jjmeter.save.saveservice.output_format=csv -e -o "$dir/relatorio" \
    -JapiUsers=$api -JadminUsers=$admin -JportalUsers=$portal -JfullFlowUsers=0 \
    -JnewSessionEachIter=false -Jduration=$dur -Jramp=$ramp > "$dir/console.txt" 2>&1
  date -u +%Y-%m-%dT%H:%M:%SZ > "$dir/fim.txt"
  grep "summary =" "$dir/console.txt" | tail -1
  sleep 30
}

rodar 1-020-usuarios 20 180 30
rodar 2-040-usuarios 40 180 30
rodar 3-080-usuarios 80 180 30
rodar 4-120-usuarios 120 180 30
rodar 5-pico-150 150 120 10
echo "fim: $BASE"
