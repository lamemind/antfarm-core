#!/usr/bin/env bash
# Contro-prove deterministiche del motore: validatore del grafo e degli eventi, vista, bus. Nessun LLM.
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

echo "validatore degli eventi"
verifica "evento messaggio ricavato da una riga del log → 0" \
  "$TMP/bin/valida-eventi.py" "$ORCHESTRA/test/eventi/messaggio-valido.jsonl"
verifica "classe fuori dall'elenco → ≠0" \
  bash -c "! '$TMP/bin/valida-eventi.py' '$ORCHESTRA/test/eventi/classe-fuori-elenco.jsonl'"
verifica "evento messaggio senza id della partita → ≠0" \
  bash -c "! '$TMP/bin/valida-eventi.py' '$ORCHESTRA/test/eventi/messaggio-senza-partita.jsonl'"
verifica "flusso sintetico dello spawn → 0" "$TMP/bin/valida-eventi.py" "$ORCHESTRA/test/flussi/spawn.jsonl"
jq -c 'if .classe == "spawn" then .delta.archi |= map(select(.da != "redattore")) else . end' \
  "$ORCHESTRA/test/flussi/spawn.jsonl" > "$TMP/spawn-senza-arco.jsonl"
verifica "spawn senza l'arco genitore → figlio nel delta → ≠0" \
  bash -c "! '$TMP/bin/valida-eventi.py' '$TMP/spawn-senza-arco.jsonl'"

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

echo "sorgente: validatore"
GRAFI="$ORCHESTRA/test/grafi"
verifica "grafo con la sorgente bottone valido → 0" "$TMP/bin/valida-grafo.py" "$GRAFI/sorgente.json"
for invalido in sorgente-percorso-assoluto sorgente-percorso-fuori sorgente-senza-percorso sorgente-che-parla percorso-su-nodo-llm; do
  verifica "$invalido → ≠0" bash -c "! '$TMP/bin/valida-grafo.py' '$GRAFI/$invalido.json'"
done

echo "sorgente: vista"
VISTA_A=$("$TMP/bin/vista.py" "$GRAFI/sorgente.json" A)
VISTA_SB=$("$TMP/bin/vista.py" "$GRAFI/sorgente.json" B)
verifica "vista di A: la sorgente non sta fra i nodi a cui scrivere" grep -qx 'Puoi scrivere a: B' <<< "$VISTA_A"
verifica "vista di A: bottone sulla riga delle sorgenti" \
  grep -qx 'Puoi scrivere i file (sorgenti, con bus scrivi): bottone' <<< "$VISTA_A"
verifica "vista di B: nessuna riga delle sorgenti" bash -c "! grep -q 'sorgenti' <<< '$VISTA_SB'"

echo "sorgente: bus scrivi"
PARTITA="$TMP/partite/prova-sorgente"
mkdir -p "$PARTITA/bottone"
cp "$GRAFI/sorgente.json" "$PARTITA/grafo.json"
printf '{"partita": "prova-sorgente", "stato": "aperta", "nodi": {}}\n' > "$PARTITA/nodi.json"
BOTTONE="$PARTITA/bottone/bottone.txt"
: > "$BOTTONE"
SHA_PRIMA=$(sha256sum "$BOTTONE" | cut -d' ' -f1)
come_nodo B "$BUS" scrivi bottone BOOM
codice_b=$?
verifica "B scrivi bottone esce ≠0" test "$codice_b" -ne 0
verifica "B scrivi bottone: riga arco-negato nel log, da B a bottone" \
  jq -e -s 'any(.[]; .partita=="prova-sorgente" and .esito=="rifiutato" and .motivo=="arco-negato" and .da=="B" and .a=="bottone")' "$LOG"
verifica "B scrivi bottone: sha256 del file uguale prima e dopo" test "$(sha256sum "$BOTTONE" | cut -d' ' -f1)" = "$SHA_PRIMA"
come_nodo A "$BUS" scrivi altro ciao
verifica "A scrivi verso un nodo fuori dal grafo → nodo-ignoto" \
  jq -e -s 'any(.[]; .partita=="prova-sorgente" and .motivo=="nodo-ignoto" and .da=="A" and .a=="altro")' "$LOG"
verifica "A scrivi altro: nessun file nuovo nella partita" \
  test -z "$(find "$PARTITA" -name 'altro*' -print -quit)"
come_nodo A "$BUS" scrivi B "a un nodo"
verifica "A scrivi verso un nodo che non è una sorgente → tipo-errato" \
  jq -e -s 'any(.[]; .partita=="prova-sorgente" and .motivo=="tipo-errato" and .da=="A" and .a=="B")' "$LOG"
come_nodo A "$BUS" send bottone "un messaggio al file"
verifica "A send verso la sorgente → tipo-errato, nessuna casella" \
  bash -c "jq -e -s 'any(.[]; .partita==\"prova-sorgente\" and .motivo==\"tipo-errato\" and .da==\"A\" and .a==\"bottone\")' '$LOG' && test ! -e '$PARTITA/caselle/bottone'"
verifica "dopo i rifiuti il file è ancora intatto" test "$(sha256sum "$BOTTONE" | cut -d' ' -f1)" = "$SHA_PRIMA"
come_nodo A "$BUS" scrivi bottone BOOM
verifica "A scrivi bottone esce 0" test $? -eq 0
verifica "A scrivi bottone: riga scritto nel log, da A a bottone, testo BOOM" \
  jq -e -s 'any(.[]; .partita=="prova-sorgente" and .esito=="scritto" and .da=="A" and .a=="bottone" and .testo=="BOOM" and (.id|startswith("m-")))' "$LOG"
verifica "A scrivi bottone: il file porta BOOM" grep -qx BOOM "$BOTTONE"
jq -c 'select(.partita=="prova-sorgente" and .esito=="scritto") | {classe: "messaggio", tipo: .esito, ts, partita, indice: 1, riga: .}' \
  "$LOG" > "$TMP/scritto.jsonl"
verifica "evento messaggio/scritto dalla riga del log → valido" "$TMP/bin/valida-eventi.py" "$TMP/scritto.jsonl"
sed -i 's/"aperta"/"chiusa"/' "$PARTITA/nodi.json"

echo "oracolo del telefono"
ORACOLO=$(python3 - "$ORCHESTRA/giochi/telefono" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
import fatti, gioco
lista = fatti.genera(42)
partenza = fatti.messaggio_di_partenza(lista)
primo, secondo = lista[0]["numero"], lista[1]["numero"]
perdente = partenza.replace(str(primo), f"{primo // 100}.{primo % 100:02d}").replace(str(secondo), "molti")
righe = [{"esito": "consegnato", "da": da, "a": a, "id": f"m-{indice}", "parole": len(testo.split()), "testo": testo}
         for indice, (da, a, testo) in enumerate([("regia", "A", partenza), ("A", "B", partenza),
                                                 ("B", "C", perdente), ("C", "D", perdente), ("D", "regia", perdente)])]
completo = gioco.oracolo(righe, lista)
interrotto = gioco.oracolo(righe[:3], lista)
print(json.dumps({"esito": completo["esito"], "persi_bc": completo["salti"][2]["persi"], "attesi": [primo, secondo],
                  "interrotto": interrotto["esito"]}))
PY
)
verifica "due numeri persi fra B e C → 8/10" jq -e '.esito == "8/10"' <<< "$ORACOLO"
verifica "i persi sono attribuiti al salto B → C, cifra riformattata compresa" jq -e '.persi_bc == .attesi' <<< "$ORACOLO"
verifica "catena senza C → D: esito interrotta" jq -e '.interrotto == "interrotta C→D"' <<< "$ORACOLO"

echo
if [[ $fallite -gt 0 ]]; then
  echo "CONTRO-PROVE FALLITE: $fallite"
  exit 1
fi
echo "contro-prove: tutte ok"
