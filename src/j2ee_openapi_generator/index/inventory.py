"""Deterministic repository inventory: sorted walk, glob filtering, sha256, kind, module."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from pathlib import PurePosixPath

from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.index.models import FileRecord, ModuleRecord

_SKIP_DIRS = {".git", ".hg", ".svn"}


@lru_cache(maxsize=512)
def _glob_regex(glob: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    n = len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if glob.startswith("**", i):
                i += 2
                if i < n and glob[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")  # zero or more directories
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def matches_globs(path: str, globs: Sequence[str]) -> bool:
    """True when the POSIX relative ``path`` matches any glob (``**`` spans directories)."""
    return any(_glob_regex(g).match(path) for g in globs)


def classify(path: str) -> str:
    name = PurePosixPath(path).name
    lower = name.lower()
    if name == "web.xml":
        return "web_xml"
    if name == "ejb-jar.xml":
        return "ejb_xml"
    if lower.endswith(".java"):
        return "java"
    if lower.endswith((".jsp", ".jspf", ".tag")):
        return "jsp"
    if lower.endswith(".properties"):
        return "properties"
    if lower.endswith(".js"):
        return "js"
    return "other"


def _hash_one(args: tuple[str, str]) -> tuple[str, str, int]:
    abs_path, rel = args
    h = hashlib.sha256()
    size = 0
    with open(abs_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return rel, h.hexdigest(), size


def _walk(root: str, include: Sequence[str], exclude: Sequence[str]) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        base = os.path.relpath(dirpath, root)
        for fn in sorted(filenames):
            rel = fn if base == "." else PurePosixPath(base, fn).as_posix()
            if matches_globs(rel, include) and not matches_globs(rel, exclude):
                found.append(rel)
    return sorted(found)


def assign_modules(files: list[FileRecord], modules: Sequence[ModuleRecord]) -> list[FileRecord]:
    """Set ``module`` on each file: the module with the longest root_dir prefix, else None."""
    ordered = sorted(modules, key=lambda m: (-len(m.root_dir), m.name))
    out: list[FileRecord] = []
    for f in files:
        owner: str | None = None
        for m in ordered:
            if m.root_dir == "" or f.path == m.root_dir or f.path.startswith(m.root_dir + "/"):
                owner = m.name
                break
        out.append(f.model_copy(update={"module": owner}))
    return out


def build_inventory(cfg: Config, workers: int = 1) -> list[FileRecord]:
    """Walk ``cfg.repo_path`` and return sorted FileRecords (read-only on the repo)."""
    from j2ee_openapi_generator.index.webxml import discover_modules

    root = str(cfg.repo_path)
    rels = _walk(root, cfg.include_globs, cfg.exclude_globs)
    jobs = [(os.path.join(root, r), r) for r in rels]
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            hashed = list(ex.map(_hash_one, jobs, chunksize=16))
    else:
        hashed = [_hash_one(j) for j in jobs]
    files = [
        FileRecord(path=rel, sha256=sha, size=size, kind=classify(rel))  # type: ignore[arg-type]
        for rel, sha, size in sorted(hashed)
    ]
    return assign_modules(files, discover_modules(cfg, files))
