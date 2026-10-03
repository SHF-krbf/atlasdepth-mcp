# Shentu · Memory Layer for AI agents (MCP over stdio)

**One line**: give the AI agent on *your own machine* a way to ask
*"where is that quotation I saw last week?"* — and get back **an answer with its
provenance**, instead of handing over your whole screen history.

This repository publishes the **memory adapter** only: a single-file,
**standard-library-only** MCP server that reads a local Shentu instance
(`screen_memory.db`) **read-only**.

---

## Why this exists (the problem it solves)

Agents are good at writing things and bad at remembering *what actually happened*.
Shentu records what you saw on screen locally (event-driven, ~1 frame per change),
OCR-indexes it, and keeps the **original image** next to the text. This adapter lets
an agent retrieve **anchored** memories — time, app, window title, a text snippet,
and the path to the source image — so the agent can answer **and show where it came from**.

That last part is the point: **a citation you can click back to**.
A model that is "usually right" is everywhere; a record that can prove itself is not.

---

## The four boundaries (they are why the compliance story still holds)

| # | Boundary | What it means in practice |
|---|---|---|
| 1 | **stdio — no port, no daemon** | The agent *spawns our subprocess* and talks JSON-RPC over stdin/stdout. **Nothing listens on any port**, so no other program can fetch your memory. (This is deliberate: an HTTP endpoint would destroy this property.) |
| 2 | **Deny by default, per-session consent** | Without explicit consent, every tool except `memory_scope` (which returns **no memory content at all**) is refused — and the refusal tells the agent how to ask the human to authorize. Consent is **time-limited** and revocable with one command. |
| 3 | **Minimal scope, private never leaves** | Only the **current project + last N minutes**, at most 20 items per call. Rows with `visibility=private` are **filtered in code**, per row. |
| 4 | **Anchors, not dumps** | Default payload is `{id, time, app, window title, text snippet (160 chars), image path, confidence}`. **Full OCR text only on an explicit second call** (`memory_evidence`) — which also keeps the agent's token bill sane. |

An anchor is exactly this (real field names — write your integration against these):

```json
{
  "id": 1,
  "time": "2026-10-02 11:47:14",
  "app": "winword.exe",
  "title": "报价单-示例.docx",
  "text": "报价单 甲方：示例客户 乙方：我方 数量 3 台 单价 500 元 合计 1500 元",
  "file": "C:\\path\\to\\instance\\screenshot\\演示_报价单.jpg",
  "conf": "高",
  "truncated": false
}
```

* `text` is capped at 160 chars and flagged with `truncated`; ask `memory_evidence` for the full text by `id`.
* `conf` is a **human-readable band** (`高` / `中` / `低` — the app's own wording), not a float.
* `file` is the path to the **original screenshot** on that machine — that is what makes the answer citable.

Every call — **including refused ones** — is appended to a local audit log
(time / which agent / which tool / scope / how many items returned).

---

## Tools

| Tool | Consent needed | Returns |
|---|---|---|
| `memory_scope` | **no** | whether consent is active, minutes left, scope description. No memory content, not even the project name. |
| `memory_recent` | yes | anchored memories from the current project within the last N minutes — args `{minutes, limit}` (default 30, max 20) |
| `memory_search` | yes | keyword search within the current project, same anchor shape — args `{query, limit}` (`query` is required) |
| `memory_evidence` | yes | full OCR text + image path for one anchor — args `{anchor_id}` (the `id` from either tool above); returns `{"item": {…same fields, `text` no longer truncated…}}` |

---

## Quick start

### Try it in 2 minutes (no Shentu install needed)

The adapter reads a local Shentu instance — but you do not need one to see how it behaves.
Build a **fictional** demo instance (it contains "示例客户 / 样例发票 / 00000001" only), and the
script prints the exact registration snippet, the authorize/revoke commands, three questions to ask
your agent, and **what counts as success**:

```bash
python demo/造演示实例.py --to ./demo_instance
```

That instance has a record from **another project**, a **private** row, an **over-long** text and a
row **outside the 30-minute window** — so you can watch all four boundaries hold in one run.
Delete the folder when you are done; it touches nothing else.

### With your own Shentu instance

```bash
# 1) check status (deny by default)
python mcp_memory_server.py --instance "/path/to/your/Shentu/folder" --status

# 2) authorize for 30 minutes — this is the human step
python mcp_memory_server.py --instance "/path/to/your/Shentu/folder" --grant 30

# 3) after you are done, cut it off; and see who asked what
python mcp_memory_server.py --instance "/path/to/your/Shentu/folder" --revoke
python mcp_memory_server.py --instance "/path/to/your/Shentu/folder" --audit 20
```

Register it in any MCP-capable client (stdio server), e.g. Claude Desktop's
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "shentu-memory": {
      "command": "python",
      "args": ["/absolute/path/to/mcp_memory_server.py",
               "--instance", "/path/to/your/Shentu/folder"]
    }
  }
}
```

Registering does **not** grant access — you still have to run `--grant`.
**Recommendation: `--revoke` when you are done.** Do not leave it authorized permanently.

The instance directory is where the Shentu app keeps `settings.json` and
`screen_memory.db` (the app itself is Windows-only for now). You can also set
`DSH_INSTANCE` instead of passing `--instance`.

---

## How to get the full app (this repository is only half of it)

This repository publishes the **agent-facing half**: the adapter, plus a fictional demo instance so
you can watch the boundaries hold without installing anything.

The other half — the Windows desktop app that actually *records your screen locally*, OCR-indexes it
and keeps the original images next to the text — is **not publicly distributed yet**. It is in a
small two-week trial right now, on purpose: the thing worth testing is not "another recorder", it is
**what people actually manage to retrieve**, and that needs a handful of real users rather than a
download button.

If you want to try the whole thing:

1. open an **issue** titled `Try request` (there is a template for it), and
2. say in one or two lines **what you would want it to help you remember** (e.g. "quotations and
   invoices I only ever see once", "meeting notes I can't find again").

That is the honest state of it. It also means "how many people asked" is a number we can actually
observe — which matters more to this project than looking finished.

---

## Verify it yourself

The boundaries above are not marketing copy — they are enforced in code, and the
test suite drives a **real subprocess over JSON-RPC**:

```bash
python tests/selftest_mcp_memory.py
```

It builds a throwaway instance containing a **private row**, a row from **another
project**, and an **over-long text**, then asserts, among others:

* nothing but `memory_scope` works before consent;
* `visibility=private` rows **never** appear in any tool;
* other projects' rows do not leak;
* item count and snippet length are capped; full text needs the second call;
* the audit log records **refused** calls too;
* after `--revoke`, calls are refused again immediately;
* the source contains **no network primitives** (no socket, no HTTP server).

---

## License

**This adapter is open source** — Apache License 2.0 (see `LICENSE`). You may use, modify and
redistribute it, including commercially; keep the license notice and the attribution below
(Apache-2.0 §6 does not grant trademark rights, so please do not present a modified build as the
official one).

Copyright © 2026 <YOUR NAME or GitHub handle>

**The Shentu app itself is not open source.** It is free to use (personal and internal business use;
see the one-page license notice shipped with the app), it is **not** published as source, and it is
handed out through the trial request route above — please do not redistribute the app.

---

## Not done yet (read this before assuming)

* **No packaged build / no download**: the app half is Windows-only and is handed out through the
  trial request route above, not as a public binary. This adapter is a source script — running it
  needs Python 3 on your machine (that is also why the demo instance exists).
* **No GUI for consent**: authorizing/revoking is a command, not a switch in the app.
* **Read-only**: agents cannot write into the memory through this adapter. Writes
  only happen through the Shentu app itself, driven by the human.
