from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import respx

from j2ee_openapi_generator.config import LLMConfig
from j2ee_openapi_generator.llm.client import OpenAICompatClient, extract_json_object
from j2ee_openapi_generator.llm.schemas import RenameMap

BASE = "http://llm.test/v1"
URL = f"{BASE}/chat/completions"
GOOD = {"tag": "Sales Orders", "operations": [], "tag_descriptions": []}


def completion(content: str | None = None, tool_args: str | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_args is not None:
        msg["tool_calls"] = [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "emit_result", "arguments": tool_args},
            }
        ]
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def make(mode: str, tmp_path: Path | None = None, **kw: Any) -> OpenAICompatClient:
    cfg = LLMConfig(base_url=BASE, model="m", structured_mode=mode, max_retries=2, **kw)  # type: ignore[arg-type]
    return OpenAICompatClient(
        cfg, usage_path=(tmp_path / "usage.jsonl") if tmp_path else None, backoff_min_s=0.001
    )


MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]


async def call(c: OpenAICompatClient) -> Any:
    return await c.structured(
        purpose="normalize", item_id="t1", messages=MSGS, response_model=RenameMap
    )


def body(route: respx.Route, i: int = -1) -> dict[str, Any]:
    return json.loads(route.calls[i].request.content)  # type: ignore[no-any-return]


@respx.mock
async def test_json_schema_mode() -> None:
    route = respx.post(URL).respond(json=completion(json.dumps(GOOD)))
    res = await call(make("json_schema"))
    assert res.ok and res.parsed and res.parsed["tag"] == "Sales Orders"
    b = body(route)
    rf = b["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "RenameMap"
    sch = rf["json_schema"]["schema"]
    assert sch["additionalProperties"] is False
    assert set(sch["required"]) == {"tag", "operations", "tag_descriptions"}
    assert "tools" not in b
    assert b["seed"] == 42
    assert res.request["mode"] == "json_schema" and res.request["model"] == "m"
    assert res.usage["total_tokens"] == 15


@respx.mock
async def test_tools_mode() -> None:
    route = respx.post(URL).respond(json=completion(None, json.dumps(GOOD)))
    res = await call(make("tools"))
    assert res.ok
    b = body(route)
    assert b["tools"][0]["function"]["parameters"]["type"] == "object"
    name = b["tools"][0]["function"]["name"]
    assert b["tool_choice"] == {"type": "function", "function": {"name": name}}
    assert "response_format" not in b


@respx.mock
async def test_json_object_mode_embeds_schema() -> None:
    route = respx.post(URL).respond(json=completion(json.dumps(GOOD)))
    res = await call(make("json_object"))
    assert res.ok
    b = body(route)
    assert b["response_format"] == {"type": "json_object"}
    assert "tag_descriptions" in b["messages"][0]["content"]
    assert b["messages"][0]["content"].startswith("sys")


@respx.mock
async def test_prompt_only_mode_tolerates_fences_and_prose() -> None:
    reply = "Sure, here you go:\n```json\n" + json.dumps(GOOD) + "\n```\nHope it helps"
    route = respx.post(URL).respond(json=completion(reply))
    res = await call(make("prompt_only"))
    assert res.ok
    b = body(route)
    assert "response_format" not in b and "tools" not in b
    assert "JSON only" in b["messages"][-1]["content"]


def test_extract_json_object_edge_cases() -> None:
    assert extract_json_object('x {"a": "}"} y') == '{"a": "}"}'
    assert extract_json_object('{broken {"a": 1}') == '{"a": 1}'
    assert extract_json_object("no json") is None


@respx.mock
async def test_repair_turn_success(tmp_path: Path) -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=completion('{"tag": 5}')),
            httpx.Response(200, json=completion(json.dumps(GOOD))),
        ]
    )
    res = await call(make("json_schema", tmp_path))
    assert res.ok and res.repaired
    msgs = body(route, 1)["messages"]
    assert msgs[-2] == {"role": "assistant", "content": '{"tag": 5}'}
    assert msgs[-1]["role"] == "user" and "tag" in msgs[-1]["content"]
    assert res.usage["total_tokens"] == 30
    line = json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[0])
    assert line == {
        "purpose": "normalize",
        "item_id": "t1",
        "model": "m",
        "prompt_tokens": 20,
        "completion_tokens": 10,
        "total_tokens": 30,
        "repaired": True,
        "ok": True,
    }


@respx.mock
async def test_double_failure(tmp_path: Path) -> None:
    route = respx.post(URL).respond(json=completion("not json at all"))
    res = await call(make("json_schema", tmp_path))
    assert not res.ok and res.repaired and res.errors and res.raw == "not json at all"
    assert route.call_count == 2
    rows = [json.loads(x) for x in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["ok"] is False


@respx.mock
async def test_retry_on_429_and_500_then_success() -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, json={"error": {"message": "slow down"}}),
            httpx.Response(500, json={"error": {"message": "boom"}}),
            httpx.Response(200, json=completion(json.dumps(GOOD))),
        ]
    )
    res = await call(make("json_schema"))
    assert res.ok and route.call_count == 3


@respx.mock
async def test_retry_on_timeout_then_success() -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.ConnectTimeout("t"),
            httpx.Response(200, json=completion(json.dumps(GOOD))),
        ]
    )
    res = await call(make("json_schema"))
    assert res.ok and route.call_count == 2


@respx.mock
async def test_retries_exhausted_reports_failure() -> None:
    route = respx.post(URL).respond(503, json={"error": {"message": "down"}})
    res = await call(make("json_schema"))
    assert not res.ok and "http 503" in res.errors[0]
    assert route.call_count == 3  # max_retries=2 -> 3 attempts


@respx.mock
async def test_non_transient_400_not_retried() -> None:
    route = respx.post(URL).respond(400, json={"error": {"message": "bad schema"}})
    res = await call(make("json_schema"))
    assert not res.ok and "http 400" in res.errors[0] and route.call_count == 1


@respx.mock
async def test_seed_rejection_falls_back_and_is_remembered() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "seed" in json.loads(request.content):
            return httpx.Response(400, json={"error": {"message": "Unknown parameter: seed"}})
        return httpx.Response(200, json=completion(json.dumps(GOOD)))

    route = respx.post(URL).mock(side_effect=handler)
    c = make("json_schema")
    assert (await call(c)).ok
    assert route.call_count == 2
    assert (await call(c)).ok
    assert route.call_count == 3  # no seed sent the second time
    assert "seed" not in body(route)


@respx.mock
async def test_seed_omitted_when_none() -> None:
    route = respx.post(URL).respond(json=completion(json.dumps(GOOD)))
    assert (await call(make("json_schema", seed=None))).ok
    assert "seed" not in body(route)
