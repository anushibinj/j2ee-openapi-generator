"""Deterministic offline LLM backed by canned JSON files."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from j2ee_openapi_generator.ids import id_to_filename
from j2ee_openapi_generator.llm.base import StructuredResult, T


class FakeLLM:
    def __init__(self, fake_dir: Path, model: str = "fake", mode: str = "json_schema") -> None:
        self.fake_dir = Path(fake_dir)
        self._model = model
        self._mode = mode

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def structured_mode(self) -> str:
        return self._mode

    @property
    def temperature(self) -> float:
        return 0.0

    def _locate(self, purpose: str, item_id: str) -> Path:
        if purpose == "analyze":
            base, sep, att = item_id.partition("@")
            attempt = int(att) if sep and att.isdigit() else 1
            stem = id_to_filename(base)
            if attempt >= 2:
                repair = self.fake_dir / f"{stem}.repair.json"
                if repair.is_file():
                    return repair
            return self.fake_dir / f"{stem}.json"
        if purpose == "normalize":
            return self.fake_dir / "_normalizer" / f"{id_to_filename(item_id)}.json"
        if purpose == "resolve":
            return self.fake_dir / "_resolver.json"
        return self.fake_dir / f"_unknown_{id_to_filename(item_id)}.json"

    async def structured(
        self,
        *,
        purpose: str,
        item_id: str,
        messages: list[dict[str, str]],
        response_model: type[T],
    ) -> StructuredResult:
        request = {
            "messages": messages,
            "mode": self._mode,
            "model": self._model,
            "temperature": 0.0,
        }
        path = self._locate(purpose, item_id)
        if not path.is_file():
            return StructuredResult(
                ok=False,
                errors=[f"no canned response: {path.name}"],
                request=request,
            )
        raw = path.read_text(encoding="utf-8")
        try:
            model = response_model.model_validate(json.loads(raw))
        except (ValueError, ValidationError) as exc:
            return StructuredResult(
                ok=False, raw=raw, errors=[f"{path.name}: {exc}"], request=request
            )
        return StructuredResult(ok=True, parsed=model.model_dump(), raw=raw, request=request)
