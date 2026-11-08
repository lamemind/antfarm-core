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
- spawn sintetico (test/flussi/spawn.jsonl, harness in modo flusso): il figlio compare fra i nodi,
  gli archi del delta fra gli archi — genitore → figlio compreso —, la testata porta la versione
  del delta, e i messaggi da e verso il figlio corrono sugli archi nati dallo spawn
- bottone a secco (copione boom): il nodo umano e la sorgente compaiono coi loro tipi; una riga
  `scritto` del log è un pallino [data-esito=scritto] che corre sull'arco A → bottone e si ferma
  sulla sorgente, e il clic porta il testo della riga
- azioni fuori bus (telefono-riformula del 27 ottobre): il clic su un nodo porta nel nodo-chiuso
  l'elenco delle azioni; il comando di B è uguale carattere per carattere a quello del suo
  transcript, letto qui senza passare dal codice del motore

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
PARTITA_SORGENTE = "2026-11-08-001923-bottone-a-secco-boom"
FLUSSO_SPAWN = ORCHESTRA / "test" / "flussi" / "spawn.jsonl"
PARTITA_AZIONI = "2026-10-27-115351-telefono-riformula"
BUS = ORCHESTRA / "bin" / "bus"
PROGETTI_CLAUDE = Path.home() / ".claude" / "projects"
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
        "() => [...document.querySelectorAll('[data-pallino][data-esito=consegnato], [data-pallino][data-esito=scritto]')]"
        ".every(p => p.hasAttribute('data-arrivato'))",
        timeout=ATTESA_MS,
    )


def misura_dom(pagina):
    return pagina.evaluate("""() => {
        const centro = (elemento) => { const r = elemento.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2, raggio: r.width / 2 }; };
        const nodi = Object.fromEntries([...document.querySelectorAll('[data-nodo]')].map(g => [g.dataset.nodo, { ...centro(g.querySelector('circle')), classi: g.getAttribute('class') }]));
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


def scenario_spawn(pagina, schermate, _log):
    print(f"spawn sintetico {FLUSSO_SPAWN.relative_to(ORCHESTRA)}")
    eventi = [json.loads(testo) for testo in FLUSSO_SPAWN.read_text(encoding="utf-8").split("\n") if testo.strip()]
    grafo = next(evento for evento in eventi if evento["tipo"] == "partita-aperta")["riga"]["grafo"]
    spawn = next(evento for evento in eventi if evento["classe"] == "spawn")
    figlio, genitore = spawn["figlio"]["id"], spawn["genitore"]
    archi_attesi = sorted({f"{a['da']}→{a['a']}" for a in grafo["archi"] + spawn["delta"]["archi"]})
    consegnati_al_figlio = [evento for evento in eventi if evento["tipo"] == "consegnato" and figlio in (evento["riga"]["da"], evento["riga"]["a"])]
    with Harness("flusso", FLUSSO_SPAWN, "--velocita", VELOCITA_REPLAY) as harness:
        apri_e_attendi(pagina, harness.indirizzo)
        dom = misura_dom(pagina)
        verifica(f"il figlio {figlio} compare fra i nodi disegnati", figlio in dom["nodi"], f"nodi: {sorted(dom['nodi'])}")
        verifica(f"nodi disegnati = nodi del grafo v{grafo['versione']} + nodi del delta",
                 len(dom["nodi"]) == len(grafo["nodi"]) + len(spawn["delta"]["nodi"]), f"{len(dom['nodi'])} nel DOM")
        verifica(f"l'arco {genitore} → {figlio} compare nel disegno", f"{genitore}→{figlio}" in dom["archi"])
        verifica("archi disegnati = archi del grafo + archi del delta", sorted(dom["archi"]) == archi_attesi,
                 f"{sorted(dom['archi'])}")
        verifica(f"la testata porta la versione del delta (v{spawn['delta']['versione']})",
                 dom["versione"].startswith(f"v{spawn['delta']['versione']} "), dom["versione"])
        for evento in consegnati_al_figlio:
            riga = evento["riga"]
            pallino = next((p for p in dom["pallini"] if p["chiave"] == f"{evento['partita']}:{evento['indice']}"), None)
            corre = pallino is not None and pallino["corsa"] == f"#{dom['archi'].get(riga['da'] + '→' + riga['a'])}"
            fermo_su = nodo_piu_vicino(dom["nodi"], pallino["posizione"]) if pallino else None
            verifica(f"il messaggio {riga['da']} → {riga['a']} corre sull'arco nato dallo spawn e si ferma su {riga['a']}",
                     corre and fermo_su == riga["a"], f"pallino {pallino and pallino['corsa']}, nodo più vicino {fermo_su}")
        if schermate:
            pagina.locator(f'[data-nodo="{figlio}"]').click()
            pagina.screenshot(path=str(schermate / "vista-spawn-sintetico.png"))


def scenario_sorgente(pagina, schermate, log):
    print(f"bottone a secco {PARTITA_SORGENTE}")
    grafo = json.loads((ORCHESTRA / "partite" / PARTITA_SORGENTE / "grafo.json").read_text(encoding="utf-8"))
    scritti = {indice: riga for indice, riga in log.items()
               if riga.get("partita") == PARTITA_SORGENTE and riga["esito"] == "scritto"}
    with Harness("replay", PARTITA_SORGENTE, "--velocita", VELOCITA_REPLAY) as harness:
        apri_e_attendi(pagina, harness.indirizzo)
        dom = misura_dom(pagina)
        verifica(f"nodi disegnati = nodi di grafo.json ({len(grafo['nodi'])})", len(dom["nodi"]) == len(grafo["nodi"]))
        for voce in grafo["nodi"]:
            if voce["tipo"] in ("umano", "sorgente"):
                verifica(f"il nodo {voce['id']} porta la classe tipo-{voce['tipo']}",
                         f"tipo-{voce['tipo']}" in (dom["nodi"].get(voce["id"], {}).get("classi") or ""))
        pallini_scritti = [p for p in dom["pallini"] if p["esito"] == "scritto"]
        verifica(f"un pallino scritto per riga scritto del log ({len(scritti)})",
                 len(scritti) >= 1 and sorted(p["chiave"] for p in pallini_scritti) == sorted(f"{PARTITA_SORGENTE}:{i}" for i in scritti),
                 f"{len(pallini_scritti)} pallini scritto nel DOM")
        for pallino in pallini_scritti:
            riga = pallino["evento"]["riga"]
            arco = f"{riga['da']}→{riga['a']}"
            fermo_su = nodo_piu_vicino(dom["nodi"], pallino["posizione"])
            verifica(f"pallino scritto {pallino['chiave']}: corsa sull'arco {arco}, fermo su {riga['a']}",
                     pallino["corsa"] == f"#{dom['archi'].get(arco)}" and fermo_su == riga["a"],
                     f"corsa {pallino['corsa']}, nodo più vicino {fermo_su}")
            dettaglio = dettaglio_dopo_clic(pagina, pallino["chiave"])
            indice = pallino["evento"]["indice"]
            verifica(f"clic sul pallino scritto: testo uguale alla riga {indice} del log",
                     dettaglio["riga"]["testo"] == log[indice]["testo"] and dettaglio["riga"]["esito"] == "scritto")
        if schermate:
            pagina.screenshot(path=str(schermate / "vista-bottone-a-secco.png"))


def comandi_fuori_bus_dal_transcript(session_id):
    """I comandi Bash che non sono del bus, letti direttamente dal transcript della sessione:
    l'oracolo del collaudo non passa dal parser del motore che sta collaudando."""
    trovati = sorted(PROGETTI_CLAUDE.glob(f"*/{session_id}.jsonl"))
    if not trovati:
        return None
    comandi = []
    for riga in trovati[0].read_text(encoding="utf-8").splitlines():
        record = json.loads(riga)
        contenuto = (record.get("message") or {}).get("content")
        if record.get("type") != "assistant" or not isinstance(contenuto, list):
            continue
        for blocco in contenuto:
            if blocco.get("type") == "tool_use" and blocco["name"] == "Bash":
                comando = blocco["input"]["command"]
                if not comando.lstrip().startswith(str(BUS)):
                    comandi.append(comando)
    return comandi


def azioni_dopo_clic(pagina, id_nodo):
    """Il clic sul nodo, poi l'elenco `azioni` del suo nodo-chiuso come lo mostra il pannello."""
    pagina.locator(f'[data-nodo="{id_nodo}"] circle').click()
    dettaglio = json.loads(pagina.locator("#dettaglio-json").text_content())
    titolo = pagina.locator("#dettaglio-titolo").text_content()
    chiusure = [evento for evento in dettaglio["eventi"] if evento["tipo"] == "nodo-chiuso"]
    return titolo, chiusure[0]["riga"]["azioni"] if len(chiusure) == 1 else None


def scenario_azioni(pagina, schermate, _log):
    print(f"azioni fuori bus {PARTITA_AZIONI}")
    nodi = json.loads((ORCHESTRA / "partite" / PARTITA_AZIONI / "nodi.json").read_text(encoding="utf-8"))["nodi"]
    with Harness("replay", PARTITA_AZIONI, "--velocita", VELOCITA_REPLAY) as harness:
        apri_e_attendi(pagina, harness.indirizzo)
        pagina.wait_for_selector('[data-nodo="D"].chiuso', state="attached", timeout=ATTESA_MS)
        titolo, azioni_b = azioni_dopo_clic(pagina, "B")
        verifica("il clic su B apre il dettaglio del nodo B", titolo == "nodo · B", titolo)
        comandi_b = comandi_fuori_bus_dal_transcript(nodi["B"]["sessionId"])
        verifica("il transcript di B si legge e porta un comando fuori bus", comandi_b is not None and len(comandi_b) == 1, f"{comandi_b}")
        verifica("clic su B: un'azione, errore no", azioni_b is not None and len(azioni_b) == 1 and azioni_b[0]["errore"] is False, f"{azioni_b}")
        if comandi_b and azioni_b:
            verifica("clic su B: comando uguale carattere per carattere al transcript", azioni_b[0]["comando"] == comandi_b[0],
                     f"{len(azioni_b[0]['comando'])} contro {len(comandi_b[0])} caratteri")
        _, azioni_a = azioni_dopo_clic(pagina, "A")
        verifica("clic su A: un'azione, errore sì", azioni_a is not None and len(azioni_a) == 1 and azioni_a[0]["errore"] is True, f"{azioni_a}")
        _, azioni_c = azioni_dopo_clic(pagina, "C")
        verifica("clic su C: elenco vuoto", azioni_c == [], f"{azioni_c}")
        if schermate:
            azioni_dopo_clic(pagina, "B")
            pagina.screenshot(path=str(schermate / "vista-azioni-fuori-bus.png"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schermate", type=Path)
    argomenti = parser.parse_args()
    if argomenti.schermate:
        argomenti.schermate.mkdir(parents=True, exist_ok=True)
    log = righe_del_log()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for scenario in (scenario_controllo, scenario_rifiuto, scenario_spawn, scenario_sorgente, scenario_azioni):
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
