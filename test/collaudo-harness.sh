#!/usr/bin/env bash
# Collaudo dell'harness di osservabilità col client di prova. Nessun LLM.
#
#   orchestra/test/collaudo-harness.sh
#
# replay — rigioca ogni partita di T42 a velocità 0 e confronta gli eventi ricevuti dal client con le
#   fonti: un evento messaggio per ogni riga della partita nel log dei messaggi ($M), cogli stessi
#   indici e in ordine di ts; un evento di lancio per ogni nodo che nodi.json dà per lanciato; ogni
#   evento valido per valida-eventi.py
# azioni fuori bus — nel replay della partita che ne ha, il nodo-chiuso di ogni nodo porta l'elenco
#   scritto dalla regia in nodi.json, e l'harness non nomina i transcript
# grafo che cambia — nel replay del cadavere a secco partita-aperta porta la versione 1, un solo
#   grafo-cambiato porta il delta e la riga di grafi/v2.json intatta, e arriva prima del lancio di D e
#   del primo messaggio con la versione 2
# sola lettura — sha256 del registro delle partite ($P) e di $M uguale prima e dopo i replay
# porta locale — con l'harness acceso, ss -ltn mostra la porta legata solo a 127.0.0.1, e una
#   connessione con un'origine estranea è rifiutata
# dal vivo a secco — su una copia del motore in una cartella temporanea, come contro-prove.sh:
#   harness acceso, client in ascolto, regia --a-secco; il client riceve ogni riga che la partita
#   aggiunge al $M della copia, un nodo-chiuso per ogni nodo dopo il suo ultimo invio, e stampa il
#   ritardo fra il ts della riga e l'arrivo (mediana, massimo); poi una partita a secco del cadavere,
#   con grafo-cambiato prima del primo messaggio con la versione 2
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
PARTITA_AZIONI=2026-10-27-115351-telefono-riformula
PARTITA_CADAVERE=2026-11-08-114717-cadavere-a-secco
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

echo "replay $PARTITA_AZIONI: il dettaglio delle azioni fuori bus"
accendi "$TMP/harness-azioni.out" "$ORCHESTRA/bin/harness.py" replay "$PARTITA_AZIONI" --velocita 0
EVENTI_AZIONI="$TMP/eventi-azioni.jsonl"
"$ORCHESTRA/bin/ascolta.py" --porta "$PORTA" --eventi "$EVENTI_AZIONI" --scadenza 60 > /dev/null
spegni
verifica "valida-eventi.py esce 0 su ogni evento ricevuto" "$ORCHESTRA/bin/valida-eventi.py" "$EVENTI_AZIONI"
chiuso() { # <nodo> <filtro jq sull'elenco delle azioni>: il nodo-chiuso del nodo è uno solo e passa il filtro
  jq -se --arg nodo "$1" "[.[] | select(.tipo == \"nodo-chiuso\" and .nodo == \$nodo)] | length == 1 and (.[0].riga.azioni | $2)" "$EVENTI_AZIONI"
}
verifica "nodo-chiuso di A: 1 azione, errore sì" chiuso A 'length == 1 and .[0].errore == true and .[0].tool == "Bash"'
verifica "nodo-chiuso di B: 1 azione, errore no" chiuso B 'length == 1 and .[0].errore == false and .[0].esito == "61"'
verifica "nodo-chiuso di C e di D: nessuna azione" bash -c "$(declare -f chiuso); EVENTI_AZIONI='$EVENTI_AZIONI'; chiuso C 'type == \"array\" and length == 0' && chiuso D 'type == \"array\" and length == 0'"
verifica "ogni nodo-chiuso porta tante azioni quante ne conta azioni_fuori_bus" \
  jq -se '[.[] | select(.tipo == "nodo-chiuso")] | length == 4 and all(.riga as $voce | ($voce.azioni | type) == "array" and ($voce.azioni | length) == $voce.azioni_fuori_bus)' "$EVENTI_AZIONI"
verifica "l'elenco dell'evento è quello di nodi.json, intatto" \
  uguali "$(jq -sc '[.[] | select(.tipo == "nodo-chiuso") | {(.nodo): .riga.azioni}] | add' "$EVENTI_AZIONI")" \
  "$(jq -c '[.nodi | to_entries[] | select(.value.chiuso) | {(.key): .value.azioni}] | add' "$ORCHESTRA/partite/$PARTITA_AZIONI/nodi.json")"
verifica "l'harness non nomina i transcript" uguali "$(grep -c '.claude/projects' "$ORCHESTRA/bin/harness.py")" 0

grafo_cambiato_in_ordine() { # <file degli eventi>: grafo-cambiato prima del lancio di D e del primo messaggio con v2
  jq -se 'to_entries as $e
    | ([$e[] | select(.value.tipo == "grafo-cambiato") | .key]) as $cambi
    | ($cambi | length) == 1
      and ([$e[] | select(.value.tipo == "nodo-lanciato" and .value.nodo == "D") | .key] | all(. > $cambi[0]))
      and ([$e[] | select(.value.classe == "messaggio" and .value.riga.versione == 2) | .key] | length > 0 and all(. > $cambi[0]))' "$1"
}

echo "replay $PARTITA_CADAVERE: il grafo che cambia"
accendi "$TMP/harness-cadavere.out" "$ORCHESTRA/bin/harness.py" replay "$PARTITA_CADAVERE" --velocita 0
EVENTI_CADAVERE="$TMP/eventi-cadavere.jsonl"
"$ORCHESTRA/bin/ascolta.py" --porta "$PORTA" --eventi "$EVENTI_CADAVERE" --scadenza 60 > /dev/null
spegni
verifica "valida-eventi.py esce 0 su ogni evento ricevuto" "$ORCHESTRA/bin/valida-eventi.py" "$EVENTI_CADAVERE"
verifica "partita-aperta porta il grafo alla versione 1, non l'ultima" \
  jq -se '[.[] | select(.tipo == "partita-aperta")] | length == 1 and .[0].riga.grafo.versione == 1 and (.[0].riga.grafo.nodi | length) == 4' "$EVENTI_CADAVERE"
verifica "il delta: entra D, sparisce C → A, nascono C → D, D → A, D → regia, versione 2" \
  jq -se '[.[] | select(.tipo == "grafo-cambiato")][0].delta | .versione == 2 and ([.nodi_aggiunti[].id] == ["D"]) and (.nodi_tolti == [])
    and ([.archi_tolti[] | [.da, .a]] == [["C","A"]]) and ([.archi_aggiunti[] | [.da, .a]] | sort == [["C","D"],["D","A"],["D","regia"]])' "$EVENTI_CADAVERE"
verifica "la riga è grafi/v2.json intatto, e il ts è l'ora di entrata in vigore" \
  bash -c "test \"\$(jq -cS 'select(.tipo == \"grafo-cambiato\") | .riga' '$EVENTI_CADAVERE')\" = \"\$(jq -cS . '$ORCHESTRA/partite/$PARTITA_CADAVERE/grafi/v2.json')\" && jq -se '[.[] | select(.tipo == \"grafo-cambiato\")][0] | .ts == .riga.in_vigore' '$EVENTI_CADAVERE'"
verifica "grafo-cambiato prima del lancio di D e del primo messaggio con v2" grafo_cambiato_in_ordine "$EVENTI_CADAVERE"

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
verifica "nodo script: azioni_fuori_bus e azioni null nella voce del nodo-chiuso" \
  jq -se '[.[] | select(.tipo == "nodo-chiuso")] | length == 4 and all(.riga.azioni_fuori_bus == null and .riga.azioni == null and .riga.tipo == "script")' "$TMP/vivo.jsonl"
verifica "ogni nodo-chiuso arriva dopo l'ultimo messaggio inviato da quel nodo" \
  jq -se 'to_entries as $e | [$e[] | select(.value.tipo == "nodo-chiuso")] | all(. as $c |
    ([$e[] | select(.value.classe == "messaggio" and .value.riga.da == $c.value.nodo) | .key] | max // -1) < $c.key)' "$TMP/vivo.jsonl"
printf '  ritardo ts → client: %s\n' "$(jq -c '.ritardo_ms' "$TMP/vivo-riepilogo.json")"

echo "dal vivo, cadavere a secco sulla stessa copia del motore"
accendi "$TMP/harness-cadavere-vivo.out" "$COPIA/bin/harness.py" vivo
"$COPIA/bin/ascolta.py" --porta "$PORTA" --fino-a-chiusura --eventi "$TMP/cadavere-vivo.jsonl" --scadenza 180 \
  > "$TMP/cadavere-vivo-riepilogo.json" &
ASCOLTO_PID=$!
"$COPIA/bin/regia.py" cadavere --a-secco > "$TMP/regia-cadavere.out" 2>&1
verifica "la regia a secco del cadavere chiude la partita riuscita" test $? -eq 0
wait "$ASCOLTO_PID"
verifica "il client si ferma su partita-chiusa" test $? -eq 0
spegni
verifica "valida-eventi.py esce 0 sugli eventi dal vivo" "$COPIA/bin/valida-eventi.py" "$TMP/cadavere-vivo.jsonl"
verifica "dal vivo partita-aperta porta la versione 1" \
  jq -se '[.[] | select(.tipo == "partita-aperta")] | length == 1 and .[0].riga.grafo.versione == 1' "$TMP/cadavere-vivo.jsonl"
verifica "dal vivo grafo-cambiato prima del lancio di D e del primo messaggio con v2" grafo_cambiato_in_ordine "$TMP/cadavere-vivo.jsonl"

echo
if [[ $fallite -gt 0 ]]; then
  echo "COLLAUDO DELL'HARNESS FALLITO: $fallite verifiche"
  exit 1
fi
echo "collaudo dell'harness: tutte ok"
