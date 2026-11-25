# API Style Guide

This guide is the single source of truth for how legacy servlet operations become REST
endpoints. It is given verbatim to the analyzer, normalizer and resolver agents, and its hash is
part of every cache key. Follow it exactly. When the legacy code forces a deviation, keep the
closest conforming shape and explain the deviation in `hint_override_reason` or `open_questions`.

## 1. Paths and resources

- The emitter prepends the API prefix (for example `/v1`). Proposals use paths relative to it:
  `/users/{userId}`, never `/v1/users/{userId}`, never the legacy servlet URL.
- Resource names are kebab-case plural nouns derived from the servlet name (`UserServlet` becomes
  `users`, `OrderServlet` becomes `orders`, `ReportServlet` becomes `reports`). Drop the
  `Servlet`, `Action`, `Controller` and `Manager` suffixes before pluralizing.
- A resource that has exactly one instance per caller (for example the signed-in user's profile)
  is a singleton and uses a singular noun: `/profile`.
- Path segments are lowercase kebab-case literals or `{camelCaseParam}` templates. No trailing
  slash, no file extensions, no verbs in resource segments.
- Literal segments such as `/orders/drafts` or `/orders/export` take precedence over the
  templated `/orders/{orderId}` sibling.
- Nest only when the child cannot exist without the parent (`/orders/{orderId}/lines`). Keep
  nesting to two levels.

## 2. Parameters

- Query parameter names are camelCase (`page`, `size`, `customerId`, `createdFrom`). The legacy
  request parameter name goes in `legacy_name` when it differs.
- The legacy `id` parameter of an item operation becomes the path parameter `{resourceId}`
  named after the singular resource: `userId`, `orderId`, `draftId`. Its `legacy_name` is `id`.
- Identifiers are opaque. Declare numeric database keys as `integer` with format `int64` only
  when the code parses them as `Long`; clients must not assume ordering or arithmetic.
  Business keys such as an order number are `string`.
- Every path and query parameter needs at least one evidence citation, usually the
  `getParameter("...")` line or the form input that submits it.
- Booleans are `true`/`false`. Enumerations list the literal values found in code or in the
  entity enum.
- Repeated parameters (`getParameterValues`) become array query parameters.

## 3. Status code matrix

| Operation shape | Success | Other statuses the proposal lists |
| --- | --- | --- |
| GET collection | 200 | none |
| GET item | 200 | 404 |
| POST create | 201 with `Location` header | 409 on uniqueness violation |
| PUT item (full replace) | 200 with body, or 204 | 404, 409, 412 |
| PATCH item (partial) | 200 with body, or 204 | 404, 409, 412 |
| DELETE item | 204 | 404, 409 when still referenced |
| Action endpoint | 200 with result, 202 when asynchronous, 204 when nothing to return | 404, 409 |
| Action that creates a resource (draft submit) | 201 with `Location` header | 404, 409 |

Rules:

- The emitter adds 400, 401, 403 and 500 to every operation and 404 to every operation with a
  path parameter. Proposals list them only when the legacy code shows a specific cause, and then
  with evidence.
- Any status other than 200, 201, 204, 400, 401, 403, 500 needs an evidence citation on its
  response. A servlet `response.sendError(409, ...)` or a caught checked exception such as
  `UserInUseException` justifies 409 and must be cited.
- A checked business exception meaning "still referenced" or "duplicate" maps to 409. An
  optimistic locking failure (`OptimisticLockException`, `@Version` mismatch) maps to 412.
- Validation errors detected by the servlet (missing or malformed parameter, `@NotNull`,
  `@Size`, `@Pattern` violations) map to 400.

## 4. Errors

All error bodies are RFC 9457 `application/problem+json` with the standard members `type`,
`title`, `status`, `detail`, `instance` plus two extensions:

- `code`: stable machine readable string in `UPPER_SNAKE_CASE`, derived from the legacy message
  key when one exists (`error.user.inuse` becomes `USER_IN_USE`).
- `errors[]`: for 400 responses, one entry per invalid field with `field`, `code` and `message`.

Proposals reference the shared schema `Problem` for every error response.

## 5. Pagination

- Collection endpoints that accept `page` and `size` (or equivalent) are paginated. Set
  `paginated: true` on the 200 response; the emitter generates the wrapper schema `{Name}Page`
  (`UserPage`) with `items`, `page`, `size`, `totalElements`, `totalPages`.
- `page` is 0-based. `size` defaults to 20 and has a maximum of 100.
- `sort` is a repeatable query parameter of the form `field,asc` or `field,desc`.
- Filters (`name`, `status`, `customerId`) are plain query parameters.

## 6. Concurrency control

- An entity with a `@Version` field gets an `ETag` response header on item GET, PUT and PATCH.
- PUT, PATCH and DELETE on such an entity require `If-Match`; a mismatch returns 412. The legacy
  hidden `version` form field is replaced by this header and must not appear in request bodies.

## 7. Security

- Authentication is a bearer JWT (`Authorization: Bearer ...`). Do not model session cookies or
  form logins.
- Roles come only from `web.xml` `security-constraint`/`security-role` entries and from
  `@RolesAllowed` on the EJB methods or classes the branch calls. Method level annotations win
  over class level ones. Each role needs evidence. Never invent a role.
- The emitter writes roles into the operation as `x-roles`. Operations with no role evidence
  list no roles.

## 8. Data formats

- Timestamps are RFC 3339 `date-time` strings in UTC (`2024-05-01T12:30:00Z`). A date only
  value (`@Temporal(TemporalType.DATE)`, `<input type="date">`) is `format: date`.
- Money and every `BigDecimal` is a decimal string (`"1234.50"`) with `format: decimal`, never a
  JSON number.
- Enumerations (`@Enumerated(EnumType.STRING)`) are strings with the enum constants listed.
- `Long` identifiers are `integer` with `format: int64`; `int` is `integer` with `format: int32`.
- Property names are camelCase and follow the entity field names.
- Schema names match the entity or DTO simple name (`User`, `Order`, `OrderSummaryDto`).
  Derived request schemas are `{Entity}CreateRequest`, `{Entity}UpdateRequest` and
  `{Entity}Patch`.

## 9. PUT versus PATCH

- `PUT /things/{thingId}` replaces the whole representation; all required fields must be sent.
  Use it for a legacy `save` that submits the complete form.
- `PATCH /things/{thingId}` changes only the supplied fields. Use it for legacy operations that
  update a few attributes, or that add to existing state (wizard steps).
- A legacy `save` that creates when `id` is absent and updates when present becomes two
  operations: `POST /things` and `PUT /things/{thingId}`.

## 10. Action sub-resources

Anything that is not create, read, update or delete becomes `POST /things/{thingId}/{verb}` with
the verb in kebab-case (`submit`, `cancel`, `reset-password`). Use 200 with the result, 202 when
the work continues in the background, 204 when there is nothing to return.

## 11. Multi-step flows (drafts)

A wizard that keeps state in the `HttpSession` across several requests becomes a draft
resource owned by the target resource:

- `POST /orders/drafts` starts the draft (first step). 201 with the draft.
- `PATCH /orders/drafts/{draftId}` records the data of an intermediate step. 200 with the
  draft.
- `POST /orders/drafts/{draftId}/submit` runs the final step that persists the entity. 201 with
  the new entity and a `Location` header.

The draft id replaces the implicit session. A missing draft is 404. Steps that only render the
next form produce no operation.

## 12. Lookups

Small reference data lists served by a servlet (countries, statuses, codes) are exposed as
`GET /lookups/{type}` with a kebab-case plural type name. The response is a JSON array. When
another operation only needs such a list for a select box, record it in `lookups_needed` instead
of creating an operation.

## 13. Files

- Upload: `POST` with `multipart/form-data`. Name the file part `file` unless the code uses
  another part name. Respond 201 with the created resource, or 200 with an import report.
- Download: `GET` returning a binary body with the real content type (`text/csv`,
  `application/pdf`). Set `schema_ref: binary`, and cite the `Content-Disposition` header line;
  the emitter documents the header and the attachment file name.
- Literal paths such as `/orders/export` are used for collection level downloads.

## 14. Mapping hints by legacy op name

The packet carries a hint chosen from the table below. Follow the hint unless the code clearly
disagrees, and then set `hint_override_reason`.

| Legacy op name or behavior | HTTP shape |
| --- | --- |
| list, search, find, query, all, browse | GET collection |
| read, view, get, show, detail, load | GET item |
| add, create, new, insert, or save without `id` | POST collection |
| update, modify, or save with `id` | PUT item (full form) or PATCH item (partial) |
| delete, remove | DELETE item |
| export, download, print | GET binary or CSV |
| upload, import | POST multipart |
| edit that only loads a form | GET item, or `no_api_needed` when an equivalent read operation exists in the same servlet |
| new that renders an empty form | `no_api_needed` |
| anything else | POST action sub-resource |

## 15. Operations that need no API

Set `no_api_needed` to a short reason (and leave `operations` empty) when the branch:

- only renders an empty form, a menu or a static page;
- only redirects or forwards to another op (typically the default branch);
- only repeats data another operation of the same servlet already exposes;
- exists purely for navigation (session wizard screens with no data).

Never leave both `operations` and `no_api_needed` empty.

## 16. Evidence

- Every claim that is not obvious from the mapping hint carries an `evidence` entry with the
  repository relative POSIX path, the 1-based line number as shown in the packet, and the exact
  text of that line (leading and trailing whitespace is ignored).
- Cite only files that appear in the packet. Never cite a line you cannot see.
- A citation is verified mechanically: identifiers and string literals of the snippet must occur
  within two lines of the cited line.

## 17. Operation ids and summaries

Operation ids are camelCase `verbNoun` and unique across the whole API: `listUsers`, `getUser`,
`createUser`, `updateUser`, `deleteUser`, `submitOrderDraft`, `exportOrders`. Standard verbs:
`list`, `get`, `create`, `update`, `delete`, plus the action verb for action endpoints. Summaries
are short imperative sentences without a trailing period.

## 18. Tags

A tag is the module tag prefix followed by the resource noun in title case, for example
`Accounts Users`, `Accounts Profile`, `Sales Orders`, `Sales Reports`, `Sales Lookups`. All
operations of one resource in one module share one tag.
