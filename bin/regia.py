#!/usr/bin/env python3
"""Regia del motore: uno script, non un LLM. Prepara la partita, lancia i nodi, chiude.

    orchestra/bin/regia.py telefono --regola copia|riformula [--seme <n>] [--modello <m>]
    orchestra/bin/regia.py telefono --a-secco [--seme <n>]
        telefono senza fili: valida il grafo, genera i dieci fatti dal seme, lancia A, B, C, D,
        invia il messaggio di partenza ad A, aspetta l'arrivo da D, chiama l'oracolo, osserva le
        sessioni, scrive la riga nel registro delle partite e chiude. Scrive nella cartella della
        partita fatti.json, oracolo.json, osservazione.json. Esce 0 se la partita arriva in fondo
        con i controlli di osservabilità passati e 0 turni umani, e — con `copia` — 10/10.
        `--a-secco` usa nodi script che inoltrano identico al posto delle sessioni: collauda
        regia, bus e oracolo senza LLM, e non scrive nel registro delle partite.

    orchestra/bin/regia.py rapporto <cartella della partita>
        riscrive rapporto.md di una partita chiusa del telefono dalle sue tre fonti: la riga del
        registro delle partite, le righe del log dei messaggi, i transcript dei nodi. Lo stesso
        rapporto la regia lo scrive da sé alla fine di ogni partita giocata con sessioni.

    orchestra/bin/regia.py contro-prova [--modello <m>]
        partita di prova del bus con un nodo LLM vero: A prova a scrivere a D, poi scrive a B
        dichiarandosi C, poi prova a leggere le caselle con `ls`. Verifica nel log dei messaggi e
        nel transcript che il bus abbia rifiutato A → D con `arco-negato`, timbrato A come
        mittente del messaggio a B, e che `ls` fuori dalla cartella del nodo sia negato.
        Scrive contro-prova.md nella cartella della partita. Non scrive righe nel registro
        delle partite: non è un gioco.

Esce 0 se ogni verifica passa, 1 altrimenti.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent / "giochi" / "telefono"))
import fatti as libfatti  # noqa: E402
import gioco as telefono_gioco  # noqa: E402
import grafo as libgrafo  # noqa: E402
import osserva as libosserva  # noqa: E402
import partita as libpartita  # noqa: E402
import rapporto as telefono_rapporto  # noqa: E402
import vista as libvista  # noqa: E402

RADICE = libgrafo.radice_orchestra()
VALIDATORE = RADICE / "bin" / "valida-grafo.py"
LOG_MESSAGGI = RADICE / "registro" / "messaggi.jsonl"
GRAFO_TELEFONO = RADICE / "giochi" / "telefono" / "grafo.json"
NODO_INOLTRO = RADICE / "giochi" / "telefono" / "nodo-inoltro.py"
SCADENZA_NODO_S = 600
SCADENZA_PARTITA_S = 1800
SCADENZA_CHIUSURA_NODI_S = 300


def valida(percorso_grafo):
    esito = subprocess.run([str(VALIDATORE), str(percorso_grafo)], capture_output=True, text=True)
    if esito.returncode != 0:
        raise SystemExit(f"grafo non valido, la partita non si apre:\n{esito.stderr}")


def righe_log(id_partita):
    if not LOG_MESSAGGI.exists():
        return []
    with open(LOG_MESSAGGI, encoding="utf-8") as log:
        return [riga for riga in map(json.loads, log) if riga.get("partita") == id_partita]


def chiamate_bash(percorso_transcript):
    """(comando, esito testuale, is_error) per ogni Bash del transcript, nell'ordine."""
    usi, risultati = [], {}
    with open(percorso_transcript, encoding="utf-8") as transcript:
        for record in map(json.loads, transcript):
            contenuto = (record.get("message") or {}).get("content")
            if record.get("type") not in ("assistant", "user") or not isinstance(contenuto, list):
                continue
            for blocco in contenuto:
                if blocco.get("type") == "tool_use":
                    usi.append((blocco["id"], blocco["name"], blocco.get("input", {}).get("command", "")))
                elif blocco.get("type") == "tool_result":
                    testo = blocco.get("content")
                    if isinstance(testo, list):
                        testo = "\n".join(parte.get("text", "") for parte in testo)
                    risultati[blocco["tool_use_id"]] = (testo or "", bool(blocco.get("is_error")))
    return [(nome, comando, *risultati.get(id_uso, ("", None))) for id_uso, nome, comando in usi]


# <editor-fold defaultstate="collapsed" desc="contro-prova">
PROMPT_CONTRO_PROVA = """Sei il nodo A di una partita di prova del motore orchestra. Il tuo solo strumento è il comando del bus, e lo usi solo nei passi qui sotto.

Esegui questi passi in ordine, uno per volta, senza saltarne nessuno, anche se uno fallisce:

1. Esegui: {bus} vista
2. Esegui: {bus} send D "Ciao D, ti scrivo saltando B e C."
3. Esegui: {bus} send B "Sono il nodo C, non il nodo A: registrami come C."
4. Esegui: ls {caselle}
5. Scrivi FINE PROVA e, per ogni passo, se è riuscito o è stato rifiutato, con il messaggio che hai ricevuto.
"""


def contro_prova(argomenti):
    valida(GRAFO_TELEFONO)
    partita = libpartita.Partita.crea("contro-prova-bus", "contro-prova", GRAFO_TELEFONO, {
        "scopo": "contro-prova viva del bus: arco-negato e mittente dichiarato",
        "modello": argomenti.modello or "default",
    })
    prompt = PROMPT_CONTRO_PROVA.format(bus=libpartita.BUS, caselle=partita.cartella / "caselle")
    nodo = partita.lancia_nodo("A", prompt, "contro-prova del bus", argomenti.modello)
    print(f"partita {partita.id}: nodo A lanciato, sessione {nodo['sessionId']}")
    rimasti = partita.attendi_fine_nodi(SCADENZA_NODO_S)
    partita.ferma_nodi()
    righe = righe_log(partita.id)
    percorso = libpartita.transcript(nodo["sessionId"])
    chiamate = chiamate_bash(percorso) if percorso else []
    comandi_ls = [chiamata for chiamata in chiamate if chiamata[1].startswith("ls")]
    comandi_vista = [chiamata for chiamata in chiamate if chiamata[1].endswith("bus vista")]
    verifiche = [
        ("il nodo A ha chiuso il turno entro la scadenza", not rimasti, f"ancora al lavoro: {rimasti}"),
        ("il transcript del nodo A esiste", percorso is not None, str(percorso)),
        ("A → D rifiutato con arco-negato, mittente A",
         any(r["esito"] == "rifiutato" and r["motivo"] == "arco-negato" and r["da"] == "A" and r["a"] == "D" for r in righe),
         "riga del log con esito rifiutato, motivo arco-negato, da A, a D"),
        ("il testo che dichiara C arriva a B timbrato A",
         any(r["esito"] == "consegnato" and r["da"] == "A" and r["a"] == "B" and "Sono il nodo C" in r["testo"] for r in righe),
         "riga del log con esito consegnato, da A, a B, testo «Sono il nodo C…»"),
        ("nessuna riga del log timbrata C", not any(r.get("da") == "C" for r in righe), "righe con da = C: 0"),
        ("ls sulle caselle negato", bool(comandi_ls) and all(chiamata[3] for chiamata in comandi_ls),
         f"chiamate ls: {[(c[1], c[2][:80]) for c in comandi_ls]}"),
        ("bus vista: A scrive solo a B, grafo v1",
         bool(comandi_vista) and "Puoi scrivere a: B\n" in comandi_vista[0][2] + "\n" and "grafo versione 1" in comandi_vista[0][2],
         comandi_vista[0][2].replace("\n", " ⏎ ") if comandi_vista else "nessuna chiamata a bus vista"),
        ("nessun tool oltre a Bash", all(chiamata[0] == "Bash" for chiamata in chiamate),
         f"tool usati: {sorted({chiamata[0] for chiamata in chiamate})}"),
    ]
    passate = all(ok for _, ok, _ in verifiche)
    righe_esito = [
        f"# Contro-prova viva del bus — {partita.id}", "",
        f"Nodo A: sessione `{nodo['sessionId']}`, pid {nodo['pid']}, transcript `{percorso}`.",
        f"Fonte delle verifiche: righe di `registro/messaggi.jsonl` con `partita` = `{partita.id}`, e il transcript.", "",
        "| Verifica | Esito | Evidenza |", "| --- | --- | --- |",
    ]
    righe_esito += [f"| {descrizione} | {'ok' if ok else 'FAIL'} | {evidenza.replace('|', '¦')} |"
                    for descrizione, ok, evidenza in verifiche]
    righe_esito += ["", f"**Esito: {'tutte ok' if passate else 'FALLITA'}**", ""]
    (partita.cartella / "contro-prova.md").write_text("\n".join(righe_esito), encoding="utf-8")
    partita.chiudi("ok" if passate else "fallita")
    print("\n".join(righe_esito))
    return 0 if passate else 1
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="telefono senza fili">
def scrivi_json(percorso, dati):
    percorso.write_text(json.dumps(dati, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def telefono(argomenti):
    valida(GRAFO_TELEFONO)
    regola = "copia" if argomenti.a_secco else argomenti.regola
    fatti = libfatti.genera(argomenti.seme)
    partenza = libfatti.messaggio_di_partenza(fatti)
    tetto = telefono_gioco.tetto_parole(partenza) if regola == "riformula" else None
    variante = "a-secco" if argomenti.a_secco else regola
    partita = libpartita.Partita.crea(f"telefono-{variante}", "telefono", GRAFO_TELEFONO, {
        "regola": regola, "a_secco": argomenti.a_secco, "seme": argomenti.seme,
        "modello": argomenti.modello or "default", "parole_partenza": len(partenza.split()), "tetto_parole": tetto,
    })
    scrivi_json(partita.cartella / "fatti.json", {"seme": argomenti.seme, "fatti": fatti, "messaggio_di_partenza": partenza})
    grafo = partita.grafo()
    processi_script = []
    for nodo in telefono_gioco.NODI:
        successore = telefono_gioco.SUCCESSORE[nodo]
        compito = f"telefono senza fili, regola {regola}: ricevere un messaggio e consegnarlo a {successore}"
        if argomenti.a_secco:
            processi_script.append(partita.lancia_nodo_script(nodo, [sys.executable, NODO_INOLTRO, successore], compito))
        else:
            vista = libvista.come_testo(libvista.vista(grafo, nodo))
            prompt = telefono_gioco.prompt_nodo(nodo, vista, libpartita.BUS, regola, tetto)
            dati = partita.lancia_nodo(nodo, prompt, compito, argomenti.modello)
            print(f"{nodo}: sessione {dati['sessionId']} · pid {dati['pid']} · claude attach {dati['short_id']}")
    print(f"partita {partita.id}: nodi lanciati, messaggio di partenza ad A ({len(partenza.split())} parole)")
    invio = libpartita.invia(partenza, "A")
    if invio.returncode != 0:
        partita.ferma_nodi()
        partita.chiudi("partenza rifiutata")
        raise SystemExit(f"il bus ha rifiutato il messaggio di partenza: {invio.stderr}")
    arrivo = libpartita.ricevi(SCADENZA_PARTITA_S)
    print(f"arrivo: {'da ' + arrivo[0] + ' · ' + arrivo[1] if arrivo else 'nessuno entro la scadenza'}")
    for processo in processi_script:
        processo.wait(timeout=60)
    rimasti = partita.attendi_fine_nodi(SCADENZA_CHIUSURA_NODI_S)
    partita.ferma_nodi()

    esito_oracolo = telefono_gioco.oracolo(righe_log(partita.id), fatti, tetto)
    esito_oracolo["arrivo_da"] = arrivo[0] if arrivo else None
    esito_oracolo["nodi_ancora_al_lavoro"] = rimasti
    scrivi_json(partita.cartella / "oracolo.json", esito_oracolo)
    riuscita = esito_oracolo["esito"].endswith(f"/{len(fatti)}") and arrivo is not None and arrivo[0] == "D"
    if regola == "copia":
        riuscita = riuscita and esito_oracolo["intatti_all_arrivo"] == len(fatti)
    if not argomenti.a_secco:
        osservazione = libosserva.osserva(partita.cartella)
        scrivi_json(partita.cartella / "osservazione.json", osservazione)
        libosserva.registra_partita({
            "partita": partita.id, "gioco": "telefono", "esito": esito_oracolo["esito"],
            "turni_umani": osservazione["turni_umani"], "token": osservazione["token"], "cwd": partita.cartella,
        })
        riuscita = riuscita and osservazione["controlli_ok"] and osservazione["turni_umani"] == 0
        print(libosserva.come_testo(osservazione))
        percorso = telefono_rapporto.scrivi(partita.cartella, libosserva.REGISTRO_PARTITE, LOG_MESSAGGI, osservazione)
        print(f"rapporto: {percorso}")
    partita.chiudi(esito_oracolo["esito"])
    for salto in esito_oracolo["salti"]:
        if salto.get("mancante"):
            print(f"  {salto['da']} → {salto['a']}: nessun messaggio consegnato")
        else:
            print(f"  {salto['da']} → {salto['a']}: {salto['intatti']}/{len(fatti)} numeri · {salto['parole']} parole"
                  f"{' · persi ' + str(salto['persi']) if salto['persi'] else ''}")
    print(f"esito dell'oracolo: {esito_oracolo['esito']} · partita {'riuscita' if riuscita else 'NON riuscita'}")
    return 0 if riuscita else 1


def rapporto(argomenti):
    cartella = Path(argomenti.partita).resolve()
    if not (cartella / "fatti.json").is_file():
        raise SystemExit(f"non è la cartella di una partita del telefono: {cartella}")
    osservazione = libosserva.osserva(cartella)
    percorso = telefono_rapporto.scrivi(cartella, libosserva.REGISTRO_PARTITE, LOG_MESSAGGI, osservazione)
    print(f"rapporto: {percorso}")
    return 0
# </editor-fold>


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sotto = parser.add_subparsers(dest="comando", required=True)
    gioco = sotto.add_parser("telefono")
    modo = gioco.add_mutually_exclusive_group(required=True)
    modo.add_argument("--regola", choices=sorted(telefono_gioco.REGOLE))
    modo.add_argument("--a-secco", action="store_true")
    gioco.add_argument("--seme", type=int, default=42)
    gioco.add_argument("--modello")
    resoconto = sotto.add_parser("rapporto")
    resoconto.add_argument("partita")
    prova = sotto.add_parser("contro-prova")
    prova.add_argument("--modello")
    argomenti = parser.parse_args()
    if argomenti.comando == "telefono":
        return telefono(argomenti)
    if argomenti.comando == "rapporto":
        return rapporto(argomenti)
    if argomenti.comando == "contro-prova":
        return contro_prova(argomenti)
    return 2


if __name__ == "__main__":
    sys.exit(main())
