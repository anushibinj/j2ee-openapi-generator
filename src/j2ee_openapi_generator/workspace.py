"""Work-dir layout shared by every stage. One place defines where artifacts live."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from j2ee_openapi_generator.config import Config
from j2ee_openapi_generator.ids import canonical_json


class Workspace:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.root = cfg.work_path
        self.index_dir = self.root / "index"
        self.packets_dir = self.root / "packets"
        self.proposals_dir = self.root / "proposals"
        self.verified_dir = self.root / "verified"
        self.cache_dir = self.root / "cache"
        self.usage_path = self.root / "usage.jsonl"
        self.db_path = self.root / "state.db"
        self.review_dir = cfg.review_path
        self.out_dir = cfg.out_path

    def ensure(self) -> None:
        for d in (self.index_dir, self.packets_dir, self.proposals_dir, self.verified_dir,
                  self.cache_dir, self.out_dir):
            d.mkdir(parents=True, exist_ok=True)

    # index files (one JSON each)
    def index_file(self, name: str) -> Path:
        return self.index_dir / f"{name}.json"


def write_json(path: Path, obj: Any) -> None:
    """Deterministic pretty JSON (sorted keys, trailing newline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["Workspace", "canonical_json", "read_json", "write_json"]
