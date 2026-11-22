"""The cards of Leavitt's game, generated from a seed: the same seed gives the same cards.

Six symbols and five cards of five symbols. Each card lacks a different symbol; the sixth is
on all of them and is the answer. A symbol is an opaque code of three characters, two uppercase
letters and a digit, like `QX4`: the oracle looks for it as an isolated word, and a code admits no
variants — `triangle`, `triangles`, `▲` — that would turn the oracle into a judgment.

The seed picks the six codes, which one is the answer, which symbol each node lacks and the order
of the symbols on each card. The letters I and O and the digits 0 and 1 are left out, because they
get confused, and so is `QX4`, the example that the node prompt cites.
"""

import random
import re

NODES = ("A", "B", "C", "D", "E")
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
DIGITS = "23456789"
EXAMPLE = "QX4"
CODES = tuple(code for code in (first_letter + second_letter + digit for first_letter in LETTERS for second_letter in LETTERS for digit in DIGITS)
               if code != EXAMPLE)
CODE_SHAPE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}[0-9](?![A-Za-z0-9])")


def generate(seed):
    """{symbols, answer, missing: node → symbol, cards: node → five symbols} from the seed."""
    generator = random.Random(seed)
    symbols = generator.sample(CODES, 6)
    answer = symbols[generator.randrange(len(symbols))]
    others = [symbol for symbol in symbols if symbol != answer]
    missing = dict(zip(NODES, generator.sample(others, len(NODES))))
    cards = {node: generator.sample([symbol for symbol in symbols if symbol != missing[node]], 5)
             for node in NODES}
    return {"seed": seed, "symbols": symbols, "answer": answer, "missing": missing, "cards": cards}


def card_message(card):
    """The director → node message: a single line, the five symbols separated by spaces."""
    return "La tua carta: " + " ".join(card)


def symbols_in(text, symbols):
    """The match symbols that appear in the text as an isolated word, each one once."""
    return [symbol for symbol in symbols
            if re.search(rf"(?<![A-Za-z0-9]){symbol}(?![A-Za-z0-9])", text or "")]


def codes_in(text):
    """The groups that have the shape of a code, in order and without repeats: it serves whoever
    does not know the six symbols, like the script node."""
    return list(dict.fromkeys(CODE_SHAPE.findall(text or "")))
