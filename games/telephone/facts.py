"""The ten facts of the telephone game, generated from a seed: the same seed gives the same facts.

Each fact carries a three-digit integer, different from the other nine. The oracle looks for the
number as an isolated group of digits, so the 1)…10) numbering of the message does not fool it.
"""

import random
import re

TEMPLATES = (
    "Nel porto di {place} attraccano {number} pescherecci.",
    "La biblioteca di {place} custodisce {number} manoscritti miniati.",
    "Il vivaio di {place} coltiva {number} varietà di rose antiche.",
    "L'osservatorio di {place} ha catalogato {number} comete.",
    "Il museo di {place} espone {number} orologi a pendolo.",
    "La cantina di {place} fa invecchiare {number} botti di rovere.",
    "Il faro di {place} conta {number} gradini fino alla lanterna.",
    "L'archivio di {place} conserva {number} mappe nautiche.",
    "Il mercato coperto di {place} ospita {number} banchi di spezie.",
    "Il conservatorio di {place} possiede {number} pianoforti a coda.",
)
PLACES = (
    "Ancona", "Bressanone", "Chioggia", "Enna", "Feltre", "Gaeta", "Imola", "Lecce", "Matera", "Nuoro",
    "Orvieto", "Pavia", "Rieti", "Sondrio", "Trani", "Urbino", "Vasto", "Zara", "Bormio", "Cefalù",
)
HEADER = "Ecco dieci fatti da tramandare."


def generate(seed):
    generator = random.Random(seed)
    templates = generator.sample(TEMPLATES, len(TEMPLATES))
    places = generator.sample(PLACES, len(TEMPLATES))
    numbers = generator.sample(range(101, 1000), len(TEMPLATES))
    return [
        {"number": number, "text": template.format(place=place, number=number)}
        for template, place, number in zip(templates, places, numbers)
    ]


def start_message(facts):
    """A single line: the whole message passes as an argument of the bus."""
    return " ".join([HEADER] + [f"{index}) {fact['text']}" for index, fact in enumerate(facts, start=1)])


def intact_numbers(text, facts):
    """The fact numbers that appear in the text as an isolated group of digits, in the order of the facts."""
    return [fact["number"] for fact in facts if re.search(rf"(?<!\d){fact['number']}(?!\d)", text or "")]
