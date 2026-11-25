# Design decisions

Ambiguities are resolved in favour of determinism and traceability.

1. **Main branch.** `claude/nifty-ride-pxol9z` plays the role of `main`. Worktree branches are named `feat/<stream>` and merged into it.
2. **Shared contracts** live in `index/models.py`, `packets/models.py`, `llm/schemas.py`, `llm/base.py`, `aggregate/models.py`, `ids.py`, `config.py` and `workspace.py`. Only the lead edits them.
3. **LLM client protocol** (`llm/base.py`) performs the validation and the single repair turn inside the client, so stages see only `StructuredResult`. Caching is done by the stage (analyzer, normalizer) using `llm/cache.py`.
4. **Fake LLM** is selected with `llm.fake: true` and `llm.fake_dir`. File names are `id_to_filename(packet_id).json`; attempt 2 uses `*.repair.json` when present.
5. **Token counting** uses `tiktoken` `cl100k_base`; when the encoding cannot be loaded (offline) it falls back to chars/4. Both are approximations.
6. **Proposal paths** are relative to `openapi.api_prefix`; the emitter prepends the prefix.
7. **Standard errors** (400, 401, 403, 500) are added by the emitter, never proposed.
8. **YAML** uses `ruamel.yaml` for the OpenAPI document, and `pyyaml` only for reading config and decision files.
9. **Page schema** uses `content`, `page`, `size`, `totalElements`, `totalPages`.
10. **Known gap:** `EntityField` has no `@Temporal`, so `java.util.Date` is always `date-time`.
11. **Contract additions during build:** `AccessCount.module`, `IndexBundle`, `ProposalRecord`, `StoreSnapshot`, `CoverageRow`.
12. **JSP parsing** uses a regex tokenizer over masked text (not selectolax) to keep real line numbers.
13. **Context chunks.** Packets also carry the dispatcher lines before the branch (or the `init()` of map dispatch), the EJB class header with class-level annotations, and the module `web.xml`, so role and registration evidence is citeable. EJB calls made before the dispatch are merged into every branch.
14. **Conflict IDs** are `c-` plus 8 hex of sha256 over kind, method and normalized path. `ejb_path_mismatch` conflicts are advisory and do not block emit.
15. **Re-analysis.** `oapi run` re-analyzes a rejected packet once (attempt 2, cached separately) with the verifier reasons as feedback. Still rejected means it appears as `rejected` in coverage for human review.
16. **Fixture app.** The unresolved `view`/`detail` conflict is kept in the golden run on purpose, so the emitted spec omits `GET /orders/{orderId}`.
