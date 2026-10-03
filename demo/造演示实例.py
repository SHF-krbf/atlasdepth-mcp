# -*- coding: utf-8 -*-
"""造一份**虚构的演示实例**——给"步 0"用（让真实 agent 读一次记忆，验机制值不值）。

## 为什么要有它

计划里的「步 0」是：**用虚构语料验一次"agent 通过本地记忆回答 + 带出处"**（15 分钟、零社交、零隐私风险）。
但仓库里的东西只提供了两条路：① 拿**你自己的真实例**去试（`D:\\深图自用`）——那违反"虚构语料"这条安全规矩；
② 跑自检脚本——它在 `%TEMP%` 里造完就删，**没有实体可以注册给 agent**。
⇒ 于是补这一个小工具：造一份**留着不删**的假实例（内容全是"示例/样例"，含一条私人库记录用来演边界），
并把「注册片段 + 授权命令 + 问什么 + 什么算成功」直接打印出来。

## 三条纪律

① **只写自己的目标目录**（默认 `%TEMP%\\深图演示实例`），**绝不碰任何真实例**；
② **内容全部虚构**：公司名、金额、发票号都是"示例/样例"，**不含任何真实数据、不含作者机器特征**；
③ **可随时删掉**：整份就是一个文件夹，删了不影响任何东西（工具会打印这句）。

用法：
    venv\\Scripts\\python.exe 组件验证\\Agent记忆层\\造演示实例.py
    venv\\Scripts\\python.exe 组件验证\\Agent记忆层\\造演示实例.py --to D:\\演示
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_TO = os.path.join(os.environ.get("TEMP") or ".", "深图演示实例")
DEMO_PROJECT = "演示项目A"


def _find_server():
    """定位适配器 —— **两种布局都要能找到**（与自检脚本同一套口径）。

    这套文件会同时出现在两个地方：仓库里（`组件验证/Agent记忆层/`）与**公开仓库**里（`demo/`）。
    公开仓库的目录结构不一样（`demo/` ↔ 根），所以照着"上跳三跳"写死会在别人机器上指向不存在的路径——
    这个坑今天刚在自检脚本上踩过一次（见 `selftest_mcp_memory.py` 的同名函数），这里一次做对。
    """
    _here = os.path.dirname(os.path.abspath(__file__))
    for _c in (os.path.join(os.path.dirname(_here), "mcp_memory_server.py"),   # 公开仓库：demo/ → 根
               os.path.join(_here, "mcp_memory_server.py"),                    # 同目录
               os.path.join(BASE, "mcp_memory_server.py")):                    # 仓库：组件验证/Agent记忆层/ → 根
        if os.path.isfile(_c):
            return os.path.abspath(_c)
    return ""

DDL = ("CREATE TABLE IF NOT EXISTS screenshots (id INTEGER PRIMARY KEY AUTOINCREMENT, file_path TEXT,"
       " ocr_text TEXT, captured_at TEXT, session_folder TEXT, is_active_session INTEGER,"
       " is_favorite INTEGER, favorite_category TEXT, visibility TEXT, app_name TEXT,"
       " window_title TEXT, project TEXT, source TEXT, duration REAL, ocr_conf REAL)")

# 演示语料：**全部是"示例/样例"**，刻意不含任何真实公司名、人名、真金额
DEMO_ROWS = [
    # (file, text, minutes_ago, visibility, app, window_title, project, conf)
    ("演示_报价单.jpg", "报价单 甲方：示例客户 乙方：我方 数量 3 台 单价 500 元 合计 1500 元",
     5, "workspace", "winword.exe", "报价单-示例.docx", DEMO_PROJECT, 0.93),
    ("演示_会议纪要.jpg", "会议纪要 议题：演示流程 结论：先验机制再决定要不要公开 参会：示例甲乙丙",
     12, "workspace", "notepad.exe", "会议纪要-示例.txt", DEMO_PROJECT, 0.90),
    ("演示_发票.jpg", "样例发票 号码 00000001 金额 1500.00 开票日期 2026-10-02 抬头：示例客户",
     20, "workspace", "chrome.exe", "电子发票-示例", DEMO_PROJECT, 0.88),
    ("演示_长文本.jpg", "样例长文：" + ("这是一段用来演示「截断」的示例文字。" * 40),
     25, "workspace", "chrome.exe", "示例长文页面", DEMO_PROJECT, 0.80),
    ("演示_别项目.jpg", "这是**另一个项目**的记录（不该出现在演示项目A的检索结果里）",
     8, "workspace", "winword.exe", "示例合同.docx", "演示项目B", 0.85),
    ("演示_私人.jpg", "私人库演示记录：这条**在任何工具里都不该出现**（除非授权时显式加 --include-private）",
     6, "private", "wechat.exe", "示例聊天窗口", DEMO_PROJECT, 0.70),
    ("演示_旧记录.jpg", "这是**一小时前**的记录（超出默认 30 分钟窗口，用来验「窗口真的在生效」）",
     65, "workspace", "chrome.exe", "示例旧页面", DEMO_PROJECT, 0.75),
]


def build(to: str, refresh: bool = False) -> dict:
    """造实例（幂等：同名文件/同一条文本不重复插入）。

    `refresh=True`：**把时间戳重新刷成"刚刚"**（记录内容不动）。
    为什么需要它：演示语料里那几条是"5/12/20/25 分钟前"，而 `memory_recent` 默认只看**最近 30 分钟**
    ⇒ **隔半小时再试，就会看到"什么都没查到"**（看起来像失败，其实是数据过期了）。
    2026-10-02 实测踩到：给作者预检时这份实例已经过期 ⇒ 补 `--refresh`，并在过期时**主动提示**。
    返回 {ok, path, rows, total, refreshed, stale_min, error}
    """
    to = os.path.abspath(to)
    os.makedirs(os.path.join(to, "screenshot"), exist_ok=True)
    with open(os.path.join(to, "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"DB_PATH": "screen_memory.db", "SCREENSHOT_DIR": "screenshot",
                   "CURRENT_PROJECT": DEMO_PROJECT, "ASK_INCLUDE_PRIVATE": False,
                   "APP_VERSION": "演示实例（虚构语料）", "AUTO_MEMORY_ENABLE": False}, f,
                  ensure_ascii=False, indent=2)
    db = os.path.join(to, "screen_memory.db")
    con = sqlite3.connect(db)
    _stale_min = 0.0
    try:
        con.execute(DDL)
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_demo_file ON screenshots(file_path)")
        n = 0
        for fp, text, mins, vis, app, title, proj, conf in DEMO_ROWS:
            _abs = os.path.join(to, "screenshot", fp)
            if not os.path.exists(_abs):
                with open(_abs, "wb") as g:      # 只是一张占位图，够 agent 拿到"原图路径"这个锚点
                    g.write(b"\xff\xd8\xff\xe0demo")
            # 时间戳**必须与真库同格式**（2026-10-03 修）：真库是 `database.insert_record` 写的
            # `.isoformat()`（T 分隔）。演示件以前用空格分隔，于是"窗口真的在生效"这条**只在演示数据上成立**
            # （适配器旧写法拿空格界比 T 格式行时永远为真）——演示与真库不同格式，就是在演一个假的前提。
            cap = (dt.datetime.now() - dt.timedelta(minutes=mins)).isoformat()
            try:
                con.execute("INSERT INTO screenshots (file_path, ocr_text, captured_at, session_folder,"
                            " is_active_session, is_favorite, visibility, app_name, window_title,"
                            " project, source, ocr_conf) VALUES (?,?,?,?,0,0,?,?,?,?, 'auto', ?)",
                            (_abs, text, cap, "演示", vis, app, title, proj, conf))
                n += 1
            except sqlite3.IntegrityError:
                pass    # 已经造过这一条（幂等）
        # 过期检测 / 刷新（口径：只看**最新那条**离现在多久——它就是"窗口还够不够"的判据）
        try:
            _row = con.execute("SELECT MAX(captured_at) FROM screenshots").fetchone()
            _newest = (_row or [None])[0]
            if _newest:
                _t = dt.datetime.fromisoformat(str(_newest)[:19])
                _stale_min = round((dt.datetime.now() - _t).total_seconds() / 60.0, 1)
        except Exception:
            _stale_min = 0.0
        if refresh:
            for fp, text, mins, vis, app, title, proj, conf in DEMO_ROWS:
                cap = (dt.datetime.now() - dt.timedelta(minutes=mins)).isoformat()
                con.execute("UPDATE screenshots SET captured_at = ? WHERE file_path = ?",
                            (cap, os.path.join(to, "screenshot", fp)))
            _stale_min = 0.0
        con.commit()
        total = con.execute("SELECT COUNT(*) FROM screenshots").fetchone()[0]
    finally:
        con.close()
    return {"ok": True, "path": to, "rows": n, "total": total, "refreshed": bool(refresh),
            "stale_min": _stale_min, "error": ""}


def guidance(to: str) -> str:
    # **布局无关**：谁在跑这个脚本就用谁当解释器（仓库里是 venv 的 python，公开仓库里是任意 python3），
    # 适配器路径由 `_find_server()` 现找。同一份文件在两个仓库布局下打印出来的命令都是对的。
    py = sys.executable
    srv = _find_server() or "mcp_memory_server.py"
    return """
======================================================================
步 0：让一个真实 agent 读一次记忆（15 分钟，零社交、零隐私风险）
======================================================================
演示实例：{to}
（**虚构语料**：示例客户/样例发票/00000001，不含任何真实数据；用完整份删掉即可，不影响任何东西）

【1】把它注册到你的 MCP 客户端（二选一，路径已填好，可直接粘）

  · Trae（命令行编辑配置）：
      trae cli config edit
    在 trae_cli.yaml 里加：
      mcp_servers:
        shentu-memory:
          command: "{py}"
          args: ["{server}", "--instance", "{to}"]

  · Claude Desktop / 其它（claude_desktop_config.json，字段名大同小异）：
    {{
      "mcpServers": {{
        "shentu-memory": {{
          "command": "{py}",
          "args": ["{server}", "--instance", "{to}"]
        }}
      }}
    }}

  ⚠️ 注册**不会**让 agent 读到任何东西——还得第 2 步授权（这是故意的：默认拒绝）。

【2】先看状态（应该是"没授权"），再授权 30 分钟：
      "{py}" "{server}" --instance "{to}" --status
      "{py}" "{server}" --instance "{to}" --grant 30

【3】在新会话里问 agent 这三句（前两句考"能不能找到"，第三句考"出处"）：
      · 我最近看过的报价单在哪里？
      · 我这半小时看过什么？（它应该只给演示项目A 的、且不超过 20 条）
      · 把第一条的原文给我看看。（这条走 memory_evidence：全文 + 原图路径）

【4】**什么算成功**（照这个看，别只看它说得像不像）：
      ✅ 拿回的是**锚点**：每条带 id / 时间 / 应用 / 窗口标题 / 原图路径（不是一段没出处的话）；
      ✅ 那两条"不该出现"的**没有出现**：另一个项目的记录、私人库记录；
      ✅ 超长那条的文本是**被截断**的（全文只能靠第二个工具单独要）；
      ✅ 问"我这半小时看过什么"时，**一小时前那条不在里面**（窗口真的在生效）。

【5】用完：
      "{py}" "{server}" --instance "{to}" --revoke
      "{py}" "{server}" --instance "{to}" --audit 20     ← 看谁问过什么（含被拒绝的记录）

【6】这一步的结论会决定下一步：
      · 机制"值"（agent 真能把带出处的东西用起来）⇒ 值得公开（P3 那 2 小时花得值）；
      · 机制"不值"⇒ 先别发，把力气留给试用机那条线（别把一个没人要的能力推出去占用注意力）。
======================================================================
""".format(to=to, py=py, server=srv)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default=DEFAULT_TO, help="演示实例目录（默认 %s）" % DEFAULT_TO)
    ap.add_argument("--refresh", action="store_true",
                    help="把时间戳刷成「刚刚」（隔半小时后再试必加，否则「最近 30 分钟」里是空的）")
    a = ap.parse_args()
    to = os.path.abspath(a.to)
    # 纪律 ①：绝不碰真实例（真实例里会有 settings.json + 真实库；我们只写自己的目标目录）
    _real_hint = os.path.join(os.path.expanduser("~"), "深图")
    if os.path.abspath(to) == os.path.abspath(_real_hint):
        print("[红] 目标目录看起来是**真实例**（%s）——演示实例必须造在别处。" % _real_hint)
        return 1
    r = build(to, refresh=a.refresh)
    print("[OK] 演示实例已就绪：%s（本次新增 %d 条，共 %d 条%s）"
          % (r["path"], r["rows"], r["total"], "，时间已刷成刚刚" if r["refreshed"] else ""))
    # **过期提示**（2026-10-02 实测踩到，见 `build()` 的 docstring）：不提示的话，
    # 作者会在 Trae 里看到"一条都没查到"，而真实原因只是数据过期。
    if not r["refreshed"] and r["stale_min"] > 25:
        print("\n⚠️ 这份演示实例是 **%.0f 分钟前**造的：「最近 30 分钟」里已经没有内容了，"
              "Trae 里会显示「什么都没查到」（看起来像失败）。\n"
              "   → 想让时间回到**刚刚**，把这条命令加一个 `--refresh` 再跑一次：\n"
              "     venv\\Scripts\\python.exe 组件验证\\Agent记忆层\\造演示实例.py --to %s --refresh\n"
              % (r["stale_min"], r["path"]))
    print(guidance(to))
    return 0
    r = build(to)
    print("[OK] 演示实例已就绪：%s（本次新增 %d 条，共 %d 条）" % (r["path"], r["rows"], r["total"]))
    print(guidance(to))
    return 0


if __name__ == "__main__":
    for _s in ("stdout", "stderr"):
        try:
            getattr(sys, _s).reconfigure(errors="replace")
        except Exception:
            pass
    sys.exit(main())
