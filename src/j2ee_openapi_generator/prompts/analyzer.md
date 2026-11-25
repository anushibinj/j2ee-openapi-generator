# Task

You convert ONE legacy J2EE entry point (a servlet operation, selected by a request parameter such as `op`) into a proposal for REST API operations. You only interpret the source excerpts in the packet below. All other work (indexing, merging, emission) is done by deterministic code, which will check every claim you make against the packet.

# Output contract

Reply with ONE JSON object and nothing else: no prose, no code fences. It must conform to the `EndpointProposal` JSON Schema at the end of this prompt. Use `null` for absent optional values.

# Rules

1. Evidence is mandatory. Every path param, query param, request body field, non standard response status (anything other than the usual 200/201/204 success), and role needs at least one evidence item with the exact `file` path as shown in the `=== FILE path (lines a-b) ===` header, the `line` number from the numbered lines (`NNNN| code`), and a `snippet` copied verbatim from that line. A verifier rejects citations whose snippet is not on that line.
2. Never invent fields, parameters, statuses, roles or types that are not visible in the packet. If something is unclear, add it to `open_questions` instead of guessing.
3. The packet contains mapping hints (suggested method, path, resource). You may deviate from a hint only if you set `hint_override_reason` on the operation and explain why.
4. The legacy request parameter `id` (or `<entity>Id`) that selects one record becomes the path parameter `{resourceId}`, for example `{userId}`. Keep the legacy name in `legacy_name`.
5. Paths are relative to the API prefix, use kebab-case plural nouns, and carry no verbs. Use `GET` for reads, `POST` for creation and actions, `PUT` for full replacement, `PATCH` for partial change, `DELETE` for removal.
6. An operation that only forwards to a page and reads or writes no data gets an empty `operations` list and a reason in `no_api_needed`.
7. Data that fills dropdowns or option lists becomes a lookup: list it in `lookups_needed` with a kebab-case `type`; it is served by `GET /lookups/{type}`.
8. A save that creates or updates depending on whether an id is present becomes two operations: `POST` on the collection and `PUT` on the item.
9. A multi step session wizard becomes a draft resource: `POST /{resource}/drafts`, `PATCH /{resource}/drafts/{draftId}`, `POST /{resource}/drafts/{draftId}/submit`.
10. File downloads (CSV, PDF, Excel) become a `GET` with `content_type` set and `schema_ref` of `binary`.
11. Set `paginated` to true only when the code reads paging parameters and returns a page.
12. Follow the style guide below for naming and shape.

# Style guide

{{style_guide}}

# Examples

Each example shows the packet situation in its label and a valid JSON reply. Line numbers and file names are illustrative.

<!-- example: list-with-paging -->
```json
{"packet_id":"shop:ItemServlet#list","operations":[{"http_method":"GET","path":"/items","summary":"List items","resource":"items","tag":"Shop Items","query_params":[{"name":"page","legacy_name":"page","type":"integer","required":false,"evidence":[{"claim":"page parameter","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":41,"snippet":"int page = parseInt(request.getParameter(\"page\"), 0);"}],"default":0},{"name":"size","legacy_name":"size","type":"integer","required":false,"evidence":[{"claim":"size parameter","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":42,"snippet":"int size = parseInt(request.getParameter(\"size\"), 20);"}],"default":20},{"name":"q","legacy_name":"q","type":"string","required":false,"evidence":[{"claim":"search text","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":43,"snippet":"String q = request.getParameter(\"q\");"}]}],"responses":[{"status":200,"description":"Page of items","schema_ref":"ItemSummaryDto","is_array":true,"paginated":true}],"legacy_source":{"module":"shop","servlet":"ItemServlet","op":"list","http_methods":["GET"],"jsps":["/WEB-INF/jsp/item/list.jsp"]},"evidence":[{"claim":"list branch","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":38,"snippet":"if (\"list\".equals(op)) {"}]}],"confidence":0.9}
```

<!-- example: read -->
```json
{"packet_id":"shop:ItemServlet#read","operations":[{"http_method":"GET","path":"/items/{itemId}","summary":"Get one item","resource":"items","tag":"Shop Items","path_params":[{"name":"itemId","legacy_name":"id","type":"integer","required":true,"evidence":[{"claim":"legacy id parameter","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":55,"snippet":"Long id = Long.valueOf(request.getParameter(\"id\"));"}],"format":"int64"}],"responses":[{"status":200,"description":"The item","schema_ref":"Item"},{"status":404,"description":"No item with that id","evidence":[{"claim":"not found is sent","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":58,"snippet":"response.sendError(404);"}]}],"legacy_source":{"module":"shop","servlet":"ItemServlet","op":"read","http_methods":["GET"],"jsps":[]},"evidence":[{"claim":"read branch","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":52,"snippet":"else if (\"read\".equals(op)) {"}]}],"confidence":0.9}
```

<!-- example: save-create-or-update -->
```json
{"packet_id":"shop:ItemServlet#save","operations":[{"http_method":"POST","path":"/items","summary":"Create an item","resource":"items","tag":"Shop Items","request_body":{"content_type":"application/json","schema_ref":"ItemCreateRequest","required":["name"],"fields":[{"name":"name","type":"string","required":true,"evidence":[{"claim":"name is read","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":70,"snippet":"item.setName(request.getParameter(\"name\"));"}]},{"name":"price","type":"number","required":false,"evidence":[{"claim":"price is read","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":71,"snippet":"item.setPrice(new BigDecimal(request.getParameter(\"price\")));"}]}]},"responses":[{"status":201,"description":"Created item","schema_ref":"Item"}],"legacy_source":{"module":"shop","servlet":"ItemServlet","op":"save","http_methods":["POST"],"jsps":[]},"evidence":[{"claim":"insert when id is empty","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":66,"snippet":"if (id == null || id.isEmpty()) { itemBean.create(item);"}]},{"http_method":"PUT","path":"/items/{itemId}","summary":"Update an item","resource":"items","tag":"Shop Items","idempotent":true,"path_params":[{"name":"itemId","legacy_name":"id","type":"integer","required":true,"evidence":[{"claim":"id selects update","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":66,"snippet":"if (id == null || id.isEmpty()) { itemBean.create(item);"}],"format":"int64"}],"request_body":{"content_type":"application/json","schema_ref":"ItemUpdateRequest","required":["name"],"fields":[{"name":"name","type":"string","required":true,"evidence":[{"claim":"name is read","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":70,"snippet":"item.setName(request.getParameter(\"name\"));"}]}]},"responses":[{"status":200,"description":"Updated item","schema_ref":"Item"}],"legacy_source":{"module":"shop","servlet":"ItemServlet","op":"save","http_methods":["POST"],"jsps":[]},"evidence":[{"claim":"update when id is present","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":74,"snippet":"} else { itemBean.update(Long.valueOf(id), item);"}]}],"confidence":0.85}
```

<!-- example: delete-in-use-conflict -->
```json
{"packet_id":"shop:ItemServlet#delete","operations":[{"http_method":"DELETE","path":"/items/{itemId}","summary":"Delete an item","resource":"items","tag":"Shop Items","idempotent":true,"path_params":[{"name":"itemId","legacy_name":"id","type":"integer","required":true,"evidence":[{"claim":"legacy id parameter","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":90,"snippet":"Long id = Long.valueOf(request.getParameter(\"id\"));"}],"format":"int64"}],"responses":[{"status":204,"description":"Deleted"},{"status":409,"description":"Item is in use by an order","evidence":[{"claim":"in use is reported as conflict","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":94,"snippet":"response.sendError(409, \"item in use\");"}]}],"roles":[{"name":"ADMIN","evidence":[{"claim":"role check","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":88,"snippet":"if (!request.isUserInRole(\"ADMIN\")) {"}]}],"legacy_source":{"module":"shop","servlet":"ItemServlet","op":"delete","http_methods":["POST"],"jsps":[]},"evidence":[{"claim":"delete branch","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":86,"snippet":"else if (\"delete\".equals(op)) {"}]}],"confidence":0.85}
```

<!-- example: wizard-to-draft-resource -->
```json
{"packet_id":"shop:ItemServlet#step1","operations":[{"http_method":"POST","path":"/items/drafts","summary":"Start an item draft (wizard step 1)","resource":"items","tag":"Shop Items","request_body":{"schema_ref":"ItemDraftCreateRequest","required":["name"],"fields":[{"name":"name","type":"string","required":true,"evidence":[{"claim":"step 1 stores name in session","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":110,"snippet":"draft.setName(request.getParameter(\"name\"));"}]}]},"responses":[{"status":201,"description":"Draft created","schema_ref":"ItemDraft"}],"legacy_source":{"module":"shop","servlet":"ItemServlet","op":"step1","http_methods":["POST"],"jsps":["/WEB-INF/jsp/item/step1.jsp"]},"evidence":[{"claim":"draft kept in session","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":112,"snippet":"session.setAttribute(\"itemDraft\", draft);"}]},{"http_method":"PATCH","path":"/items/drafts/{draftId}","summary":"Update an item draft (wizard step 2)","resource":"items","tag":"Shop Items","path_params":[{"name":"draftId","legacy_name":"draft","type":"string","required":true,"evidence":[{"claim":"draft read back from session","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":120,"snippet":"ItemDraft d = (ItemDraft) session.getAttribute(\"itemDraft\");"}]}],"request_body":{"schema_ref":"ItemDraftUpdateRequest","fields":[{"name":"price","type":"number","evidence":[{"claim":"step 2 sets price","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":122,"snippet":"d.setPrice(new BigDecimal(request.getParameter(\"price\")));"}]}]},"responses":[{"status":200,"description":"Draft updated","schema_ref":"ItemDraft"}],"legacy_source":{"module":"shop","servlet":"ItemServlet","op":"step2","http_methods":["POST"],"jsps":["/WEB-INF/jsp/item/step2.jsp"]},"evidence":[{"claim":"step 2 branch","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":118,"snippet":"else if (\"step2\".equals(op)) {"}]},{"http_method":"POST","path":"/items/drafts/{draftId}/submit","summary":"Submit an item draft (wizard confirm)","resource":"items","tag":"Shop Items","path_params":[{"name":"draftId","legacy_name":"draft","type":"string","required":true,"evidence":[{"claim":"draft read back from session","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":130,"snippet":"ItemDraft d = (ItemDraft) session.getAttribute(\"itemDraft\");"}]}],"responses":[{"status":201,"description":"Item created from draft","schema_ref":"Item"}],"legacy_source":{"module":"shop","servlet":"ItemServlet","op":"confirm","http_methods":["POST"],"jsps":[]},"evidence":[{"claim":"confirm persists the draft","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":133,"snippet":"itemBean.create(d.toItem());"}]}],"confidence":0.8}
```

<!-- example: export-binary-get -->
```json
{"packet_id":"shop:ItemServlet#export","operations":[{"http_method":"GET","path":"/items/export","summary":"Export items as CSV","resource":"items","tag":"Shop Items","query_params":[{"name":"status","legacy_name":"status","type":"string","required":false,"evidence":[{"claim":"optional status filter","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":150,"snippet":"String status = request.getParameter(\"status\");"}]}],"responses":[{"status":200,"description":"CSV file","content_type":"text/csv","schema_ref":"binary","evidence":[{"claim":"content type is csv","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":152,"snippet":"response.setContentType(\"text/csv\");"}]}],"legacy_source":{"module":"shop","servlet":"ItemServlet","op":"export","http_methods":["GET"],"jsps":[]},"evidence":[{"claim":"export branch","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":147,"snippet":"else if (\"export\".equals(op)) {"}]}],"confidence":0.9}
```

<!-- example: dropdown-lookup -->
```json
{"packet_id":"shop:ItemServlet#new","operations":[],"no_api_needed":null,"lookups_needed":[{"type":"categories","reason":"The form fills a category dropdown from the database","evidence":[{"claim":"categories loaded for the form","file":"shop/src/main/java/com/acme/shop/web/ItemServlet.java","line":30,"snippet":"request.setAttribute(\"categories\", categoryBean.findAll());"}]}],"open_questions":[],"confidence":0.8}
```

<!-- example: page-only -->
```json
{"packet_id":"shop:ItemServlet#help","operations":[],"no_api_needed":"Only forwards to a static help page, no data is read or written","lookups_needed":[],"open_questions":[],"confidence":0.95}
```

# Feedback from a previous attempt

{{feedback}}

# Packet

{{packet}}

# JSON Schema of the reply

{{schema}}
