"""Windows readiness: text files are always read and written as UTF-8.

On Korean Windows the default encoding is cp949, so open()/read_text()/write_text() without encoding= breaks on
Korean text (exam pack, docs, collected pages). This test finds such calls in src/, scripts/, app/ and tests/.
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
            continue                                        # zipfile/sqlite/... .open(), not builtins
        if any(k.arg == "encoding" for k in node.keywords) or _binary(node):
            continue
        lines.append(node.lineno)
    return lines


def test_all_text_io_declares_utf8():
    offenders = {str(p.relative_to(ROOT)): ln for p in sorted(ROOT.glob("*/**/*.py"))
                 if p.parts[len(ROOT.parts)] in {"src", "scripts", "app", "tests"}
                 for ln in [text_io_without_encoding(p)] if ln}
    assert offenders == {}, f"add encoding='utf-8' (Korean Windows defaults to cp949): {offenders}"
