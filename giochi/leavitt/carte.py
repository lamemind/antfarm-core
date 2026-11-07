"""Le carte del gioco di Leavitt, generate da un seme: lo stesso seme dà le stesse carte.

Sei simboli e cinque carte da cinque simboli. A ogni carta manca un simbolo diverso; il sesto sta
su tutte ed è la risposta. Un simbolo è un codice opaco di tre caratteri, due lettere maiuscole e
una cifra, come `QX4`: l'oracolo lo cerca come parola isolata, e un codice non ammette varianti
— `triangolo`, `triangoli`, `▲` — che farebbero dell'oracolo un giudizio.

Il seme sceglie i sei codici, quale è la risposta, quale simbolo manca a quale nodo e l'ordine
dei simboli su ogni carta. Le lettere I e O e le cifre 0 e 1 non entrano, perché si confondono, e
non entra `QX4`, l'esempio che il prompt dei nodi cita.
"""

import random
import re

NODI = ("A", "B", "C", "D", "E")
LETTERE = "ABCDEFGHJKLMNPQRSTUVWXYZ"
CIFRE = "23456789"
ESEMPIO = "QX4"
CODICI = tuple(codice for codice in (prima + seconda + cifra for prima in LETTERE for seconda in LETTERE for cifra in CIFRE)
               if codice != ESEMPIO)
FORMA_CODICE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}[0-9](?![A-Za-z0-9])")


def genera(seme):
    """{simboli, risposta, mancante: nodo → simbolo, carte: nodo → cinque simboli} dal seme."""
    generatore = random.Random(seme)
    simboli = generatore.sample(CODICI, 6)
    risposta = simboli[generatore.randrange(len(simboli))]
    altri = [simbolo for simbolo in simboli if simbolo != risposta]
    mancante = dict(zip(NODI, generatore.sample(altri, len(NODI))))
    carte = {nodo: generatore.sample([simbolo for simbolo in simboli if simbolo != mancante[nodo]], 5)
             for nodo in NODI}
    return {"seme": seme, "simboli": simboli, "risposta": risposta, "mancante": mancante, "carte": carte}


def messaggio_carta(carta):
    """Il messaggio regia → nodo: una riga sola, i cinque simboli separati da spazi."""
    return "La tua carta: " + " ".join(carta)


def simboli_in(testo, simboli):
    """I simboli della partita che compaiono nel testo come parola isolata, ognuno una volta."""
    return [simbolo for simbolo in simboli
            if re.search(rf"(?<![A-Za-z0-9]){simbolo}(?![A-Za-z0-9])", testo or "")]


def codici_in(testo):
    """I gruppi che hanno la forma di un codice, nell'ordine e senza ripetizioni: serve a chi non
    conosce i sei simboli, come il nodo script."""
    return list(dict.fromkeys(FORMA_CODICE.findall(testo or "")))
