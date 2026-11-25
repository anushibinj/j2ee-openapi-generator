"""LLM client contract. Stages depend on this protocol, never on a concrete client."""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class StructuredResult(BaseModel):
    """Outcome of one structured call (including its optional repair turn)."""

    ok: bool
    parsed: dict[str, Any] | None = None  # model_dump of the validated pydantic model
    raw: str = ""
    errors: list[str] = []
    repaired: bool = False
    usage: dict[str, int] = {}
    request: dict[str, Any] = {}  # messages + params, for the cache record


class LLMClient(Protocol):
    """Async structured-output client.

    ``item_id`` identifies the packet/tag (used by the fake and by usage logs).
    ``purpose`` is one of ``analyze``, ``normalize``, ``resolve``.
    Implementations validate against ``response_model`` and perform one repair turn.
    """

    async def structured(
        self,
        *,
        purpose: str,
        item_id: str,
        messages: list[dict[str, str]],
        response_model: type[T],
    ) -> StructuredResult: ...

    @property
    def model_name(self) -> str: ...

    @property
    def structured_mode(self) -> str: ...

    @property
    def temperature(self) -> float: ...
