"""I dieci fatti del telefono senza fili, generati da un seme: lo stesso seme dà gli stessi fatti.

Ogni fatto porta un numero intero di tre cifre, diverso dagli altri nove. L'oracolo cerca il
numero come gruppo di cifre isolato, quindi la numerazione 1)…10) del messaggio non lo inganna.
"""

import random
import re

MODELLI = (
    "Nel porto di {luogo} attraccano {numero} pescherecci.",
    "La biblioteca di {luogo} custodisce {numero} manoscritti miniati.",
    "Il vivaio di {luogo} coltiva {numero} varietà di rose antiche.",
    "L'osservatorio di {luogo} ha catalogato {numero} comete.",
    "Il museo di {luogo} espone {numero} orologi a pendolo.",
    "La cantina di {luogo} fa invecchiare {numero} botti di rovere.",
    "Il faro di {luogo} conta {numero} gradini fino alla lanterna.",
    "L'archivio di {luogo} conserva {numero} mappe nautiche.",
    "Il mercato coperto di {luogo} ospita {numero} banchi di spezie.",
    "Il conservatorio di {luogo} possiede {numero} pianoforti a coda.",
)
LUOGHI = (
    "Ancona", "Bressanone", "Chioggia", "Enna", "Feltre", "Gaeta", "Imola", "Lecce", "Matera", "Nuoro",
    "Orvieto", "Pavia", "Rieti", "Sondrio", "Trani", "Urbino", "Vasto", "Zara", "Bormio", "Cefalù",
)
INTESTAZIONE = "Ecco dieci fatti da tramandare."


def genera(seme):
    generatore = random.Random(seme)
    modelli = generatore.sample(MODELLI, len(MODELLI))
    luoghi = generatore.sample(LUOGHI, len(MODELLI))
    numeri = generatore.sample(range(101, 1000), len(MODELLI))
    return [
        {"numero": numero, "testo": modello.format(luogo=luogo, numero=numero)}
        for modello, luogo, numero in zip(modelli, luoghi, numeri)
    ]


def messaggio_di_partenza(fatti):
    """Una riga sola: il messaggio passa intero come argomento del bus."""
    return " ".join([INTESTAZIONE] + [f"{indice}) {fatto['testo']}" for indice, fatto in enumerate(fatti, start=1)])


def numeri_intatti(testo, fatti):
    """I numeri dei fatti che compaiono nel testo come gruppo di cifre isolato, nell'ordine dei fatti."""
    return [fatto["numero"] for fatto in fatti if re.search(rf"(?<!\d){fatto['numero']}(?!\d)", testo or "")]
