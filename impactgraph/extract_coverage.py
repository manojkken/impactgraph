"""Test -> function links from a coverage.py database recorded with
`pytest --cov=<pkg> --cov-context=test`.

Each test context ("tests/test_x.py::test_y[param]|run") becomes a Test, and
every function whose lines that test executed becomes a COVERS edge carrying
how many of the function's lines it hit.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import coverage

from .extract_code import Func, function_at


def extract(coverage_file: Path, src_root: Path, funcs: dict[str, Func]):
    data = coverage.CoverageData(str(coverage_file))
    data.read()

    by_module: dict[str, list[Func]] = defaultdict(list)
    for f in funcs.values():
        by_module[f.module].append(f)

    # test_id -> function_id -> lines hit
    covers: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    src_root = src_root.resolve()
    for measured in data.measured_files():
        try:
            rel = Path(measured).resolve().relative_to(src_root).as_posix()
        except ValueError:
            continue
        mod_funcs = by_module.get(rel)
        if not mod_funcs:
            continue
        for line, ctxs in (data.contexts_by_lineno(measured) or {}).items():
            fn = function_at(mod_funcs, line)
            if fn is None:
                continue
            for ctx in ctxs:
                if not ctx:
                    continue
                test_id = ctx.rsplit("|", 1)[0]  # drop |setup/|run/|teardown
                covers[test_id][fn.id] += 1
    return covers


def split_test_id(test_id: str) -> tuple[str, str]:
    file, _, name = test_id.partition("::")
    return file, name
