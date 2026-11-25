from __future__ import annotations

import json
from typing import Any

import httpx
import respx

from j2ee_openapi_generator.config import Config, LLMConfig
from j2ee_openapi_generator.llm.client import make_client, run_doctor
from j2ee_openapi_generator.llm.fake import FakeLLM

BASE = "http://doc.test/v1"


def comp(content: str | None, tool: bool = False) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool:
        msg["tool_calls"] = [
            {
                "id": "1",
                "type": "function",
                "function": {"name": "emit_result", "arguments": '{"ok": true}'},
            }
        ]
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
    }


def cfg(ctx: int = 8000) -> Config:
    return Config(
        repo_root=".", llm=LLMConfig(base_url=BASE, model="m", context_tokens=ctx, max_retries=0)
    )


@respx.mock
def test_doctor_reports_modes_and_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        b = json.loads(request.content)
        rf = b.get("response_format", {}).get("type")
        if rf == "json_schema":
            return httpx.Response(400, json={"error": {"message": "json_schema unsupported"}})
        if "tools" in b:
            return httpx.Response(200, json=comp(None, tool=True))
        return httpx.Response(200, json=comp('{"ok": true}'))

    respx.post(f"{BASE}/chat/completions").mock(side_effect=handler)
    respx.get(f"{BASE}/models").respond(
        json={"object": "list", "data": [{"id": "m", "object": "model", "max_model_len": 4096}]}
    )
    rows = {r["check"]: r for r in run_doctor(cfg())}
    assert rows["mode:json_schema"]["status"] == "unsupported"
    assert "unsupported" in rows["mode:json_schema"]["error"]
    assert rows["mode:tools"]["status"] == "supported"
    assert rows["mode:json_object"]["status"] == "supported"
    assert rows["mode:prompt_only"]["status"] == "supported"
    assert rows["context"]["status"] == "too_large"
    assert rows["context"]["server_context"] == 4096


@respx.mock
def test_doctor_never_raises() -> None:
    respx.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{BASE}/models").mock(side_effect=httpx.ConnectError("down"))
    rows = run_doctor(cfg())
    assert len(rows) == 5
    assert all(r["status"] == "error" for r in rows)


def test_make_client_fake(tmp_path: Any) -> None:
    c = Config(repo_root=".", llm=LLMConfig(fake=True, fake_dir="canned"), base_dir=tmp_path)
    client = make_client(c)
    assert isinstance(client, FakeLLM) and client.fake_dir == tmp_path / "canned"
