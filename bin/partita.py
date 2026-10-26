"""Ciclo di vita di una partita: cartella, registrazione dei processi, lancio dei nodi, chiusura.

Libreria della regia. Scrive i due file che legano una sessione alla partita senza chiedere
niente all'agente: nodi.json — la coppia (pid, starttime) che il bus confronta con gli antenati
del chiamante, più sessionId, genitore e compito — e meta.json, i parametri della partita.
"""

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import grafo as libgrafo

RADICE = libgrafo.radice_orchestra()
PARTITE = RADICE / "partite"
BUS = RADICE / "bin" / "bus"
PROGETTI_CLAUDE = Path.home() / ".claude" / "projects"
TIMEOUT_BASH_MS = 900000
VARIABILI_DA_NON_PASSARE = ("LOOM_TASK", "PROJECT_ROOT", "PTYXIS_PROFILE")
ESPRESSIONE_LANCIO = re.compile(r"backgrounded · ([0-9a-f]{8}) · ")
ATTESA_SERVIZIO_S = 60


def adesso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def avvio_processo(pid):
    with open(f"/proc/{pid}/stat", encoding="utf-8") as file_stat:
        contenuto = file_stat.read()
    return int(contenuto[contenuto.rindex(")") + 2:].split()[19])


def scrivi_json(percorso, dati):
    temporaneo = percorso.with_name(f".{percorso.name}.tmp")
    temporaneo.write_text(json.dumps(dati, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporaneo, percorso)


class Partita:
    def __init__(self, cartella):
        self.cartella = Path(cartella)
        self.id = self.cartella.name

    @property
    def file_nodi(self):
        return self.cartella / "nodi.json"

    @property
    def file_meta(self):
        return self.cartella / "meta.json"

    def nodi(self):
        return json.loads(self.file_nodi.read_text(encoding="utf-8"))

    def meta(self):
        return json.loads(self.file_meta.read_text(encoding="utf-8"))

    def aggiorna_meta(self, **campi):
        dati = self.meta()
        dati.update(campi)
        scrivi_json(self.file_meta, dati)

    def grafo(self):
        return libgrafo.carica(self.cartella / "grafo.json")

    @classmethod
    def crea(cls, nome, gioco, grafo_sorgente, meta):
        """Cartella nuova sotto partite/, grafo copiato e già validato dal chiamante, regia registrata."""
        stampo = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        cartella = PARTITE / f"{stampo}-{nome}"
        (cartella / "caselle").mkdir(parents=True)
        (cartella / "nodi").mkdir()
        shutil.copyfile(grafo_sorgente, cartella / "grafo.json")
        partita = cls(cartella)
        scrivi_json(partita.file_meta, {"partita": partita.id, "gioco": gioco, "inizio": adesso(), **meta})
        scrivi_json(partita.file_nodi, {
            "partita": partita.id,
            "stato": "aperta",
            "nodi": {
                "regia": {
                    "tipo": "script",
                    "pid": os.getpid(),
                    "starttime": avvio_processo(os.getpid()),
                    "lanciata_da_sessione": os.environ.get("CLAUDE_CODE_SESSION_ID"),
                },
            },
        })
        return partita

    def registra(self, id_nodo, dati):
        nodi = self.nodi()
        nodi["nodi"][id_nodo] = dati
        scrivi_json(self.file_nodi, nodi)

    def chiudi(self, esito):
        nodi = self.nodi()
        nodi["stato"] = "chiusa"
        scrivi_json(self.file_nodi, nodi)
        self.aggiorna_meta(fine=adesso(), esito=esito)

    def lancia_nodo(self, id_nodo, prompt, compito, modello=None):
        """Sessione `claude --bg` isolata nella cartella vuota del nodo; restituisce i dati registrati."""
        cwd = self.cartella / "nodi" / id_nodo
        cwd.mkdir(parents=True)
        file_prompt = self.cartella / f"prompt-{id_nodo}.txt"
        file_prompt.write_text(prompt, encoding="utf-8")
        nome = f"orchestra · {self.id} · {id_nodo}"
        comando = [
            "claude", "--bg", "--safe-mode", "--name", nome,
            "--tools", "Bash",
            "--allowedTools", f"Bash({BUS} *)",
            "--permission-mode", "dontAsk",
            "--settings", json.dumps({"env": {
                "BASH_DEFAULT_TIMEOUT_MS": str(TIMEOUT_BASH_MS),
                "BASH_MAX_TIMEOUT_MS": str(TIMEOUT_BASH_MS),
            }}),
        ]
        if modello:
            comando += ["--model", modello]
        comando.append(prompt)
        ambiente = {chiave: valore for chiave, valore in os.environ.items() if chiave not in VARIABILI_DA_NON_PASSARE}
        lancio = subprocess.run(comando, cwd=cwd, env=ambiente, capture_output=True, text=True, timeout=120)
        trovato = ESPRESSIONE_LANCIO.search(lancio.stdout + lancio.stderr)
        if lancio.returncode != 0 or not trovato:
            raise RuntimeError(f"lancio di {id_nodo} fallito (exit {lancio.returncode}): {lancio.stdout}{lancio.stderr}")
        sessione = attendi_sessione(trovato.group(1))
        dati = {
            "tipo": "llm",
            "pid": sessione["pid"],
            "starttime": avvio_processo(sessione["pid"]),
            "sessionId": sessione["sessionId"],
            "short_id": sessione["id"],
            "nome": nome,
            "cwd": str(cwd),
            "genitore": {"nodo": "regia", "pid": os.getpid()},
            "compito": compito,
            "prompt": file_prompt.name,
            "lanciato": adesso(),
            "comando": [parte if parte != prompt else f"<{file_prompt.name}>" for parte in comando],
        }
        self.registra(id_nodo, dati)
        return dati

    def sessioni(self):
        return {id_nodo: dati for id_nodo, dati in self.nodi()["nodi"].items() if dati.get("tipo") == "llm"}

    def attendi_fine_nodi(self, scadenza_s):
        """Aspetta che ogni nodo LLM abbia chiuso il turno; restituisce i nodi ancora al lavoro alla scadenza."""
        limite = time.monotonic() + scadenza_s
        while True:
            stati = stati_sessioni()
            al_lavoro = [id_nodo for id_nodo, dati in self.sessioni().items()
                         if stati.get(dati["short_id"], {}).get("status") == "busy"]
            if not al_lavoro or time.monotonic() >= limite:
                return al_lavoro
            time.sleep(5)

    def ferma_nodi(self):
        for dati in self.sessioni().values():
            subprocess.run(["claude", "stop", dati["short_id"]], capture_output=True, text=True, timeout=60)


def stati_sessioni():
    """short id → voce di `claude agents --json`, per le sessioni vive."""
    uscita = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=60)
    try:
        return {voce["id"]: voce for voce in json.loads(uscita.stdout) if voce.get("id")}
    except json.JSONDecodeError:
        return {}


def attendi_sessione(short_id):
    limite = time.monotonic() + ATTESA_SERVIZIO_S
    while time.monotonic() < limite:
        voce = stati_sessioni().get(short_id)
        if voce and voce.get("pid") and voce.get("sessionId"):
            return voce
        time.sleep(1)
    raise RuntimeError(f"la sessione {short_id} non compare in `claude agents --json` entro {ATTESA_SERVIZIO_S} s")


def transcript(session_id):
    """Il transcript di una sessione, cercato per id fra i progetti di Claude Code."""
    trovati = sorted(PROGETTI_CLAUDE.glob(f"*/{session_id}.jsonl"))
    return trovati[0] if trovati else None


def invia(testo, destinatario):
    """Invio dalla regia: passa dal bus come ogni altro nodo, figlio di un processo registrato come regia."""
    return subprocess.run([str(BUS), "send", destinatario], input=testo, capture_output=True, text=True)


def ricevi(attesa_s):
    """Il primo messaggio nella casella della regia entro attesa_s, come (mittente, id, testo); None alla scadenza."""
    limite = time.monotonic() + attesa_s
    while time.monotonic() < limite:
        restante = max(1, min(840, int(limite - time.monotonic())))
        uscita = subprocess.run([str(BUS), "recv", "--attesa", str(restante)], capture_output=True, text=True)
        if uscita.returncode == 0:
            intestazione, _, testo = uscita.stdout.partition("\n---\n")
            trovato = re.match(r"messaggio (\S+) da (\S+) · grafo v(\d+)", intestazione)
            return trovato.group(2), trovato.group(1), testo.rstrip("\n")
        if uscita.returncode != 3:
            raise RuntimeError(f"bus recv della regia: exit {uscita.returncode}: {uscita.stderr}")
    return None
