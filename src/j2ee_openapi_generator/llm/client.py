"""OpenAI-compatible structured-output client (official ``openai`` SDK only)."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path
from typing import Any

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from j2ee_openapi_generator.config import Config, LLMConfig
from j2ee_openapi_generator.llm.base import LLMClient, StructuredResult, T
from j2ee_openapi_generator.llm.fake import FakeLLM

MODES = ("json_schema", "tools", "json_object", "prompt_only")
_TOOL_NAME = "emit_result"


# ------------------------------------------------------------------ helpers
def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema of ``model`` made friendly to strict structured-output servers."""
    schema = copy.deepcopy(model.model_json_schema())
    _strictify(schema)
    return schema


def _strictify(node: Any) -> None:
    if not isinstance(node, dict):
        return
    node.pop("default", None)
    props = node.get("properties")
    if isinstance(props, dict):
        node["required"] = list(props.keys())
        node["additionalProperties"] = False
        for sub in props.values():
            _strictify(sub)
    for key in ("$defs", "definitions"):
        defs = node.get(key)
        if isinstance(defs, dict):
            for sub in defs.values():
                _strictify(sub)
    for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
        seq = node.get(key)
        if isinstance(seq, list):
            for sub in seq:
                _strictify(sub)
    _strictify(node.get("items"))


def extract_json_object(text: str) -> str | None:
    """First balanced JSON object in ``text`` (tolerates fences and leading prose)."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    cand = text[start : i + 1]
                    try:
                        json.loads(cand)
                        return cand
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, openai.APIConnectionError):  # includes timeouts
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


def _seed_rejected(exc: BaseException) -> bool:
    if isinstance(exc, openai.APIStatusError) and exc.status_code in (400, 422):
        msg = str(exc).lower()
        return "seed" in msg or "unknown parameter" in msg or "unrecognized" in msg
    return False


def _describe(exc: BaseException) -> str:
    if isinstance(exc, openai.APIStatusError):
        return f"http {exc.status_code}: {exc.message}"
    return f"request failed: {type(exc).__name__}: {exc}"


# ------------------------------------------------------------------ client
class OpenAICompatClient:
    def __init__(
        self,
        cfg: LLMConfig,
        usage_path: Path | None = None,
        backoff_min_s: float = 1.0,
        mode: str | None = None,
    ) -> None:
        self.cfg = cfg
        self._mode = mode or cfg.structured_mode
        self.usage_path = usage_path
        self.backoff_min_s = backoff_min_s
        self._seed_ok = True
        self._lock = threading.Lock()
        self._client = AsyncOpenAI(
            base_url=cfg.base_url, api_key=cfg.api_key, timeout=cfg.timeout_s, max_retries=0
        )

    @property
    def model_name(self) -> str:
        return self.cfg.model

    @property
    def structured_mode(self) -> str:
        return self._mode

    @property
    def temperature(self) -> float:
        return self.cfg.temperature

    # -- message shaping
    def _shape(
        self, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> list[dict[str, str]]:
        msgs = [dict(m) for m in messages]
        schema_txt = json.dumps(schema, sort_keys=True)
        if self._mode == "json_object":
            note = (
                "Respond with a single JSON object that conforms to this JSON Schema:\n"
                f"{schema_txt}"
            )
            if msgs and msgs[0]["role"] == "system":
                msgs[0]["content"] = f"{msgs[0]['content']}\n\n{note}"
            else:
                msgs.insert(0, {"role": "system", "content": note})
        elif self._mode == "prompt_only":
            note = (
                "Output must conform to this JSON Schema:\n"
                f"{schema_txt}\n\nRespond with JSON only, no prose and no code fences."
            )
            for m in reversed(msgs):
                if m["role"] == "user":
                    m["content"] = f"{m['content']}\n\n{note}"
                    break
            else:
                msgs.append({"role": "user", "content": note})
        return msgs

    def _kwargs(
        self, messages: list[dict[str, str]], schema: dict[str, Any], name: str
    ) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_output_tokens,
        }
        if self._mode == "json_schema":
            kw["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            }
        elif self._mode == "tools":
            kw["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": _TOOL_NAME,
                        "description": "Return the result.",
                        "parameters": schema,
                    },
                }
            ]
            kw["tool_choice"] = {"type": "function", "function": {"name": _TOOL_NAME}}
        elif self._mode == "json_object":
            kw["response_format"] = {"type": "json_object"}
        if self.cfg.seed is not None and self._seed_ok:
            kw["seed"] = self.cfg.seed
        return kw

    async def _create(self, kw: dict[str, Any]) -> Any:
        retrying = AsyncRetrying(
            retry=retry_if_exception(_is_transient),
            stop=stop_after_attempt(self.cfg.max_retries + 1),
            wait=wait_exponential(multiplier=self.backoff_min_s, min=self.backoff_min_s, max=30),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                return await self._client.chat.completions.create(**kw)
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _complete(
        self, messages: list[dict[str, str]], schema: dict[str, Any], name: str
    ) -> tuple[str, dict[str, int]]:
        kw = self._kwargs(messages, schema, name)
        try:
            resp = await self._create(kw)
        except openai.APIStatusError as exc:
            if "seed" in kw and _seed_rejected(exc):
                self._seed_ok = False
                kw.pop("seed")
                resp = await self._create(kw)
            else:
                raise
        msg = resp.choices[0].message
        text = ""
        if self._mode == "tools" and getattr(msg, "tool_calls", None):
            text = msg.tool_calls[0].function.arguments or ""
        if not text:
            text = msg.content or ""
        u = getattr(resp, "usage", None)
        usage = {
            "prompt_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(u, "total_tokens", 0) or 0),
        }
        return text, usage

    @staticmethod
    def _validate(text: str, model: type[T]) -> tuple[T | None, list[str]]:
        cand = extract_json_object(text)
        if cand is None:
            return None, ["reply contains no JSON object"]
        try:
            return model.model_validate(json.loads(cand)), []
        except ValidationError as exc:
            return None, [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        except ValueError as exc:
            return None, [str(exc)]

    def _log(
        self, purpose: str, item_id: str, usage: dict[str, int], repaired: bool, ok: bool
    ) -> None:
        if self.usage_path is None:
            return
        line = json.dumps(
            {
                "purpose": purpose,
                "item_id": item_id,
                "model": self.cfg.model,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "repaired": repaired,
                "ok": ok,
            },
            sort_keys=True,
        )
        with self._lock:
            self.usage_path.parent.mkdir(parents=True, exist_ok=True)
            with self.usage_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    async def structured(
        self,
        *,
        purpose: str,
        item_id: str,
        messages: list[dict[str, str]],
        response_model: type[T],
    ) -> StructuredResult:
        schema = strict_schema(response_model)
        sent = self._shape(messages, schema)
        request: dict[str, Any] = {
            "messages": sent,
            "mode": self._mode,
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
        }
        total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        def add(u: dict[str, int]) -> None:
            for k in total:
                total[k] += u.get(k, 0)

        raw = ""
        errors: list[str] = []
        repaired = False
        convo = list(sent)
        try:
            for turn in (0, 1):
                raw, u = await self._complete(convo, schema, response_model.__name__)
                add(u)
                obj, errors = self._validate(raw, response_model)
                if obj is not None:
                    self._log(purpose, item_id, total, repaired, True)
                    return StructuredResult(
                        ok=True,
                        parsed=obj.model_dump(),
                        raw=raw,
                        repaired=repaired,
                        usage=dict(total),
                        request=request,
                    )
                if turn == 0:
                    repaired = True
                    convo = [
                        *convo,
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": "Your reply failed validation:\n- "
                            + "\n- ".join(errors)
                            + "\nReply again with the corrected JSON object only.",
                        },
                    ]
        except (TimeoutError, openai.APIError) as exc:
            errors = [_describe(exc)]
        self._log(purpose, item_id, total, repaired, False)
        return StructuredResult(
            ok=False,
            raw=raw,
            errors=errors,
            repaired=repaired,
            usage=dict(total),
            request=request,
        )


def make_client(cfg: Config) -> LLMClient:
    if cfg.llm.fake:
        fd = Path(cfg.llm.fake_dir or "llm")
        if not fd.is_absolute():
            fd = cfg.base_dir / fd
        return FakeLLM(fd, model=cfg.llm.model, mode=cfg.llm.structured_mode)
    return OpenAICompatClient(cfg.llm, usage_path=cfg.work_path / "usage.jsonl")


# ------------------------------------------------------------------ doctor
class _Ping(BaseModel):
    ok: bool


async def probe_modes(cfg: LLMConfig) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    pcfg = cfg.model_copy(
        update={"max_retries": 0, "max_output_tokens": min(cfg.max_output_tokens, 256)}
    )
    for mode in MODES:
        client = OpenAICompatClient(pcfg, mode=mode)
        res = await client.structured(
            purpose="probe",
            item_id=mode,
            messages=[
                {"role": "system", "content": "You are a JSON API."},
                {"role": "user", "content": 'Reply with {"ok": true}.'},
            ],
            response_model=_Ping,
        )
        if res.ok and res.parsed and res.parsed.get("ok") is True:
            out[mode] = {"status": "supported", "error": None}
        else:
            err = res.errors[0] if res.errors else "unexpected reply"
            hard = err.startswith("request failed") or err.startswith("http 5")
            out[mode] = {"status": "error" if hard else "unsupported", "error": err}
    return out


def _server_context(item: Any) -> int | None:
    extra = dict(getattr(item, "model_extra", None) or {})
    for key in ("max_model_len", "context_length", "context_window"):
        val = extra.get(key, getattr(item, key, None))
        if isinstance(val, int) and not isinstance(val, bool):
            return val
    return None


async def _context_check(cfg: LLMConfig) -> dict[str, Any]:
    client = AsyncOpenAI(
        base_url=cfg.base_url, api_key=cfg.api_key, timeout=cfg.timeout_s, max_retries=0
    )
    try:
        page = await client.models.list()
        items = [m async for m in page]
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        return {"status": "error", "error": _describe(exc)}
    for m in items:
        if m.id == cfg.model:
            ctx = _server_context(m)
            if ctx is None:
                return {"status": "unknown", "error": "server reports no context length"}
            if cfg.context_tokens > ctx:
                return {
                    "status": "too_large",
                    "error": f"configured context_tokens {cfg.context_tokens} > server {ctx}",
                    "server_context": ctx,
                }
            return {"status": "ok", "error": None, "server_context": ctx}
    return {"status": "unknown", "error": f"model {cfg.model} not listed by server"}


def run_doctor(cfg: Config) -> list[dict[str, Any]]:
    async def go() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            modes = await probe_modes(cfg.llm)
        except Exception as exc:  # noqa: BLE001
            modes = {m: {"status": "error", "error": _describe(exc)} for m in MODES}
        for mode in MODES:
            rows.append({"check": f"mode:{mode}", **modes[mode]})
        rows.append({"check": "context", **await _context_check(cfg.llm)})
        return rows

    return asyncio.run(go())
