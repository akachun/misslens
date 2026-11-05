"""BM25 tokenizer that handles exam English terms and Korean (ARCHITECTURE §8).

Whitespace splitting fails on Korean (particles stick to nouns: "도구를", "도구가"), so Hangul runs become
character bigrams. Latin tokens keep inner "_", "-", "." and "/" so exam terms such as tool_choice,
--allowedTools, CLAUDE.md and .claude/settings.json stay one token; their parts are added too, so a query for
"settings" still matches ".claude/settings.json".
"""

from __future__ import annotations

import re

_LATIN = re.compile(r"[a-z0-9]+(?:[_\-./][a-z0-9]+)*")
_HANGUL = re.compile(r"[가-힣]+")
_PARTS = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens: list[str] = []
    for m in _LATIN.finditer(text):
        word = m.group()
        tokens.append(word)
        parts = _PARTS.findall(word)
        if len(parts) > 1:
            tokens.extend(parts)
    for m in _HANGUL.finditer(text):
        run = m.group()
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens
