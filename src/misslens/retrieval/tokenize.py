"""시험 영어 용어와 한국어를 함께 다루는 BM25 tokenizer(ARCHITECTURE §8).

한국어는 조사가 명사에 붙어서("도구를", "도구가") 공백 분리가 통하지 않으므로, 연속된 한글은 글자 bigram으로
나눈다. 라틴 문자 토큰은 안쪽의 "_", "-", ".", "/"를 유지해 tool_choice, --allowedTools, CLAUDE.md,
.claude/settings.json 같은 시험 용어가 한 토큰으로 남는다. 그 부분들도 함께 추가하므로 "settings"로 검색해도
".claude/settings.json"에 걸린다.
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
