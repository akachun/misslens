"""Windows 대비: 텍스트 파일은 항상 UTF-8로 읽고 쓴다.

한국어 Windows의 기본 인코딩은 cp949라서 encoding= 없는 open()/read_text()/write_text()는 한국어 텍스트
(시험 팩, 문서, 수집한 페이지)에서 깨진다. 이 테스트는 src/, scripts/, app/, tests/에서 그런 호출을 찾는다.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
TEXT_CALLS = {"open", "read_text", "write_text"}


def _binary(call: ast.Call) -> bool:
    modes = [a for a in call.args[1:2] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    modes += [k.value for k in call.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)]
    return any("b" in (m.value if isinstance(m, ast.Constant) else m) for m in modes)


def text_io_without_encoding(path: Path) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name not in TEXT_CALLS:
            continue
        if name == "open" and isinstance(f, ast.Attribute):
            continue                                        # 내장 함수가 아니라 zipfile/sqlite/... 의 .open()
        if any(k.arg == "encoding" for k in node.keywords) or _binary(node):
            continue
        lines.append(node.lineno)
    return lines


def test_all_text_io_declares_utf8():
    offenders = {str(p.relative_to(ROOT)): ln for p in sorted(ROOT.glob("*/**/*.py"))
                 if p.parts[len(ROOT.parts)] in {"src", "scripts", "app", "tests"}
                 for ln in [text_io_without_encoding(p)] if ln}
    assert offenders == {}, f"add encoding='utf-8' (Korean Windows defaults to cp949): {offenders}"
