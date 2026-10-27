Stato e requisiti rinviati: factory · T41 §Custodia — un limite di questo motore si segnala lì, non si apre una task qui.

# orchestra

Il motore minimo dell'orchestrazione agentica: sessioni che si parlano lungo un **grafo di accesso**, e una regia che le lancia per giocare partite col proprio oracolo.

## I quattro pezzi

- **il grafo** — `grafo.json` di una partita: nodi, archi orientati con un insieme di diritti (`conosce` · `legge` · `scrive` · `modifica` · `spawn`), versione. `bin/valida-grafo.py <grafo.json>` lo controlla contro `schema/grafo.schema.json` e contro la coerenza che lo schema non esprime (estremi esistenti, coppie non ripetute): esce 0 o 1, ed è il gate che la regia passa prima di aprire una partita
- **la vista per agente** — `bin/vista.py <grafo.json> <nodo>` stampa ciò che un nodo riceve: i suoi archi uscenti, i nodi verso cui ha ogni diritto, la versione. Gli archi entranti e il resto del grafo non ci sono
- **il bus** — `bin/bus send <destinatario> [testo]`, `bin/bus recv`, `bin/bus vista`. È il solo pezzo che fa rispettare il grafo: controlla l'arco `scrive`, timbra mittente e versione del grafo, deposita nella casella del destinatario, e scrive ogni invio, rifiuto e lettura nel log dei messaggi. Il mittente non è un argomento: il bus risale `/proc` e il chiamante è il primo antenato che una partita aperta ha registrato in `nodi.json` come coppia (pid, starttime). Motivi di rifiuto: `arco-negato`, `nodo-ignoto`, `mittente-ignoto`
- **la regia** — `bin/regia.py`, uno script e non un LLM: prepara la partita, lancia i nodi, registra i loro processi, invia il messaggio di partenza, aspetta l'arrivo, chiama l'oracolo, scrive la riga del registro e il rapporto, chiude la partita

## Dove sta cosa

| Path | Contenuto |
| --- | --- |
| `registro/partite.tsv` | registro delle partite (`$P`): una riga per partita, colonne `partita · gioco · esito · turni_umani · token · cwd` |
| `registro/messaggi.jsonl` | log dei messaggi (`$M`): una riga per invio consegnato, invio rifiutato, lettura; ogni riga porta l'id della partita e la versione del grafo |
| `template/` | template di spawn (`$G`) |
| `giochi/<gioco>/` | definizione di un gioco: grafo, prompt dei nodi, regole |
| `partite/<partita>/` | una partita: `grafo.json`, `nodi.json`, `meta.json`, `caselle/<nodo>/`, `nodi/<nodo>/` (cwd della sessione del nodo), `rapporto.md` |
| `schema/` | schema del grafo e schema degli eventi dell'harness |
| `vista/` | la pagina della vista web, servita dall'harness |
| `test/contro-prove.sh` | contro-prove deterministiche dei due validatori, della vista per agente e del bus, su una copia isolata del motore |
| `test/eventi/`, `test/flussi/` | eventi di contro-prova del validatore, flussi di eventi sintetici per l'harness |

## Collaudo

- `test/contro-prove.sh` — senza LLM, su una copia del motore in una cartella temporanea: il registro vero non riceve righe
- `test/collaudo-harness.sh` — senza LLM: rigioca le partite di riferimento e confronta gli eventi ricevuti dal client di prova (`bin/ascolta.py`) col log dei messaggi e con `nodi.json`; controlla che registro e log non cambino, che la porta sia legata solo a `127.0.0.1`, e gioca una partita a secco dal vivo su una copia del motore, misurando il ritardo fra la riga del log e l'arrivo al client
- `test/collaudo-vista.py` — senza LLM, Chromium headless pilotato da Playwright: conta nodi, archi e pallini dal DOM contro `grafo.json` e il log, controlla che ogni pallino corra sul proprio arco, che il rifiuto resti sul mittente, che il clic porti la riga del log intatta, che lo spawn sintetico aggiunga figlio e archi
- `bin/regia.py contro-prova` — un nodo LLM vero prova a scrivere fuori dal grafo, a dichiararsi un altro nodo e a leggere le caselle con `ls`; la regia verifica log e transcript e scrive `contro-prova.md` nella partita. Una partita `*-contro-prova-*` è un collaudo del motore, non un gioco: non ha riga nel registro delle partite, e le sue righe nel log dei messaggi — `arco-negato` compreso — si riconoscono dall'id della partita

## I giochi

- **telefono senza fili** — `giochi/telefono/`. Catena `regia → A → B → C → D → regia`; il messaggio di partenza porta dieci fatti con un numero di tre cifre ciascuno, generati da un seme (`fatti.py`). Due regole (`gioco.py`): `copia` inoltra identico ed è la partita di controllo del motore — sotto 10/10 è un difetto del motore —, `riformula` riscrive con parole proprie entro il 60% delle parole di partenza. L'oracolo conta, salto per salto dal log dei messaggi, i numeri che arrivano intatti come gruppo di cifre isolato: `834` scritto `8.34` o «molti» è perso. `bin/regia.py telefono --regola copia|riformula [--seme N]` gioca una partita e chiude con `rapporto.md` (`rapporto.py`): esito, numeri persi per salto, messaggi per arco, token per nodo, turni umani, ogni tabella con la sua fonte — la riga del registro, le righe del log, i transcript — e mai il racconto di un agente; `bin/regia.py rapporto <partita>` lo riscrive. `--a-secco` sostituisce le sessioni con `nodo-inoltro.py`, un nodo script che riceve dal bus come le sessioni, e collauda regia e oracolo senza LLM e senza riga nel registro

## Il nodo

Un nodo LLM è una sessione Claude Code lanciata dalla regia con `claude --bg --safe-mode --tools Bash --allowedTools "Bash(<orchestra>/bin/bus *)" --permission-mode dontAsk`, nella propria cartella `partite/<partita>/nodi/<nodo>/`:

- **nessun tool oltre a Bash**, e in Bash solo il bus: `SendMessage` e `ListAgents` non arrivano al modello, quindi il canale nativo fra sessioni non scavalca il grafo
- **la cartella del nodo è vuota e le caselle stanno fuori**: in `dontAsk` Claude Code lascia passare i comandi di sola lettura dentro la cartella di lavoro (`ls` passa, `cat /etc/hostname` no), e una casella dentro la cartella sarebbe leggibile senza bus
- **l'id della sessione lo assegna il servizio**: `--bg` ignora `--session-id`, e la regia legge `sessionId` e `pid` da `claude agents --json` e li scrive in `nodi.json`
- **`--safe-mode` spegne CLAUDE.md, hook, plugin e output style**: il transcript del nodo porta solo il prompt della regia e i messaggi del bus

Un messaggio arriva al nodo come `tool_result` di `bus recv`, quindi nel transcript non è mai un turno umano.

## Osservabilità

`bin/osserva.py` legge i transcript dei nodi di una partita e ne verifica i tre controlli: transcript con almeno un turno dell'assistente, genitore scritto dalla regia in `nodi.json`, somma dei quattro campi `usage` calcolabile. Conta anche i turni umani — record `user` con `origin.kind` = `human` dopo il primo, che è il lancio — e i token, somma di `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` su ogni messaggio dell'assistente. Un messaggio si conta una volta per `message.id`: Claude Code scrive un record per blocco — thinking, testo, tool_use — e ognuno ripete l'`usage` del messaggio, quindi la somma per record conta lo stesso messaggio due o tre volte. Accanto ai controlli `osserva.py` conta i messaggi nativi ricevuti (`origin.kind` = `peer`) e le chiamate a `SendMessage`: su un nodo isolato sono 0.

## Harness e vista

`bin/harness.py` legge le sorgenti del motore, ne fa eventi e li pubblica su un WebSocket; `vista/` è la pagina che li disegna. L'harness legge soltanto: non scrive in `registro/` né in `partite/`, perché un osservatore che scrive dove scrive il motore ne sposterebbe le misure.

- **gli eventi** — tre classi, schema in `schema/evento.schema.json`: `sessione` (`partita-aperta`, `partita-chiusa`, `nodo-lanciato`, `nodo-chiuso`, da `meta.json` e `nodi.json`), `messaggio` (`consegnato`, `rifiutato`, `letto`: una riga del log dei messaggi ciascuno), `spawn`. Ogni evento porta in cima `classe`, `tipo`, `ts`, `partita` e nel campo `riga` la riga sorgente intatta, quindi un campo nuovo del bus non cambia lo schema. Un messaggio porta anche `indice`, il numero della sua riga nel log: la chiave di un messaggio è partita più indice, perché un rifiuto ha `id` null. `partita-aperta` porta `meta.json` e `grafo.json`, e la vista disegna da lì. Lo spawn porta genitore, figlio, template e il delta del grafo — nodi, archi, versione —, perché il template fissa gli archi del figlio per ruolo. Le righe del log senza partita (`mittente-ignoto`) non diventano eventi: l'harness le conta e stampa il conteggio su stderr alla chiusura. `bin/valida-eventi.py <eventi.jsonl>` è il gate del formato: esce 0 se ogni riga è un evento valido e coerente con la sua riga sorgente, 1 altrimenti
- **l'accensione** — una porta sola, legata a `127.0.0.1`, default 8740 (`--porta 0` la fa scegliere al sistema): `/` serve la vista, `/canale.json` il modo e la velocità, `/eventi` il WebSocket. La prima riga su stdout porta l'indirizzo. Il WebSocket rifiuta con 403 una pagina d'altra origine, perché il log porta il testo integrale dei messaggi. Si ferma con SIGINT o SIGTERM
- **il replay** — `bin/harness.py replay <partita>... [--velocita V]` rigioca partite chiuse, una dopo l'altra, in ordine di ts e coi tempi divisi per V; `--velocita 0` emette senza attesa. Ogni client riceve il replay intero dall'inizio, poi il canale si chiude con «fine replay»
- **il dal vivo** — `bin/harness.py vivo` segue le partite aperte all'accensione e quelle che nascono dopo: legge il log dei messaggi dall'inizio e ne segue la coda ogni 100 ms, quindi può partire prima o dopo la regia senza perdere righe. Prima di emettere una chiusura — di un nodo o della partita — rilegge il log, così la chiusura arriva dopo l'ultima riga che la precede. Ogni client riceve prima gli eventi già emessi, poi i nuovi
- **il flusso** — `bin/harness.py flusso <eventi.jsonl> [--velocita V]` invia un file di eventi così come è scritto, coi tempi dei ts: collauda ciò che il motore non produce ancora, come lo spawn (`test/flussi/spawn.jsonl`)
- **la vista** — SVG scritto a mano, nessuna libreria: nodi su un cerchio nell'ordine di `grafo.json`, archi come curve orientate. Un messaggio consegnato è un pallino che corre lungo l'arco `da → a` e resta acceso sul destinatario; la lettura lo spegne senza toglierlo; un rifiuto è un segno fermo sul nodo mittente, perché non ha un arco su cui correre. Uno spawn applica il delta e ricalcola il cerchio. Il clic su un pallino mostra l'evento grezzo, su un arco i messaggi che vi sono corsi, su un nodo i suoi eventi di sessione. La pagina disegna l'ultima partita aperta e non manda niente all'harness: la velocità si sceglie all'accensione. I collaudi leggono gli attributi `data-nodo`, `data-arco`, `data-pallino` (chiave `partita:indice`), `data-esito`, `data-evento`
- **la chiusura dei nodi** — a fine partita la regia scrive nella voce di ogni nodo di `nodi.json` `chiuso`, l'ora al millesimo, e `azioni_fuori_bus`: le chiamate a tool del transcript che non sono un comando Bash del bus, `null` per un nodo script. L'harness emette `nodo-chiuso` da questi due campi e non apre mai i transcript, e il clic sul nodo li mostra. Le partite giocate prima di questi campi non hanno `nodo-chiuso` nel replay

Dal vivo, con la vista nel browser:

```
orchestra/bin/harness.py vivo                     # poi apri http://127.0.0.1:8740/
orchestra/bin/regia.py telefono --regola riformula
```
