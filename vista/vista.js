// Vista della partita: si collega al canale dell'harness e disegna ciò che riceve.
//
// Il grafo arriva dentro l'evento partita-aperta (riga.grafo) e si disegna da lì: nodi su un
// cerchio nell'ordine del file, archi come curve orientate. Un messaggio consegnato è un pallino
// che corre lungo l'arco da → a e resta fermo, acceso, sul nodo destinatario; la lettura lo
// spegne ma non lo toglie. Una scrittura in una sorgente è un pallino uguale, verde, che corre
// verso la sorgente e porta data-esito="scritto": nessuno la legge, quindi resta acceso. Un rifiuto
// è un segno fermo sul nodo mittente: non ha un arco su cui correre. Ogni pallino e ogni segno
// porta l'evento grezzo nell'attributo data-evento, e il clic lo mostra così com'è arrivato. Uno
// spawn e un grafo-cambiato applicano il loro delta e ricalcolano il cerchio. La pagina non scrive
// niente verso l'harness.

"use strict";

const NS = "http://www.w3.org/2000/svg";
const CENTRO = { x: 500, y: 350 };
const RAGGIO_CERCHIO = 260;
const RAGGIO_NODO = 34;
const CURVATURA = 0.16;
const DURATA_CORSA_S = 0.9;
const PASSO_SEGNI_RIFIUTO = 26;
const MASSIMO_REGISTRO = 400;

const stato = {
  partita: null,
  grafo: null,
  nodi: new Map(),       // id → { voce, elemento, x, y, eventi, rifiuti }
  archi: new Map(),      // "da→a" → { voce, elemento, percorso, eventi }
  pallini: new Map(),    // id del messaggio → elemento del pallino consegnato
  selezionato: null,
  contatoreArchi: 0,
};

const strati = {
  archi: document.getElementById("strato-archi"),
  nodi: document.getElementById("strato-nodi"),
  pallini: document.getElementById("strato-pallini"),
};

function elementoSvg(tag, attributi = {}) {
  const elemento = document.createElementNS(NS, tag);
  for (const [nome, valore] of Object.entries(attributi)) elemento.setAttribute(nome, valore);
  return elemento;
}

function testo(id, valore) {
  document.getElementById(id).textContent = valore;
}

function chiaveArco(da, a) {
  return `${da}→${a}`;
}

// <editor-fold desc="Dettaglio e registro">
function mostra(titolo, oggetto, elemento) {
  if (stato.selezionato) stato.selezionato.classList.remove("selezionato");
  stato.selezionato = elemento || null;
  if (elemento) elemento.classList.add("selezionato");
  testo("dettaglio-titolo", titolo);
  testo("dettaglio-json", JSON.stringify(oggetto, null, 2));
}

function descrivi(evento) {
  const riga = evento.riga || {};
  if (evento.classe === "messaggio") {
    const motivo = riga.motivo ? ` · ${riga.motivo}` : "";
    return `${evento.tipo} ${riga.da ?? "?"} → ${riga.a ?? "?"}${motivo}`;
  }
  if (evento.classe === "spawn") return `spawn ${evento.genitore} → ${evento.figlio.id} · ${evento.template}`;
  if (evento.tipo === "grafo-cambiato") {
    const { versione, nodi_aggiunti: nodi, archi_aggiunti: aggiunti, archi_tolti: tolti } = evento.delta;
    return `grafo-cambiato v${versione} · +${nodi.length} nodi · +${aggiunti.length} −${tolti.length} archi`;
  }
  if (evento.nodo) return `${evento.tipo} ${evento.nodo}`;
  return `${evento.tipo} ${evento.partita}`;
}

function registra(evento) {
  const lista = document.getElementById("registro-eventi");
  const voce = document.createElement("li");
  if (evento.tipo === "rifiutato") voce.classList.add("rifiutato");
  const ora = document.createElement("span");
  ora.className = "ora";
  ora.textContent = (evento.ts.split("T")[1] || "").slice(0, 12);
  voce.append(ora, descrivi(evento));
  voce.addEventListener("click", () => mostra(`evento · ${evento.classe}/${evento.tipo}`, evento));
  lista.prepend(voce);
  while (lista.children.length > MASSIMO_REGISTRO) lista.lastElementChild.remove();
}
// </editor-fold>

// <editor-fold desc="Disegno del grafo">
function posizioni() {
  const nodi = stato.grafo.nodi;
  nodi.forEach((voce, indice) => {
    const nodo = stato.nodi.get(voce.id);
    if (nodi.length === 1) {
      nodo.x = CENTRO.x;
      nodo.y = CENTRO.y;
      return;
    }
    const angolo = -Math.PI / 2 + (2 * Math.PI * indice) / nodi.length;
    nodo.x = CENTRO.x + RAGGIO_CERCHIO * Math.cos(angolo);
    nodo.y = CENTRO.y + RAGGIO_CERCHIO * Math.sin(angolo);
  });
}

function versore(dx, dy) {
  const lunghezza = Math.hypot(dx, dy) || 1;
  return { x: dx / lunghezza, y: dy / lunghezza };
}

// Curva quadratica da bordo a bordo, piegata a sinistra del verso: A → B e B → A non si sovrappongono.
// null se manca un estremo o se i due nodi coincidono.
function puntiArco(da, a) {
  const origine = stato.nodi.get(da);
  const destino = stato.nodi.get(a);
  if (!origine || !destino) return null;
  const dx = destino.x - origine.x;
  const dy = destino.y - origine.y;
  const lunghezza = Math.hypot(dx, dy);
  const controllo = {
    x: (origine.x + destino.x) / 2 - dy * CURVATURA,
    y: (origine.y + destino.y) / 2 + dx * CURVATURA,
  };
  if (lunghezza === 0) return null;
  const uscita = versore(controllo.x - origine.x, controllo.y - origine.y);
  const entrata = versore(controllo.x - destino.x, controllo.y - destino.y);
  const inizio = { x: origine.x + uscita.x * RAGGIO_NODO, y: origine.y + uscita.y * RAGGIO_NODO };
  const fine = { x: destino.x + entrata.x * (RAGGIO_NODO + 3), y: destino.y + entrata.y * (RAGGIO_NODO + 3) };
  return { inizio, controllo, fine };
}

function geometriaArco(da, a) {
  const punti = puntiArco(da, a);
  if (!punti) return "";
  const { inizio, controllo, fine } = punti;
  return `M ${inizio.x.toFixed(1)} ${inizio.y.toFixed(1)} Q ${controllo.x.toFixed(1)} ${controllo.y.toFixed(1)} ${fine.x.toFixed(1)} ${fine.y.toFixed(1)}`;
}

function posizioneRifiuto(nodo, indice) {
  const verso = versore(nodo.x - CENTRO.x, nodo.y - CENTRO.y);
  const fuori = verso.x === 0 && verso.y === 0 ? { x: 0, y: -1 } : verso;
  const distanza = RAGGIO_NODO + 18 + indice * PASSO_SEGNI_RIFIUTO;
  return { x: nodo.x + fuori.x * distanza, y: nodo.y + fuori.y * distanza };
}

function aggiornaGeometria() {
  posizioni();
  for (const nodo of stato.nodi.values()) {
    nodo.elemento.setAttribute("transform", `translate(${nodo.x.toFixed(1)} ${nodo.y.toFixed(1)})`);
    nodo.rifiuti.forEach((segno, indice) => {
      const punto = posizioneRifiuto(nodo, indice);
      segno.setAttribute("transform", `translate(${punto.x.toFixed(1)} ${punto.y.toFixed(1)})`);
    });
  }
  for (const arco of stato.archi.values()) {
    const forma = geometriaArco(arco.voce.da, arco.voce.a);
    for (const percorso of arco.elemento.querySelectorAll("path")) percorso.setAttribute("d", forma);
  }
}

function disegnaNodo(voce) {
  const gruppo = elementoSvg("g", { class: `nodo tipo-${voce.tipo}`, "data-nodo": voce.id });
  gruppo.append(elementoSvg("circle", { r: RAGGIO_NODO }));
  const etichetta = elementoSvg("text", { class: "etichetta" });
  etichetta.textContent = voce.id;
  // Un id lungo resta dentro il cerchio: il corpo scende con la lunghezza.
  etichetta.style.fontSize = `${voce.id.length <= 4 ? 18 : voce.id.length <= 6 ? 15 : voce.id.length <= 8 ? 13 : 11}px`;
  const ruolo = elementoSvg("text", { class: "ruolo", y: RAGGIO_NODO + 18 });
  ruolo.textContent = voce.ruolo || voce.tipo;
  gruppo.append(etichetta, ruolo);
  const nodo = { voce, elemento: gruppo, x: 0, y: 0, eventi: [], rifiuti: [] };
  gruppo.addEventListener("click", () => mostra(`nodo · ${voce.id}`, { nodo: voce, eventi: nodo.eventi }, gruppo));
  strati.nodi.append(gruppo);
  stato.nodi.set(voce.id, nodo);
}

function disegnaArco(voce) {
  const chiave = chiaveArco(voce.da, voce.a);
  const gruppo = elementoSvg("g", { "data-arco": chiave });
  const percorso = elementoSvg("path", { class: "arco", id: `arco-${++stato.contatoreArchi}`, "marker-end": "url(#freccia)" });
  const presa = elementoSvg("path", { class: "arco-presa" });
  gruppo.append(percorso, presa);
  const arco = { voce, elemento: gruppo, percorso, eventi: [], pallini: [] };
  gruppo.addEventListener("click", () => mostra(`arco · ${chiave}`, { arco: voce, messaggi: arco.eventi }, gruppo));
  strati.archi.append(gruppo);
  stato.archi.set(chiave, arco);
}

function testoVersione() {
  testo("versione", `v${stato.grafo.versione} · ${stato.grafo.nodi.length} nodi · ${stato.grafo.archi.length} archi`);
}

function nuovaPartita(evento) {
  for (const strato of Object.values(strati)) strato.replaceChildren();
  stato.partita = evento.partita;
  stato.grafo = structuredClone(evento.riga.grafo);
  stato.nodi.clear();
  stato.archi.clear();
  stato.pallini.clear();
  stato.selezionato = null;
  stato.grafo.nodi.forEach(disegnaNodo);
  stato.grafo.archi.forEach(disegnaArco);
  aggiornaGeometria();
  const meta = evento.riga.meta;
  testo("partita", evento.partita);
  testo("gioco", [meta.gioco, meta.regola].filter(Boolean).join(" · "));
  testoVersione();
  testo("stato-partita", "aperta");
  document.body.dataset.partita = "aperta";
}

// Lo spawn porta il delta del grafo: il figlio e tutti gli archi che il template gli dà per
// ruolo, non solo genitore → figlio. Il figlio entra nel cerchio e la disposizione si ricalcola.
function applicaSpawn(evento) {
  for (const voce of evento.delta.nodi) {
    if (stato.nodi.has(voce.id)) continue;
    stato.grafo.nodi.push(voce);
    disegnaNodo(voce);
  }
  for (const voce of evento.delta.archi) {
    if (stato.archi.has(chiaveArco(voce.da, voce.a))) continue;
    stato.grafo.archi.push(voce);
    disegnaArco(voce);
  }
  stato.grafo.versione = evento.delta.versione;
  aggiornaGeometria();
  testoVersione();
  stato.nodi.get(evento.genitore)?.eventi.push(evento);
  const figlio = stato.nodi.get(evento.figlio.id);
  if (figlio) {
    figlio.eventi.push(evento);
    figlio.elemento.classList.add("nato-da-spawn");
  }
}

// Un cambio del grafo scritto dalla regia a partita aperta: la vista applica il delta dell'evento.
// I pallini già corsi su un arco tolto restano accesi sul loro destinatario: l'animazione segue il
// percorso dell'arco, quindi con l'arco se ne va anche lei, e il pallino si ferma nel punto d'arrivo.
function applicaGrafoCambiato(evento) {
  const delta = evento.delta;
  const orfani = [];
  for (const voce of delta.archi_tolti) {
    const chiave = chiaveArco(voce.da, voce.a);
    const arco = stato.archi.get(chiave);
    if (!arco) continue;
    const arrivo = puntiArco(voce.da, voce.a)?.fine;
    orfani.push(...arco.pallini.map((pallino) => ({ pallino, voce, arrivo })));
    arco.elemento.remove();
    stato.archi.delete(chiave);
  }
  for (const voce of delta.nodi_tolti) {
    stato.nodi.get(voce.id)?.elemento.remove();
    stato.nodi.delete(voce.id);
  }
  const archiTolti = new Set(delta.archi_tolti.map((voce) => chiaveArco(voce.da, voce.a)));
  const nodiTolti = new Set(delta.nodi_tolti.map((voce) => voce.id));
  stato.grafo.archi = stato.grafo.archi.filter((voce) => !archiTolti.has(chiaveArco(voce.da, voce.a)));
  stato.grafo.nodi = stato.grafo.nodi.filter((voce) => !nodiTolti.has(voce.id));
  for (const voce of delta.nodi_aggiunti) {
    stato.grafo.nodi.push(voce);
    disegnaNodo(voce);
    const nodo = stato.nodi.get(voce.id);
    nodo.eventi.push(evento);
    nodo.elemento.classList.add("entrato");
  }
  for (const voce of delta.archi_aggiunti) {
    stato.grafo.archi.push(voce);
    disegnaArco(voce);
  }
  stato.grafo.versione = delta.versione;
  aggiornaGeometria();
  testoVersione();
  for (const { pallino, voce, arrivo } of orfani) {
    const punto = puntiArco(voce.da, voce.a)?.fine ?? arrivo;
    pallino.querySelector("animateMotion")?.remove();
    if (punto) pallino.setAttribute("transform", `translate(${punto.x.toFixed(1)} ${punto.y.toFixed(1)})`);
    pallino.setAttribute("data-arrivato", "");
  }
}
// </editor-fold>

// <editor-fold desc="Messaggi: pallini e rifiuti">
function pallinoConsegnato(evento) {
  const riga = evento.riga;
  const arco = stato.archi.get(chiaveArco(riga.da, riga.a));
  const pallino = elementoSvg("circle", {
    class: evento.tipo === "scritto" ? "pallino scritto" : "pallino",
    r: 7,
    "data-pallino": `${evento.partita}:${evento.indice}`,
    "data-esito": evento.tipo,
    "data-evento": JSON.stringify(evento),
  });
  const genere = evento.tipo === "scritto" ? "scrittura" : "messaggio";
  pallino.addEventListener("click", () => mostra(`${genere} · ${riga.da} → ${riga.a}`, JSON.parse(pallino.dataset.evento), pallino));
  strati.pallini.append(pallino);
  if (riga.id) stato.pallini.set(riga.id, pallino);
  if (!arco) {
    // Il bus non consegna fuori dal grafo: un consegnato senza arco vuol dire un grafo diverso da quello disegnato.
    const destino = stato.nodi.get(riga.a);
    pallino.classList.add("fuori-arco");
    if (destino) pallino.setAttribute("transform", `translate(${destino.x} ${destino.y})`);
    pallino.setAttribute("data-arrivato", "");
    return;
  }
  arco.eventi.push(evento);
  arco.pallini.push(pallino);
  const corsa = elementoSvg("animateMotion", { dur: `${DURATA_CORSA_S}s`, fill: "freeze", begin: "indefinite", calcMode: "paced" });
  const traccia = elementoSvg("mpath", { href: `#${arco.percorso.id}` });
  corsa.append(traccia);
  corsa.addEventListener("endEvent", () => pallino.setAttribute("data-arrivato", ""));
  pallino.append(corsa);
  corsa.beginElement();
}

function segnoRifiuto(evento) {
  const riga = evento.riga;
  const mittente = stato.nodi.get(riga.da);
  const segno = elementoSvg("g", {
    class: "rifiuto",
    "data-pallino": `${evento.partita}:${evento.indice}`,
    "data-esito": "rifiutato",
    "data-evento": JSON.stringify(evento),
  });
  segno.append(
    elementoSvg("circle", { r: 10 }),
    elementoSvg("line", { x1: -5, y1: -5, x2: 5, y2: 5 }),
    elementoSvg("line", { x1: -5, y1: 5, x2: 5, y2: -5 }),
  );
  segno.addEventListener("click", () => mostra(`rifiuto · ${riga.motivo}`, JSON.parse(segno.dataset.evento), segno));
  strati.pallini.append(segno);
  if (mittente) {
    mittente.rifiuti.push(segno);
    mittente.eventi.push(evento);
    const punto = posizioneRifiuto(mittente, mittente.rifiuti.length - 1);
    segno.setAttribute("transform", `translate(${punto.x.toFixed(1)} ${punto.y.toFixed(1)})`);
  } else {
    segno.setAttribute("transform", "translate(40 40)");
  }
}

function lettura(evento) {
  const pallino = stato.pallini.get(evento.riga.id);
  if (pallino) {
    pallino.classList.add("spento");
    pallino.setAttribute("data-letto", "");
  }
  const destinatario = stato.nodi.get(evento.riga.a);
  if (destinatario) destinatario.eventi.push(evento);
}
// </editor-fold>

function gestisci(evento) {
  registra(evento);
  if (evento.tipo === "partita-aperta") {
    nuovaPartita(evento);
    return;
  }
  if (evento.partita !== stato.partita) return;
  if (evento.classe === "spawn") {
    applicaSpawn(evento);
    return;
  }
  if (evento.tipo === "grafo-cambiato") {
    applicaGrafoCambiato(evento);
    return;
  }
  if (evento.classe === "messaggio") {
    if (evento.tipo === "consegnato" || evento.tipo === "scritto") pallinoConsegnato(evento);
    else if (evento.tipo === "rifiutato") segnoRifiuto(evento);
    else if (evento.tipo === "letto") lettura(evento);
    return;
  }
  if (evento.tipo === "nodo-lanciato" || evento.tipo === "nodo-chiuso") {
    const nodo = stato.nodi.get(evento.nodo);
    if (!nodo) return;
    nodo.eventi.push(evento);
    nodo.elemento.classList.add(evento.tipo === "nodo-lanciato" ? "lanciato" : "chiuso");
    return;
  }
  if (evento.tipo === "partita-chiusa") {
    testo("stato-partita", `chiusa · esito ${evento.riga.esito ?? "—"}`);
    document.body.dataset.partita = "chiusa";
  }
}

async function collega() {
  try {
    const canale = await (await fetch("/canale.json")).json();
    const velocita = canale.velocita === null ? "" : canale.velocita === 0 ? " · senza attese" : ` · ×${canale.velocita}`;
    testo("canale", (canale.modo === "vivo" ? "dal vivo" : canale.modo) + velocita);
  } catch (errore) {
    testo("canale", "sconosciuto: apri la vista dall'indirizzo dell'harness");
  }
  const presa = new WebSocket(`ws://${location.host}/eventi`);
  presa.addEventListener("open", () => {
    document.body.dataset.stato = "aperto";
    testo("connessione", "aperta");
  });
  presa.addEventListener("message", (messaggio) => gestisci(JSON.parse(messaggio.data)));
  presa.addEventListener("close", (chiusura) => {
    document.body.dataset.stato = "finito";
    testo("connessione", `chiusa${chiusura.reason ? " · " + chiusura.reason : ""}`);
  });
}

collega();
