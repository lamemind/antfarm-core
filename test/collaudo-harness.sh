#!/usr/bin/env bash
# Collaudo dell'harness di osservabilità col client di prova. Nessun LLM.
#
#   orchestra/test/collaudo-harness.sh
#
# replay — rigioca ogni partita di T42 a velocità 0 e confronta gli eventi ricevuti dal client con le
#   fonti: un evento messaggio per ogni riga della partita nel log dei messaggi ($M), cogli stessi
#   indici e in ordine di ts; un evento di lancio per ogni nodo che nodi.json dà per lanciato; ogni
#   evento valido per valida-eventi.py
# sola lettura — sha256 del registro delle partite ($P) e di $M uguale prima e dopo i replay
# porta locale — con l'harness acceso, ss -ltn mostra la porta legata solo a 127.0.0.1, e una
#   connessione con un'origine estranea è rifiutata
# dal vivo a secco — su una copia del motore in una cartella temporanea, come contro-prove.sh:
#   harness acceso, client in ascolto, regia --a-secco; il client riceve ogni riga che la partita
#   aggiunge al $M della copia, un nodo-chiuso per ogni nodo dopo il suo ultimo invio, e stampa il
#   ritardo fra il ts della riga e l'arrivo (mediana, massimo)
#
# Esce 1 se una verifica fallisce.
set -uo pipefail

ORCHESTRA=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
P="$ORCHESTRA/registro/partite.tsv"
M="$ORCHESTRA/registro/messaggi.jsonl"
PARTITE_T42=(
  2026-10-26-120840-contro-prova-bus
  2026-10-26-121310-telefono-a-secco
  2026-10-26-121427-telefono-copia
  2026-10-26-121519-telefono-riformula
)
TMP=$(mktemp -d)
HARNESS_PID=""
trap '[[ -n $HARNESS_PID ]] && kill "$HARNESS_PID" 2>/dev/null; rm -rf "$TMP"' EXIT

fallite=0
verifica() { # <descrizione> <comando...>
  local descrizione=$1; shift
  if "$@" > /dev/null 2>&1; then
    printf '  ok   %s\n' "$descrizione"
  else
    printf '  FAIL %s\n' "$descrizione"
    fallite=$((fallite + 1))
  fi
}

accendi() { # <file d'uscita> <comando dell'harness...>: lo accende in background, legge la porta
  local uscita=$1; shift
  "$@" --porta 0 > "$uscita" 2> "$uscita.err" &
  HARNESS_PID=$!
  for _ in $(seq 200); do grep -q 'http://' "$uscita" 2>/dev/null && break; sleep 0.1; done
  PORTA=$(grep -oP '127\.0\.0\.1:\K[0-9]+' "$uscita")
}

spegni() {
  kill "$HARNESS_PID" 2>/dev/null
  wait "$HARNESS_PID" 2>/dev/null
  HARNESS_PID=""
}

uguali() { [[ "$1" == "$2" ]]; }

echo "sola lettura: impronte prima dei replay"
IMPRONTA_PRIMA=$(sha256sum "$P" "$M")
printf '%s\n' "$IMPRONTA_PRIMA" | sed 's/^/  /'

for partita in "${PARTITE_T42[@]}"; do
  echo "replay $partita"
  accendi "$TMP/harness-$partita.out" "$ORCHESTRA/bin/harness.py" replay "$partita" --velocita 0
  if [[ $partita == "${PARTITE_T42[0]}" ]]; then
    ASCOLTO=$(ss -ltnH "sport = :$PORTA" | awk '{print $4}')
    printf '  ss -ltn: %s\n' "$ASCOLTO"
    verifica "porta $PORTA in ascolto" test -n "$ASCOLTO"
    verifica "porta legata solo a 127.0.0.1, mai a 0.0.0.0 né a ::" \
      bash -c "! grep -qv '^127\\.0\\.0\\.1:$PORTA\$' <<< '$ASCOLTO'"
    ESTRANEA=$("$ORCHESTRA/bin/ascolta.py" --porta "$PORTA" --origine http://esempio.invalid --scadenza 5)
    verifica "pagina d'altra origine rifiutata con 403" \
      jq -e '.fine == "rifiutato" and .chiusura.codice == 403' <<< "$ESTRANEA"
  fi
  EVENTI="$TMP/$partita.jsonl"
  RIEPILOGO=$("$ORCHESTRA/bin/ascolta.py" --porta "$PORTA" --eventi "$EVENTI" --scadenza 60)
  verifica "il canale si chiude da sé a fine replay" jq -e '.fine == "canale-chiuso"' <<< "$RIEPILOGO"
  spegni
  verifica "valida-eventi.py esce 0 su ogni evento ricevuto" "$ORCHESTRA/bin/valida-eventi.py" "$EVENTI"
  RIGHE_ATTESE=$(jq -c --arg p "$partita" 'select(.partita == $p) | input_line_number' "$M" | jq -sc .)
  INDICI_RICEVUTI=$(jq -c '[.indici_messaggi[][1]]' <<< "$RIEPILOGO")
  printf '  righe in $M: %s · eventi messaggio: %s\n' "$(jq length <<< "$RIGHE_ATTESE")" "$(jq length <<< "$INDICI_RICEVUTI")"
  verifica "un evento messaggio per ogni riga della partita in \$M, stessi indici" \
    uguali "$(jq -c sort <<< "$INDICI_RICEVUTI")" "$RIGHE_ATTESE"
  verifica "messaggi in ordine di ts" jq -e '.messaggi_in_ordine_di_ts' <<< "$RIEPILOGO"
  LANCIATI_ATTESI=$(jq -c '[.nodi | to_entries[] | select(.value.lanciato) | .key] | sort' "$ORCHESTRA/partite/$partita/nodi.json")
  LANCIATI_RICEVUTI=$(jq -c '[.nodi_lanciati[][1]] | sort' <<< "$RIEPILOGO")
  verifica "un evento di lancio per ogni nodo lanciato di nodi.json: $LANCIATI_ATTESI" \
    uguali "$LANCIATI_RICEVUTI" "$LANCIATI_ATTESI"
  verifica "una partita-aperta e una partita-chiusa" \
    jq -e '.per_tipo["sessione/partita-aperta"] == 1 and .per_tipo["sessione/partita-chiusa"] == 1' <<< "$RIEPILOGO"
  if [[ $partita == *-telefono-copia ]]; then
    verifica "partita di controllo: 10 messaggi, 5 consegnati e 5 letti" \
      jq -e '.messaggi == 10 and .per_tipo["messaggio/consegnato"] == 5 and .per_tipo["messaggio/letto"] == 5' <<< "$RIEPILOGO"
    verifica "partita di controllo: lanciati A, B, C, D" uguali "$LANCIATI_RICEVUTI" '["A","B","C","D"]'
  fi
done

echo "replay: le quattro partite in un flusso solo"
cat "$TMP"/2026-*.jsonl > "$TMP/tutte.jsonl"
verifica "valida-eventi.py esce 0 sul flusso intero, nessun messaggio emesso due volte" \
  "$ORCHESTRA/bin/valida-eventi.py" "$TMP/tutte.jsonl"

echo "sola lettura: impronte dopo i replay"
IMPRONTA_DOPO=$(sha256sum "$P" "$M")
verifica "sha256 di \$P e \$M uguale prima e dopo" uguali "$IMPRONTA_PRIMA" "$IMPRONTA_DOPO"

echo "dal vivo, partita a secco su una copia del motore"
COPIA="$TMP/motore"
mkdir -p "$COPIA"
cp -r "$ORCHESTRA/bin" "$ORCHESTRA/giochi" "$ORCHESTRA/schema" "$COPIA/"
[[ -d "$ORCHESTRA/vista" ]] && cp -r "$ORCHESTRA/vista" "$COPIA/"
accendi "$TMP/harness-vivo.out" "$COPIA/bin/harness.py" vivo
"$COPIA/bin/ascolta.py" --porta "$PORTA" --fino-a-chiusura --ritardo --eventi "$TMP/vivo.jsonl" --scadenza 120 \
  > "$TMP/vivo-riepilogo.json" &
ASCOLTO_PID=$!
"$COPIA/bin/regia.py" telefono --a-secco > "$TMP/regia.out" 2>&1
verifica "la regia a secco chiude la partita riuscita" test $? -eq 0
wait "$ASCOLTO_PID"
verifica "il client si ferma su partita-chiusa" test $? -eq 0
spegni
sed 's/^/  /' "$TMP/harness-vivo.out.err"
PARTITA_VIVO=$(jq -r 'select(.tipo == "partita-aperta") | .partita' "$TMP/vivo.jsonl")
RIGHE_VIVO=$(jq -c --arg p "$PARTITA_VIVO" 'select(.partita == $p) | input_line_number' "$COPIA/registro/messaggi.jsonl" | jq -sc .)
INDICI_VIVO=$(jq -c '[.indici_messaggi[][1]] | sort' "$TMP/vivo-riepilogo.json")
printf '  partita %s · righe in $M: %s · eventi messaggio: %s\n' "$PARTITA_VIVO" \
  "$(jq length <<< "$RIGHE_VIVO")" "$(jq length <<< "$INDICI_VIVO")"
verifica "ogni riga che la partita aggiunge a \$M arriva al client, stessi indici" uguali "$INDICI_VIVO" "$RIGHE_VIVO"
verifica "valida-eventi.py esce 0 sugli eventi dal vivo" "$COPIA/bin/valida-eventi.py" "$TMP/vivo.jsonl"
verifica "lanciati A, B, C, D" jq -e '[.nodi_lanciati[][1]] | sort == ["A","B","C","D"]' "$TMP/vivo-riepilogo.json"
verifica "un nodo-chiuso per ogni nodo lanciato, scritto dalla regia in nodi.json" \
  jq -e '[.nodi_chiusi[][1]] | sort == ["A","B","C","D"]' "$TMP/vivo-riepilogo.json"
verifica "nodo script: azioni_fuori_bus null nella voce del nodo-chiuso" \
  jq -se '[.[] | select(.tipo == "nodo-chiuso")] | length == 4 and all(.riga.azioni_fuori_bus == null and .riga.tipo == "script")' "$TMP/vivo.jsonl"
verifica "ogni nodo-chiuso arriva dopo l'ultimo messaggio inviato da quel nodo" \
  jq -se 'to_entries as $e | [$e[] | select(.value.tipo == "nodo-chiuso")] | all(. as $c |
    ([$e[] | select(.value.classe == "messaggio" and .value.riga.da == $c.value.nodo) | .key] | max // -1) < $c.key)' "$TMP/vivo.jsonl"
printf '  ritardo ts → client: %s\n' "$(jq -c '.ritardo_ms' "$TMP/vivo-riepilogo.json")"

echo
if [[ $fallite -gt 0 ]]; then
  echo "COLLAUDO DELL'HARNESS FALLITO: $fallite verifiche"
  exit 1
fi
echo "collaudo dell'harness: tutte ok"
