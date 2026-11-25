# Test fixtures

This directory is the contract other workstreams test against. Everything is deterministic.
Line numbers below were checked with grep against the committed sources; if you edit a fixture
file, update this file and the canned JSON in the same commit (the canned citations must stay
exact).

```
tests/fixtures/
  oapi.yaml                  config for the fake app (fake LLM, fake_dir: llm)
  legacy-app/                fake Java EE monorepo with two modules (accounts, sales)
  llm/                       canned LLM output, see below
```

`oapi.yaml` points `repo_root` at `legacy-app`, sets `work_dir: .oapi-test`, `out_dir: out-test`,
`review_dir: review-test` (tests override these with tmp dirs), `style_guide:
../../style-guide/api-style-guide.md`, `llm.fake: true`, `llm.fake_dir: llm`, tag prefixes
`Accounts` and `Sales`, context roots `/accounts` and `/sales`.

## Layout of legacy-app

| Module | web.xml | Context root |
| --- | --- | --- |
| accounts | `accounts/src/main/webapp/WEB-INF/web.xml` | `/accounts` |
| sales | `sales/src/main/webapp/WEB-INF/web.xml` | `/sales` |

Sources under `<module>/src/main/java/com/acme/<module>/{web,ejb,model,dto}`, JSPs and JS under
`<module>/src/main/webapp`, bundles under `<module>/src/main/resources`, and
`accounts/src/main/resources/META-INF/ejb-jar.xml`. File discovery with the default globs finds
every `*.java`, `*.jsp`, `*.jspf`, `web.xml`, `ejb-jar.xml`, `*.properties`, `*.js` file except
the excluded one:

- `accounts/src/test/java/com/acme/accounts/web/UserServletTest.java` matches `**/test/**` and
  must be excluded (it contains `"list".equals(op)` and a branch literal `hidden` that must never
  show up in the index).

## Entry point IDs

IDs are `{module}:{ServletSimpleName}#{op}` (`ids.entry_point_id`). The canned file for an ID is
`llm/{id_to_filename(id)}.json`. The default branch of a dispatcher uses the op name
`<default>` (file stem has three underscores, e.g. `accounts--UserServlet___default`). If the
indexer chooses a different default op token, rename the `<default>` files and the
`packet_id`/`legacy_source.op` fields accordingly (two files).

Total: 21 entry points.

### accounts: UserServlet (url `/UserServlet`, web.xml, doGet and doPost both call `process`)

Style: if / else-if chain on `request.getParameter("op")` (line 41). `"list".equals(op)` style
comparisons, `"read".equalsIgnoreCase(op)`. File:
`accounts/src/main/java/com/acme/accounts/web/UserServlet.java` (`UserServlet.java` below).

| ID | Branch lines | HTTP methods | Canned file | Result |
| --- | --- | --- | --- | --- |
| `accounts:UserServlet#list` | 42-49 | GET, POST | `accounts--UserServlet__list.json` | `GET /users`, paginated, query page/size/name |
| `accounts:UserServlet#read` | 50-58 | GET, POST | `accounts--UserServlet__read.json` | `GET /users/{userId}`, 200, 404 |
| `accounts:UserServlet#edit` | 59-63 | GET, POST | `accounts--UserServlet__edit.json` | `no_api_needed` (same data as read) |
| `accounts:UserServlet#new` | 64-67 | GET, POST | `accounts--UserServlet__new.json` | `no_api_needed` (empty form) |
| `accounts:UserServlet#save` | 68-69 plus helper `saveUser` 83-117 | POST (form), GET also reaches it | `accounts--UserServlet__save.json` | two operations: `POST /users` 201, `PUT /users/{userId}` 200/404/412 |
| `accounts:UserServlet#delete` | 70-77 | POST (ajax) | `accounts--UserServlet__delete.json` | `DELETE /users/{userId}` 204, 404, 409 |
| `accounts:UserServlet#<default>` | 78-80 (final else) | GET, POST | `accounts--UserServlet___default.json` | `no_api_needed` (redirect to list) |

Facts for extractors:

- Params per branch: list reads `page` (43), `size` (44), `name` (45); read reads `id` (51);
  edit `id` (60); delete `id` (71). `saveUser` reads `id` (85), `name` (86), `email` (87),
  `status` (88), `birthDate` (89), `version` (90); helper-following (depth 1) from the save branch
  must reach them.
- Forwards: list -> `/userList.jsp` (49), read -> `/userView.jsp` (58), edit -> `/userEdit.jsp`
  (63), new -> `/userEdit.jsp` (67), save (helper, validation error) -> `/userEdit.jsp` (93).
  JSPs live in `accounts/src/main/webapp/`.
- Status codes: read 404 via `SC_NOT_FOUND` (54); delete 204 via `SC_NO_CONTENT` (74) and literal
  `409` (76); save 400 via `SC_BAD_REQUEST` (97) and 409 via `SC_CONFLICT` (113).
- Redirects: default (79) and save success (116).
- Exceptions: delete catches `UserInUseException` (75-76); save catches `OptimisticLockException`
  (112).
- EJB calls (field `userManager`, type `UserManagerLocal`): `findUsers(String,int,int)`,
  `countUsers(String)`, `findUser(Long)`, `create(User)`, `update(User,Long)`, `delete(Long)`.
- Session usage: none. Attributes set: `users`, `total`, `user`, `statuses`, `error`.
- Dispatch param `op`; the default branch is the final `else`.

### accounts: ProfileServlet (`@WebServlet(name = "ProfileServlet", urlPatterns = {"/profile"})`, annotation source)

Style: constants `OP_VIEW`/`OP_UPDATE` (lines 19-20) compared via `OP_VIEW.equals(op)` (41) and
`OP_UPDATE.equals(op)` (44) on `op` from line 39. No default branch (no entry point for it).
Both `doGet` and `doPost` call `handle`. Class annotation at line 15.

| ID | Branch lines | Canned file | Result |
| --- | --- | --- | --- |
| `accounts:ProfileServlet#view` | 41-43 | `accounts--ProfileServlet__view.json` | `GET /profile`, 200 |
| `accounts:ProfileServlet#update` | 44-57 | `accounts--ProfileServlet__update.json` | `PATCH /profile`, body UserPatch, 200/400 |

`update` reads `displayName` (45), `email` (46), `locale` (47); forwards to `/userView.jsp` in
view (43); redirect (56); 400 via `SC_BAD_REQUEST` (49); EJB `findByLogin(String)` (40, outside the
branches, shared) and `update(User,Long)`.

### sales: OrderServlet (url `/OrderServlet`)

Style: `switch (op)` (line 54) whose case labels are `static final String` constants declared on
lines 24-30 (`OP_LIST`..`OP_EXPORT`, values `list view detail step1 step2 confirm export`).
`op` is null-guarded at 50-52 and the `default:` label is at line 133. File:
`sales/src/main/java/com/acme/sales/web/OrderServlet.java`.

| ID | Case lines | Canned file | Result |
| --- | --- | --- | --- |
| `sales:OrderServlet#list` | 55-65 | `sales--OrderServlet__list.json` | `GET /orders` paginated, query page/size/status/customerId |
| `sales:OrderServlet#view` | 66-75 | `sales--OrderServlet__view.json` | `GET /orders/{orderId}` (integer), 200 Order, 404. CONFLICT with `detail` |
| `sales:OrderServlet#detail` | 76-81 | `sales--OrderServlet__detail.json` | `GET /orders/{orderId}` (string order number), 200 OrderSummaryDto. CONFLICT with `view` |
| `sales:OrderServlet#step1` | 82-94 | `sales--OrderServlet__step1.json` | `POST /orders/drafts` 201 (wizard step 1; GET renders `wizardStep1.jsp`) plus lookup need `countries` |
| `sales:OrderServlet#step2` | 95-109 | `sales--OrderServlet__step2.json` | `PATCH /orders/drafts/{draftId}` 200/404 |
| `sales:OrderServlet#confirm` | 110-120 | `sales--OrderServlet__confirm.json` | `POST /orders/drafts/{draftId}/submit` 201/404 |
| `sales:OrderServlet#export` | 121-132 | `sales--OrderServlet__export.json` | `GET /orders/export`, `text/csv`, binary |
| `sales:OrderServlet#<default>` | 133-134 | `sales--OrderServlet___default.json` | `no_api_needed` (redirect to list) |

Facts for extractors:

- Params: list `page` (56), `size` (57), `status` (58), `customerId` (59); view `id` (67);
  detail `orderNumber` (77); step1 `customerId` (85), `notes` (86); step2 `sku` (102),
  `quantity` (103), `unitPrice` (104); export `status` (122). `getParameter("op")` at line 49.
- Session: `request.getSession()` at 53; step1 writes `orderDraft` (88); step2 reads (96) and
  writes (106) `orderDraft`; confirm reads (111) and removes (117, `removeAttribute`).
- Forwards: list/view/detail -> `/orderList.jsp` (63, 73, 79), step1 -> `/wizardStep2.jsp` (89)
  or `/wizardStep1.jsp` (91), step2 -> `/wizardStep2.jsp` (107).
- Status codes: view 404 `SC_NOT_FOUND` (69); step2 400 (98); confirm 400 (113).
- Redirects: confirm (118), default (134).
- Export: `setContentType("text/csv")` (123), `Content-Disposition` header (124), writer output.
- EJB calls (`orderManager`, `OrderManagerLocal`): `findOrders`, `countOrders`, `findById`,
  `findSummary`, `createFromDraft`.
- Security: web.xml constraint on `/OrderServlet` POST, role `sales` (web.xml lines 35, 36, 39);
  `OrderManagerBean` is `@RolesAllowed({"sales", "manager"})` (line 20) with
  `@RolesAllowed("sales")` on `createFromDraft` (line 53).

### sales: ReportServlet (url `/ReportServlet`) extends abstract `BaseServlet`

Style: reflection. `BaseServlet.dispatch` reads `op` (BaseServlet.java:30) and calls
`getClass().getMethod("do" + capitalize(op), HttpServletRequest.class, HttpServletResponse.class)`
(line 37). `BaseServlet` is not mapped in web.xml and yields no entry points; the ops of
`ReportServlet` are its public `do*` methods (excluding inherited doGet/doPost).

| ID | Method lines | Canned file | Result |
| --- | --- | --- | --- |
| `sales:ReportServlet#summary` | `doSummary` 21-27 | `sales--ReportServlet__summary.json` | `GET /reports/summary`, query from/to (date), array of OrderSummaryDto |
| `sales:ReportServlet#daily` | `doDaily` 29-36 | `sales--ReportServlet__daily.json` | `GET /reports/daily`, query day, JSON `DailyReport` |

`doSummary` reads `from` (23), `to` (24), forwards `/report.jsp` (26). `doDaily` reads `day` (31),
writes `application/json` (33). The op names derive from the method names: `doSummary` ->
`summary`, `doDaily` -> `daily` (pattern `do{Op}`). Files:
`sales/src/main/java/com/acme/sales/web/{BaseServlet,ReportServlet}.java`.

### sales: LookupServlet (url `/LookupServlet`)

Style: map dispatch. `Map<String, Handler> handlers` filled in `init()`: `handlers.put("countries",
this::writeCountries)` (line 24), `handlers.put("statuses", this::writeStatuses)` (line 25);
`doGet` (29-36) does `handlers.get(request.getParameter("op"))` (31) and answers 404 for unknown
(33). No default branch, no `doPost`.

| ID | Handler lines | Canned file | Result |
| --- | --- | --- | --- |
| `sales:LookupServlet#countries` | `writeCountries` 39-43 | `sales--LookupServlet__countries.json` | `GET /lookups/countries` |
| `sales:LookupServlet#statuses` | `writeStatuses` 45-48 | `sales--LookupServlet__statuses.json` (+ `.repair.json`) | `GET /lookups/statuses` (BAD CITATION fixture) |

## Deliberate verifier/aggregator fixtures

### (a) Bad citation: `sales:LookupServlet#statuses`

`llm/sales--LookupServlet__statuses.json` (attempt 1) contains two broken citations:

1. operation evidence: file `LookupServlet.java`, line 25, snippet
   `handlers.put("statuses", this::loadStatusesFromDatabase);` (line 25 is really
   `handlers.put("statuses", this::writeStatuses);`, so the snippet identifiers are absent);
2. response evidence: line `9999`, outside the file.

`llm/sales--LookupServlet__statuses.repair.json` holds the corrected proposal. Fake LLM rule:
attempt 1 for a packet reads `{id}.json`; attempt 2 (re-analysis after verifier rejection) reads
`{id}.repair.json` if it exists, otherwise `{id}.json` again. Expected pipeline outcome: first
verification rejects the packet, the retry loads the repair file, which verifies, and the final
spec contains `GET /lookups/statuses`. All other proposals have exact citations (every snippet is
the stripped text of its cited line; checked by script).

### (b) Conflicting pair: `sales:OrderServlet#view` and `sales:OrderServlet#detail`

Both claim `GET /orders/{orderId}` (normalized key `GET /orders/{}`), with incompatible shapes:

- `view`: path param `orderId` is `integer`/`int64` (legacy `id`), 200 schema `Order`, 404;
- `detail`: path param `orderId` is `string` (legacy `orderNumber`), 200 schema
  `OrderSummaryDto`.

`llm/_resolver.json` is the canned `ResolverOutput`: conflict id `c-4eb5d4f7`
(`{METHOD} {path with every {param} replaced by {}}`; adapt if the aggregator uses another
id), action `rename_path`, chosen packet `sales:OrderServlet#detail`, new path
`/orders/by-number/{orderNumber}`. After resolution, `detail` becomes `GET
/orders/by-number/{orderNumber}`; `llm/_normalizer/Sales_Orders.json` already has rename entries
for both `GET /orders/{orderId}` (`getOrder`) and `GET /orders/by-number/{orderNumber}`
(`getOrderByNumber`).

## Tags

Tag = module `tag_prefix` + space + resource noun in title case. A normalizer file is
`llm/_normalizer/{id_to_filename(tag)}.json` (spaces become `_`); the `tag` field inside is the
real tag.

| Tag | File | Operations (key -> operationId) |
| --- | --- | --- |
| `Accounts Users` | `Accounts_Users.json` | `GET /users` listUsers, `POST /users` createUser, `GET /users/{userId}` getUser, `PUT /users/{userId}` updateUser, `DELETE /users/{userId}` deleteUser |
| `Accounts Profile` | `Accounts_Profile.json` | `GET /profile` getProfile, `PATCH /profile` updateProfile |
| `Sales Orders` | `Sales_Orders.json` | `GET /orders` listOrders, `GET /orders/{orderId}` getOrder, `GET /orders/by-number/{orderNumber}` getOrderByNumber, `POST /orders/drafts` createOrderDraft, `PATCH /orders/drafts/{draftId}` updateOrderDraft, `POST /orders/drafts/{draftId}/submit` submitOrderDraft, `GET /orders/export` exportOrders |
| `Sales Reports` | `Sales_Reports.json` | `GET /reports/summary` getSalesSummary, `GET /reports/daily` getDailyReport |
| `Sales Lookups` | `Sales_Lookups.json` | `GET /lookups/countries` listCountries, `GET /lookups/statuses` listOrderStatuses |

Expected final operation count after resolution: 18 operations (accounts 7: 5 users + 2 profile;
sales 11: 7 orders + 2 reports + 2 lookups). The no-API entry points are `accounts:UserServlet#edit`, `#new`, `#<default>` and
`sales:OrderServlet#<default>`.

## Per-file indexer expectations

### Web index

- accounts web.xml: servlet `UserServlet` -> `com.acme.accounts.web.UserServlet`, pattern
  `/UserServlet` (lines 14-25), init-param `defaultPageSize=20` (18-19); filter `EncodingFilter`
  (`com.acme.accounts.web.EncodingFilter`, pattern `/*`); security constraint on `/UserServlet`
  with methods POST and DELETE and role `admin` (lines 27-37); security roles `admin`, `user`.
  `ProfileServlet` is only found through its `@WebServlet` annotation (source `annotation`,
  pattern `/profile`).
- sales web.xml: servlets `OrderServlet` (`/OrderServlet`), `ReportServlet` (`/ReportServlet`),
  `LookupServlet` (`/LookupServlet`); constraint on `/OrderServlet` POST role `sales`; roles
  `sales`, `manager`. `BaseServlet` is not registered.
- `accounts/src/main/resources/META-INF/ejb-jar.xml` is classified `ejb_xml` and declares
  `UserManagerBean`/`UserManagerLocal`.

### Java index

- `User` (accounts): `@Entity`; `@Id @GeneratedValue Long id` (lines 30-31),
  `@Version Long version`, `@NotNull @Size(min = 2, max = 80) @Column(nullable = false,
  length = 80) String name`, `@Pattern` email, `@Enumerated(EnumType.STRING) UserStatus status`,
  `@Temporal(DATE) Date birthDate`, `BigDecimal creditLimit`, `String locale`,
  `@OneToMany(mappedBy = "user", cascade = CascadeType.ALL) List<Phone> phones`.
- `Phone` (accounts): `@ManyToOne User user`, `@Pattern number`.
- `UserStatus`: enum `ACTIVE, SUSPENDED, CLOSED`.
- `UserInUseException`: checked (`extends Exception`), `@ApplicationException(rollback = true)`.
- `UserManagerLocal` `@Local`; `UserManagerBean` `@Stateless @RolesAllowed({"admin", "user"})`,
  `create` and `delete` carry `@RolesAllowed("admin")` (lines 50 and 65); `delete` declares
  `throws UserInUseException`.
- Sales: `Customer`, `Order` (`@Table(name = "SALES_ORDER")`, `@Version Long version`,
  `@ManyToOne Customer customer`, `@Enumerated OrderStatus status`, `BigDecimal total`,
  `Date orderDate`, `@OneToMany List<OrderLine> lines`), `OrderLine` (`@ManyToOne Order order`,
  `@Min(1) int quantity`, `BigDecimal unitPrice`), enum `OrderStatus`
  (`DRAFT, SUBMITTED, SHIPPED, CANCELLED`), DTOs `OrderSummaryDto` and `OrderDraft` (session
  state, `Serializable`), `OrderManagerLocal` `@Local`, `OrderManagerBean` `@Stateless`.
- Constants for `OrderServlet`: `OP_LIST`..`OP_EXPORT` (lines 24-30); for `ProfileServlet`:
  `OP_VIEW`, `OP_UPDATE` (lines 19-20).

### JSP index

All form targets, hidden `op` inputs and links are listed with the request that they trigger.
JSP comments `<%-- ... --%>` (single and multi-line) and scriptlet bodies must be masked while
keeping line numbers.

- `accounts/src/main/webapp/userList.jsp`: directive include (line 2) of
  `WEB-INF/jspf/header.jspf`; single-line comment at 3 containing a fake link
  `UserServlet?op=ghost` (must NOT be indexed); declaration `<%! ... %>` 4-8; `jsp:include` (15);
  multi-line comment 17-22 containing a fake form with `op=purge` (must NOT be indexed); GET
  form to `UserServlet` with hidden `op=list` (24) and inputs `name` (maxlength 80), `size`
  (select 10/20/50), `page`; `c:forEach items="${users}" var="u"` (39); links
  `UserServlet?op=read&id=${u.id}` (41), `UserServlet?op=edit&id=${u.id}` (44),
  `UserServlet?op=new` (50); scriptlets at 40 (expression), 51-53; jQuery `$.ajax({url:
  'UserServlet?op=delete', type: 'POST', data: {id: id}})` (58-62) -> POST op=delete param id.
- `userEdit.jsp`: POST form to `UserServlet` with hidden `op=save` (10), hidden `id`, hidden
  `version`, `name` (required, maxlength 80), `email` (required, pattern `[^@]+@[^@]+`), `status`
  select (options generated by `c:forEach`), `birthDate` (type date).
- `userView.jsp`: link `UserServlet?op=edit&id=${user.id}` (16), link `UserServlet?op=list`
  (17), POST form to `profile` with hidden `op=update` (20) and `displayName`, `email`, `locale`
  select (en/fr). EL `${user.name}` etc.
- `WEB-INF/jspf/header.jspf`: taglibs, `fmt:message` keys `menu.users`, `menu.profile`, links
  `UserServlet?op=list` and `profile?op=view`.
- `accounts/src/main/webapp/js/users.js`: `XMLHttpRequest` GET `UserServlet?op=list&page=` (4);
  `$.post('UserServlet', {op: 'delete', id: id})` (10); `$.get('UserServlet?op=read&id=' + id)`
  (16).
- `sales/src/main/webapp/orderList.jsp`: GET form to `OrderServlet` hidden `op=list` (10) with
  `status` select, `customerId`, `page`, `size`; links `OrderServlet?op=view&id=${o.id}` (26),
  `OrderServlet?op=detail&orderNumber=${o.orderNumber}` (30), `OrderServlet?op=step1` (34),
  `OrderServlet?op=export&status=SUBMITTED` (35).
- `wizardStep1.jsp`: POST form hidden `op=step1` (8), `customerId` (required), `notes`
  (maxlength 500); `fetch('LookupServlet?op=countries')` (19).
- `wizardStep2.jsp`: scriptlet 5-8; POST form hidden `op=step2` (13) with `sku`
  (required, pattern, maxlength 20), `quantity`, `unitPrice`; second POST form hidden
  `op=confirm` (26).
- `report.jsp`: GET form to `ReportServlet` hidden `op=summary` (8) with `from`, `to`;
  `$.getJSON('ReportServlet?op=daily&day=2024-01-31')` (19).
- `sales/src/main/webapp/js/orders.js`: `$.get('LookupServlet?op=statuses')` (3);
  `XMLHttpRequest` GET `OrderServlet?op=export&status=` (7-8).

### Properties

- `accounts/src/main/resources/messages.properties` and `messages_fr.properties` (same keys):
  labels `user.name`, `user.email`, `user.status`, ...; error keys `error.user.inuse`
  (English text `User is still referenced`), `error.user.name.required`, `error.user.email.invalid`,
  `error.user.modified`. The French file is locale `fr`.
- `sales/src/main/resources/messages.properties`: `order.new`, `order.export`, `order.customer`,
  `order.notes`, `action.*`, `error.order.empty`.

## Roles (all citable)

| Role | Evidence |
| --- | --- |
| `admin` | accounts web.xml line 35 (auth-constraint), `UserManagerBean.java` lines 13, 50, 65 |
| `user` | accounts web.xml line 43, `UserManagerBean.java` line 13 |
| `sales` | sales web.xml line 39, `OrderManagerBean.java` lines 20, 53 |
| `manager` | sales web.xml line 47, `OrderManagerBean.java` line 20 |
