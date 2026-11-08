#!/usr/bin/env bash
# Contro-prove deterministiche del motore: validatore del grafo e degli eventi, vista, bus, azioni fuori bus. Nessun LLM.
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

verifica "flusso sintetico del grafo cambiato → 0" "$TMP/bin/valida-eventi.py" "$ORCHESTRA/test/flussi/grafo-cambiato.jsonl"
FLUSSO_CAMBIO="$ORCHESTRA/test/flussi/grafo-cambiato.jsonl"
jq -c 'if .tipo == "grafo-cambiato" then del(.delta) else . end' "$FLUSSO_CAMBIO" > "$TMP/cambio-senza-delta.jsonl"
verifica "grafo-cambiato senza delta → ≠0" bash -c "! '$TMP/bin/valida-eventi.py' '$TMP/cambio-senza-delta.jsonl'"
jq -c 'if .tipo == "grafo-cambiato" then .delta.versione = 3 else . end' "$FLUSSO_CAMBIO" > "$TMP/cambio-altra-versione.jsonl"
verifica "delta con versione diversa da quella del grafo → ≠0" bash -c "! '$TMP/bin/valida-eventi.py' '$TMP/cambio-altra-versione.jsonl'"
jq -c 'if .tipo == "grafo-cambiato" then .ts = "2026-11-08T10:00:11.000+01:00" else . end' "$FLUSSO_CAMBIO" > "$TMP/cambio-altra-ora.jsonl"
verifica "ts dell'evento diverso da in_vigore → ≠0" bash -c "! '$TMP/bin/valida-eventi.py' '$TMP/cambio-altra-ora.jsonl'"
jq -c 'if .tipo == "grafo-cambiato" then .delta.archi_tolti = [{"da": "A", "a": "B", "diritti": ["scrive"]}] else . end' "$FLUSSO_CAMBIO" > "$TMP/cambio-tolto-presente.jsonl"
verifica "un arco tolto ancora presente nel grafo → ≠0" bash -c "! '$TMP/bin/valida-eventi.py' '$TMP/cambio-tolto-presente.jsonl'"
jq -c 'if .tipo == "partita-aperta" then .delta = {"versione": 2, "nodi_aggiunti": [], "nodi_tolti": [], "archi_aggiunti": [], "archi_tolti": []} else . end' "$FLUSSO_CAMBIO" > "$TMP/delta-fuori-posto.jsonl"
verifica "delta su un evento che non è grafo-cambiato → ≠0" bash -c "! '$TMP/bin/valida-eventi.py' '$TMP/delta-fuori-posto.jsonl'"

echo "delta del grafo"
DELTA=$(python3 - "$TMP" <<'PY'
import json, sys
sys.path.insert(0, f"{sys.argv[1]}/bin")
import grafo as libgrafo

arco = lambda da, a, *diritti: {"da": da, "a": a, "diritti": list(diritti)}
nodo = lambda identita, ruolo="x": {"id": identita, "tipo": "llm", "ruolo": ruolo}
prima = {"versione": 1, "nodi": [nodo("A"), nodo("B"), nodo("X")],
         "archi": [arco("A", "B", "conosce", "scrive"), arco("B", "A", "scrive"), arco("A", "X", "scrive")]}
dopo = {"versione": 2, "nodi": [nodo("A"), nodo("B", "nuovo ruolo"), nodo("C")],
        "archi": [arco("A", "B", "scrive", "conosce"), arco("B", "A", "conosce", "scrive"), arco("B", "C", "scrive")]}
print(json.dumps(libgrafo.delta(prima, dopo)))
PY
)
verifica "delta: la versione è quella nuova" jq -e '.versione == 2' <<< "$DELTA"
verifica "delta: nodo nuovo fra gli aggiunti, nodo sparito fra i tolti" \
  jq -e '([.nodi_aggiunti[].id] | index("C")) and ([.nodi_tolti[].id] | index("X"))' <<< "$DELTA"
verifica "delta: un nodo che cambia campo è tolto e aggiunto" \
  jq -e '([.nodi_aggiunti[].id] | index("B")) and ([.nodi_tolti[].id] | index("B"))' <<< "$DELTA"
verifica "delta: gli stessi diritti in altro ordine non sono un cambio" \
  jq -e '[.archi_aggiunti[], .archi_tolti[] | select(.da == "A" and .a == "B")] | length == 0' <<< "$DELTA"
verifica "delta: un arco che cambia diritti è tolto e aggiunto" \
  jq -e '([.archi_aggiunti[] | select(.da == "B" and .a == "A")] | length == 1) and ([.archi_tolti[] | select(.da == "B" and .a == "A")] | length == 1)' <<< "$DELTA"
verifica "delta: arco nuovo fra gli aggiunti, arco sparito fra i tolti" \
  jq -e '([.archi_aggiunti[] | select(.a == "C")] | length == 1) and ([.archi_tolti[] | select(.a == "X")] | length == 1)' <<< "$DELTA"

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

echo "versione nuova del grafo a partita aperta"
PARTITA_VERSIONE=$(python3 - "$TMP" "$GRAFO_TELEFONO" <<'PY'
import copy, hashlib, json, sys
from pathlib import Path

radice, sorgente = sys.argv[1], sys.argv[2]
sys.path.insert(0, f"{radice}/bin")
import partita as libpartita

partita = libpartita.Partita.crea("prova-versione", "prova", sorgente, {})
base = json.loads(Path(sorgente).read_text(encoding="utf-8"))
impronta = lambda: hashlib.sha256((partita.cartella / "grafo.json").read_bytes()).hexdigest()
prima = impronta()
stesso_numero = copy.deepcopy(base)
salta_un_numero = copy.deepcopy(base)
salta_un_numero["versione"] = 3
arco_senza_nodo = copy.deepcopy(base)
arco_senza_nodo["versione"] = 2
arco_senza_nodo["archi"].append({"da": "A", "a": "Z", "diritti": ["scrive"]})
rifiuti = {}
for nome, grafo in (("stesso_numero", stesso_numero), ("salta_un_numero", salta_un_numero), ("arco_senza_nodo", arco_senza_nodo)):
    try:
        partita.scrivi_versione(grafo)
        rifiuti[nome] = None
    except libpartita.GrafoRifiutato as errore:
        rifiuti[nome] = str(errore)
Path(radice, "versione-rifiuti.json").write_text(json.dumps({
    "rifiuti": rifiuti, "impronta_prima": prima, "impronta_dopo": impronta(),
    "versioni": sorted(file.name for file in (partita.cartella / "grafi").iterdir()),
    "nascosti": sorted(file.name for file in partita.cartella.iterdir() if file.name.startswith(".") and file.name != libpartita.libgrafo.LOCK_GRAFO),
}), encoding="utf-8")
print(partita.cartella)
PY
)
RIFIUTI_VERSIONE=$(cat "$TMP/versione-rifiuti.json")
PARTITA=$PARTITA_VERSIONE
verifica "versione non cresciuta → rifiutata" jq -e '.rifiuti.stesso_numero | contains("versione 1 invece di 2")' <<< "$RIFIUTI_VERSIONE"
verifica "versione che salta un numero → rifiutata" jq -e '.rifiuti.salta_un_numero | contains("versione 3 invece di 2")' <<< "$RIFIUTI_VERSIONE"
verifica "arco verso un nodo che non esiste → rifiutato da valida-grafo.py" jq -e '.rifiuti.arco_senza_nodo | contains("nodo inesistente")' <<< "$RIFIUTI_VERSIONE"
verifica "dopo i rifiuti lo sha256 di grafo.json è quello di prima" jq -e '.impronta_prima == .impronta_dopo' <<< "$RIFIUTI_VERSIONE"
verifica "dopo i rifiuti resta solo la versione 1, nessun candidato nascosto" jq -e '.versioni == ["v1.json"] and .nascosti == []' <<< "$RIFIUTI_VERSIONE"
verifica "la prima versione sta in grafi/v1.json con la sua ora" jq -e '.grafo.versione == 1 and (.in_vigore | test("T"))' "$PARTITA/grafi/v1.json"
ID_VERSIONE=$(basename "$PARTITA")
come_nodo A "$BUS" send B "prima del cambio"
verifica "prima del cambio A → B porta versione 1" \
  jq -e -s --arg p "$ID_VERSIONE" 'any(.[]; .partita==$p and .esito=="consegnato" and .da=="A" and .a=="B" and .versione==1)' "$LOG"
IN_VIGORE=$(python3 - "$TMP" "$PARTITA" "$GRAFO_TELEFONO" <<'PY'
import copy, json, sys
from pathlib import Path

radice, cartella, sorgente = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, f"{radice}/bin")
import partita as libpartita

grafo = json.loads(Path(sorgente).read_text(encoding="utf-8"))
grafo["versione"] = 2
grafo["nodi"].append({"id": "E", "tipo": "script", "ruolo": "entra a partita aperta"})
grafo["archi"] = [arco for arco in grafo["archi"] if (arco["da"], arco["a"]) != ("A", "B")]
grafo["archi"].append({"da": "A", "a": "E", "diritti": ["conosce", "scrive"]})
print(libpartita.Partita(cartella).scrivi_versione(grafo))
PY
)
verifica "la versione valida entra: grafo.json porta la 2" jq -e '.versione == 2' "$PARTITA/grafo.json"
verifica "la versione 2 sta in grafi/v2.json, uguale a grafo.json, con la sua ora" \
  bash -c "jq -e --arg ora '$IN_VIGORE' '.in_vigore == \$ora' '$PARTITA/grafi/v2.json' && test \"\$(jq -cS .grafo '$PARTITA/grafi/v2.json')\" = \"\$(jq -cS . '$PARTITA/grafo.json')\""
VISTA_NUOVA=$(come_nodo A "$BUS" vista)
verifica "bus vista legge la versione nuova al comando successivo" grep -q 'grafo versione 2$' <<< "$VISTA_NUOVA"
verifica "bus vista: A scrive a E e non più a B" grep -qx 'Puoi scrivere a: E' <<< "$VISTA_NUOVA"
come_nodo A "$BUS" send B "dopo il cambio"
codice_tolto=$?
verifica "un arco tolto dalla versione nuova esce ≠0" test "$codice_tolto" -ne 0
verifica "A → B dopo il cambio: arco-negato con versione 2" \
  jq -e -s --arg p "$ID_VERSIONE" 'any(.[]; .partita==$p and .esito=="rifiutato" and .motivo=="arco-negato" and .da=="A" and .a=="B" and .versione==2)' "$LOG"
come_nodo A "$BUS" send E "sull'arco nuovo"
verifica "A → E dopo il cambio: consegnato con versione 2" \
  jq -e -s --arg p "$ID_VERSIONE" 'any(.[]; .partita==$p and .esito=="consegnato" and .da=="A" and .a=="E" and .versione==2)' "$LOG"
RICEVUTO_DOPO=$(come_nodo B "$BUS" recv --attesa 5)
verifica "il messaggio partito prima del cambio, letto dopo, conserva la versione 1" grep -q '^messaggio m-[0-9a-f]* da A · grafo v1$' <<< "$RICEVUTO_DOPO"
verifica "ora e versione coincidono: v1 prima di in_vigore, v2 dopo" \
  jq -e -s --arg p "$ID_VERSIONE" --arg ora "$IN_VIGORE" '
    [.[] | select(.partita==$p and .esito=="consegnato")] as $righe
    | ($righe | length) == 2 and all($righe[]; if .versione == 1 then .ts <= $ora else .ts >= $ora end)' "$LOG"
sed -i 's/"aperta"/"chiusa"/' "$PARTITA/nodi.json"

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

echo "nodo umano"
verifica "grafo col nodo umano valido → 0" "$TMP/bin/valida-grafo.py" "$GRAFI/umano.json"
PARTITA="$TMP/partite/prova-umano"
mkdir -p "$PARTITA/caselle" "$PARTITA/nodi"
cp "$GRAFI/umano.json" "$PARTITA/grafo.json"
printf '{"partita": "prova-umano", "stato": "aperta", "nodi": {}}\n' > "$PARTITA/nodi.json"
come_nodo A "$BUS" send umano "B mi chiede una cosa: confermi?"
verifica "A send umano esce 0" test $? -eq 0
verifica "A send umano: una riga consegnato nel log, da A a umano" \
  jq -e -s '[.[] | select(.partita=="prova-umano" and .esito=="consegnato" and .da=="A" and .a=="umano")] | length == 1' "$LOG"
verifica "il messaggio resta nella casella dell'umano" \
  test "$(find "$PARTITA/caselle/umano" -maxdepth 1 -name '[0-9]*.json' | wc -l)" -eq 1
REGISTRA_UMANO=$(python3 - "$TMP/bin" "$PARTITA" <<'PY' 2>&1
import sys
sys.path.insert(0, sys.argv[1])
import partita
try:
    partita.Partita(sys.argv[2]).registra("umano", {"tipo": "umano", "pid": 1, "starttime": 1})
except ValueError as errore:
    print(f"rifiutato: {errore}")
    sys.exit(0)
print("registrato")
sys.exit(1)
PY
)
verifica "la regia non registra un processo per umano" grep -q '^rifiutato: umano è un nodo umano' <<< "$REGISTRA_UMANO"
verifica "nodi.json non porta una voce per umano" jq -e '.nodi | has("umano") | not' "$PARTITA/nodi.json"
CONTEGGIO_UMANO=$(python3 - "$TMP/bin" "$LOG" "$PARTITA" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
import misure
conteggio = misure.nodo_umano(misure.righe_log(sys.argv[2], "prova-umano"), sys.argv[3])
print(json.dumps({"messaggi": len(conteggio["messaggi"]), "letti": conteggio["letti"],
                  "risposte": conteggio["risposte"], "in_casella": conteggio["in_casella"]}))
PY
)
verifica "conteggio del rapporto: 1 messaggio, 0 letti, 0 risposte, 1 in casella" \
  jq -e '.messaggi == 1 and .letti == 0 and .risposte == 0 and .in_casella == 1' <<< "$CONTEGGIO_UMANO"
sed -i 's/"aperta"/"chiusa"/' "$PARTITA/nodi.json"

echo "azioni fuori bus"
AZIONI=$(python3 - "$TMP" <<'PY'
import json, subprocess, sys
from pathlib import Path

radice = Path(sys.argv[1])
sys.path.insert(0, str(radice / "bin"))
import partita as libpartita

libpartita.PROGETTI_CLAUDE = radice / "transcript"
subprocess.run = lambda *argomenti, **opzioni: None  # `claude stop` non ha una sessione da fermare
bus = str(libpartita.BUS)
DENIED = "Permission to use Bash has been denied."


def uso(indice, comando, nome="Bash"):
    return {"type": "tool_use", "id": f"u{indice}", "name": nome, "input": {"command": comando} if comando else {}}


def esito(indice, testo, errore=False):
    return {"type": "tool_result", "tool_use_id": f"u{indice}", "content": testo, "is_error": errore}


def transcript(sessione, blocchi):
    cartella = libpartita.PROGETTI_CLAUDE / "progetto"
    cartella.mkdir(parents=True, exist_ok=True)
    with open(cartella / f"{sessione}.jsonl", "w", encoding="utf-8") as file_transcript:
        for tipo, blocco in blocchi:
            file_transcript.write(json.dumps({"type": tipo, "message": {"content": [blocco]}}) + "\n")


cartella = radice / "partite" / "prova-azioni"
cartella.mkdir(parents=True)
partita = libpartita.Partita(cartella)
voci = {
    "regia": {"tipo": "script"},
    "script": {"tipo": "script", "chiuso": "ieri", "azioni_fuori_bus": None},
    "A": {"tipo": "llm", "short_id": "aaaa", "sessionId": "sess-a"},
    "B": {"tipo": "llm", "short_id": "bbbb", "sessionId": "sess-b"},
    "S": {"tipo": "llm", "short_id": "ssss", "sessionId": "sess-sparito"},
    "C": {"tipo": "llm", "short_id": "cccc", "sessionId": "sess-c"},
}
libpartita.scrivi_json(partita.file_nodi, {"partita": "prova-azioni", "stato": "chiusa", "nodi": voci})
COMANDO_SU_DUE_RIGHE = "echo 'ciao   mondo' | wc -w\n# seconda riga"
transcript("sess-a", [
    ("assistant", uso(1, f"{bus} send B ciao")), ("user", esito(1, "consegnato")),
    ("assistant", uso(2, "cat /etc/hostname")), ("user", esito(2, DENIED, True)),
    ("assistant", uso(3, COMANDO_SU_DUE_RIGHE)), ("user", esito(3, "x" * 5000)),
    ("assistant", uso(4, f"  {bus} recv")), ("user", esito(4, "messaggio")),
    ("assistant", uso(5, "sleep 900")),
])
transcript("sess-b", [("assistant", uso(1, f"{bus} recv")), ("user", esito(1, "messaggio"))])
# Il filtro guarda il comando intero: un pezzo fuori bus lo fa entrare, una catena di soli bus no.
ENTRANO = [
    f"{bus} send A x && cat /etc/hostname",
    f"{bus} send A x; ls",
    f"{bus} send A x | tee f",
    f"{bus} send A x\ncat /etc/hostname",
    f"{bus} send A \"$(cat /etc/hostname)\"",
    f"{bus} send A x & cat /etc/hostname",
    f"{bus} send A `cat /etc/hostname`",
    f"{bus} send A <<EOF\n$(cat /etc/hostname)\nEOF",
    f"{bus} send A <(ls)",
]
RESTANO_FUORI = [
    f"{bus} send A x && {bus} send B y",
    f"{bus} send A \"testo con && dentro\"",
    f"{bus} send A <<'EOF'\ntesto con && dentro\n$(non eseguito)\nEOF",
    f"{bus} send A <<EOF\ntesto con && dentro; ls | wc\nEOF",
    f"{bus} send A x 2>&1",
    f"{bus} send A x &> /dev/null",
    f"{bus} send A 'con `backtick` letterale e $(x)'",
    f"{bus} send A x &&\n  {bus} send B y",
    f"{bus} send A \"$((1+2))\"",
]
blocchi_c = []
for indice, comando in enumerate(sum(zip(ENTRANO, RESTANO_FUORI), ()), start=1):
    blocchi_c += [("assistant", uso(indice, comando)), ("user", esito(indice, "esito"))]
transcript("sess-c", blocchi_c)
partita.ferma_nodi()
dopo_chiusura = partita.nodi()["nodi"]
a, b, s, c = dopo_chiusura["A"], dopo_chiusura["B"], dopo_chiusura["S"], dopo_chiusura["C"]
entrati_c = [azione["comando"] for azione in c["azioni"]]

elenco = a["azioni"]
senza_transcript = {"azioni": s["azioni"], "conteggio": s["azioni_fuori_bus"]}

# Le partite già giocate: la voce porta il conteggio, non l'elenco.
a.pop("azioni")
b.pop("azioni")
s.pop("azioni")
c.pop("azioni")
s["azioni_fuori_bus"] = 3
dopo_chiusura["script"].pop("azioni", None)
dopo_chiusura["N"] = {"tipo": "llm", "sessionId": "sess-b"}  # voce senza conteggio
dopo_chiusura["D"] = {"tipo": "llm", "sessionId": "sess-a", "azioni_fuori_bus": 99}  # conteggio diverso dall'elenco
libpartita.scrivi_json(partita.file_nodi, {"partita": "prova-azioni", "stato": "chiusa", "nodi": dopo_chiusura})
scritte, problemi = partita.riscrivi_azioni()
riscritte = partita.nodi()["nodi"]
impronta = partita.file_nodi.read_bytes()
partita.riscrivi_azioni()
print(json.dumps({
    "elenco": elenco,
    "conteggio_a": a["azioni_fuori_bus"],
    "c_entrati": entrati_c,
    "c_attesi": ENTRANO,
    "c_mancanti": [comando for comando in ENTRANO if comando not in entrati_c],
    "c_fuori_entrati": [comando for comando in RESTANO_FUORI if comando in entrati_c],
    "comando_atteso": COMANDO_SU_DUE_RIGHE,
    "senza_transcript": senza_transcript,
    "scritte": scritte,
    "problemi": problemi,
    "riscritto_a": riscritte["A"].get("azioni"),
    "script_ha_azioni": "azioni" in riscritte["script"],
    "senza_conteggio_ha_azioni": "azioni" in riscritte["N"],
    "diverso": {"azioni": "azioni" in riscritte["D"], "conteggio": riscritte["D"]["azioni_fuori_bus"]},
    "idempotente": impronta == partita.file_nodi.read_bytes(),
}, ensure_ascii=False))
PY
)
verifica "a fine partita: 3 azioni, le chiamate del bus escluse" jq -e '.elenco | length == 3' <<< "$AZIONI"
verifica "il conteggio è la lunghezza dell'elenco" jq -e '.conteggio_a == (.elenco | length)' <<< "$AZIONI"
verifica "un nodo senza transcript alla chiusura: elenco e conteggio null" \
  jq -e '.senza_transcript.azioni == null and .senza_transcript.conteggio == null' <<< "$AZIONI"
verifica "ordine progressivo da 1" jq -e '[.elenco[].ordine] == [1,2,3]' <<< "$AZIONI"
verifica "il comando resta intero, a capo compreso" jq -e '.elenco[1].comando == .comando_atteso' <<< "$AZIONI"
verifica "un rifiuto porta errore sì e il testo del rifiuto" \
  jq -e '.elenco[0].errore == true and (.elenco[0].esito | startswith("Permission to use Bash"))' <<< "$AZIONI"
verifica "l'esito si tronca a 1000 caratteri, errore no" \
  jq -e '.elenco[1].errore == false and (.elenco[1].esito | length) == 1000' <<< "$AZIONI"
verifica "una chiamata senza risposta: esito vuoto, errore null" \
  jq -e '.elenco[2].esito == "" and .elenco[2].errore == null' <<< "$AZIONI"
verifica "verso dentro: ogni comando con un pezzo fuori bus entra, intero e in ordine — &&, ;, |, a capo, \$(…), &, backtick, heredoc non quotato, <(…)" \
  jq -e '.c_mancanti == [] and .c_entrati == .c_attesi' <<< "$AZIONI"
verifica "verso fuori: catena di soli bus, && nelle virgolette, heredoc, 2>&1, &>, backtick fra apici singoli, \$((…)) restano fuori" \
  jq -e '.c_fuori_entrati == []' <<< "$AZIONI"
verifica "riscrittura: stesso elenco della chiusura" jq -e '.riscritto_a == .elenco' <<< "$AZIONI"
verifica "riscrittura: B con 0 azioni porta l'elenco vuoto, C i suoi entrati" \
  jq -e '.scritte.A == 3 and .scritte.B == 0 and .scritte.C == (.c_attesi | length)' <<< "$AZIONI"
verifica "riscrittura: né un nodo script né una voce senza conteggio ricevono l'elenco" \
  jq -e '.script_ha_azioni == false and .senza_conteggio_ha_azioni == false' <<< "$AZIONI"
verifica "riscrittura: conteggio diverso dall'elenco → problema, voce e conteggio intatti" \
  jq -e '(.problemi.D | contains("99 nel conteggio")) and .diverso.azioni == false and .diverso.conteggio == 99' <<< "$AZIONI"
verifica "riscrittura: transcript sparito → problema" jq -e '.problemi.S | contains("assente")' <<< "$AZIONI"
verifica "riscrittura: un secondo giro non cambia il file" jq -e '.idempotente' <<< "$AZIONI"

echo "classe di un'azione"
CLASSI=$(python3 - "$TMP" <<'PY'
import json, sys
from pathlib import Path

radice = Path(sys.argv[1])
sys.path.insert(0, str(radice / "bin"))
import partita as libpartita

bus = str(libpartita.BUS)
NEGATA = "Permission to use Bash has been denied because Claude Code is running in dontAsk mode."


def classe(comando, esito=NEGATA):
    return libpartita.classe_azione({"ordine": 1, "tool": "Bash", "comando": comando, "esito": esito, "errore": True})


print(json.dumps({
    "variabile": classe(f"B={bus}; $B send A x"),
    "virgolette_e_graffe": classe(f"B={bus}; M=\"uno; due && tre\"; $B send A \"$M\" && ${{B}} send C \"$M\""),
    "lettura": classe("cat /etc/hostname"),
    "scrittura": classe(f"echo BOOM > {radice}/bottone.txt"),
    "legge_il_bus": classe(f"cat {bus}"),
    "bus_poi_altro": classe(f"{bus} send A x && cat /etc/hostname"),
    "forma_del_telefono": classe(f"T=\"a; b\"; echo \"$T\" | wc -w; {bus} send B \"$T\""),
    "bus_non_negato": classe(f"B={bus}; $B send A x", "consegnato"),
    "sole_assegnazioni": classe("X=1"),
    "assegnazione_davanti_a_un_comando": classe("X=1 cat /etc/hostname"),
    "variabile_non_del_bus": classe("B=/bin/cat; $B /etc/hostname"),
    "variabile_riassegnata": classe(f"B={bus}; B=/bin/cat; $B /etc/hostname"),
    "virgolette_sbilanciate": classe(f"{bus} send A \"x"),
    "sostituzione": classe(f"{bus} send A \"$(cat /etc/hostname)\""),
    "sostituzione_letterale": classe(f"{bus} send A '$(x)'"),
    "heredoc_quotato": classe(f"{bus} send A <<'EOF'\n$(x) && ls\nEOF"),
    "heredoc_con_sostituzione": classe(f"{bus} send A <<EOF\n$(x)\nEOF"),
    "segmenti": libpartita.segmenti_del_comando("a \"b; c\" && d || e | f\ng; 'h\ni';"),
    "segmenti_heredoc_e_sfondo": libpartita.segmenti_del_comando("cat <<EOF\na; b\nEOF\nls & wc 2>&1 &> f"),
}, ensure_ascii=False))
PY
)
BUS_NEGATA="chiamata al bus negata"
FUORI_CANALE="azione fuori dal canale"
verifica_classe() { # <descrizione> <chiave> <classe attesa>
  verifica "$1" jq -e --arg attesa "$3" ".$2 == \$attesa" <<< "$CLASSI"
}
verifica_classe "B=<bus>; \$B send A x negato → chiamata al bus negata" variabile "$BUS_NEGATA"
verifica_classe "; e && dentro le virgolette non spezzano, \${B} è il bus → chiamata al bus negata" virgolette_e_graffe "$BUS_NEGATA"
verifica_classe "cat /etc/hostname negato → fuori dal canale" lettura "$FUORI_CANALE"
verifica_classe "echo BOOM > <file> negato → fuori dal canale" scrittura "$FUORI_CANALE"
verifica_classe "cat <bus> negato → fuori dal canale" legge_il_bus "$FUORI_CANALE"
verifica_classe "<bus> send A x && cat /etc/hostname negato → fuori dal canale" bus_poi_altro "$FUORI_CANALE"
verifica_classe "la forma del telefono, con echo | wc in mezzo → fuori dal canale" forma_del_telefono "$FUORI_CANALE"
verifica_classe "la stessa forma del bus, non negata → fuori dal canale" bus_non_negato "$FUORI_CANALE"
verifica_classe "sole assegnazioni, negate: nessuna chiamata al bus → fuori dal canale" sole_assegnazioni "$FUORI_CANALE"
verifica_classe "X=1 davanti a un comando non è un'assegnazione → fuori dal canale" assegnazione_davanti_a_un_comando "$FUORI_CANALE"
verifica_classe "\$B assegnata a un altro programma → fuori dal canale" variabile_non_del_bus "$FUORI_CANALE"
verifica_classe "\$B riassegnata dopo il bus → fuori dal canale" variabile_riassegnata "$FUORI_CANALE"
verifica_classe "virgolette sbilanciate → fuori dal canale" virgolette_sbilanciate "$FUORI_CANALE"
verifica_classe "<bus> send A \"\$(cat …)\" negato: la sostituzione esce dal canale → fuori dal canale" sostituzione "$FUORI_CANALE"
verifica_classe "\$(x) fra apici singoli è testo → chiamata al bus negata" sostituzione_letterale "$BUS_NEGATA"
verifica_classe "heredoc quotato con \$(x) && ls nel corpo → chiamata al bus negata" heredoc_quotato "$BUS_NEGATA"
verifica_classe "heredoc non quotato con \$(x) nel corpo → fuori dal canale" heredoc_con_sostituzione "$FUORI_CANALE"
verifica "lo spezzatore divide fuori dalle virgolette e scarta i vuoti" \
  jq -e '.segmenti == ["a \"b; c\"", "d", "e", "f", "g", "'"'"'h\ni'"'"'"]' <<< "$CLASSI"
verifica "lo spezzatore salta il corpo di un heredoc, taglia su & solo e tiene 2>&1 e &>" \
  jq -e '.segmenti_heredoc_e_sfondo == ["cat <<EOF", "ls", "wc 2>&1 &> f"]' <<< "$CLASSI"

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
