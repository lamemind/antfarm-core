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
| `schema/` | schema del grafo |
| `test/contro-prove.sh` | contro-prove deterministiche di validatore, vista e bus, su una copia isolata del motore |

## Il nodo

Un nodo LLM è una sessione Claude Code lanciata dalla regia con `claude --bg --safe-mode --tools Bash --allowedTools "Bash(<orchestra>/bin/bus *)" --permission-mode dontAsk`, nella propria cartella `partite/<partita>/nodi/<nodo>/`:

- **nessun tool oltre a Bash**, e in Bash solo il bus: `SendMessage` e `ListAgents` non arrivano al modello, quindi il canale nativo fra sessioni non scavalca il grafo
- **la cartella del nodo è vuota e le caselle stanno fuori**: in `dontAsk` Claude Code lascia passare i comandi di sola lettura dentro la cartella di lavoro (`ls` passa, `cat /etc/hostname` no), e una casella dentro la cartella sarebbe leggibile senza bus
- **l'id della sessione lo assegna il servizio**: `--bg` ignora `--session-id`, e la regia legge `sessionId` e `pid` da `claude agents --json` e li scrive in `nodi.json`
- **`--safe-mode` spegne CLAUDE.md, hook, plugin e output style**: il transcript del nodo porta solo il prompt della regia e i messaggi del bus

Un messaggio arriva al nodo come `tool_result` di `bus recv`, quindi nel transcript non è mai un turno umano.

## Osservabilità

`bin/osserva.py` legge i transcript dei nodi di una partita e ne verifica i tre controlli: transcript con almeno un turno dell'assistente, genitore scritto dalla regia in `nodi.json`, somma dei quattro campi `usage` calcolabile. Conta anche i turni umani — record `user` con `origin.kind` = `human` dopo il primo, che è il lancio — e i token, somma di `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` su ogni record dell'assistente.
