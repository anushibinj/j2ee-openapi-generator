# Task

You polish the naming of the operations that belong to ONE tag of an API. You produce a rename map only. You must never change structure: do not add, remove or reorder operations, paths, parameters or schemas.

# Output contract

Reply with ONE JSON object and nothing else (no prose, no code fences), conforming to the `RenameMap` JSON Schema at the end of this prompt.

# Rules

1. `tag` is the tag given in the listing, unchanged.
2. For every operation in the listing, give one entry in `operations` whose `key` is exactly the `METHOD /path` string from the listing.
3. `operation_id` is camelCase in verbNoun form, for example `listOrders`, `getOrder`, `createOrderDraft`. Every operationId must be unique across the whole listing and must contain only letters and digits.
4. `summary` is a short imperative sentence without trailing period. `description` is one or two plain sentences, or null.
5. Add one `tag_descriptions` entry for the tag with a one sentence description.
6. Follow the style guide below. Do not use em dashes.

# Style guide

{{style_guide}}

# Operations of this tag

{{listing}}

# JSON Schema of the reply

{{schema}}
