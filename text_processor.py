from __future__ import annotations

import re
from dataclasses import dataclass

TAG = re.compile(r"(\[red\]|\[/red\])", re.I)
SENTENCES = re.compile(r"(?<=[.!?…])\s+(?=[^\s])")
WORDS = re.compile(r"\S+")


@dataclass(frozen=True)
class Word:
    text: str
    accent: bool = False


def parse_words(source: str) -> list[Word]:
    red = False
    output: list[Word] = []
    current: list[str] = []
    current_accent = False

    def flush() -> None:
        nonlocal current, current_accent
        if current:
            output.append(Word("".join(current).upper(), current_accent))
            current = []
            current_accent = False

    for piece in TAG.split(source.strip()):
        if piece.lower() == "[red]":
            red = True
        elif piece.lower() == "[/red]":
            red = False
        else:
            # Tags may wrap only part of a word. Keep adjacent text together so
            # rendered word counts stay aligned with the tag-free speech text.
            for match in re.finditer(r"\s+|\S+", piece):
                token = match.group()
                if token.isspace():
                    flush()
                else:
                    current.append(token)
                    current_accent = current_accent or red
    flush()
    return output


def scenes(source: str, max_words: int = 13) -> list[list[Word]]:
    clean = source.strip()
    if not clean:
        raise ValueError("Введите текст цитаты.")
    if len(clean) > 5000:
        raise ValueError("Одна цитата должна быть короче 5000 символов.")
    blocks = re.split(r"\n\s*\n", clean)
    result: list[list[Word]] = []
    for block in blocks:
        for sentence in SENTENCES.split(block.replace("\n", " ")):
            words = parse_words(sentence)
            if not words:
                continue
            while words:
                if len(words) <= max_words:
                    result.append(words)
                    break
                candidates = [i for i, w in enumerate(words[:max_words], 1) if w.text.endswith((",", ";", ":", "—")) and i >= 4]
                split = candidates[-1] if candidates else max_words
                result.append(words[:split])
                words = words[split:]
    if not result:
        raise ValueError("В тексте нет слов.")
    return result


def apply_auto_accent(words: list[Word]) -> list[Word]:
    if any(w.accent for w in words):
        return words
    candidates = [i for i, w in enumerate(words) if len(w.text.strip(".,!?…;:")) >= 5]
    if not candidates:
        return words
    target = candidates[-1]
    return [Word(w.text, i == target) for i, w in enumerate(words)]
