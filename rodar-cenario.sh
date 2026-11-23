#!/bin/sh
# Roda um cenário do Business Case (SPEC-cenarios-bc §3.3).
#
#   ./rodar-cenario.sh cenarios/conservador.properties [plataforma|pipeline|pico] [-y]
#
# plataforma  degraus até o portal simultâneo do cenário + polling equivalente; pipeline DESLIGADO
# pipeline    chegada constante de docs_min_pico (modelo aberto) com OCR, Gemini e e-mail LIGADOS;
#             30 min em regime + 10 min de drenagem
# pico        rajada de 3x docs_min_pico por 5 min e volta ao normal; pipeline LIGADO
#
# Credenciais: TRIAIGE_HOST, TRIAIGE_API_TOKEN e TRIAIGE_SENHA no ambiente ou no .env (fora do git;
# modelo em .env.exemplo). O JMX lê do ambiente, então o token não aparece no jmeter.log.
# -y pula a confirmação de custo dos modos com pipeline ligado.
set -e
cd "$(dirname "$0")"

CEN=$1
MODO=${2:-plataforma}
CONFIRMADO=$3
if [ -z "$CEN" ] || [ ! -f "$CEN" ]; then
  echo "uso: $0 cenarios/<cenario>.properties [plataforma|pipeline|pico] [-y]" >&2
  exit 1
fi
case $MODO in plataforma|pipeline|pico) ;; *) echo "modo inválido: $MODO" >&2; exit 1 ;; esac

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi
: "${TRIAIGE_HOST:?defina TRIAIGE_HOST (veja .env.exemplo)}"
: "${TRIAIGE_API_TOKEN:?defina TRIAIGE_API_TOKEN (veja .env.exemplo)}"
: "${TRIAIGE_SENHA:?defina TRIAIGE_SENHA (veja .env.exemplo)}"
export TRIAIGE_HOST TRIAIGE_API_TOKEN TRIAIGE_SENHA
# Cache de DNS na JVM do JMeter: falhas intermitentes de resolução na máquina do gerador
# (UnknownHostException) invalidaram uma rodada inteira. 5 min de cache positivo, sem cache negativo.
export JVM_ARGS="${JVM_ARGS:-} -Dsun.net.inetaddr.ttl=300 -Dsun.net.inetaddr.negative.ttl=0"

[ -f dados/pdfs/sintetico-10p.pdf ] || python3 gerar_pdfs.py

# Mostra o cenário, o plano e (com pipeline) a estimativa de custo. Falha se o cenário estiver incompleto.
python3 calc_carga.py "$CEN" --modo "$MODO"

if [ "$MODO" != plataforma ]; then
  cat <<'EOF'

Pré-requisitos (SPEC §4) antes de ligar o pipeline:
  [ ] Service Quotas do Textract assíncrono (TPS de StartDocumentTextDetection e jobs simultâneos)
  [ ] Limite de requisições por minuto e por dia do Gemini no tier da chave
  [ ] Destino de e-mail de teste (caixa coletora ou SES sandbox), sem e-mail real
  [ ] AWS Budgets com alerta para a janela do teste
EOF
  if [ "$CONFIRMADO" != "-y" ]; then
    printf '\nConfirma a rodada com pipeline ligado? [s/N] '
    read -r resp
    case $resp in s|S|sim|SIM) ;; *) echo "cancelado"; exit 0 ;; esac
  fi
fi

BASE="resultados/$(date +%Y%m%d-%H%M)-$(basename "$CEN" .properties)-$MODO"
mkdir -p "$BASE"
echo "$BASE" > .ultimo-cenario
python3 calc_carga.py "$CEN" --modo "$MODO" > "$BASE/cenario.txt"
RODADAS=$(python3 calc_carga.py "$CEN" --modo "$MODO" --gerar "$BASE")

primeira=1
for nome in $RODADAS; do
  dir="$BASE/$nome"
  [ $primeira = 1 ] || sleep 30
  primeira=0
  echo "$(date +%H:%M:%S) $nome: $(grep -v '^#' "$dir/carga.properties" | tr '\n' ' ')"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$dir/inicio.txt"
  jmeter -n -t triaige-carga-mista.jmx -l "$dir/resultados.jtl" -j "$dir/jmeter.log" \
    -q "$dir/carga.properties" -JpipelineCsv="$dir/pipeline.csv" \
    -Jport="${TRIAIGE_PORT:-80}" -Jprotocol="${TRIAIGE_PROTOCOL:-http}" \
    -Jjmeter.save.saveservice.output_format=csv -e -o "$dir/relatorio" \
    < /dev/null > "$dir/console.txt" 2>&1 || echo "  jmeter terminou com erro (veja $dir/jmeter.log)"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$dir/fim.txt"
  grep "summary =" "$dir/console.txt" | tail -1
done

echo "fim: $BASE"
if [ "$MODO" = plataforma ]; then
  echo "análise: python3 comparar.py $BASE"
else
  echo "análise (aguarde ~5 min para o CloudWatch consolidar): python3 analisar.py $BASE/$MODO"
fi
