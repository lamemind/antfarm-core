"""Ciclo di vita di una partita: cartella, registrazione dei processi, lancio dei nodi, chiusura.

Libreria della regia. Scrive i due file che legano una sessione alla partita senza chiedere
niente all'agente: nodi.json — la coppia (pid, starttime) che il bus confronta con gli antenati
del chiamante, più sessionId, genitore e compito — e meta.json, i parametri della partita.

A fine partita scrive nella voce di ogni nodo `chiuso`, l'ora al millesimo, `azioni`, l'elenco
delle chiamate a tool del transcript che non sono un comando Bash del bus — un comando lo è solo
se ogni suo segmento chiama il bus e niente dentro esegue altro —, e `azioni_fuori_bus`, la sua
lunghezza. Per un nodo senza transcript, come un nodo script, i due campi valgono null.
L'harness legge questi campi da nodi.json e non apre mai i transcript: il formato di Claude Code
si legge solo qui.

Il grafo di una partita può cambiare a partita aperta, e solo la regia lo scrive. `grafo.json` è
la versione corrente, quella che il bus rilegge a ogni comando; `grafi/v<N>.json` conserva ogni
versione, la prima compresa, con l'ora in cui è entrata in vigore. Una versione nuova entra solo
se porta il numero corrente + 1 e `valida-grafo.py` esce 0: altrimenti niente cambia.
"""

import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import grafo as libgrafo

RADICE = libgrafo.radice_orchestra()
PARTITE = RADICE / "partite"
BUS = RADICE / "bin" / "bus"
VALIDATORE_GRAFO = RADICE / "bin" / "valida-grafo.py"
PROGETTI_CLAUDE = Path.home() / ".claude" / "projects"
TIMEOUT_BASH_MS = 900000
VARIABILI_DA_NON_PASSARE = ("LOOM_TASK", "PROJECT_ROOT", "PTYXIS_PROFILE")
ESPRESSIONE_LANCIO = re.compile(r"backgrounded · ([0-9a-f]{8}) · ")
ATTESA_SERVIZIO_S = 60
TIPI_SENZA_PROCESSO = ("umano", "sorgente")
LUNGHEZZA_ESITO_AZIONE = 1000
PREFISSO_RIFIUTO = "Permission to use Bash has been denied"
CHIAMATA_BUS_NEGATA = "chiamata al bus negata"
AZIONE_FUORI_CANALE = "azione fuori dal canale"
ASSEGNAZIONE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)
RIFERIMENTO_VARIABILE = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})")
APERTURA_HEREDOC = re.compile(
    r"<<(?P<meno>-?)[ \t]*(?:(?P<virgoletta>['\"])(?P<parola_quotata>[^'\"]*)(?P=virgoletta)"
    r"|\\(?P<parola_protetta>\S+)|(?P<parola_nuda>[^\s;|&<>()'\"\\]+))")


def adesso(precisione="seconds"):
    return datetime.now(timezone.utc).astimezone().isoformat(timespec=precisione)


def avvio_processo(pid):
    with open(f"/proc/{pid}/stat", encoding="utf-8") as file_stat:
        contenuto = file_stat.read()
    return int(contenuto[contenuto.rindex(")") + 2:].split()[19])


def scrivi_json(percorso, dati):
    temporaneo = percorso.with_name(f".{percorso.name}.tmp")
    temporaneo.write_text(json.dumps(dati, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporaneo, percorso)


class GrafoRifiutato(Exception):
    """Una versione nuova del grafo che non può entrare: numero sbagliato o grafo non valido."""


class Partita:
    def __init__(self, cartella):
        self.cartella = Path(cartella)
        self.id = self.cartella.name
        self.processi_script = {}

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
        """Cartella nuova sotto partite/, grafo copiato e già validato dal chiamante, file delle
        sorgenti creati vuoti, regia registrata."""
        stampo = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        cartella = PARTITE / f"{stampo}-{nome}"
        (cartella / "caselle").mkdir(parents=True)
        (cartella / "nodi").mkdir()
        shutil.copyfile(grafo_sorgente, cartella / "grafo.json")
        partita = cls(cartella)
        partita.prepara_sorgenti()
        partita.conserva_versione(partita.grafo(), adesso("milliseconds"))
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

    def conserva_versione(self, grafo, in_vigore):
        """`grafi/v<N>.json`: il grafo di questa versione e l'ora in cui è entrato in vigore. Il
        replay ne ha bisogno, perché a partita chiusa grafo.json è già l'ultima versione."""
        cartella_versioni = self.cartella / "grafi"
        cartella_versioni.mkdir(exist_ok=True)
        scrivi_json(cartella_versioni / f"v{grafo['versione']}.json", {"in_vigore": in_vigore, "grafo": grafo})

    def scrivi_versione(self, grafo_nuovo):
        """Sostituisce il grafo della partita con una versione nuova e restituisce l'ora in cui è
        entrata in vigore. Solleva GrafoRifiutato, e non cambia niente, se il numero non è il
        corrente + 1 o se `valida-grafo.py` non esce 0 sul grafo nuovo.

        La sostituzione è atomica: il bus legge o la versione vecchia o la nuova, mai un file a
        metà. Il lock esclusivo tiene insieme l'ora e la sostituzione. La versione entra in
        `grafi/` prima di `grafo.json`: chi segue la partita trova il file di una versione
        appena un messaggio la porta."""
        attesa = self.grafo()["versione"] + 1
        if grafo_nuovo.get("versione") != attesa:
            raise GrafoRifiutato(f"versione {grafo_nuovo.get('versione')} invece di {attesa}")
        candidato = self.cartella / ".grafo-candidato.json"
        scrivi_json(candidato, grafo_nuovo)
        try:
            esito = subprocess.run([str(VALIDATORE_GRAFO), str(candidato)], capture_output=True, text=True)
        finally:
            candidato.unlink()
        if esito.returncode != 0:
            raise GrafoRifiutato(f"valida-grafo.py esce {esito.returncode}: {esito.stderr.strip()}")
        with open(self.cartella / libgrafo.LOCK_GRAFO, "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            in_vigore = adesso("milliseconds")
            self.conserva_versione(grafo_nuovo, in_vigore)
            scrivi_json(self.cartella / "grafo.json", grafo_nuovo)
        return in_vigore

    def file_sorgente(self, id_sorgente):
        """Il file di una sorgente del grafo, al suo percorso relativo alla cartella della partita."""
        return self.cartella / libgrafo.nodo(self.grafo(), id_sorgente)["percorso"]

    def prepara_sorgenti(self):
        """Crea vuoto il file di ogni sorgente del grafo: esiste prima di ogni scrittura, quindi
        lo sha256 di partenza e l'oracolo che lo legge hanno sempre un file su cui lavorare."""
        for nodo in self.grafo()["nodi"]:
            if nodo["tipo"] == "sorgente":
                percorso = self.file_sorgente(nodo["id"])
                percorso.parent.mkdir(parents=True, exist_ok=True)
                percorso.touch()

    def registra(self, id_nodo, dati):
        """Scrive la voce del nodo in nodi.json. Un nodo `umano` o `sorgente` del grafo non ha un
        processo: registrarne uno vorrebbe dire che il bus scambia quel processo per l'umano, o per
        un file, e lo timbra come mittente. La regia che ci prova è un errore, e si ferma qui."""
        voce_grafo = libgrafo.nodo(self.grafo(), id_nodo)
        if voce_grafo is not None and voce_grafo["tipo"] in TIPI_SENZA_PROCESSO:
            raise ValueError(f"{id_nodo} è un nodo {voce_grafo['tipo']} del grafo: non ha un processo da registrare")
        nodi = self.nodi()
        nodi["nodi"][id_nodo] = dati
        scrivi_json(self.file_nodi, nodi)

    def segna_chiuso(self, id_nodo, azioni):
        """`chiuso`, `azioni` e il conteggio `azioni_fuori_bus` nella voce del nodo; un nodo già
        chiuso resta com'è. Il conteggio nasce dall'elenco, quindi i due campi non divergono.
        `azioni` None vuol dire nessun transcript da leggere: i due campi restano null."""
        nodi = self.nodi()
        voce = nodi["nodi"][id_nodo]
        if "chiuso" in voce:
            return
        voce["chiuso"] = adesso("milliseconds")
        voce["azioni"] = azioni
        voce["azioni_fuori_bus"] = None if azioni is None else len(azioni)
        scrivi_json(self.file_nodi, nodi)

    def riscrivi_azioni(self):
        """Riscrive `azioni` dai transcript nelle voci che portano già un conteggio, e solo in
        quelle: una voce senza conteggio non emette nodo-chiuso nel replay, o è un nodo script.

        Il conteggio scritto alla chiusura è la misura originale e non cambia. Una voce il cui
        transcript è sparito, o il cui elenco ha una lunghezza diversa dal conteggio, resta com'è
        e finisce fra i problemi. Restituisce nodo → numero di azioni scritte, nodo → problema."""
        nodi = self.nodi()
        scritte, problemi = {}, {}
        for id_nodo, voce in nodi["nodi"].items():
            conteggio = voce.get("azioni_fuori_bus")
            if conteggio is None:
                continue
            percorso = transcript(voce["sessionId"])
            if percorso is None:
                problemi[id_nodo] = f"transcript {voce['sessionId']} assente"
                continue
            azioni = azioni_del_transcript(percorso)
            if len(azioni) != conteggio:
                problemi[id_nodo] = f"{len(azioni)} azioni nel transcript, {conteggio} nel conteggio"
                continue
            voce["azioni"] = azioni
            scritte[id_nodo] = len(azioni)
        scrivi_json(self.file_nodi, nodi)
        return scritte, problemi

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

    def lancia_nodo_script(self, id_nodo, comando, compito):
        """Processo locale figlio della regia. Parte bloccato su stdin e la regia lo sblocca solo
        dopo averlo registrato: prima, il bus troverebbe la regia fra i suoi antenati e lo
        scambierebbe per lei."""
        cwd = self.cartella / "nodi" / id_nodo
        cwd.mkdir(parents=True)
        uscita = open(self.cartella / f"uscita-{id_nodo}.txt", "w", encoding="utf-8")
        processo = subprocess.Popen(comando, cwd=cwd, stdin=subprocess.PIPE, stdout=uscita,
                                    stderr=subprocess.STDOUT, text=True)
        self.processi_script[id_nodo] = processo
        self.registra(id_nodo, {
            "tipo": "script",
            "pid": processo.pid,
            "starttime": avvio_processo(processo.pid),
            "cwd": str(cwd),
            "genitore": {"nodo": "regia", "pid": os.getpid()},
            "compito": compito,
            "lanciato": adesso(),
            "comando": [str(parte) for parte in comando],
        })
        processo.stdin.write("via\n")
        processo.stdin.close()
        return processo

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

    def attendi_nodi_script(self, scadenza_s):
        """Aspetta la fine di ogni nodo script e ne segna la chiusura; solleva TimeoutExpired alla scadenza."""
        for id_nodo, processo in self.processi_script.items():
            processo.wait(timeout=scadenza_s)
            self.segna_chiuso(id_nodo, None)

    def termina_nodi_script(self):
        """Termina i nodi script ancora vivi — una partita chiusa alla scadenza — e ne segna la chiusura."""
        for id_nodo, processo in self.processi_script.items():
            if processo.poll() is None:
                processo.terminate()
                try:
                    processo.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    processo.kill()
                    processo.wait()
            self.segna_chiuso(id_nodo, None)

    def ferma_nodi(self):
        """Ferma ogni sessione, poi ne segna la chiusura con le azioni fuori bus lette dal transcript."""
        for id_nodo, dati in self.sessioni().items():
            subprocess.run(["claude", "stop", dati["short_id"]], capture_output=True, text=True, timeout=60)
            percorso = transcript(dati["sessionId"])
            self.segna_chiuso(id_nodo, azioni_del_transcript(percorso) if percorso else None)

    def termina_bus_rimasti(self):
        """Termina i processi del bus ancora vivi con la cartella di lavoro sotto nodi/ della partita;
        restituisce i loro pid. Una sessione fermata mentre aspetta in `bus recv` può lasciare il
        bus orfano fino alla sua attesa, e il bus orfano leggerebbe un messaggio a partita chiusa."""
        radice = (self.cartella / "nodi").resolve()
        terminati = []
        for voce in Path("/proc").iterdir():
            if not voce.name.isdigit():
                continue
            try:
                cartella_di_lavoro = Path(os.readlink(voce / "cwd"))
                argomenti = (voce / "cmdline").read_bytes().decode(errors="replace").split("\0")
            except OSError:
                continue
            if str(BUS) in argomenti and radice in cartella_di_lavoro.parents:
                try:
                    os.kill(int(voce.name), signal.SIGTERM)
                    terminati.append(int(voce.name))
                except ProcessLookupError:
                    continue
        return terminati


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


def chiamate_bash(percorso_transcript):
    """(nome del tool, comando, esito testuale, is_error) per ogni chiamata a tool del transcript, nell'ordine."""
    usi, risultati = [], {}
    with open(percorso_transcript, encoding="utf-8") as file_transcript:
        for record in map(json.loads, file_transcript):
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


def azioni_del_transcript(percorso_transcript):
    """Le chiamate a tool che non sono un comando Bash del bus, cioè tutto ciò che un nodo fa fuori
    dal canale, come elenco di voci nell'ordine del transcript. Un comando Bash resta fuori solo se
    passa `comando_del_bus`: basta un segmento che non chiama il bus, o una sostituzione di comando,
    e il comando entra intero.

    Il comando resta intero; l'esito si tronca a LUNGHEZZA_ESITO_AZIONE caratteri, perché può
    contenere il testo di un messaggio. `errore` è null se la chiamata non ha ricevuto risposta,
    come un comando interrotto dalla fermata della sessione."""
    fuori_bus = [chiamata for chiamata in chiamate_bash(percorso_transcript)
                 if chiamata[0] != "Bash" or not comando_del_bus(chiamata[1])]
    return [{"ordine": ordine, "tool": nome, "comando": comando, "esito": esito[:LUNGHEZZA_ESITO_AZIONE], "errore": errore}
            for ordine, (nome, comando, esito, errore) in enumerate(fuori_bus, start=1)]


def comando_del_bus(comando):
    """True se il comando non fa altro che chiamare il bus: ogni segmento comincia col path del bus
    e niente, nel testo, esegue un altro comando. È il filtro di `azioni`: il comando che non passa
    entra nell'elenco intero, com'è scritto. Un'assegnazione `B=<bus>` o un `$B send …` non sono
    il path: il comando entra, e a dire che voleva il bus pensa `classe_azione`."""
    if esegue_altro_comando(comando):
        return False
    segmenti = segmenti_del_comando(comando)
    return bool(segmenti) and all(invocazione_del_bus(segmento) for segmento in segmenti)


def invocazione_del_bus(segmento, variabili_del_bus=frozenset()):
    """True se la prima parola del segmento è il path del bus, oppure `$NOME` o `${NOME}` di una
    variabile fra quelle date. Un segmento con le virgolette sbilanciate non si legge: False."""
    try:
        parole = shlex.split(segmento)
    except ValueError:
        return False
    if not parole:
        return False
    riferimento = RIFERIMENTO_VARIABILE.fullmatch(parole[0])
    return parole[0] == str(BUS) or bool(riferimento and (riferimento.group(1) or riferimento.group(2)) in variabili_del_bus)


def esegue_altro_comando(comando):
    """True se il comando esegue qualcosa dentro di sé, oltre ai suoi segmenti: `$(…)`, un backtick,
    `<(…)` o `>(…)` fuori dagli apici singoli, nel testo o nel corpo di un heredoc non quotato.
    `$((…))` è aritmetica, non un comando. Dentro gli apici singoli e nel corpo di un heredoc
    quotato niente si espande, e lì questi caratteri sono testo."""
    testo, corpi = senza_corpi_heredoc(comando)
    return _sostituzione(testo, con_virgolette=True) or any(_sostituzione(corpo, con_virgolette=False) for corpo in corpi)


def _sostituzione(testo, con_virgolette):
    """Il motore di `esegue_altro_comando`. Con `con_virgolette` legge le virgolette come la shell:
    gli apici singoli spengono tutto, le doppie lasciano vivi `$(` e il backtick. Senza, è il corpo
    di un heredoc non quotato: `$(` e backtick vivi ovunque, le virgolette sono caratteri."""
    virgoletta = None
    indice = 0
    while indice < len(testo):
        carattere = testo[indice]
        if carattere == "\\" and virgoletta != "'":
            indice += 2
            continue
        if virgoletta == "'":
            if carattere == "'":
                virgoletta = None
        elif con_virgolette and carattere in "\"'" and (virgoletta is None or carattere == virgoletta):
            virgoletta = None if virgoletta else carattere
        elif carattere == "`":
            return True
        elif testo.startswith("$(", indice) and not testo.startswith("$((", indice):
            return True
        elif virgoletta is None and con_virgolette and carattere in "<>" and testo.startswith("(", indice + 1):
            return True
        indice += 1
    return False


def senza_corpi_heredoc(comando):
    """(il comando senza i corpi dei suoi heredoc, i corpi degli heredoc non quotati).

    `<<PAROLA`, `<<-PAROLA`, `<<'PAROLA'`, `<<"PAROLA"` e `<<\\PAROLA` fuori dalle virgolette aprono
    un heredoc: le righe dopo quella del comando, fino alla riga uguale a PAROLA, sono il testo che
    il comando legge da stdin, non comandi, e qui spariscono insieme alla riga di chiusura. Il corpo
    di un heredoc non quotato espande `$(…)` e i backtick, quindi torna a parte perché qualcuno lo
    guardi; quello quotato o protetto è letterale e non torna. `<<<` è una here-string e non apre
    niente. Senza riga di chiusura il corpo arriva alla fine del comando."""
    testo, corpi, pendenti = [], [], []
    virgoletta = None
    indice = 0
    while indice < len(comando):
        carattere = comando[indice]
        if carattere == "\\" and virgoletta != "'" and indice + 1 < len(comando):
            testo.append(comando[indice:indice + 2])
            indice += 2
            continue
        if virgoletta:
            if carattere == virgoletta:
                virgoletta = None
        elif carattere in "\"'":
            virgoletta = carattere
        elif comando.startswith("<<", indice) and not comando.startswith("<<<", indice) and comando[indice - 1:indice] != "<":
            apertura = APERTURA_HEREDOC.match(comando, indice)
            if apertura:
                parola = apertura.group("parola_quotata") or apertura.group("parola_protetta") or apertura.group("parola_nuda")
                letterale = apertura.group("parola_nuda") is None
                pendenti.append((parola, letterale, apertura.group("meno") == "-"))
                testo.append(apertura.group(0))
                indice = apertura.end()
                continue
        elif carattere == "\n" and pendenti:
            testo.append(carattere)
            indice += 1
            for parola, letterale, meno in pendenti:
                righe_corpo = []
                while indice < len(comando):
                    fine_riga = comando.find("\n", indice)
                    riga = comando[indice:] if fine_riga == -1 else comando[indice:fine_riga]
                    indice = len(comando) if fine_riga == -1 else fine_riga + 1
                    if (riga.lstrip("\t") if meno else riga) == parola:
                        break
                    righe_corpo.append(riga)
                if not letterale:
                    corpi.append("\n".join(righe_corpo))
            pendenti = []
            continue
        testo.append(carattere)
        indice += 1
    return "".join(testo), corpi


def segmenti_del_comando(comando):
    """Il comando spezzato su `;`, `&&`, `||`, `|`, `&` e a capo, fuori dalle virgolette, senza i
    segmenti vuoti. Una barra rovescia protegge il carattere che segue, tranne dentro gli apici.
    `&` da solo separa come `;` — manda in background il segmento prima — ma `>&`, `<&` e `&>` sono
    redirezioni e restano nel segmento. Il corpo di un heredoc non è un segmento: sparisce prima
    del taglio (`senza_corpi_heredoc`), e la riga che lo apre resta."""
    testo, _ = senza_corpi_heredoc(comando)
    segmenti, corrente, virgoletta = [], [], None
    indice = 0
    while indice < len(testo):
        carattere = testo[indice]
        if carattere == "\\" and virgoletta != "'" and indice + 1 < len(testo):
            corrente.append(testo[indice:indice + 2])
            indice += 2
            continue
        if virgoletta:
            if carattere == virgoletta:
                virgoletta = None
        elif carattere in "\"'":
            virgoletta = carattere
        else:
            lunghezza = _lunghezza_separatore(testo, indice)
            if lunghezza:
                segmenti.append("".join(corrente))
                corrente = []
                indice += lunghezza
                continue
        corrente.append(carattere)
        indice += 1
    segmenti.append("".join(corrente))
    return [segmento.strip() for segmento in segmenti if segmento.strip()]


def _lunghezza_separatore(testo, indice):
    """Quanti caratteri occupa il separatore di segmenti che comincia in `indice`, 0 se lì non ce
    n'è: 2 per `&&` e `||`, 1 per `;`, `|`, a capo e `&` solo. `>&`, `<&` e `&>` sono redirezioni."""
    if testo.startswith(("&&", "||"), indice):
        return 2
    carattere = testo[indice]
    if carattere in ";|\n":
        return 1
    if carattere == "&":
        prima = testo[indice - 1] if indice else ""
        dopo = testo[indice + 1] if indice + 1 < len(testo) else ""
        return 0 if prima in ("<", ">") or dopo == ">" else 1
    return 0


def classe_azione(azione):
    """Se una voce di `azioni` è un tentativo negato di usare il bus o un'azione fuori dal canale.

    È una chiamata al bus negata quando l'esito è il rifiuto di `dontAsk`, niente nel comando
    esegue un altro comando (`esegue_altro_comando`) e ogni segmento è un'assegnazione `NOME=…`
    oppure un'invocazione del bus — col path, o con una variabile assegnata al path prima nello
    stesso comando — e almeno uno è un'invocazione: un comando di sole assegnazioni non chiama
    niente. Un segmento con le virgolette sbilanciate non si legge, e ricade fra le azioni fuori
    dal canale."""
    if not azione["esito"].startswith(PREFISSO_RIFIUTO) or esegue_altro_comando(azione["comando"]):
        return AZIONE_FUORI_CANALE
    variabili_del_bus, invocazioni = set(), 0
    for segmento in segmenti_del_comando(azione["comando"]):
        try:
            parole = shlex.split(segmento)
        except ValueError:
            return AZIONE_FUORI_CANALE
        assegnazione = ASSEGNAZIONE.fullmatch(parole[0]) if len(parole) == 1 else None
        if assegnazione:
            if assegnazione.group(2) == str(BUS):
                variabili_del_bus.add(assegnazione.group(1))
            else:
                variabili_del_bus.discard(assegnazione.group(1))
            continue
        if not invocazione_del_bus(segmento, variabili_del_bus):
            return AZIONE_FUORI_CANALE
        invocazioni += 1
    return CHIAMATA_BUS_NEGATA if invocazioni else AZIONE_FUORI_CANALE


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
