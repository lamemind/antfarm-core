"""Il cadavere squisito dei surrealisti: ognuno scrive una riga vedendo solo l'ultima del precedente.

Il testo si butta. Il gioco mette alla prova il grafo che cambia a partita aperta: dopo il sesto
messaggio fra nodi la regia scrive la versione 2 del grafo, D entra fra C e A e l'arco C → A sparisce.

A, B, C si passano la riga in cerchio. La regia manda ad A il turno 0. Chi riceve il turno N scrive
una riga sola e la manda al successore come turno N+1; chi riceve l'ultimo turno, il 16, la manda
alla regia e la partita chiude. Il successore lo dice la vista: quando il grafo cambia la regia
avvisa C, che ha appena scritto ed è fermo in `bus recv`, e l'avviso dice di rileggere la vista
senza nominare il successore. Il primo arrivo alla regia da un nodo chiude la partita, anche se
quel nodo ha sbagliato il conto dei turni: il numero del turno lo scrive il nodo, e l'oracolo non
lo usa.

L'oracolo sono quattro controlli deterministici, e l'esito è il numero di controlli superati su 4.
Tre leggono le righe del log dei messaggi; il terzo legge prompt-D.txt, perché misura la vista che
il nodo nuovo riceve dal meccanismo, non la scelta di D di guardarla.
"""

import json
import re
from datetime import datetime
from pathlib import Path

NODI_INIZIALI = ("A", "B", "C")
NODO_NUOVO = "D"
NODI = (*NODI_INIZIALI, NODO_NUOVO)
TURNO_DEL_CAMBIO = 6
ULTIMO_TURNO = 16
CARTELLA = Path(__file__).resolve().parent
GRAFO_V1 = CARTELLA / "grafo-v1.json"
GRAFO_V2 = CARTELLA / "grafo-v2.json"
PRIMA_RIGA = "turno 0: Il cadavere squisito berrà il vino nuovo."
AVVISO = "Il grafo è cambiato: rileggi la tua vista con `bus vista`. Questo messaggio non è un turno."
ESPRESSIONE_TURNO = re.compile(r"\s*turno\s+(\d+)\s*:", re.IGNORECASE)

INGRESSO_A_PARTITA_APERTA = """Entri a partita già cominciata: i turni precedenti non li hai visti, e non ti servono.

"""

PROMPT = """Sei il nodo {nodo} di una partita di «cadavere squisito» nel motore orchestra: un testo scritto a turno, dove ognuno vede solo l'ultima riga del precedente.

{ingresso}La tua vista del grafo, cioè a chi puoi scrivere:

{vista}

Il tuo successore è il nodo, diverso dalla regia, che la vista elenca su «Puoi scrivere a».

Il tuo solo strumento è il bus, un comando Bash:

  {bus} recv
      aspetta il prossimo messaggio per te e lo stampa: la prima riga dice chi lo manda, il testo sta dopo la riga ---. Ogni chiamata restituisce un messaggio solo; se il comando esce senza un messaggio, richiamalo
  {bus} send <nodo> "<testo>"
      consegna il testo al nodo; il testo fra virgolette doppie, su una sola riga
  {bus} vista
      stampa la tua vista del grafo, com'è adesso

Scrivi ogni comando col path intero del bus. Puoi unire l'invio e l'attesa del messaggio dopo in un solo comando: `{bus} send <nodo> "<testo>" && {bus} recv`.

Le regole:

1. Ogni turno arriva come messaggio nella forma `turno N: <riga>`.
2. Quando ricevi il turno N scrivi una riga sola, nuova, di fantasia, di al massimo 12 parole, che continua quella che hai ricevuto. Non riportare mai le righe precedenti.
3. Manda la tua riga al tuo successore nella forma `turno N+1: <la tua riga>`, col numero calcolato.
4. Se hai ricevuto il turno {ultimo}, manda la tua riga alla regia invece che al successore, nella forma `turno {dopo_ultimo}: <la tua riga>`. Alla regia scrivi solo in questo caso.
5. Se la regia ti avvisa che il grafo è cambiato, l'avviso non è un turno: esegui `{bus} vista`, ricava da lì il tuo successore e torna ad aspettare con `{bus} recv`.
6. La partita finisce quando la regia ti ferma. Fino ad allora non chiudere da solo: dopo ogni invio torna ad aspettare con `{bus} recv`.

Comincia eseguendo `{bus} recv`: il primo messaggio può arrivare dopo alcuni minuti, ed è previsto.
"""


def prompt_nodo(nodo, vista, bus):
    """Il prompt di lancio: la regola della riga sola, l'uso del bus, la vista alla versione corrente."""
    ingresso = INGRESSO_A_PARTITA_APERTA if nodo == NODO_NUOVO else ""
    return PROMPT.format(nodo=nodo, ingresso=ingresso, vista=vista, bus=bus, ultimo=ULTIMO_TURNO, dopo_ultimo=ULTIMO_TURNO + 1)


def fra_nodi(righe_log):
    """Le righe `consegnato` fra due nodi del gioco, nell'ordine del log: i turni del cadavere."""
    return [riga for riga in righe_log
            if riga.get("esito") == "consegnato" and riga.get("da") in NODI and riga.get("a") in NODI]


def arrivi(righe_log):
    """Le righe `consegnato` da un nodo del gioco alla regia."""
    return [riga for riga in righe_log
            if riga.get("esito") == "consegnato" and riga.get("da") in NODI and riga.get("a") == "regia"]


def turno_dichiarato(testo):
    """Il numero di turno che il nodo ha scritto nel testo; None se il testo non comincia con `turno N:`."""
    trovato = ESPRESSIONE_TURNO.match(testo or "")
    return int(trovato.group(1)) if trovato else None


def istante(ts):
    return datetime.fromisoformat(ts)


def versione_coerente(riga, in_vigore_v2):
    """La versione di un messaggio contro l'ora di entrata in vigore della v2. Il bus legge ora e
    versione sotto lo stesso lock con cui la regia scrive il cambio, quindi un messaggio con l'ora
    precedente porta la v1 e uno con l'ora successiva la v2. Il log ha il millesimo: nello stesso
    millesimo del cambio valgono entrambe."""
    if in_vigore_v2 is None:
        return riga["versione"] == 1
    partenza, cambio = istante(riga["ts"]), istante(in_vigore_v2)
    if partenza == cambio:
        return riga["versione"] in (1, 2)
    return riga["versione"] == (1 if partenza < cambio else 2)


def oracolo(righe_log, in_vigore_v2, testo_prompt_d):
    """I quattro controlli. `righe_log` sono le righe della partita, `in_vigore_v2` l'ora di
    grafi/v2.json (None se la v2 non è entrata), `testo_prompt_d` il contenuto di prompt-D.txt
    (None se il file manca)."""
    turni = fra_nodi(righe_log)
    versione_sbagliata = [riga for riga in turni if not versione_coerente(riga, in_vigore_v2)]
    c_a_con_v2 = [riga for riga in turni if riga["da"] == "C" and riga["a"] == "A" and riga["versione"] == 2]
    piu_righe = [riga for riga in turni if len((riga["testo"] or "").splitlines()) != 1]
    vista_v2 = testo_prompt_d is not None and "grafo versione 2" in testo_prompt_d
    arco_d_a = testo_prompt_d is not None and "- D → A: " in testo_prompt_d
    controlli = [
        {"controllo": 1, "nome": "versione di partenza", "superato": not versione_sbagliata,
         "evidenza": f"{len(turni)} messaggi fra nodi, {len(versione_sbagliata)} con la versione sbagliata",
         "fuori_regola": [riga["id"] for riga in versione_sbagliata]},
        {"controllo": 2, "nome": "nessuna consegna su C → A con v2", "superato": not c_a_con_v2,
         "evidenza": f"{len(c_a_con_v2)} consegne su C → A con versione 2",
         "fuori_regola": [riga["id"] for riga in c_a_con_v2]},
        {"controllo": 3, "nome": "la vista del nodo nuovo", "superato": vista_v2 and arco_d_a,
         "evidenza": ("prompt-D.txt assente" if testo_prompt_d is None
                      else f"grafo versione 2: {'sì' if vista_v2 else 'no'} · arco D → A: {'sì' if arco_d_a else 'no'}"),
         "fuori_regola": []},
        {"controllo": 4, "nome": "una riga per messaggio", "superato": not piu_righe,
         "evidenza": f"{len(piu_righe)} messaggi fra nodi con più di una riga",
         "fuori_regola": [riga["id"] for riga in piu_righe]},
    ]
    superati = sum(controllo["superato"] for controllo in controlli)
    return {"controlli": controlli, "superati": superati, "esito": f"{superati}/{len(controlli)}", "messaggi_fra_nodi": len(turni)}


def in_vigore_v2(cartella_partita):
    """L'ora in cui la versione 2 è entrata in vigore; None se la partita non ci è arrivata."""
    percorso = Path(cartella_partita) / "grafi" / "v2.json"
    return json.loads(percorso.read_text(encoding="utf-8"))["in_vigore"] if percorso.is_file() else None


def oracolo_della_partita(cartella_partita, righe_log):
    """L'oracolo sui file di una partita: il log, grafi/v2.json, prompt-D.txt."""
    file_prompt = Path(cartella_partita) / f"prompt-{NODO_NUOVO}.txt"
    testo_prompt = file_prompt.read_text(encoding="utf-8") if file_prompt.is_file() else None
    cambio = in_vigore_v2(cartella_partita)
    return {**oracolo(righe_log, cambio, testo_prompt), "in_vigore_v2": cambio}


def letti_dopo_il_cambio(righe_log, in_vigore_v2):
    """I messaggi partiti con la v1 e letti dopo l'entrata in vigore della v2."""
    if in_vigore_v2 is None:
        return []
    cambio = istante(in_vigore_v2)
    letture = {riga["id"]: riga for riga in righe_log if riga.get("esito") == "letto"}
    risultato = []
    for riga in righe_log:
        lettura = letture.get(riga.get("id"))
        if riga.get("esito") == "consegnato" and riga.get("versione") == 1 and lettura and istante(lettura["ts"]) > cambio:
            risultato.append({**riga, "letto": lettura["ts"]})
    return risultato
