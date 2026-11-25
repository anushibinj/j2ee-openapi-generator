# j2ee-openapi-generator

A deterministic, multi-agent OpenAPI contract extractor for legacy Java EE monorepos
(Servlets, JSPs, EJBs, JPA entities and `.properties` bundles on JBoss). It produces ONE bundled
`openapi.yml` describing a resource-oriented, stateless REST API (v1) that covers every
user-facing capability currently implemented through Servlets and JSPs.

Deterministic code does discovery, slicing, merging and YAML emission. LLM agents only
interpret bounded work packets and normalize names, and their output is mechanically verified
against cited source lines before it is used.

```
index -> packets -> analyze (LLM) -> verify -> aggregate -> normalize (LLM) -> emit -> report/validate
```

Same repo + same config + same cache gives a byte-identical `openapi.yml`. The target repo is
never written to.

## Setup

```bash
uv sync
cp oapi.example.yaml oapi.yaml
cp .env.example .env        # LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
```

Edit `oapi.yaml` (`repo_root`, optional `modules`, dispatch parameter names, `llm.*`). Secrets are
interpolated from the environment with `${NAME}`. Any OpenAI-compatible endpoint works through
the official `openai` SDK (vLLM, Ollama, LM Studio, LiteLLM, TGI, ...).

```bash
uv run oapi doctor          # which structured modes the endpoint supports, context sanity check
```

Set `llm.structured_mode` to a supported mode: `json_schema`, `tools`, `json_object` or
`prompt_only`. Output is always validated with pydantic, with one repair turn on failure.

## Running

```bash
uv run oapi run                       # every stage in order
uv run oapi index                     # .oapi/index/*.json and .oapi/packets/*.json
uv run oapi analyze [--only ID] [--offline] [--force]
uv run oapi verify                    # lists rejected proposals with reasons (e.g. E-NOAPI-DATA)
uv run oapi aggregate [--resolve-with-llm]
uv run oapi normalize
uv run oapi emit                      # out/openapi.yml
uv run oapi report                    # out/coverage.{md,csv}, open-questions.md, conflicts.md
uv run oapi validate [--baseline previous/openapi.yml]
```

Every stage is idempotent and resumable from `work_dir` (default `.oapi`). LLM calls are cached
by packet hash, prompt version, schema version, style-guide hash, model, mode, temperature and
attempt, so a re-run is free and `--offline` fails on a cache miss. `oapi run` re-analyzes
rejected proposals once, feeding the verifier reasons back to the model.

Try it on the bundled fake monorepo, with the fake LLM and no network:

```bash
uv run oapi run --config tests/fixtures/oapi.yaml
```

## Review workflow

1. Run `oapi run` and read `out/coverage.md`, `out/conflicts.md` and `out/open-questions.md`.
   Every entry point is `mapped`, `no_api_needed`, `rejected`, `conflict` or `failed`.
2. Record human decisions in `review/decisions.yaml` (see `review/decisions.example.yaml`),
   keyed by packet ID (`module:Servlet#op`) or conflict ID (`c-xxxxxxxx`). Actions: `accept`,
   `override`, `drop` (needs a reason) and `rename_path`. Commit the file.
3. `oapi aggregate --resolve-with-llm` writes advisory `review/suggestions.yaml`. Suggestions are
   never applied automatically; copy the ones you agree with into `decisions.yaml`.
4. Re-run `oapi aggregate && oapi normalize && oapi emit && oapi report`.

Incompatible duplicates are never auto-resolved: they go to `conflicts.md`.

## Locking v1 with oasdiff in CI

```bash
cp out/openapi.yml api/baseline-v1.yml          # commit once the contract is accepted
```

In CI, run `uv run oapi run` and then:

```bash
uv run oapi validate --baseline api/baseline-v1.yml
```

`validate` always runs `openapi-spec-validator`, runs Spectral (`pnpm dlx @stoplight/spectral-cli
lint out/openapi.yml -r rules/.spectral.yaml`) when `pnpm` is on PATH, and runs
`oasdiff breaking` when `oasdiff` is on PATH and a baseline is given. A breaking change fails
the command, which keeps the contract additive-only.

## How it works

* **Index (no LLM):** tree-sitter Java symbol table, `web.xml`/`ejb-jar.xml`, `@WebServlet`,
  dispatch extraction for `if` chains, `switch`, constants, `Map<String, Handler>` registries,
  reflection naming patterns and default branches; branch facts (params, session, attributes,
  forwards, status codes, EJB calls); EJB and entity registries; JSP facts with scriptlet masking
  that preserves line numbers; properties; a linker from JSP/JS URLs to (servlet, op); optional
  access-log usage counts. File parsing runs in a process pool (`workers`).
* **Packets:** one numbered-source work packet per entry point (`=== FILE path (lines a-b) ===`,
  every line prefixed `NNNN| `), trimmed to the token budget, with deterministic mapping hints.
* **Verify:** every evidence citation must exist in the packet and its snippet identifiers and
  literals must appear within two lines. Params, statuses, roles and schema refs are cross-checked.
* **Aggregate:** SQLite store, dedupe, conflicts, human decisions, schema generation from the
  entity registry by code (not by the LLM).
* **Emit:** `ruamel.yaml`, sorted, no timestamps, enforced status matrix, RFC 9457 problem
  responses, bearer auth, `x-roles` and optional `x-legacy-source` provenance.

The conventions fed to the model are in `style-guide/api-style-guide.md`, and its hash is part of
the cache key. Design choices are in `DECISIONS.md`.

## Known limitations

* **No full type resolution.** The Java index resolves simple names through imports, the same
  package and wildcard imports only. No generics bounds, overload resolution or lambda typing, so
  EJB overloads are matched by argument count.
* **Dynamic dispatch.** Reflection is detected only when the method that reads the op also calls
  `getMethod`/`invoke`; map dispatch built through `Map.of`, loops or other builders is missed;
  an op passed to a helper that dispatches is not followed; negated conditions are not handled.
* **Scriptlet-heavy JSPs.** Anything inside `<% %>` is masked, so forms or links emitted from
  scriptlets are invisible, and a `%>` inside a scriptlet string ends the mask early.
* **URLs built in JS.** Only literal URLs in `fetch`, XHR, `$.ajax`, `$.get`, `$.post` and
  `$.getJSON` are linked; `fetch` bodies and `xhr.send(...)` params are not extracted.
* `java.util.Date` always becomes `date-time` (no `@Temporal` info).
* Access counts are keyed on (module, url pattern, op) from the module's context root.
* Token counts are approximate (`cl100k_base`, or chars/4 when the encoding is unavailable).

## Development

```bash
uv run pytest
uv run ruff check src tests
uv run mypy src
UPDATE_GOLDEN=1 uv run pytest tests/e2e     # regenerate tests/golden/openapi.yml
```

Tests run offline with a fake LLM and `respx`-mocked OpenAI-compatible servers.
