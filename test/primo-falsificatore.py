#!/usr/bin/env python3
"""Primo falsificatore del custode dell'orchestrazione: i giochi girano senza l'umano?

    orchestra/test/primo-falsificatore.py [--json]

Per ogni partita del registro delle partite legge i transcript dei suoi nodi LLM, trovati dal
`sessionId` di nodi.json, e conta le voci `user` successive alla prima che non sono un `tool_result`.
La prima voce `user` è il messaggio di lancio della regia. Il formato del transcript si legge qui
direttamente, senza passare da `bin/osserva.py`: questa misura controlla i `turni_umani` che il
motore dichiara nel registro, quindi non ne eredita il parser.

Una partita conta se ha `rapporto.md`, ogni transcript si trova e le voci estranee sono 0. Stampa una
riga per partita e il conteggio dei giochi diversi con almeno una partita che conta.
Registro e partite li legge dalla radice dello stato, ANTFARM_STATE_DIR.
Esce 0 se i giochi diversi sono almeno 3, 1 altrimenti, 2 senza ANTFARM_STATE_DIR.
"""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import grafo as libgrafo  # noqa: E402

STATO = libgrafo.radice_stato()
REGISTRO = STATO / "registro" / "partite.tsv"
PARTITE = STATO / "partite"
PROGETTI_CLAUDE = Path.home() / ".claude" / "projects"
SOGLIA_GIOCHI = 3


def voci_user(percorso):
    voci = (json.loads(riga) for riga in percorso.read_text(encoding="utf-8").splitlines() if riga.strip())
    return [voce for voce in voci if voce.get("type") == "user"]


def e_tool_result(voce):
    contenuto = (voce.get("message") or {}).get("content")
    return isinstance(contenuto, list) and bool(contenuto) and all(isinstance(blocco, dict) and blocco.get("type") == "tool_result" for blocco in contenuto)


def misura_partita(riga):
    cartella = PARTITE / riga["partita"]
    nodi = json.loads((cartella / "nodi.json").read_text(encoding="utf-8"))["nodi"]
    sessioni = {nodo: voce["sessionId"] for nodo, voce in nodi.items() if voce.get("tipo") == "llm"}
    voci_estranee, mancanti, tool_result = 0, [], 0
    for nodo, sessione in sessioni.items():
        trovati = sorted(PROGETTI_CLAUDE.glob(f"*/{sessione}.jsonl"))
        if not trovati:
            mancanti.append(nodo)
            continue
        dopo_il_lancio = voci_user(trovati[0])[1:]
        voci_estranee += sum(not e_tool_result(voce) for voce in dopo_il_lancio)
        tool_result += sum(e_tool_result(voce) for voce in dopo_il_lancio)
    rapporto = (cartella / "rapporto.md").is_file()
    return {"partita": riga["partita"], "gioco": riga["gioco"], "nodi": len(sessioni), "tool_result": tool_result,
            "voci_estranee": voci_estranee, "transcript_mancanti": mancanti, "rapporto": rapporto,
            "conta": rapporto and not mancanti and voci_estranee == 0 and bool(sessioni)}


def main():
    with open(REGISTRO, encoding="utf-8", newline="") as registro:
        misure = [misura_partita(riga) for riga in csv.DictReader(registro, delimiter="\t")]
    giochi = sorted({misura["gioco"] for misura in misure if misura["conta"]})
    if "--json" in sys.argv:
        print(json.dumps({"partite": misure, "giochi": giochi, "soglia": SOGLIA_GIOCHI}, ensure_ascii=False, indent=2))
    else:
        for misura in misure:
            print(f"{misura['partita']} · {misura['gioco']} · nodi {misura['nodi']} · tool_result {misura['tool_result']} · "
                  f"voci estranee {misura['voci_estranee']} · transcript mancanti {misura['transcript_mancanti'] or 0} · "
                  f"rapporto {'sì' if misura['rapporto'] else 'no'} · {'conta' if misura['conta'] else 'NON conta'}")
        print(f"giochi diversi con almeno una partita che conta: {len(giochi)} ({', '.join(giochi)}) · soglia {SOGLIA_GIOCHI}")
    return 0 if len(giochi) >= SOGLIA_GIOCHI else 1


if __name__ == "__main__":
    sys.exit(main())
