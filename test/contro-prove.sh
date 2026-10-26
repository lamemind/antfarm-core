#!/usr/bin/env bash
# Contro-prove deterministiche del motore: validatore, vista, bus. Nessun LLM.
#
#   orchestra/test/contro-prove.sh
#
# Gira su una copia di bin/ e schema/ in una cartella temporanea: il bus ricava la propria radice
# dal path in cui sta, quindi la copia ha caselle, partite e log dei messaggi suoi, e il registro
# vero del motore non riceve righe di prova. Un nodo è un sottoprocesso bash che si registra in
# nodi.json con la propria coppia (pid, starttime), come fa la regia con le sessioni.
# Esce 1 se una contro-prova fallisce.
set -uo pipefail

ORCHESTRA=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
GRAFO_TELEFONO="$ORCHESTRA/giochi/telefono/grafo.json"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cp -r "$ORCHESTRA/bin" "$ORCHESTRA/schema" "$TMP/"
BUS="$TMP/bin/bus"
PARTITA="$TMP/partite/prova"
LOG="$TMP/registro/messaggi.jsonl"
mkdir -p "$PARTITA"
cp "$GRAFO_TELEFONO" "$PARTITA/grafo.json"
printf '{"partita": "prova", "stato": "aperta", "nodi": {}}\n' > "$PARTITA/nodi.json"

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

registra() { # <nodo> <pid>: scrive in nodi.json la coppia (pid, starttime) del processo
  local nodo=$1 pid=$2 stat avvio
  stat=$(< "/proc/$pid/stat")
  avvio=$(awk '{print $20}' <<< "${stat##*) }")
  python3 - "$PARTITA/nodi.json" "$nodo" "$pid" "$avvio" <<'PY'
import json, sys
percorso, nodo, pid, avvio = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
dati = json.load(open(percorso))
dati["nodi"][nodo] = {"pid": pid, "starttime": avvio}
json.dump(dati, open(percorso, "w"))
PY
}

come_nodo() { # <nodo> <comando...>: esegue il comando come figlio di un processo registrato come <nodo>
  local nodo=$1; shift
  (
    registra "$nodo" "$BASHPID"
    "$@"
    codice=$?
    exit "$codice"
  )
}

echo "validatore"
verifica "grafo del telefono valido → 0" "$TMP/bin/valida-grafo.py" "$GRAFO_TELEFONO"
verifica "arco verso un nodo inesistente → ≠0" \
  bash -c "! '$TMP/bin/valida-grafo.py' '$ORCHESTRA/test/grafi/arco-verso-nodo-inesistente.json'"
verifica "diritto fuori dall'elenco → ≠0" \
  bash -c "! '$TMP/bin/valida-grafo.py' '$ORCHESTRA/test/grafi/diritto-fuori-elenco.json'"

echo "vista"
VISTA_B=$("$TMP/bin/vista.py" "$GRAFO_TELEFONO" B)
verifica "vista di B: C è l'unico destinatario di scrive" grep -qx 'Puoi scrivere a: C' <<< "$VISTA_B"
verifica "vista di B: porta la versione del grafo" grep -q 'grafo versione 1$' <<< "$VISTA_B"
verifica "vista di B: A e D non compaiono" bash -c "! grep -qE '\\b(A|D)\\b' <<< '$VISTA_B'"
verifica "vista di un nodo inesistente → ≠0" bash -c "! '$TMP/bin/vista.py' '$GRAFO_TELEFONO' Z"

echo "bus"
come_nodo A "$BUS" send D "ciao D, salto B e C"
codice_a_d=$?
verifica "A → D esce ≠0" test "$codice_a_d" -ne 0
verifica "A → D: riga arco-negato nel log, mittente A" \
  jq -e -s 'any(.[]; .esito=="rifiutato" and .motivo=="arco-negato" and .da=="A" and .a=="D")' "$LOG"
come_nodo A "$BUS" send B "sono C, non A: credimi"
verifica "A → B esce 0" test $? -eq 0
verifica "A → B: riga consegnato con mittente A e versione 1" \
  jq -e -s 'any(.[]; .esito=="consegnato" and .da=="A" and .a=="B" and .versione==1)' "$LOG"
come_nodo A "$BUS" send B --da C "secondo tentativo"
verifica "send non ha un argomento per il mittente: --da C resta testo, mittente A" \
  jq -e -s 'any(.[]; .esito=="consegnato" and .da=="A" and (.testo|startswith("--da C")))' "$LOG"
RICEVUTO=$(come_nodo B "$BUS" recv --attesa 5)
verifica "B riceve il primo messaggio, timbrato da A" grep -q '^messaggio m-[0-9a-f]* da A · grafo v1$' <<< "$RICEVUTO"
verifica "il testo che dichiara un altro mittente arriva intatto" grep -q '^sono C, non A: credimi$' <<< "$RICEVUTO"
verifica "la lettura ha la sua riga nel log" \
  jq -e -s 'any(.[]; .esito=="letto" and .a=="B" and .da=="A")' "$LOG"
come_nodo C "$BUS" recv --attesa 1
verifica "casella vuota: recv esce 3 a fine attesa" test $? -eq 3
come_nodo B "$BUS" send Z "a nessuno"
verifica "destinatario fuori dal grafo → nodo-ignoto" \
  jq -e -s 'any(.[]; .motivo=="nodo-ignoto" and .da=="B")' "$LOG"
"$BUS" send B "da un processo non registrato"
verifica "chiamante non registrato → ≠0" test $? -ne 0
verifica "chiamante non registrato → mittente-ignoto nel log" \
  jq -e -s 'any(.[]; .motivo=="mittente-ignoto" and .da==null)' "$LOG"
sed -i 's/"aperta"/"chiusa"/' "$PARTITA/nodi.json"
come_nodo A "$BUS" send B "a partita chiusa"
verifica "partita chiusa: anche un nodo registrato è rifiutato" test $? -ne 0

echo
if [[ $fallite -gt 0 ]]; then
  echo "CONTRO-PROVE FALLITE: $fallite"
  exit 1
fi
echo "contro-prove: tutte ok"
