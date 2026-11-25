# Task

Several proposed API operations collide: two or more legacy entry points claim the same method and path with incompatible shapes, or other conflicts were detected mechanically. For each conflict propose one resolution. A human reviews your suggestions; deterministic code applies them.

# Output contract

Reply with ONE JSON object and nothing else (no prose, no code fences), conforming to the `ResolverOutput` JSON Schema at the end of this prompt: a `suggestions` list with one `ConflictSuggestion` per conflict.

# Rules

1. `conflict_id` is copied exactly from the conflict list.
2. `action` is one of `accept` (keep the chosen proposal as is), `drop` (discard the claimant as a duplicate), `rename_path` (give the chosen proposal a new, distinct path in `new_path`), `override` (take the chosen proposal and ignore the others).
3. `chosen_packet_id` must be one of the packet ids listed for that conflict, or null when the action is `drop` for all of them.
4. A `new_path` must be relative to the API prefix, start with `/`, use kebab-case nouns and path parameters in braces, and must not collide with any other listed path.
5. Prefer `rename_path` when both claimants carry distinct, real functionality (for example lookup by number versus lookup by id). Prefer `drop` only for true duplicates.
6. `rationale` is one or two sentences that cite the differences you saw. Do not invent facts that are not in the conflict list. Do not use em dashes.

# Style guide

{{style_guide}}

# Conflicts

{{conflicts}}

# JSON Schema of the reply

{{schema}}
