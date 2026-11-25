"""Configuration loading (``oapi.yaml``) with ``${ENV}`` interpolation."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

StructuredMode = Literal["json_schema", "tools", "json_object", "prompt_only"]


class ModuleConfig(BaseModel):
    name: str
    web_xml: str
    context_root: str = ""
    tag_prefix: str = ""


class DispatchConfig(BaseModel):
    param_names: list[str] = Field(default_factory=lambda: ["op", "action", "cmd"])
    method_name_patterns: list[str] = Field(
        default_factory=lambda: ["do{Op}", "handle{Op}", "{op}"]
    )


class OpenApiConfig(BaseModel):
    version: Literal["3.1.0", "3.0.3"] = "3.1.0"
    title: str = "Product API"
    api_prefix: str = "/v1"
    servers: list[dict[str, str]] = Field(default_factory=list)
    provenance: bool = True


class LLMConfig(BaseModel):
    base_url: str = "http://localhost:8000/v1"
    api_key: str = "none"
    model: str = "model"
    context_tokens: int = 32000
    max_output_tokens: int = 4096
    temperature: float = 0.0
    seed: int | None = 42
    structured_mode: StructuredMode = "json_schema"
    max_concurrency: int = 4
    timeout_s: float = 180
    max_retries: int = 3
    fake: bool = False  # use FakeLLM (tests / offline demo)
    fake_dir: str | None = None  # directory of canned JSON, used when fake=true


class Config(BaseModel):
    repo_root: Path
    modules: list[ModuleConfig] = Field(default_factory=list)
    include_globs: list[str] = Field(
        default_factory=lambda: [
            "**/*.java",
            "**/*.jsp",
            "**/*.jspf",
            "**/*.tag",
            "**/web.xml",
            "**/ejb-jar.xml",
            "**/*.properties",
            "**/*.js",
        ]
    )
    exclude_globs: list[str] = Field(
        default_factory=lambda: [
            "**/test/**",
            "**/target/**",
            "**/build/**",
            "**/node_modules/**",
            "**/generated/**",
        ]
    )
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    access_logs: list[str] = Field(default_factory=list)
    openapi: OpenApiConfig = Field(default_factory=OpenApiConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    work_dir: Path = Path(".oapi")
    out_dir: Path = Path("out")
    review_dir: Path = Path("review")
    style_guide: Path = Path("style-guide/api-style-guide.md")
    workers: int | Literal["auto"] = "auto"

    # resolved at load time; not part of the file format
    base_dir: Path = Path(".")

    @field_validator("workers")
    @classmethod
    def _workers(cls, v: int | str) -> int | str:
        if isinstance(v, int) and v < 1:
            raise ValueError("workers must be >= 1 or 'auto'")
        return v

    def worker_count(self) -> int:
        if self.workers == "auto":
            return max(1, os.cpu_count() or 1)
        return int(self.workers)

    # --- resolved paths (relative values resolve against the config file directory)
    def _abs(self, p: Path) -> Path:
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def repo_path(self) -> Path:
        return self._abs(self.repo_root).resolve()

    @property
    def work_path(self) -> Path:
        return self._abs(self.work_dir)

    @property
    def out_path(self) -> Path:
        return self._abs(self.out_dir)

    @property
    def review_path(self) -> Path:
        return self._abs(self.review_dir)

    @property
    def style_guide_path(self) -> Path:
        return self._abs(self.style_guide)


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")

        return _ENV.sub(sub, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def load_config(path: Path | str) -> Config:
    """Load ``oapi.yaml``; loads a sibling ``.env`` if present; interpolates ``${ENV}``."""
    p = Path(path).resolve()
    load_dotenv(p.parent / ".env", override=False)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(_interpolate(raw))
    cfg.base_dir = p.parent
    return cfg
