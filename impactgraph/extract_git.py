"""Change history from git: which functions and test files each commit touched.

Functions are resolved against the file *as it was at that commit* (parsed with
`ast`), so line numbers from old diffs map to the right function.
"""
from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .extract_code import _Visitor, function_at

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Commit:
    sha: str
    date: str
    subject: str
    functions: set[str] = field(default_factory=set)   # function ids
    test_files: set[str] = field(default_factory=set)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout


def changed_lines(diff: str, prefix: str) -> dict[str, set[int]]:
    """Parse a unified diff (-U0) into {path: {new-side line numbers}} for paths under prefix.
    A pure deletion is recorded as the line it happened at, so the enclosing function counts."""
    out: dict[str, set[int]] = {}
    path = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            p = p[2:] if p.startswith("b/") else p
            path = p if p.startswith(prefix) and p.endswith(".py") else None
        elif path and (m := HUNK.match(line)):
            start, count = int(m.group(1)), int(m.group(2) or 1)
            out.setdefault(path, set()).update(range(start, start + max(count, 1)))
    return out


def functions_for_lines(source: str, module: str, lines: set[int]) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    v = _Visitor(module)
    v.visit(tree)
    return {f.id for ln in lines if (f := function_at(v.funcs, ln))}


def extract(repo: Path, src_prefix: str, test_prefix: str, max_commits: int = 400):
    """src_prefix e.g. 'src/' (stripped to get module paths like 'click/core.py')."""
    log = _git(repo, "log", f"-{max_commits}", "--no-merges",
               "--pretty=format:%H%x09%cs%x09%s", "--", src_prefix, test_prefix)
    commits = []
    for row in log.splitlines():
        sha, date, subject = row.split("\t", 2)
        c = Commit(sha, date, subject)
        diff = _git(repo, "show", "-U0", "--format=", sha)
        for path, lines in changed_lines(diff, src_prefix).items():
            try:
                src = _git(repo, "show", f"{sha}:{path}")
            except subprocess.CalledProcessError:
                continue  # file deleted in this commit
            c.functions |= functions_for_lines(src, path[len(src_prefix):], lines)
        c.test_files = {p for p in changed_lines(diff, test_prefix)}
        if c.functions or c.test_files:
            commits.append(c)
    return commits
