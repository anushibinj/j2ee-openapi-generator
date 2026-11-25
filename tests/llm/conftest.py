from __future__ import annotations

import functools

import httpx
import openai
import pytest

from j2ee_openapi_generator.llm import client as client_mod


@pytest.fixture(autouse=True)
def _respx_compatible_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the SDK through a classic ``httpx`` client so ``respx`` can intercept it.

    Newer openai SDKs use their own httpx fork by default, which respx does not patch.
    Proxy env vars are dropped so nothing leaves the process.
    """
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)

    def factory(**kw: object) -> openai.AsyncOpenAI:
        return openai.AsyncOpenAI(http_client=httpx.AsyncClient(), **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(
        client_mod, "AsyncOpenAI", functools.update_wrapper(factory, openai.AsyncOpenAI)
    )
