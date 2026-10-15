"""Static extraction: modules, functions/methods and an approximate call graph.

Uses only Python's built-in `ast` module. Call resolution is name-based:
`self.foo()` resolves to `foo` on the enclosing class first, a bare `foo()`
resolves to the same module first, then anywhere in the package. Every CALLS
edge carries a `resolution` field so the graph is honest about how sure it is.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Func:
    id: str            # "click/core.py::Command.invoke"
    name: str          # "invoke"
    qualname: str      # "Command.invoke"
    module: str        # "click/core.py"
    cls: str | None
    start: int
    end: int
    calls: list[tuple[str, str | None]] = field(default_factory=list)  # (name, receiver)


@dataclass
class Module:
    path: str
    imports: set[str] = field(default_factory=set)


class _Visitor(ast.NodeVisitor):
    def __init__(self, module: str):
        self.module = module
        self.funcs: list[Func] = []
        self._cls: list[str] = []
        self._fn: list[Func] = []

    def visit_ClassDef(self, node: ast.ClassDef):
        self._cls.append(node.name)
        self.generic_visit(node)
        self._cls.pop()

    def _visit_fn(self, node):
        cls = self._cls[-1] if self._cls and not self._fn else None
        parent = self._fn[-1].qualname + ".<locals>." if self._fn else ""
        qual = f"{parent}{cls + '.' if cls else ''}{node.name}"
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        f = Func(f"{self.module}::{qual}", node.name, qual, self.module, cls, start, node.end_lineno)
        self.funcs.append(f)
        self._fn.append(f)
        self.generic_visit(node)
        self._fn.pop()

    visit_FunctionDef = _visit_fn
    visit_AsyncFunctionDef = _visit_fn

    def visit_Call(self, node: ast.Call):
        if self._fn:
            fn = node.func
            if isinstance(fn, ast.Name):
                self._fn[-1].calls.append((fn.id, None))
            elif isinstance(fn, ast.Attribute):
                recv = fn.value.id if isinstance(fn.value, ast.Name) else "?"
                self._fn[-1].calls.append((fn.attr, recv))
        self.generic_visit(node)


def _module_imports(tree: ast.AST, pkg: str) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                out.add(f"{pkg}/{node.module.replace('.', '/')}.py")
            elif node.module and node.module.startswith(pkg + "."):
                out.add(node.module.replace(".", "/") + ".py")
    return out


def extract(src_root: Path, package: str):
    """src_root: dir containing the package (e.g. click/src). Returns (modules, funcs, calls)."""
    modules: dict[str, Module] = {}
    funcs: dict[str, Func] = {}
    for path in sorted((src_root / package).rglob("*.py")):
        rel = path.relative_to(src_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        modules[rel] = Module(rel, _module_imports(tree, package))
        v = _Visitor(rel)
        v.visit(tree)
        for f in v.funcs:
            funcs[f.id] = f
    for m in modules.values():
        m.imports &= modules.keys()

    by_name: dict[str, list[Func]] = {}
    for f in funcs.values():
        by_name.setdefault(f.name, []).append(f)

    calls: set[tuple[str, str, str]] = set()
    for f in funcs.values():
        for name, recv in f.calls:
            cands = by_name.get(name, [])
            if not cands:
                continue
            if recv in ("self", "cls") and f.cls:
                hit = [c for c in cands if c.module == f.module and c.cls == f.cls]
                if hit:
                    calls |= {(f.id, c.id, "same_class") for c in hit}
                    continue
            same_mod = [c for c in cands if c.module == f.module and c.cls is None]
            if recv is None and same_mod:
                calls |= {(f.id, c.id, "same_module") for c in same_mod}
            elif len(cands) <= 3:  # avoid exploding on generic names like "get"
                calls |= {(f.id, c.id, "name_match") for c in cands if c.id != f.id}
    return modules, funcs, calls


def function_at(funcs_in_file: list[Func], line: int) -> Func | None:
    """Innermost function containing `line`."""
    best = None
    for f in funcs_in_file:
        if f.start <= line <= f.end and (best is None or f.start >= best.start):
            best = f
    return best
