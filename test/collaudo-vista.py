#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.60.0"]
# ///
"""Collaudo della vista dal DOM, con Chromium headless pilotato da Playwright. Nessun LLM.

    orchestra/test/collaudo-vista.py [--schermate <cartella>]

Accende l'harness in replay, apre la pagina, aspetta la chiusura del canale e la fine delle corse,
poi conta e misura dal DOM — mai da un contatore della pagina:

- partita di controllo (telefono-copia): elementi [data-nodo] e [data-arco] tanti quanti nodi e
  archi di grafo.json; un [data-pallino] per messaggio consegnato del log, ognuno con la corsa
  sull'arco da → a del suo evento e fermo sul nodo destinatario; il clic su ogni pallino apre il
  dettaglio con id e testo uguali carattere per carattere alla riga del log dei messaggi
- contro-prova del bus: il rifiuto arco-negato A → D è un segno sul nodo A, senza corsa, e nel
  disegno non c'è nessun arco A → D

Playwright è fissato alla versione il cui Chromium è già in ~/.cache/ms-playwright. Con
--schermate salva una schermata per scenario. Esce 1 se una verifica fallisce.
"""

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ORCHESTRA = Path(__file__).resolve().parent.parent
HARNESS = ORCHESTRA / "bin" / "harness.py"
LOG_MESSAGGI = ORCHESTRA / "registro" / "messaggi.jsonl"
PARTITA_CONTROLLO = "2026-10-26-121427-telefono-copia"
PARTITA_CONTRO_PROVA = "2026-10-26-120840-contro-prova-bus"
VELOCITA_REPLAY = "20"
ATTESA_MS = 60000

fallite = 0


def verifica(descrizione, esito, evidenza=""):
    global fallite
    if not esito:
        fallite += 1
    print(f"  {'ok  ' if esito else 'FAIL'} {descrizione}" + (f" — {evidenza}" if evidenza and not esito else ""))


def righe_del_log():
    """Numero di riga da 1 → riga del log. Taglio sul solo a capo, come l'harness."""
    contenuto = LOG_MESSAGGI.read_bytes().split(b"\n")
    return {indice: json.loads(blocco) for indice, blocco in enumerate(contenuto, start=1) if blocco.strip()}


class Harness:
    """L'harness acceso per la durata di uno scenario, su una porta scelta dal sistema."""

    def __init__(self, *argomenti):
        self.argomenti = [str(HARNESS), *argomenti, "--porta", "0"]

    def __enter__(self):
        self.processo = subprocess.Popen(self.argomenti, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        prima_riga = self.processo.stdout.readline()
        trovato = re.search(r"http://127\.0\.0\.1:(\d+)/", prima_riga)
        if not trovato:
            self.processo.kill()
            raise SystemExit(f"l'harness non si accende: {prima_riga}{self.processo.stderr.read()}")
        self.indirizzo = trovato.group(0)
        return self

    def __exit__(self, *_):
        self.processo.terminate()
        self.processo.wait(timeout=10)


def apri_e_attendi(pagina, indirizzo):
    pagina.goto(indirizzo)
    pagina.wait_for_selector('body[data-stato="finito"]', state="attached", timeout=ATTESA_MS)
    pagina.wait_for_function(
        "() => [...document.querySelectorAll('[data-pallino][data-esito=consegnato]')].every(p => p.hasAttribute('data-arrivato'))",
        timeout=ATTESA_MS,
    )


def misura_dom(pagina):
    return pagina.evaluate("""() => {
        const centro = (elemento) => { const r = elemento.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2, raggio: r.width / 2 }; };
        const nodi = Object.fromEntries([...document.querySelectorAll('[data-nodo]')].map(g => [g.dataset.nodo, centro(g.querySelector('circle'))]));
        const archi = Object.fromEntries([...document.querySelectorAll('[data-arco]')].map(g => [g.dataset.arco, g.querySelector('path.arco').id]));
        const pallini = [...document.querySelectorAll('[data-pallino]')].map(p => ({
            chiave: p.dataset.pallino,
            esito: p.dataset.esito,
            evento: JSON.parse(p.dataset.evento),
            corsa: p.querySelector('animateMotion mpath')?.getAttribute('href') ?? null,
            posizione: centro(p),
        }));
        return { nodi, archi, pallini, versione: document.getElementById('versione').textContent };
    }""")


def nodo_piu_vicino(nodi, punto):
    return min(nodi, key=lambda id_nodo: math.dist((nodi[id_nodo]["x"], nodi[id_nodo]["y"]), (punto["x"], punto["y"])))


def dettaglio_dopo_clic(pagina, chiave):
    pagina.locator(f'[data-pallino="{chiave}"]').click()
    return json.loads(pagina.locator("#dettaglio-json").text_content())


def scenario_controllo(pagina, schermate, log):
    print(f"partita di controllo {PARTITA_CONTROLLO}")
    grafo = json.loads((ORCHESTRA / "partite" / PARTITA_CONTROLLO / "grafo.json").read_text(encoding="utf-8"))
    consegnati = {indice: riga for indice, riga in log.items()
                  if riga.get("partita") == PARTITA_CONTROLLO and riga["esito"] == "consegnato"}
    with Harness("replay", PARTITA_CONTROLLO, "--velocita", VELOCITA_REPLAY) as harness:
        apri_e_attendi(pagina, harness.indirizzo)
        dom = misura_dom(pagina)
        verifica(f"nodi disegnati = nodi di grafo.json ({len(grafo['nodi'])})", len(dom["nodi"]) == len(grafo["nodi"]),
                 f"{len(dom['nodi'])} nel DOM")
        verifica(f"archi disegnati = archi di grafo.json ({len(grafo['archi'])})", len(dom["archi"]) == len(grafo["archi"]),
                 f"{len(dom['archi'])} nel DOM")
        verifica("ogni arco del file ha il suo elemento", sorted(dom["archi"]) == sorted(f"{a['da']}→{a['a']}" for a in grafo["archi"]))
        verifica(f"un pallino per messaggio consegnato ({len(consegnati)})",
                 len(dom["pallini"]) == len(consegnati) and all(p["esito"] == "consegnato" for p in dom["pallini"]),
                 f"{len(dom['pallini'])} pallini nel DOM")
        chiavi_attese = sorted(f"{PARTITA_CONTROLLO}:{indice}" for indice in consegnati)
        verifica("le chiavi dei pallini sono partita + indice delle righe consegnate", sorted(p["chiave"] for p in dom["pallini"]) == chiavi_attese)
        for pallino in dom["pallini"]:
            riga = pallino["evento"]["riga"]
            arco_atteso = f"{riga['da']}→{riga['a']}"
            sul_suo_arco = pallino["corsa"] is not None and pallino["corsa"] == f"#{dom['archi'].get(arco_atteso)}"
            fermo_su = nodo_piu_vicino(dom["nodi"], pallino["posizione"])
            destino = dom["nodi"][riga["a"]]
            distanza = math.dist((destino["x"], destino["y"]), (pallino["posizione"]["x"], pallino["posizione"]["y"]))
            verifica(f"pallino {pallino['chiave']}: corsa sull'arco {arco_atteso}, fermo su {riga['a']}",
                     sul_suo_arco and fermo_su == riga["a"] and distanza <= destino["raggio"] * 1.4,
                     f"corsa {pallino['corsa']}, nodo più vicino {fermo_su}, distanza {distanza:.0f}px")
        for pallino in dom["pallini"]:
            indice = pallino["evento"]["indice"]
            dettaglio = dettaglio_dopo_clic(pagina, pallino["chiave"])
            verifica(f"clic su {pallino['chiave']}: id e testo uguali alla riga {indice} del log",
                     dettaglio["riga"]["id"] == log[indice]["id"] and dettaglio["riga"]["testo"] == log[indice]["testo"],
                     f"id {dettaglio['riga']['id']} contro {log[indice]['id']}")
        if schermate:
            pagina.screenshot(path=str(schermate / "vista-telefono-copia.png"))


def scenario_rifiuto(pagina, schermate, log):
    print(f"contro-prova del bus {PARTITA_CONTRO_PROVA}")
    righe = {indice: riga for indice, riga in log.items() if riga.get("partita") == PARTITA_CONTRO_PROVA}
    rifiuti = {indice: riga for indice, riga in righe.items() if riga["esito"] == "rifiutato"}
    consegnati = {indice: riga for indice, riga in righe.items() if riga["esito"] == "consegnato"}
    with Harness("replay", PARTITA_CONTRO_PROVA, "--velocita", VELOCITA_REPLAY) as harness:
        apri_e_attendi(pagina, harness.indirizzo)
        dom = misura_dom(pagina)
        verifica("nessun arco A → D nel disegno", "A→D" not in dom["archi"], f"archi: {sorted(dom['archi'])}")
        verifica(f"un segno per rifiuto e un pallino per consegnato ({len(rifiuti)} + {len(consegnati)})",
                 sum(p["esito"] == "rifiutato" for p in dom["pallini"]) == len(rifiuti)
                 and sum(p["esito"] == "consegnato" for p in dom["pallini"]) == len(consegnati))
        for segno in (p for p in dom["pallini"] if p["esito"] == "rifiutato"):
            riga = segno["evento"]["riga"]
            fermo_su = nodo_piu_vicino(dom["nodi"], segno["posizione"])
            verifica(f"rifiuto {riga['motivo']} {riga['da']} → {riga['a']}: senza corsa, fermo sul mittente {riga['da']}",
                     segno["corsa"] is None and fermo_su == riga["da"], f"corsa {segno['corsa']}, nodo più vicino {fermo_su}")
            dettaglio = dettaglio_dopo_clic(pagina, segno["chiave"])
            indice = segno["evento"]["indice"]
            verifica(f"clic sul rifiuto: motivo e testo uguali alla riga {indice} del log",
                     dettaglio["riga"]["motivo"] == log[indice]["motivo"] and dettaglio["riga"]["testo"] == log[indice]["testo"])
        if schermate:
            pagina.screenshot(path=str(schermate / "vista-contro-prova-bus.png"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schermate", type=Path)
    argomenti = parser.parse_args()
    if argomenti.schermate:
        argomenti.schermate.mkdir(parents=True, exist_ok=True)
    log = righe_del_log()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for scenario in (scenario_controllo, scenario_rifiuto):
            pagina = browser.new_page(viewport={"width": 1400, "height": 900})
            errori_pagina = []
            pagina.on("pageerror", lambda errore: errori_pagina.append(str(errore)))
            scenario(pagina, argomenti.schermate, log)
            verifica("nessun errore JavaScript nella pagina", not errori_pagina, "; ".join(errori_pagina))
            pagina.close()
        browser.close()
    print()
    if fallite:
        print(f"COLLAUDO DELLA VISTA FALLITO: {fallite} verifiche")
        return 1
    print("collaudo della vista: tutte ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
