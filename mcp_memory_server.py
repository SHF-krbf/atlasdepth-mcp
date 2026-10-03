# -*- coding: utf-8 -*-
"""深图 · Agent 记忆层（MCP over stdio，**只读**，v0）

**形态与边界**（严格照 `文档/最新标准/Agent记忆层_设计与精度策略_2026-09-23.md` §四，
作者已在 §15.12 第 3 条签字）：

1. **stdio：不开端口、不常驻服务**——agent 启动本进程，用 stdin/stdout 说 JSON-RPC。
   合规说明里那句「本机不存在任何能被别的程序 fetch 到记忆的端口」**继续成立**
   （这恰恰是我们相对云端记忆产品的卖点：**做成本地 HTTP 端口，整个合规故事就废了**）。
2. **按次（按会话）授权，默认拒绝**：用户显式授权一段时间（`--grant 30`）才读得到；
   授权/撤销/查审计都是一条命令；**每次调用都写审计**（哪个 agent、要了什么范围、给了几条）。
3. **最小集**：默认只给「**当前项目 + 最近 N 分钟**」，条数有上限；
   **私人库（`visibility=private`）永不出**——授权文件里**另写** `include_private: true` 才考虑，
   且那是一个需要用户**明确动手**的动作。
4. **锚点化摘要，不倾倒原文**：默认给 `{id, 时间, 应用, 窗口标题, 文本(截断), 原图路径, 可信度档}`；
   **OCR 全文只在 agent 明确索要时**（`memory_evidence`）才给——顺带解决 agent 的 token 成本。

用法（都在你本机、都要你自己动手）：

    python mcp_memory_server.py --instance "D:\\深图自用"            # 当 MCP server 跑（由 agent 启动）
    python mcp_memory_server.py --instance "D:\\深图自用" --grant 30 # 授权 30 分钟
    python mcp_memory_server.py --instance "D:\\深图自用" --revoke   # 立刻断掉
    python mcp_memory_server.py --instance "D:\\深图自用" --status   # 看现在授没授权
    python mcp_memory_server.py --instance "D:\\深图自用" --audit 20 # 看最近 20 条审计

**stdout 只承载 JSON-RPC**（任何日志/提示都走 stderr 与审计文件）——否则 agent 会解析失败。
"""
import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys
import time

SERVER_NAME = "shentu-memory"
SERVER_VERSION = "0.1"
PROTOCOL_VERSION = "2024-11-05"
# 2026-10-02：**默认实例目录不写死任何人的机器**（本模块是要单独发给别人的：
# 把作者本机的 `<你的深图目录>` 烧进默认值，别人一跑就指向一个不存在的目录）。
# 取值顺序：--instance > 环境变量 DSH_INSTANCE > `%USERPROFILE%\深图`；找不到时**明确报错**。
DEFAULT_INSTANCE = os.path.join(os.path.expanduser("~"), "深图")
CONSENT_FILE = "mcp_consent.json"
AUDIT_FILE = "mcp_audit.log"
# 单次最多给几条：**最小集**的一部分（不给 agent"把库拉走"的能力）
MAX_ITEMS = 20
TEXT_LIMIT = 160      # 锚点化摘要里的文本截断长度（全文要单独索要）

# 时间窗比较**必须归一化**（2026-10-03 真机实测抓到的"假绿"，比事故本身更该记）：
#   · 真库里的 `captured_at` 由 `database.insert_record` 写成 `datetime.isoformat()` ⇒
#     "2026-10-03T18:47:12.470178"（**T 分隔 + 微秒**）；
#   · 本文件原先拿 `strftime("%Y-%m-%d %H:%M:%S")`（**空格分隔**）去比。字符串比较在第 11 个字符就分胜负：
#     'T'(0x54) > ' '(0x20) ⇒ **任何 T 格式的行都"大于"任何空格格式的界** ⇒ 时间窗**从未真正生效**：
#     `minutes=1` 与 `minutes=43200` 返回的是同一批"全库最新的 N 条"。而自检夹具恰好用空格格式写库，
#     于是"窗口外的不许出现"这条断言**一直在测一个假前提**（夹具与真实存储格式不一致 ⇒ 假绿）。
# 修法：**两边都归一化**（`substr(...,1,19)` + T→空格）再比——T 格式、空格格式都对，且不依赖 SQLite 的日期函数。
# 同一条修法也必须用在 `screenshot_service.backfill_missing_ocr` 的"最近 7 天"上（同一个坑，同一天踩到两次）。
_TS_GE = "REPLACE(SUBSTR(captured_at, 1, 19), 'T', ' ') >= ?"


# ---------------- 实例与配置 ----------------
def instance_dir(argv_instance=""):
    d = (argv_instance or os.environ.get("DSH_INSTANCE") or DEFAULT_INSTANCE).strip().strip('"')
    return os.path.abspath(d)


def load_cfg(inst):
    try:
        with open(os.path.join(inst, "settings.json"), encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def _db_path(inst, cfg):
    p = str(cfg.get("DB_PATH") or "screen_memory.db")
    return p if os.path.isabs(p) else os.path.join(inst, p)


# ---------------- 按次（按会话）授权 ----------------
def consent_state(inst):
    """当前授权状态：`{"ok":bool, "until":float, "minutes_left":int, "include_private":bool}`。

    默认**拒绝**——没有授权文件、过期、文件坏了，一律按"没授权"处理。
    """
    p = os.path.join(inst, CONSENT_FILE)
    out = {"ok": False, "until": 0.0, "minutes_left": 0, "include_private": False, "path": p}
    try:
        with open(p, encoding="utf-8-sig") as f:
            d = json.load(f)
        until = float(d.get("until") or 0)
        out["include_private"] = bool(d.get("include_private", False))
        out["until"] = until
        out["ok"] = until > time.time()
        out["minutes_left"] = int(max(0, (until - time.time()) // 60))
    except Exception:
        pass
    return out


def grant(inst, minutes, include_private=False):
    minutes = max(1, int(minutes))
    until = time.time() + minutes * 60
    p = os.path.join(inst, CONSENT_FILE)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"until": until, "minutes": minutes,
                   "granted_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "include_private": bool(include_private),
                   "scope": "current_project + recent"},
                  f, ensure_ascii=False, indent=2)
    return p, until


def revoke(inst):
    p = os.path.join(inst, CONSENT_FILE)
    try:
        os.remove(p)
        return True
    except OSError:
        return False


def audit(inst, agent, tool, scope, n):
    """每次调用写一行（审计：哪个 agent、要了什么、给了几条）。失败静默，不影响服务。"""
    try:
        with open(os.path.join(inst, AUDIT_FILE), "a", encoding="utf-8") as f:
            f.write("%s | agent=%s | tool=%s | scope=%s | returned=%s\n"
                    % (_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       (agent or "?")[:40], tool, scope, n))
    except Exception:
        pass


# ---------------- 记忆读取（只读） ----------------
def _conf_bucket(v):
    """可信度**人话档**。

    2026-10-03 修（作者真实数据实测抓到）：`0.0` 原先落进 `else` ⇒ 被标成「**低**」。
    而全项目的口径是「**0 ＝ 未知/还没识别，不假装有**」（见 `ocr_service` 与交付物那一侧）——
    把"还不知道"说成"低可信度"是**诬告**：agent 会据此告诉用户"这几条不太可靠"，
    而真相是"这几张还没识别完"。
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "未知"
    if x <= 0:
        return "未知"          # 0／缺失 ⇒ 还没识别出来，不是"低"
    return "高" if x >= 0.85 else ("中" if x >= 0.6 else "低")


def _anchor(row):
    text = (row["ocr_text"] or "").strip()
    more = len(text) > TEXT_LIMIT
    out = {"id": int(row["id"]),
           "time": str(row["captured_at"] or ""),
           "app": str(row["app_name"] or ""),
           "title": str(row["window_title"] or "")[:80],
           "text": text[:TEXT_LIMIT] + ("…（要全文就用 memory_evidence）" if more else ""),
           "file": str(row["file_path"] or ""),
           "conf": _conf_bucket(row["ocr_conf"]),
           "truncated": more,
           # 2026-10-03 加（真实数据实测）：**"有图无文字"的占位帧必须自报家门**。
           # 现象：作者关掉深图后 8 秒就让 agent 去查，拿到 20 条 text 全空的锚点 ⇒ 回答没用。
           # 口径：`text` 是**内容**，不许拿它写说明；空就空，另用 `text_ready`/`note` 说清。
           "text_ready": bool(text)}
    if not text:
        out["note"] = ("这一帧刚截下来，文字还没识别出来（深图需要开着跑一会儿才会回填）。"
                       "别据此回答「没有内容」——要么稍后再查，要么告诉用户先让深图继续运行。")
    return out


def _query(inst, cfg, sql, args, limit, include_private):
    """只读查库；**私人库过滤在这里统一做**（不靠调用方自觉）。"""
    dbp = _db_path(inst, cfg)
    if not os.path.isfile(dbp):
        return [], "找不到记忆库：%s" % dbp
    try:
        con = sqlite3.connect("file:%s?mode=ro" % dbp.replace("\\", "/"), uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(sql, args).fetchall()
        finally:
            con.close()
    except Exception as e:
        return [], "读库失败：%s" % e
    out = []
    for r in rows:
        if (not include_private) and str(r["visibility"] or "") == "private":
            continue
        out.append(_anchor(r))
        if len(out) >= max(1, min(int(limit or MAX_ITEMS), MAX_ITEMS)):
            break
    return out, ""


def _project(cfg):
    return str(cfg.get("CURRENT_PROJECT", "") or "").strip()


# ---------------- 四个工具 ----------------
def tool_scope(inst, cfg, _args):
    st = consent_state(inst)
    return {"authorized": bool(st["ok"]),
            "minutes_left": int(st["minutes_left"]),
            # MD-OK: 给 agent 看的工具输出（agent 会渲染 markdown；不是直接给用户看的界面文案）
            "scope": "当前项目 + 最近 N 分钟；条数上限 %d；私人库%s"
                     % (MAX_ITEMS, "**可出**（你在授权时显式打开了）" if st["include_private"] else "**永不出**"),
            "project": _project(cfg) if st["ok"] else "",
            "how_to_authorize": "请在深图这台电脑上执行：python mcp_memory_server.py --instance \"%s\" --grant 30" % inst}


def tool_recent(inst, cfg, args):
    minutes = max(1, min(int(args.get("minutes") or 30), 60 * 24 * 30))   # 加固：回看窗口封顶 30 天
    limit = int(args.get("limit") or MAX_ITEMS)
    st = consent_state(inst)
    since = (_dt.datetime.now() - _dt.timedelta(minutes=max(1, minutes))).strftime("%Y-%m-%d %H:%M:%S")
    proj = _project(cfg)
    sql = ("SELECT id, captured_at, app_name, window_title, ocr_text, file_path, visibility, ocr_conf "
           "FROM screenshots WHERE " + _TS_GE)
    a = [since]
    if proj:
        sql += " AND (project = ? OR project IS NULL OR project = '')"
        a.append(proj)
    # 2026-10-03 加（真实数据实测）：**优先给"有文字"的记录**。
    # 现象：作者刚关掉深图就让 agent 查，30 分钟窗口里 20 条全是"有图无文字"的占位帧 ⇒ 拿回一屏空卡片。
    # 口径：同一时间窗里先把已识别出文字的排前面（空壳只在"确实没有别的"时才被给到）。
    # 只改**排序**，不改过滤——空壳仍然要给：让 agent 知道"刚截了东西，还没识别完"。
    sql += (" ORDER BY (CASE WHEN TRIM(COALESCE(ocr_text, '')) = '' THEN 1 ELSE 0 END),"
            " captured_at DESC LIMIT ?")
    a.append(max(1, min(limit, MAX_ITEMS)) * 3)   # 多取一些，私人库过滤后再截断
    items, err = _query(inst, cfg, sql, a, limit, st["include_private"])
    return {"items": items, "count": len(items), "error": err,
            "window": {"since": since, "project": proj, "minutes": minutes}}


def tool_search(inst, cfg, args):
    q = str(args.get("query") or "").strip()[:200]   # 加固：查询词封顶（别让 agent 拿 10MB 来撞）
    if not q:
        return {"items": [], "count": 0, "error": "query 不能为空"}
    limit = int(args.get("limit") or MAX_ITEMS)
    minutes = max(1, min(int(args.get("minutes") or 1440), 60 * 24 * 30))   # 加固：回看窗口封顶 30 天
    st = consent_state(inst)
    since = (_dt.datetime.now() - _dt.timedelta(minutes=max(1, minutes))).strftime("%Y-%m-%d %H:%M:%S")
    proj = _project(cfg)
    sql = ("SELECT id, captured_at, app_name, window_title, ocr_text, file_path, visibility, ocr_conf "
           "FROM screenshots WHERE " + _TS_GE + " AND ocr_text LIKE ?")
    a = [since, "%" + q + "%"]
    if proj:
        sql += " AND (project = ? OR project IS NULL OR project = '')"
        a.append(proj)
    sql += " ORDER BY captured_at DESC LIMIT ?"
    a.append(max(1, min(limit, MAX_ITEMS)) * 3)
    items, err = _query(inst, cfg, sql, a, limit, st["include_private"])
    return {"items": items, "count": len(items), "error": err,
            "window": {"since": since, "project": proj, "query": q}}


def tool_evidence(inst, cfg, args):
    aid = args.get("anchor_id")
    st = consent_state(inst)
    try:
        aid = int(aid)
    except (TypeError, ValueError):
        return {"error": "anchor_id 必须是数字"}
    items, err = _query(inst, cfg,
                        "SELECT id, captured_at, app_name, window_title, ocr_text, file_path,"
                        " visibility, ocr_conf FROM screenshots WHERE id = ?", [aid], 1,
                        st["include_private"])
    if err:
        return {"error": err}
    if not items:
        return {"error": "没有这条（可能已过期清理，或它是私人库记录）"}
    dbp = _db_path(inst, cfg)
    full = ""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % dbp.replace("\\", "/"), uri=True, timeout=5)
        try:
            r = con.execute("SELECT ocr_text FROM screenshots WHERE id = ?", [aid]).fetchone()
            full = (r[0] or "") if r else ""
        finally:
            con.close()
    except Exception:
        pass
    it = dict(items[0])
    it["text"] = full                      # 只有这一步给全文（"明确索要"才算）
    it["truncated"] = False
    return {"item": it}


# 2026-10-02 实测（作者在 Trae CN 里的第一次真机验证）：工具**挂上了、授权也给了，但 agent 没调用**——
# 它把"我最近看过的报价单在哪里？"当成了"在代码仓库里找『报价单』这个词"，转而调它自己的
# Grep/Read/Glob 去翻测试夹具和文档，最后给了一个没用的答案。
# **根因不在连接，在"抢活"**：IDE 里的 agent 手里已经有一整套代码搜索工具，而我们的描述没告诉它
# "这类问题该归我"。⇒ 描述改写三件事（每条都针对那次实测）：
#   ① **先划清地盘**：这是"用户**自己屏幕**上看过的东西"，与当前代码仓库无关；
#   ② **指明触发场景**：用户问"我刚才/最近/昨天看过的某个东西在哪"时，**优先用这几个工具**；
#   ③ **明说别拿代码搜索代替**（对 IDE 类客户端尤其重要）。
# 口径提醒：描述是**给 agent 读的**，不是给用户看的界面文案 —— 但仍要短、要能被执行，不许写成说明书。
# MD-OK: 工具描述是给 agent 读的（markdown 是 MCP 工具描述的通用写法）
_SCOPE_NOTE = ("这是「深图」在本机记下的**用户屏幕历史**（他自己在电脑上看过、复制过的内容），"
               "**与当前打开的代码仓库无关**。")
# MD-OK: 同上（同样的描述片段，拼接进给 agent 的工具描述）
_USE_WHEN = ("**当用户在问「我刚才 / 最近 / 昨天看过的某个东西（某个词、某张单子、某段话）在哪里」时，"
             "优先调用本工具**，不要用代码搜索（grep/glob/读文件）去代替——"
             "代码仓库里出现的同名文字不是他看过的东西。")
# 2026-10-03 加（真实数据实测）："有图无文字"要说清楚，别让它变成"什么都没有"。
# MD-OK: 同上——这一段也是拼进**给 agent 的工具描述**里的（它不出现在用户界面上）
_TEXT_READY_NOTE = ("返回里若某条 `text_ready` 为 false，说明那一帧**刚截下来、文字还没识别完**"
                    "（深图需要开着跑一会儿才会回填）；此时**不要**回答「没有内容/没有记录」，"
                    "应当说明「这几帧还在识别中」，或稍后重试，或建议用户先让深图继续运行。")

TOOLS = [
    # MD-OK: 工具描述是给 **agent** 读的（markdown 是 MCP 工具描述的通用写法）
    {"name": "memory_scope",
     "description": "看深图记忆层当前是否被授权、授权还剩多少分钟、范围是什么。**不需要授权**也能问。"
                    + _SCOPE_NOTE,
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "memory_recent",
     "description": "取「当前项目 + 最近 N 分钟」的屏幕记忆（默认 30 分钟、最多 20 条）。"
                    "每条含时间/应用/窗口标题/文本片段/原图路径/可信度档；要全文再用 memory_evidence。"
                    + _USE_WHEN + _TEXT_READY_NOTE + _SCOPE_NOTE,
     "inputSchema": {"type": "object", "properties": {
         "minutes": {"type": "integer", "description": "往回看多少分钟，默认 30"},
         "limit": {"type": "integer", "description": "最多几条，默认 20（上限 20）"}}}},
    {"name": "memory_search",
     "description": "在用户的屏幕记忆里按关键词检索（默认回看 24 小时、最多 20 条），返回同样的锚点化摘要。"
                    "用户问「某个词/某张单子/某段话我在哪看到的」时用这个；"
                    "**这不是搜代码，也不是搜本地文件**——搜的是他屏幕上出现过的文字。" + _USE_WHEN,
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "关键词（中文/英文都行，按原文匹配）"},
         "minutes": {"type": "integer"},
         "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "memory_evidence",
     # MD-OK: 工具描述是给 agent 读的（同上）
     "description": "取某条记忆锚点的**全文**与原图路径（用 memory_recent/memory_search 返回的 id）。"
                    "用户要「看看原文/看看当时那一屏」时用这个。" + _SCOPE_NOTE,
     "inputSchema": {"type": "object", "properties": {"anchor_id": {"type": "integer"}},
                     "required": ["anchor_id"]}},
]
_DISPATCH = {"memory_scope": tool_scope, "memory_recent": tool_recent,
             "memory_search": tool_search, "memory_evidence": tool_evidence}
_NO_CONSENT = ("memory_scope",)      # 唯一不需要授权的工具（它本身不含任何记忆内容）


# ---------------- JSON-RPC / MCP ----------------
def _reply(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _err(mid, code, msg):
    _reply({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}})


def _tool_result(mid, payload, is_error=False):
    _reply({"jsonrpc": "2.0", "id": mid,
            "result": {"content": [{"type": "text",
                                    "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                       "isError": bool(is_error)}})


def handle(msg, inst, cfg, agent):
    mid = msg.get("id")
    method = msg.get("method") or ""
    if method == "initialize":
        _reply({"jsonrpc": "2.0", "id": mid,
                "result": {"protocolVersion": PROTOCOL_VERSION,
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}})
        return
    if method in ("notifications/initialized", "initialized"):
        return                       # 通知：不回
    if method == "ping":
        _reply({"jsonrpc": "2.0", "id": mid, "result": {}})
        return
    if method == "tools/list":
        _reply({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        return
    if method == "tools/call":
        # 2026-10-02 加固：`params` 不是对象（客户端传了数组/字符串）时**给干净的错误**，
        # 而不是让 AttributeError 冒到外层变成"内部错误"。
        p = msg.get("params")
        if not isinstance(p, dict):
            _err(mid, -32602, "params 必须是对象")
            return
        name = str(p.get("name") or "")
        args = p.get("arguments")
        if not isinstance(args, dict):
            args = {}
        fn = _DISPATCH.get(name)
        if fn is None:
            _err(mid, -32602, "未知工具：%s" % name)
            return
        st = consent_state(inst)
        if not st["ok"] and name not in _NO_CONSENT:
            # **按次授权**：没授权就拒绝，并把"怎么授权"原样告诉 agent（它要回去问人）
            payload = {"error": "未授权", "detail": "深图记忆层默认关闭，需要用户在本机显式授权。",
                       "how_to_authorize": "python mcp_memory_server.py --instance \"%s\" --grant 30" % inst}
            audit(inst, agent, name, "denied", 0)
            _tool_result(mid, payload, is_error=True)
            return
        try:
            out = fn(inst, cfg, args)
        except Exception as e:
            audit(inst, agent, name, "error", 0)
            _tool_result(mid, {"error": "%s: %s" % (type(e).__name__, e)}, is_error=True)
            return
        n = int(out.get("count") or (1 if out.get("item") else 0))
        scope = "project=%s" % (_project(cfg) or "-")
        audit(inst, agent, name, scope, n)
        _tool_result(mid, out, is_error=bool(out.get("error") and not out.get("items")
                                            and not out.get("item")))
        return
    if mid is None:
        return                       # 其他通知：忽略
    _err(mid, -32601, "不支持的方法：%s" % method)


def _note_client(msg):
    """从 `initialize` 的 clientInfo 里记下"是哪个 agent"（写审计用）。"""
    try:
        ci = ((msg.get("params") or {}).get("clientInfo") or {})
        if isinstance(ci, dict) and ci.get("name"):
            return str(ci["name"])
    except Exception:
        pass
    return ""


def serve(inst):
    """主循环。**加固要点（2026-10-02）**：畸形输入一律不致命、不静默死掉。

    · 解析不了的一行 ⇒ 记 stderr、继续（客户端可能发半行/心跳噪声）；
    · JSON-RPC **批量**（顶层是数组）⇒ 逐条处理（规范允许，且实现很便宜）；
    · 单条处理抛异常 ⇒ 回 `-32603` 而不是让进程退出（**agent 的会话不能因为我们崩掉**）。
    """
    cfg = load_cfg(inst)
    agent = ""
    # Windows 上 stdin/stdout 默认按代码页解码 ⇒ 必须显式 UTF-8（否则中文 JSON 直接炸）。
    # 2026-10-02：**stderr 也一起统一**（我们往 stderr 写中文诊断；三条流同编码才可预期）。
    for _s in ("stdin", "stdout", "stderr"):
        try:
            getattr(sys, _s).reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            sys.stderr.write("[mcp] 收到无法解析的一行（已忽略，继续服务）\n")
            continue
        items = msg if isinstance(msg, list) else [msg]
        for one in items:
            if not isinstance(one, dict):
                continue
            _c = _note_client(one)
            if _c:
                agent = _c
            try:
                handle(one, inst, cfg, agent)
            except Exception as e:
                sys.stderr.write("[mcp] 处理失败：%r\n" % (e,))
                if one.get("id") is not None:
                    _err(one.get("id"), -32603, "内部错误：%s" % e)
    return 0


def main():
    ap = argparse.ArgumentParser(description="深图 · Agent 记忆层（MCP over stdio，只读）")
    ap.add_argument("--instance", default="", help="深图实例目录（默认 %s）" % DEFAULT_INSTANCE)
    ap.add_argument("--grant", type=int, default=0, metavar="MINUTES", help="授权这么多分钟")
    ap.add_argument("--include-private", action="store_true",
                    # MD-OK: 命令行 --help 文本（给能跑命令的人看，不是产品界面文案）
                    help="连同私人库一起授权（**默认不出**；只有你明确要这么做时才加）")
    ap.add_argument("--revoke", action="store_true", help="立刻撤销授权")
    ap.add_argument("--status", action="store_true", help="看当前授权状态")
    ap.add_argument("--audit", type=int, default=0, metavar="N", help="打印最近 N 条审计")
    a = ap.parse_args()
    inst = instance_dir(a.instance)

    # 2026-10-02：实例目录不存在就**当场说清**（别让 agent 收到一堆"找不到记忆库"的怪答复；
    # 也别让人以为"点了没反应"）。--grant/--revoke/--status 同样要求目录存在。
    if not os.path.isdir(inst):
        sys.stderr.write(
            "[mcp] 找不到实例目录：%s\n"
            "      请用 --instance 指定深图的软件目录（那里有 settings.json 与 screen_memory.db），\n"
            "      或设置环境变量 DSH_INSTANCE。\n" % inst)
        return 2

    if a.grant:
        p, until = grant(inst, a.grant, include_private=a.include_private)
        print("已授权：%s 分钟内可读（到 %s）\n  %s"
              % (a.grant, _dt.datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S"), p))
        if a.include_private:
            print("⚠️ 私人库也被授权外出了——用完请立刻 --revoke")
        return 0
    if a.revoke:
        print("已撤销授权（文件已删）。" if revoke(inst) else "本来就没有授权文件。")
        return 0
    if a.status:
        st = consent_state(inst)
        print("授权：%s｜剩余 %d 分钟｜私人库：%s"
              % ("开" if st["ok"] else "关（默认）", st["minutes_left"],
                 "可出（你显式打开了）" if st["include_private"] else "永不出"))
        return 0
    if a.audit:
        p = os.path.join(inst, AUDIT_FILE)
        if not os.path.isfile(p):
            print("还没有审计记录。")
            return 0
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        print("\n".join(lines[-a.audit:]))
        return 0
    return serve(inst)


if __name__ == "__main__":
    sys.exit(main())
