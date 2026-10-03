# -*- coding: utf-8 -*-
"""Agent 记忆层自检（MCP over stdio）：**真起子进程、真说 JSON-RPC**。

**为什么必须有它**（§15.4 的形态是"合规故事还成不成立"的关键，不能只靠人读代码）：
  ① **默认拒绝**：没授权时除 `memory_scope` 之外全部拒绝，并把"怎么授权"告诉 agent；
  ② **私人库永不出**：`visibility=private` 的记录**在任何工具里都不出现**（授权文件没显式打开时）；
  ③ **最小集 + 锚点化**：单次条数有上限、文本截断，**全文只能在 `memory_evidence` 里单独索要**；
  ④ **审计**：每次调用（含被拒绝的）都留一行——哪个 agent、哪个工具、给了几条；
  ⑤ **stdout 只走 JSON-RPC**（任何杂音都会让 agent 解析失败）。

全程在 `%TEMP%` 里造一个假实例，**不碰真库**。退出码 0=全过。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _find_server():
    """定位适配器脚本——**两种布局都要能找到**。

    **2026-10-02 修（陌生人实测抓到的真缺陷）**：原先写死
    `BASE = dirname(dirname(dirname(__file__)))` 再拼 `mcp_memory_server.py`。
    这套在**仓库里**成立（`组件验证/Agent记忆层/` ↔ 仓库根），但公开仓库是 `tests/` ↔ 根，
    三跳会跳到 clone 的**上一级目录** ⇒ `SERVER` 指向一个不存在的文件 ⇒
    **别人 clone 下来照 README 跑，这条"自己怎么验"的命令必然失败**（而 P3 的全部可信度就挂在它上面）。
    现在按"离自己最近的先找"依次试，并允许用 `--server <路径>` 或环境变量覆盖。
    """
    _override = ""
    try:
        if "--server" in sys.argv:
            _i = sys.argv.index("--server")
            if _i + 1 < len(sys.argv):
                _override = sys.argv[_i + 1]
    except Exception:
        _override = ""
    _override = _override or os.environ.get("SHENTU_SERVER", "")
    if _override and os.path.isfile(_override):
        return os.path.abspath(_override)
    _here = os.path.dirname(os.path.abspath(__file__))
    for _c in (os.path.join(os.path.dirname(_here), "mcp_memory_server.py"),   # 公开仓库：tests/ → 根
               os.path.join(_here, "mcp_memory_server.py"),                    # 同目录
               os.path.join(BASE, "mcp_memory_server.py")):                    # 仓库：组件验证/Agent记忆层/ → 根
        if os.path.isfile(_c):
            return os.path.abspath(_c)
    return ""


SERVER = _find_server()
DDL = ("CREATE TABLE screenshots (id INTEGER PRIMARY KEY AUTOINCREMENT, file_path TEXT,"
       " ocr_text TEXT, captured_at TEXT, session_folder TEXT, is_active_session INTEGER,"
       " is_favorite INTEGER, favorite_category TEXT, visibility TEXT, app_name TEXT,"
       " window_title TEXT, project TEXT, source TEXT, duration REAL, ocr_conf REAL)")


def child_env():
    """起子进程时用的环境：**把子进程的 stdout/stderr 钉成 UTF-8**（2026-10-04 修，作者实测抓到的真缺陷）。

    为什么必须钉：适配器只在 **JSON-RPC 主线**里把三条流 `reconfigure` 成 UTF-8，
    而 `--grant` / `--revoke` / `--status` / `--audit` 这些**命令行路径在它之前**就打印完了 ⇒
    在**中文 Windows**（控制台 cp936）上，它们吐的是 **cp936 字节**，而本脚本按 UTF-8 解码 ⇒
    `"已授权"` 找不到 ⇒ 判红。**产品没问题，是"验的方法"挑了机器**：
    仓库挂出去之后，陌生人照 README 跑这条命令就会看到一个红的失败（实测复现）。
    钉法：与门禁判据 0.0.60 同一条规矩——只设 `PYTHONIOENCODING`，**不动** `PYTHONUTF8`（那会顺带改别的编码行为）。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def now(delta_min=0):
    """夹具的时间戳**必须与真库同格式**（2026-10-03 修，这是个"门禁假绿"的教训）。

    真库里 `captured_at` 是 `database.insert_record` 写的 `datetime.isoformat()`
    ⇒ `2026-10-03T18:47:12.470178`（**T 分隔 + 微秒**）。原先这里写空格分隔的
    `"%Y-%m-%d %H:%M:%S"`，于是"时间窗"这条被夹在里面的前提**是假的**：
    适配器拿空格格式的界去比真库的 T 格式行，字符串比较在第 11 个字符就分胜负（'T' > ' '），
    **任何 T 格式的行都通过** ⇒ 时间窗从未真正生效，而这里全绿（夹具替被测对象把 bug 盖住了）。
    ⇒ 现在**照真实格式造**，并显式断言窗口（见下面「时间窗真生效」两条）。
    """
    import datetime as dt
    return (dt.datetime.now() - dt.timedelta(minutes=delta_min)).isoformat()


def build(tmp):
    os.makedirs(os.path.join(tmp, "screenshot"), exist_ok=True)
    with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"DB_PATH": "screen_memory.db", "SCREENSHOT_DIR": "screenshot",
                   "CURRENT_PROJECT": "项目A", "ASK_INCLUDE_PRIVATE": False}, f)
    con = sqlite3.connect(os.path.join(tmp, "screen_memory.db"))
    con.execute(DDL)
    rows = [
        # 当前项目 + 窗口内：应出现
        ("s1.jpg", "报价单 甲方 乙方 金额 12000", now(5), "workspace", "winword.exe", "报价单.docx",
         "项目A", 0.92),
        ("s2.jpg", "会议纪要 第三季度 目标", now(10), "workspace", "chrome.exe", "项目页",
         "项目A", 0.7),
        # 长文本：摘要必须截断、全文要单独索要
        ("s3.jpg", "长" * 400, now(12), "workspace", "notepad.exe", "长文", "项目A", None),
        # 私人库：**任何工具都不许出现**
        ("p1.jpg", "私人聊天内容 报价单", now(6), "private", "wechat.exe", "微信", "项目A", 0.9),
        # 别的项目 + 窗口外：不许出现
        ("o1.jpg", "别的项目的报价单", now(5), "workspace", "excel.exe", "别的项目", "项目B", 0.9),
        ("o2.jpg", "很久以前的报价单", now(60 * 24 * 3), "workspace", "excel.exe", "旧记录",
         "项目A", 0.9),
    ]
    for r in rows:
        con.execute("INSERT INTO screenshots (file_path, ocr_text, captured_at, visibility,"
                    " app_name, window_title, project, ocr_conf, source) VALUES (?,?,?,?,?,?,?,?,?)",
                    (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], "auto"))
    con.commit()
    con.close()


class Client:
    """极简 MCP 客户端：一行一个 JSON-RPC。"""

    def __init__(self, inst):
        # 起不来要**说清是什么起不来**（2026-10-02 修）：原先脚本不存在时抛的是
        # "服务端没有回应（stdout 空）——它是不是把日志写到 stdout 了？" ⇒ **把人指向错误的方向**
        # （他会去查"日志是不是污染了 stdout"，而真实原因是"这个文件根本不存在"）。
        # 这个项目最讨厌的就是"错误信息指向假因"，所以这里显式分三种：
        #   ① 找不到脚本 → 告诉他在哪放/怎么用 --server 指定；
        #   ② 起不来（权限/解释器异常）→ 把解释器的原始错误打出来；
        #   ③ 起来了但不回应 → 才轮到"是不是把日志写到 stdout 了"。
        if not SERVER or not os.path.isfile(SERVER):
            raise AssertionError(
                "找不到适配器脚本 `mcp_memory_server.py`。\n"
                "  期望位置（任一即可）：与本文件同级、上一级目录、或仓库根。\n"
                "  也可以显式指定：python tests/selftest_mcp_memory.py --server <适配器路径>\n"
                "（当前解析到：%r）" % SERVER)
        try:
            self.p = subprocess.Popen([sys.executable, SERVER, "--instance", inst],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, cwd=BASE, env=child_env(),
                                      text=True, encoding="utf-8", errors="replace", bufsize=1)
        except Exception as _e:
            raise AssertionError("适配器进程起不来：%s: %s\n（脚本：%s）"
                                 % (type(_e).__name__, _e, SERVER))
        self.id = 0

    def call(self, method, params=None):
        self.id += 1
        msg = {"jsonrpc": "2.0", "id": self.id, "method": method}
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            _err = ""
            try:
                _err = (self.p.stderr.read() or "")[-400:]
            except Exception:
                pass
            raise AssertionError("服务端没有回应（stdout 空）——它是不是把日志写到 stdout 了？\n"
                                 "  适配器 stderr 末尾：%s" % (_err.strip() or "（空）"))
        return json.loads(line)

    def tool(self, name, args=None):
        return self.call("tools/call", {"name": name, "arguments": args or {}})

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


def main():
    tmp = tempfile.mkdtemp(prefix="mcp_selftest_")
    fails = []

    def ck(name, cond, why=""):
        if cond:
            print("  [OK] %s" % name)
        else:
            fails.append("%s %s" % (name, why))
            print("  [红] %s %s" % (name, why))

    try:
        build(tmp)
        c = Client(tmp)
        # ① 握手
        r = c.call("initialize", {"protocolVersion": "2024-11-05",
                                  "clientInfo": {"name": "自检客户端", "version": "1"}})
        ck("initialize 握手", (r.get("result") or {}).get("serverInfo", {}).get("name") == "shentu-memory",
           str(r)[:160])
        # ② 工具清单
        r = c.call("tools/list")
        names = [t["name"] for t in (r.get("result") or {}).get("tools", [])]
        ck("tools/list 四个工具", names == ["memory_scope", "memory_recent", "memory_search",
                                            "memory_evidence"], str(names))
        # ③ 未授权：scope 可问、其它全拒
        r = c.tool("memory_scope")
        payload = json.loads(r["result"]["content"][0]["text"])
        ck("未授权时 memory_scope.authorized=False", payload.get("authorized") is False, str(payload)[:150])
        ck("未授权时 scope 不含项目名", payload.get("project") == "", str(payload.get("project")))
        r = c.tool("memory_recent", {"minutes": 30})
        p2 = json.loads(r["result"]["content"][0]["text"])
        ck("未授权时 memory_recent 被拒", r["result"].get("isError") is True and p2.get("error") == "未授权",
           str(r)[:160])
        ck("拒绝时告诉 agent 怎么授权", "--grant 30" in json.dumps(p2, ensure_ascii=False), str(p2)[:200])
        # ④ 授权（走 CLI，与用户实际动作一致）
        g = subprocess.run([sys.executable, SERVER, "--instance", tmp, "--grant", "5"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=BASE, env=child_env(), timeout=60)
        ck("--grant 命令行可用", g.returncode == 0 and "已授权" in (g.stdout or ""), (g.stdout or "")[:120])
        r = c.tool("memory_recent", {"minutes": 30, "limit": 20})
        p3 = json.loads(r["result"]["content"][0]["text"])
        items = p3.get("items") or []
        ids = [i["app"] for i in items]
        ck("授权后 memory_recent 有结果", len(items) >= 3, str(p3)[:200])
        ck("私人库永不出", "wechat.exe" not in ids, str(ids))
        ck("别的项目不出", "excel.exe" not in ids or all(
            i["title"] != "别的项目" for i in items), str(ids))
        ck("条数不超上限", len(items) <= 20, str(len(items)))
        long_one = [i for i in items if i["app"] == "notepad.exe"]
        ck("长文本摘要被截断且标了 truncated",
           bool(long_one) and long_one[0]["truncated"] is True and len(long_one[0]["text"]) < 200,
           str(long_one)[:180])
        ck("摘要带原图路径与可信度档",
           all(("file" in i and "conf" in i) for i in items), str(items[:1])[:180])
        # ④b **时间窗真生效**（2026-10-03 加：这条原先没人验，而它恰好是坏的）
        #   · 上一条用 o2.jpg（3 天前）：它守的是"跨天的记录不许因为格式问题混进来"；
        #   · 下一条用 `minutes=1` 才是**真正能抓住那个缺陷**的那条——因为字符串比较的差异
        #     （'T' > ' '）只在**日期前缀完全相同**时才起作用 ⇒ 缺陷的表现是
        #     "**同一天**的历史记录无视时间窗全被端出来"（真机实测：`minutes=1` 回了 20 条）。
        #     射程说明（如实）：这条用例假定运行时刻与"5 分钟前"同一天——除了一天里的头 5 分钟，
        #     恒成立；真在那 5 分钟里跑，它只是**变宽松**（不会假红）。
        ck("时间窗真生效（3 天前的不许出现）",
           all(i["title"] != "旧记录" for i in items), str(ids))
        r = c.tool("memory_recent", {"minutes": 1})
        p3w = json.loads(r["result"]["content"][0]["text"])
        ck("minutes=1 时窗口外的记录一条都不给", (p3w.get("count") or 0) == 0, str(p3w)[:200])
        # ⑤ 检索
        r = c.tool("memory_search", {"query": "报价单", "minutes": 1440})
        p4 = json.loads(r["result"]["content"][0]["text"])
        ck("memory_search 命中当前项目", (p4.get("count") or 0) >= 1, str(p4)[:200])
        ck("检索结果也不含私人/别项目",
           all(i["app"] not in ("wechat.exe",) for i in (p4.get("items") or [])), str(p4)[:200])
        # ⑥ 二级：全文
        aid = long_one[0]["id"]
        r = c.tool("memory_evidence", {"anchor_id": aid})
        p5 = json.loads(r["result"]["content"][0]["text"])
        ck("memory_evidence 给全文", len((p5.get("item") or {}).get("text") or "") > 200,
           str(len((p5.get("item") or {}).get("text") or "")))
        r = c.tool("memory_evidence", {"anchor_id": 999999})
        p6 = json.loads(r["result"]["content"][0]["text"])
        ck("不存在的 id 给明确错误", bool(p6.get("error")), str(p6)[:150])
        # ⑦ 审计
        time.sleep(0.2)
        ap = os.path.join(tmp, "mcp_audit.log")
        ck("审计文件已写", os.path.isfile(ap), ap)
        txt = open(ap, encoding="utf-8", errors="replace").read() if os.path.isfile(ap) else ""
        ck("审计含被拒记录", "denied" in txt, txt[-200:])
        ck("审计含 agent 名与工具名", ("自检客户端" in txt) and ("memory_recent" in txt), txt[-200:])
        # ⑧ 撤销
        rv = subprocess.run([sys.executable, SERVER, "--instance", tmp, "--revoke"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            cwd=BASE, env=child_env(), timeout=60)
        ck("--revoke 可用", rv.returncode == 0, (rv.stdout or "")[:120])
        r = c.tool("memory_recent", {"minutes": 30})
        p7 = json.loads(r["result"]["content"][0]["text"])
        ck("撤销后立刻拒绝", p7.get("error") == "未授权", str(p7)[:150])
        c.close()
        # ⑨ 源级：绝不开端口
        src = open(SERVER, encoding="utf-8").read()
        ck("源码里没有任何网络监听", all(x not in src for x in
                                    ("import socket", "http.server", ".bind(", ".listen(",
                                     "socketserver", "uvicorn", "flask")), "发现有网络相关引用")
        if fails:
            print("\n自检 FAIL：")
            for f in fails:
                print("   - %s" % f)
            return 1
        print("\nAgent 记忆层自检：全部通过")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for _s in ("stdout", "stderr"):
        try:
            getattr(sys, _s).reconfigure(errors="replace")
        except Exception:
            pass
    sys.exit(main())
