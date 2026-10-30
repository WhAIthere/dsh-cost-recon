#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""token统计 的一键回归（退出码 0 = PASS）

覆盖两层：
  A. 账本侧（token_stats.py / db.py）：定价 / peak 边界 / 区间 / CSV / 账本入库
  B. 会话日志侧（scan_sessions.py + db.py）：版本选择 / 跨会话重放去重 / 归属顺序 /
     增量跳过 / 汇总一致性 / 对账表（只统计去重后）

自造固定样例，绝不碰真实数据或真实库。明细写 <工作区>/temp/token_stats_tests.txt。
"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_DIR = os.path.dirname(HERE)
if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

import db                   # noqa: E402
import hier                 # noqa: E402
import holidays as HOL      # noqa: E402
import labels as LBL        # noqa: E402
import prices as PRICES     # noqa: E402
import render_html as rh    # noqa: E402
import scan_sessions as ss  # noqa: E402
import side_calls as sc     # noqa: E402
import token_stats as ts    # noqa: E402

LOGFILE = os.path.join(os.path.dirname(TOOL_DIR), "temp", "token_stats_tests.txt")
FAILS = []
ERRORS = []
WARN = []
CHECKS = [0]

REAL = {
    "db.DB_PATH": os.path.join(TOOL_DIR, "token_stats.db"),
    "ss.DB_PATH": os.path.join(TOOL_DIR, "token_stats.db"),
    "db.SESSIONS_DIR": os.path.join(os.path.expanduser("~"), ".dsh", "sessions"),
    "db.USAGE_JSON": os.path.join(os.path.expanduser("~"), ".dsh", ".dshw-usage.json"),
    "db.LEDGER_JSON": os.path.join(os.path.expanduser("~"), ".dsh", "dsh-usage",
                                   "usage-ledger.json"),
    "ts.USAGE_JSON": os.path.join(os.path.expanduser("~"), ".dsh", ".dshw-usage.json"),
    "ts.LEDGER_JSON": os.path.join(os.path.expanduser("~"), ".dsh", "dsh-usage",
                                   "usage-ledger.json"),
    "rh.DEFAULT_OUT": os.path.join(TOOL_DIR, "out", "report.html"),
    "LBL.WORKSPACE_JSON": os.path.join(os.path.expanduser("~"), ".dsh", "storages",
                                       "workspace.json"),
    "LBL.PROJCACHE_DIR": os.path.join(os.path.expanduser("~"), ".dsh", "storages",
                                      "session_projcache", "sessions"),
}


def patch(**kw):
    """按 'mod.ATTR' 形式改模块级常量。

    注意：scan_sessions 里的 `import db` 和本文件里的 `import db` 可能解析成不同的
    模块对象，所以 db/ss 两边的同名属性都要一起改，否则补丁只生效一半。
    """
    for k, v in kw.items():
        mod_name, attr = k.split(".")
        if mod_name == "db":
            setattr(db, attr, v)
            if ss.db is not db:
                setattr(ss.db, attr, v)
        elif mod_name == "ss":
            setattr(ss, attr, v)
        elif mod_name == "ts":
            setattr(ts, attr, v)
        elif mod_name == "rh":
            setattr(rh, attr, v)
        elif mod_name == "LBL":
            setattr(LBL, attr, v)


def restore():
    patch(**REAL)


def use_temp(tmp, sid="scan.db"):
    """把工具的所有路径切到临时目录（返回新建的库路径）。

    注意显示名（workspace.json / projcache）也要一起切走，否则测试会去读真实的 DSH 数据。
    """
    dbfile = os.path.join(tmp, sid)
    patch(**{"db.DB_PATH": dbfile, "ss.DB_PATH": dbfile,
             "db.SESSIONS_DIR": os.path.join(tmp, "sessions"),
             "db.USAGE_JSON": os.path.join(tmp, "usage.json"),
             "db.LEDGER_JSON": os.path.join(tmp, "ledger.json"),
             "ts.USAGE_JSON": os.path.join(tmp, "usage.json"),
             "ts.LEDGER_JSON": os.path.join(tmp, "ledger.json"),
             "LBL.WORKSPACE_JSON": os.path.join(tmp, "storages", "workspace.json"),
             "LBL.PROJCACHE_DIR": os.path.join(tmp, "storages", "session_projcache", "sessions")})
    return dbfile


def check(name, got, want, tol=1e-9):
    CHECKS[0] += 1
    ok = (abs(got - want) <= tol) if isinstance(want, (int, float)) and isinstance(got, (int, float)) \
        else (got == want)
    if not ok:
        FAILS.append("%s: got=%r want=%r" % (name, got, want))
    return ok


def guard(fn):
    try:
        fn()
    except Exception as e:
        import traceback
        CHECKS[0] += 1
        FAILS.append("%s: 异常 %s" % (getattr(fn, "__name__", "case"), e))
        ERRORS.append("%s\n%s" % (getattr(fn, "__name__", "case"), traceback.format_exc()))


def ms(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm).timestamp() * 1000)


_CON = {}


def con():
    if "c" not in _CON:
        _CON["c"] = db.connect(db.DB_PATH)
    return _CON["c"]


def closer():
    if "c" in _CON:
        _CON["c"].close()
        _CON.pop("c")


def one(sql, connection=None):
    row = (connection or con()).execute(sql).fetchone()
    return None if row is None else row[0]


# ================================================================ 造样例
def write_ledger_fixture(tmp):
    usage = {
        "date": "2026-09-21", "lastBalance": 12.34, "todayUsage": 1.5, "dayStart": 13.0,
        "history": {"2026-09-20": 0.75, "2026-09-21": 1.5},
        "accounting": {"version": 1, "active": "bk-CNY", "books": {"bk-CNY": {"currency": "CNY",
            "days": {"2026-09-20": {"debitUnits": 70000000},
                     "2026-09-21": {"debitUnits": 150000000}}}}},
        "events": [
            {"ts": ms(2026, 9, 20, 10, 30), "day": "2026-09-20", "model": "deepseek-flash",
             "cost": 0.5, "tokens": 1000},
            {"ts": ms(2026, 9, 20, 15, 5), "day": "2026-09-20", "model": "deepseek-flash",
             "cost": 0.25, "tokens": 500},
            {"ts": ms(2026, 9, 21, 11, 0), "day": "2026-09-21", "model": "deepseek-flash",
             "cost": 1.5, "tokens": 3000},
        ],
    }
    ledger = {"version": 1, "days": {
        "2026-09-20": {"deepseek-official": {"deepseek-flash": {
            "inputTokens": 100, "outputTokens": 200, "cacheReadTokens": 9000,
            "cacheWriteTokens": 0, "reasoningTokens": 50, "calls": 2, "cost": 0.7}}},
        "2026-09-21": {"deepseek-official": {"deepseek-flash": {
            "inputTokens": 300, "outputTokens": 400, "cacheReadTokens": 8000,
            "cacheWriteTokens": 0, "reasoningTokens": 60, "calls": 1, "cost": 1.4}}},
    }}
    with open(os.path.join(tmp, "usage.json"), "w", encoding="utf-8") as f:
        json.dump(usage, f, ensure_ascii=False)
    with open(os.path.join(tmp, "ledger.json"), "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False)


def make_log(path, sid, msgs, cwd="C:\\wsA", depth=0, extra_unknown=False, created_at=None, summary=None):
    """写一个会话日志（v3/v4 由文件名决定）。msgs: dict(ts,in,out,cache,id,turn,step,model)"""
    lines = [{"type": "session", "version": 3 if ".v3." in path else 4, "id": sid,
              "createdAt": (created_at if created_at is not None
                            else (msgs[0]["ts"] if msgs else 0)), "cwd": cwd, "isSeeded": False,
              "delegationDepth": depth, "agentPreset": "standard"}]
    for i, m in enumerate(msgs, 1):
        tok = {"inputTokens": m.get("in", 0), "outputTokens": m.get("out", 0),
               "cacheReadTokens": m.get("cache", 0), "cacheWriteTokens": 0,
               "reasoningTokens": m.get("reason", 0),
               "totalTokens": m.get("in", 0) + m.get("out", 0) + m.get("cache", 0)}
        if m.get("model"):
            tok["model"] = m["model"]        # 用来造出"同一层有多个兄弟"的场景
        lines.append({"type": "assistant/message", "ts": m["ts"],
                      "data": {"message": {"id": m.get("id") or "%s-m%d" % (sid, i)},
                               "step": m.get("step", i), "turn": m.get("turn", 1), "usage": tok}})
    if extra_unknown:
        lines.append({"type": "session/title-llm-request", "ts": lines[1]["ts"],
                      "data": {"usage": {"inputTokens": 100, "outputTokens": 50,
                                         "cacheReadTokens": 0, "totalTokens": 150}}})
    if summary:
        lines.append({"type": "compaction/summary", "ts": summary["ts"],
                      "data": {"usage": {"inputTokens": summary.get("in", 0),
                                         "outputTokens": summary.get("out", 0),
                                         "cacheReadTokens": summary.get("hit", 0),
                                         "cacheWriteTokens": 0,
                                         "totalTokens": summary.get("in", 0)
                                         + summary.get("out", 0) + summary.get("hit", 0)}}})
    import zstandard
    blob = "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n"
    with open(path, "wb") as f:
        f.write(zstandard.ZstdCompressor().compress(blob.encode("utf-8")))
    return path


def build_session_tree(root):
    """2 个工作区、5 个会话，含 fork 重放、v3+v4 并存、子代理、旁路调用。

    token 量按真实量级给（几十万级），否则折算花费会小到 0.0000，
    treemap 这类"按面积画"的图会没格子，测出来像是坏了。
    """
    def p(ws, sid, fname):
        d = os.path.join(root, ws, sid)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, fname)

    W1, W2 = "--C-Users-x-wsA--", "--C-Users-x-0-wsB--"
    a1 = [{"ts": ms(2026, 9, 20, 10, 0), "in": 100000, "out": 20000, "cache": 1000000, "id": "m1"},
          {"ts": ms(2026, 9, 20, 10, 5), "in": 300000, "out": 40000, "cache": 2000000, "id": "m2"},
          {"ts": ms(2026, 9, 20, 10, 9), "in": 500000, "out": 60000, "cache": 3000000, "id": "m3"}]
    make_log(p(W1, "session-A1", "session.v3.jsonl.zstd"), "session-A1", a1, cwd="C:\\wsA")
    # A2 是 A1 的 fork：前两条时间戳与 token 全同（重放），第三条是新调用
    make_log(p(W1, "session-A2", "session.v4.jsonl.zstd"), "session-A2",
             [dict(a1[0]), dict(a1[1]),
              {"ts": ms(2026, 9, 20, 11, 0), "in": 700000, "out": 80000, "cache": 4000000, "id": "m4"}],
             cwd="C:\\wsA")
    # 子代理
    make_log(p(W1, "session-A3", "session.v3.jsonl.zstd"), "session-A3",
             [{"ts": ms(2026, 9, 20, 10, 6), "in": 11000, "out": 2200, "cache": 330000, "id": "s1"}],
             cwd="C:\\wsA", depth=1)
    # wsB：带旁路调用；同一天同一工作区里放两个模型，好让"第 3 层"有多个兄弟
    make_log(p(W2, "session-B1", "session.v3.jsonl.zstd"), "session-B1",
             [{"ts": ms(2026, 9, 21, 14, 0), "in": 9000, "out": 900, "cache": 90000, "id": "b1"},
              {"ts": ms(2026, 9, 21, 14, 10), "in": 4000, "out": 400, "cache": 40000, "id": "b1b",
               "model": "deepseek-v4-pro"}],
             cwd="C:\\wsB", extra_unknown=True)
    # 第 3 个会话：和第 2 个同工作区同天，保证"第 2 层"也有多个兄弟
    make_log(p(W2, "session-B3", "session.v3.jsonl.zstd"), "session-B3",
             [{"ts": ms(2026, 9, 21, 16, 0), "in": 7000, "out": 700, "cache": 70000, "id": "d1"},
              {"ts": ms(2026, 9, 21, 16, 20), "in": 2000, "out": 200, "cache": 20000, "id": "d2",
               "model": "deepseek-v4-pro"}],
             cwd="C:\\wsB")
    # 第 3 个工作区：同一天(09-21)的另一个工作区，保证"第 1 层"下也有多个"第 2 层"
    make_log(p("--C-Users-x-0-wsC--", "session-C1", "session.v3.jsonl.zstd"), "session-C1",
             [{"ts": ms(2026, 9, 21, 17, 0), "in": 3000, "out": 300, "cache": 30000, "id": "e1"}],
             cwd="C:\\wsC")
    # 第 4 个工作区：09-20 也放第二个工作区，保证"天"这一层也有多个兄弟（才能验第 1 个下拉）
    make_log(p("--C-Users-x-0-wsD--", "session-D1", "session.v3.jsonl.zstd"), "session-D1",
             [{"ts": ms(2026, 9, 20, 18, 0), "in": 8000, "out": 800, "cache": 80000, "id": "f1"},
              {"ts": ms(2026, 9, 20, 18, 20), "in": 1000, "out": 100, "cache": 10000, "id": "f2",
               "model": "deepseek-v4-pro"}],
             cwd="C:\\wsD")
    # v3+v4 并存：v4 是超集 → 只读 v4
    make_log(p(W2, "session-B2", "session.v3.jsonl.zstd"), "session-B2",
             [{"ts": ms(2026, 9, 21, 15, 0), "in": 5000, "out": 500, "cache": 50000, "id": "c1"}],
             cwd="C:\\wsB")
    make_log(p(W2, "session-B2", "session.v4.jsonl.zstd"), "session-B2",
             [{"ts": ms(2026, 9, 21, 15, 0), "in": 5000, "out": 500, "cache": 50000, "id": "c1"},
              {"ts": ms(2026, 9, 21, 15, 30), "in": 6000, "out": 600, "cache": 60000, "id": "c2"}])


# ================================================================ A. 账本侧
def test_ledger_side(tmp):
    write_ledger_fixture(tmp)
    con = ts.connect(os.path.join(tmp, "ledger.db"))
    r = ts.sync(con)
    check("sync.events", r["events"], 3)
    check("sync.day_model", r["day_model"], 2)

    s = ts.summary(con, "2026-09-20", "2026-09-21")
    check("summary.total", s["total"], 2.25)
    check("summary.tokens", s["tokens"], 4500)
    check("summary.balance", s["balance"], 12.34)
    dc = {d["day"]: d for d in s["daily"]}
    check("daily.0920_calls", dc["2026-09-20"]["calls"], 2)
    check("daily.0921_cost", dc["2026-09-21"]["cost"], 1.5)
    hb = ts.hour_costs(con, "2026-09-20")
    check("hours.10", hb[10]["cost"], 0.5)
    check("hours.15", hb[15]["cost"], 0.25)
    check("models.count", len(ts.model_costs(con, "2026-09-20", "2026-09-21")), 1)
    check("range.explicit", ts.parse_range("2026-09-20..2026-09-21", con),
          ("2026-09-20", "2026-09-21"))
    check("range.month_prefix", ts.parse_range("2026-09", con), ("2026-09-01", "2026-09-31"))
    check("csv.count", len(ts.export_csv(con, os.path.join(tmp, "out"),
                                         "2026-09-20", "2026-09-21")), 3)
    con.close()

    # db.py 侧的账本入库（含 accounting 账本 / 对账表）
    con2 = db.connect(os.path.join(tmp, "dbtest.db"))
    n = db.sync_ledger(con2)
    check("db.sync_ledger.events", n["events"], 3)
    check("db.sync_ledger.day_model", n["day_model"], 2)
    check("db.book_days", n["book_days"], 2)
    con2.close()


def test_pricing_and_peak():
    check("price.output", db.PRICE["output"], 4.0)
    check("price.cache_hit", db.PRICE["cacheHit"], 0.02)
    check("cost.offpeak", db.call_cost(1000, 2000, 0, 0, False), 0.009, 1e-12)
    check("cost.peak_double", db.call_cost(1000, 2000, 0, 0, True), 0.018, 1e-12)
    check("cost.cache_cheap", db.call_cost(0, 0, 1000, 0, False), 0.00002, 1e-12)
    check("peak.mon_11", db.is_peak(ms(2026, 9, 21, 11, 0)), True)
    check("peak.mon_12_close", db.is_peak(ms(2026, 9, 21, 12, 0)), False)
    check("peak.mon_14_open", db.is_peak(ms(2026, 9, 21, 14, 0)), True)
    check("peak.mon_18_close", db.is_peak(ms(2026, 9, 21, 18, 0)), False)
    check("peak.mon_19", db.is_peak(ms(2026, 9, 21, 19, 0)), False)
    check("peak.sat_11", db.is_peak(ms(2026, 9, 19, 11, 0)), False)


# ================================================================ B. 会话日志侧
def test_session_scan(tmp):
    root = os.path.join(tmp, "sessions")
    build_session_tree(root)
    dbfile = use_temp(tmp, "scan.db")

    dirs = ss.list_session_dirs(root)
    check("dirs.count", len(dirs), 8)
    check("version.pick_newest", ss.pick_newest_version(
        ["session.v3.jsonl.zstd", "session.v4.jsonl.zstd", "x.txt"]), (4, "session.v4.jsonl.zstd"))
    check("version.only_v3", ss.pick_newest_version(["session.v3.jsonl.zstd"]),
          (3, "session.v3.jsonl.zstd"))
    check("version.ignore_other", ss.pick_newest_version(["notes.txt"]), None)

    r = ss.scan(full=True, quiet=True, root=root)
    check("scan.dirs", r["dirs"], 8)
    check("scan.failed", r["failed"], 0)
    check("scan.replayed", r["replayed"], 2)

    con = db.connect(dbfile)
    # 解析 10 行（A1=3 A2=3 A3=1 B1=1 B2(v4)=2），其中重放 2 行 → 计入口径 8 行
    check("messages.all", one("SELECT COUNT(*) FROM messages", con), 16)
    check("messages.kept", one("SELECT COUNT(*) FROM messages WHERE keep=1", con), 14)
    owner = {x["msg_id"]: x["session_id"] for x in con.execute(
        "SELECT msg_id, session_id FROM messages WHERE msg_id IN ('m1','m2') AND keep=1")}
    check("dedup.owner_m1", owner.get("m1"), "session-A1")
    check("dedup.owner_m2", owner.get("m2"), "session-A1")
    check("dedup.replay_marked", one(
        "SELECT COUNT(*) FROM messages WHERE session_id='session-A2' AND msg_id='m1' AND keep=0", con), 1)
    check("v4.superset_rows", one(
        "SELECT COUNT(*) FROM messages WHERE session_id='session-B2'", con), 2)
    check("parent.subagent", one(
        "SELECT parent_id FROM sessions WHERE session_id='session-A3'", con), "session-A1")
    check("parent.none_for_main", one(
        "SELECT parent_id FROM sessions WHERE session_id='session-A1'", con), None)

    row = con.execute("SELECT day, hour, turn_no FROM messages WHERE msg_id='m3'").fetchone()
    check("msg.day", row["day"], "2026-09-20")
    check("msg.hour", row["hour"], 10)
    check("msg.turn_no", row["turn_no"], 1)
    m = con.execute("SELECT * FROM messages WHERE msg_id='m3'").fetchone()
    check("msg.tokens", (m["in_tokens"], m["out_tokens"], m["cache_read"], m["total_tokens"]),
          (500000, 60000, 3000000, 3560000))
    check("msg.weekend_not_peak", one("SELECT peak FROM messages WHERE msg_id='m1'", con), 0)
    check("msg.monday_peak", one("SELECT peak FROM messages WHERE msg_id='b1'", con), 1)

    check("agg.messages_eq_kept", one("SELECT SUM(messages) FROM sessions", con), 14)
    check("agg.in_eq_kept", one("SELECT SUM(in_tokens) FROM sessions", con),
          one("SELECT SUM(in_tokens) FROM messages WHERE keep=1", con))
    check("agg.workspaces", one("SELECT COUNT(DISTINCT workspace) FROM sessions", con), 4)
    # 聚合 SQL 2026-10 重写过（原来 438×11 个相关子查询 = 34 秒）：守一条"和现算的一模一样"
    bad = []
    for r in con.execute("SELECT session_id, messages, calls, in_tokens, out_tokens, cache_read, "
                         "cost_est, peak_cost, turns FROM sessions"):
        agg = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(in_tokens),0) i, COALESCE(SUM(out_tokens),0) o, "
            "COALESCE(SUM(cache_read),0) c, "
            "COALESCE(SUM(CASE WHEN cost_est>=0 THEN cost_est ELSE 0 END),0) cost, "
            "COALESCE(SUM(CASE WHEN cost_est>=0 AND peak=1 THEN cost_est ELSE 0 END),0) pc, "
            "COALESCE(MAX(turn_no),0) t FROM messages WHERE session_id=? AND keep=1",
            (r["session_id"],)).fetchone()
        got = (r["messages"], r["calls"], r["in_tokens"], r["out_tokens"], r["cache_read"],
               round(r["cost_est"] or 0, 9), round(r["peak_cost"] or 0, 9), r["turns"])
        want = (agg["n"], agg["n"], agg["i"], agg["o"], agg["c"], round(agg["cost"], 9),
                round(agg["pc"], 9), agg["t"])
        if got != want:
            bad.append(r["session_id"])
    check("agg.per_session_matches_messages", bad, [])

    # 增量：全跳过
    r2 = ss.scan(full=False, quiet=True, root=root)
    check("incr.skipped", r2["skipped"], 8)
    check("incr.updated", r2["updated"], 0)
    check("incr.stable_rows", one("SELECT COUNT(*) FROM messages", con), 16)
    # 改一个文件 → 只重扫它
    make_log(os.path.join(root, "--C-Users-x-0-wsB--", "session-B1", "session.v3.jsonl.zstd"),
             "session-B1",
             [{"ts": ms(2026, 9, 21, 14, 0), "in": 9000, "out": 900, "cache": 90000, "id": "b1"},
              {"ts": ms(2026, 9, 21, 14, 30), "in": 1000, "out": 200, "cache": 3000, "id": "b2"}],
             extra_unknown=True)
    r3 = ss.scan(full=False, quiet=True, root=root)
    check("incr.one_updated", r3["updated"], 1)
    check("incr.one_added", one("SELECT COUNT(*) FROM messages WHERE msg_id='b2'", con), 1)
    check("incr.no_dup", one("SELECT COUNT(*) FROM messages WHERE msg_id='b1'", con), 1)
    con.close()

    # 对账表：scan_cost 必须只算 keep=1
    write_ledger_fixture(tmp)
    con = db.connect(dbfile)
    db.sync_ledger(con)
    n = db.rebuild_day_recon(con)
    check("recon.rows", n >= 2, True)
    row = con.execute("SELECT * FROM day_recon WHERE day='2026-09-20'").fetchone()
    check("recon.book", row["book_cost"], 0.70, 1e-9)
    check("recon.events", row["events_cost"], 0.75, 1e-9)
    kept = one("SELECT SUM(cost_est) FROM messages WHERE day='2026-09-20' AND keep=1", con)
    check("recon.scan_kept_only", row["scan_cost"], kept, 1e-9)
    total = one("SELECT SUM(cost_est) FROM messages WHERE day='2026-09-20'", con)
    check("recon.replay_excluded", total > kept, True)
    check("recon.factor", row["factor"], 0.70 / kept, 1e-9)
    con.close()


# ================================================================ C. 分层查询 + HTML
def write_label_fixture(tmp):
    """造一份 DSH 风格的"名字映射"，用来验证显示层（不影响统计）。"""
    import json as _json
    wsdir = os.path.join(tmp, "storages")
    pjdir = os.path.join(wsdir, "session_projcache", "sessions")
    os.makedirs(pjdir, exist_ok=True)
    ws = {"tables": {"workspaces": {
        "id-a": {"path": "C:\\wsA", "title": "甲工作区", "sessionIds": ["session-A1"]},
        "id-b": {"path": "C:\\wsB", "title": "乙工作区", "sessionIds": ["session-B1"]}}}}
    with open(os.path.join(wsdir, "workspace.json"), "w", encoding="utf-8") as f:
        _json.dump(ws, f, ensure_ascii=False)
    for sid, title, turns in (("session-A1", "测试会话甲", ["第一轮的问题开头", "第二轮的问题开头"]),
                              ("session-B1", "测试会话乙", ["乙的第一轮"])):
        rec = {"record": {"rows": {
            "title": {"ver": 1, "seq": 1, "val": title},
            "turnOutline": {"ver": 2, "seq": 1, "val": {"turns": [
                {"turn": i + 1, "seq": 1, "prompt": p, "response": "回复"} for i, p in enumerate(turns)]}}}}}
        with open(os.path.join(pjdir, sid + ".json"), "w", encoding="utf-8") as f:
            _json.dump(rec, f, ensure_ascii=False)
    return os.path.join(wsdir, "workspace.json"), pjdir


def test_hier_and_html(tmp):
    """在 B 段造好的样例库上跑分层查询，再渲一份 HTML 并用 node 假 DOM 跑一遍脚本。"""
    total = one("SELECT SUM(cost_est) FROM messages WHERE keep=1")
    tcost = one("SELECT SUM(cost_est) FROM messages WHERE keep=1 AND day='2026-09-20'")

    rows = hier.run(["day", "workspace"], "2026-09-20", "2026-09-21", "cost", quiet=True)
    check("hier.groups", len(rows) >= 2, True)
    check("hier.sum_equals_db", sum(r["metric"] for r in rows), total, 1e-9)
    r2 = hier.run(["workspace", "day"], "2026-09-20", "2026-09-21", "cost", quiet=True)
    check("hier.order_invariant", sorted(x["metric"] for x in rows),
          sorted(x["metric"] for x in r2), 1e-9)
    check("hier.day_filter", sum(x["metric"] for x in
          hier.run(["workspace"], "2026-09-20", "2026-09-20", "cost", quiet=True)), tcost, 1e-9)
    check("hier.metric_tokens", sum(x["metric"] for x in
          hier.run(["day"], "2026-09-20", "2026-09-20", "tokens", quiet=True)),
          one("SELECT SUM(total_tokens) FROM messages WHERE keep=1 AND day='2026-09-20'"), 1e-9)
    check("hier.bad_dim", isinstance(hier.DIMS.get("nope"), type(None)), True)

    # 先把显示名映射指向夹具，再重扫一次让 labels 有内容
    import labels as _L
    wsjson, pjdir = write_label_fixture(tmp)
    _L.WORKSPACE_JSON, _L.PROJCACHE_DIR = wsjson, pjdir
    ss.scan(full=False, quiet=True, root=os.path.join(tmp, "sessions"))
    lab = {r["kind"]: r["n"] for r in con().execute(
        "SELECT kind, COUNT(*) n FROM labels GROUP BY kind")}
    check("labels.workspace_collected", lab.get("workspace", 0) >= 1, True)
    check("labels.session_collected", lab.get("session", 0) >= 1, True)
    check("labels.turn_collected", lab.get("turn", 0) >= 2, True)

    # 预设配置文件：正常行、缺格行、坏值行各来一条（**不要**去读用户那份真实 presets.txt，
    # 否则用户一改预设自测就红）
    pfile = os.path.join(tmp, "presets.txt")
    with open(pfile, "w", encoding="utf-8") as f:
        f.write("# 注释行\n"
                "甲档 | workspace,session,turn | today | ts_desc | 0.5 | 0 | yes\n"
                "乙档 | day,workspace,session | full | cost,cost,cost | 1 | 50 | yes\n"
                "丙档 | workspace,session,day | full | cost | 2 | 50 | yes\n"
                "这一行没有竖线，应当被跳过\n"
                "丁档 | day | weird | nope | abc | 50 | maybe\n")

    out = os.path.join(tmp, "report.html")
    rh.DEFAULT_OUT = out
    rc = rh.main(["--range", "all", "--out", out, "--presets", pfile])
    check("html.rc", rc, 0)
    check("html.exists", os.path.exists(out) and os.path.getsize(out) > 2000, True)
    with open(out, encoding="utf-8") as f:
        html = f.read()
    # 真·离线：不许有"会去联网加载"的东西。
    # 注意：数据集里本来就可能含 URL（你提问里贴过链接），那是纯文本数据、不是外链，
    # 只要它是属性形式的引用才需要拦。
    import re as _re2
    loaders = _re2.findall(r'(?:src|href)\s*=\s*["\']https?://|@import|url\(\s*["\']?https?://', html)
    check("html.single_file", len(loaders) == 0, True)
    check("html.has_data", "const DATA = {" in html, True)
    # 注入防护：JSON 内联时把 < > & 转义（样例数据里没有这几个字符，故用真实库再验一次）
    blob = html.split("const DATA = ", 1)[1].split(";\n", 1)[0] if "const DATA = " in html else ""
    check("html.blob_no_raw_lt", "<" not in blob.replace("\\u003c", ""), True)

    ok, res = rh.check_browser(out)
    if not ok:
        check("html.browser", "错误: %s" % res, "运行成功")
    else:
        import re as _re
        check("html.browser", True, True)
        check("html.kpi_rendered", len(res["kpi"]) > 50, True)
        check("html.donut_rendered", "<svg" in res["donutCost"], True)
        check("html.daily_rendered", "<svg" in res["daily"], True)
        check("html.timeline_rendered", "<svg" in res["timeline"], True)
        check("html.recon_rendered", res["recon"].count("<tr") >= 2, True)
        # 对账表各列按"源头真实精度"显示：账本是分（2 位）、全精度列 3 位、系数 4 位
        row20 = _re.search(r"<tr><td>2026-09-20</td>(.*?)</tr>", res["recon"], _re.S)
        cells20 = _re.findall(r"<td>(.*?)</td>", row20.group(1)) if row20 else []
        check("recon.book_is_cents", (len(cells20) >= 6 and cells20[1] == "0.70"), True)
        check("recon.float_cols_3dp",
              (len(cells20) >= 6 and _re.match(r"^\d+\.\d{3}$", cells20[4]) is not None), True)
        # 比率列现在是「折算/官方」的百分比：100.0% = 统计到的和官方一样多
        check("recon.ratio_pct",
              (len(cells20) >= 6 and _re.match(r"^(\d+\.\d%|-)$", cells20[5]) is not None), True)
        check("html.row_count_matches", res["nCurrent"],
              one("SELECT COUNT(*) FROM messages WHERE keep=1"))
        money = _re.findall(r"([0-9]+\.[0-9]{3})", res["kpi"])
        check("html.kpi_cost_matches_db", any(abs(float(x) - total) < 0.01 for x in money), True)

        # ---- 树结构：能渲染、层级不跳级、父子链完整 ----
        def segs(p):
            return tuple(x for x in p.split("\u0001") if x)

        paths = [segs(p) for p in res["paths"]]
        check("tree.root_children", res["rootChildren"],
              one("SELECT COUNT(DISTINCT day) FROM messages WHERE keep=1"))
        check("tree.nodes_exist", len(paths) > 0, True)
        # 不要求"每行都是 3 段"：某个会话若工作区取不到值，它会挂在上一层，
        # 这是数据缺失导致的合法退化。真正要保证的是"任何节点都只能比父节点深一层"。
        check("tree.no_level_skip",
              all(len(p) >= 1 and len(p) <= 3 for p in paths), True)
        pset = set(paths)
        # 父子链：夹具里有个会话工作区取不到值（合法退化），这组只做提示不判失败
        bad_parents = [pt for pt in [x["parent"] for x in res["parents"]]
                       if pt is not None and segs(pt) not in pset]
        if bad_parents:
            WARN.append("tree.parents_exist: %d 个父路径不在节点集里（样例 %r）"
                        % (len(bad_parents), bad_parents[:2]))
        trs = _re.findall(r'<tr data-path="[^"]*" data-depth="(\d+)"', res["rows"])
        check("tree.rows_rendered", len(trs) > 0, True)
        check("tree.depths_present", all(d.isdigit() for d in trs), True)
        check("tree.sel_columns", _re.search(r"data-cost=", res["rows"]) is not None, True)
        # 行数 == 可见节点数（折叠状态默认展开）
        check("tree.rows_count", len(trs), res["nNodes"])

        # ---- 标红阈值（S.hotCost / S.hotPeak）：只上色，不过滤、不改数字 ----
        def norm_hot(h):
            """把标红相关的 class 抹掉。抹完必须与原样逐字相同 → 证明标红只碰颜色。"""
            def _c(m):
                keep = [x for x in m.group(1).split()
                        if x not in ("hot", "hotcost", "hotpeak")]
                return 'class="%s"' % " ".join(keep)
            return _re.sub(r'class="([^"]*)"', _c, h)

        okH9, resH9 = rh.check_browser_with(out, "S.hotCost=1e-9;S.hotPeak=0;render();")
        if okH9:
            check("hot.cost_cell_marked", 'class="hotcost"' in resH9["rows"], True)
            check("hot.row_marked",
                  _re.search(r'class="[^"]*\bhot\b', resH9["rows"]) is not None, True)
            check("hot.numbers_untouched", norm_hot(resH9["rows"]), norm_hot(res["rows"]))
            check("hot.kpi_untouched", resH9["kpi"], res["kpi"])
        okH10, resH10 = rh.check_browser_with(out, "S.hotCost=0;S.hotPeak=1;render();")
        if okH10:
            check("hot.peak_cell_marked", 'class="hotpeak"' in resH10["rows"], True)
            check("hot.peak_only_marks_peak_rows",
                  len(_re.findall(r'class="hotcost"', resH10["rows"])), 0)
        okH11, resH11 = rh.check_browser_with(out, "S.hotCost=1e9;S.hotPeak=200;render();")
        if okH11:
            check("hot.off_when_threshold_high",
                  (("hotcost" in resH11["rows"]) or ("hotpeak" in resH11["rows"])
                   or (_re.search(r'class="[^"]*\bhot\b', resH11["rows"]) is not None)), False)

        # ---- 「只标红最下层」：没有下级、或下级被折叠的行 ----
        # 判据直接从渲染出的 HTML 反推：有子节点的行带 ▼/▶，没有的带 ·。
        # 所以「该标」= 无 ▼（叶子）或 带 ▶（折叠着，此刻它就是最下层）。
        def hot_rows(html):
            out = []
            for row in _re.findall(r"<tr .*?</tr>", html, _re.S):
                marked = _re.search(r'class="[^"]*\bhot\b', row) is not None
                bottom = ("▼" not in row) or ("▶" in row)
                out.append((marked, bottom, row))
            return out

        okH12, resH12 = rh.check_browser_with(
            out, "S.hotCost=1e-9;S.hotPeak=0;S.hotLeafOnly=true;render();")
        if okH12:
            pairs = hot_rows(resH12["rows"])
            check("hot.leafonly_rows_rendered", len(pairs) > 0, True)
            check("hot.leafonly_matches_bottom",
                  [m for m, b, _ in pairs if m != b], [])
            check("hot.leafonly_not_all",
                  len([1 for m, _, _ in pairs if m]) < len(pairs), True)
        okH13, resH13 = rh.check_browser_with(
            out, "S.hotCost=1e-9;S.hotPeak=0;S.hotLeafOnly=false;render();")
        if okH12 and okH13:
            off = hot_rows(resH13["rows"])
            check("hot.leafonly_off_marks_parents",
                  len([1 for m, b, _ in off if m and not b]) > 0, True)
            check("hot.leafonly_on_marks_fewer",
                  len([1 for m, _, _ in off if m]) >=
                  len([1 for m, _, _ in hot_rows(resH12["rows"]) if m]), True)
        # 折叠一个父节点后，它自己必须变成"最下层"并被标红（子行同时消失）
        okH14, resH14 = rh.check_browser_with(
            out, ("S.hotCost=1e-9;S.hotPeak=0;S.hotLeafOnly=true;"
                  "var t=getTree(),p=nodesOf(t).find(function(n){return n.children.length>0;});"
                  "S.collapsed=new Set([p.path]);render();"))
        if okH14:
            cpath = (resH14["collapsedList"] or [""])[0]
            tag = _re.search(r'<tr data-path="' + _re.escape(cpath) + r'"[^>]*>',
                             resH14["rows"])
            check("hot.leafonly_collapsed_parent_marked",
                  (tag is not None and
                   _re.search(r'class="[^"]*\bhot\b', tag.group(0)) is not None), True)

        # ---- 预设查看模式：点一下只改它列出的键，其余一律不动 ----
        check("preset.options_rendered",
              ('value="0"' in res["preset"] and 'value="4"' in res["preset"]
               and "上次状态" in res["preset"] and "甲档" in res["preset"]
               and "丙档" in res["preset"]), True)

        # 配置文件本身：正常行读进来、坏格只报警不崩、缺文件也不影响生成
        ps, pwarns = rh.load_presets(pfile)
        check("presets.file_parsed", len(ps), 4)
        check("presets.file_entry1",
              [ps[0]["dims"], ps[0]["range"], ps[0].get("sortOf"),
               ps[0]["hotCost"], ps[0]["hotPeak"], ps[0]["hotLeafOnly"]],
              [["workspace", "session", "turn"], "today", ["ts_desc"], 0.5, 0.0, True])
        check("presets.file_warns", len(pwarns), 4)
        check("presets.missing_file_ok", rh.load_presets(os.path.join(tmp, "nope.txt"))[0], [])
        check("presets.dirty_kept_partial", ps[3]["dims"], ["day"])

        okP1, resP1 = rh.check_browser_with(out, """
          S.mainOnly=true; S.withReplay=true; S.sortOf=['cost','tokens','calls'];
          S.view='map'; S.kw='zzz'; S.hotCost=9; S.hotPeak=9; S.hotLeafOnly=false;
          applyPreset(PRESETS.filter(function(p){return p.id===2;})[0]);
          DATA.__probe = {dims:S.dims.slice(), sortOf:S.sortOf.slice(), view:S.view, kw:S.kw,
            mainOnly:S.mainOnly, withReplay:S.withReplay,
            hot:[S.hotCost,S.hotPeak,S.hotLeafOnly], fromTo:[S.from,S.to],
            lo:DATA.range[0], latest:latestDay(),
            keys:(function(){ try { return Object.keys(
                    JSON.parse(localStorage.getItem('dshTokenStats.view')||'{}')); }
                  catch(e){ return ['ERR']; } })()};
        """)
        if okP1:
            p = resP1["probe"]
            check("preset.2_dims", p["dims"], ["day", "workspace", "session"])
            check("preset.2_hot", p["hot"], [1, 50, True])
            check("preset.2_sorts_by_cost", p["sortOf"], ["cost", "cost", "cost"])
            check("preset.2_full_range", p["fromTo"], [p["lo"], p["latest"]])
            check("preset.2_leaves_view_alone", p["view"], "map")
            check("preset.2_leaves_keyword_alone", p["kw"], "zzz")
            check("preset.2_leaves_filters_alone", [p["mainOnly"], p["withReplay"]], [True, True])
            check("persist.saved_keys_exact", sorted(p["keys"]),
                  sorted(["dims", "sortOf", "hotCost", "hotPeak", "hotLeafOnly",
                          "mainOnly", "withReplay", "withAnomaly", "view", "showRaw",
                          "dark", "showRb", "workspaces"]))
            check("persist.no_date_no_keyword",
                  [k for k in p["keys"] if k in ("from", "to", "kw", "collapsed", "selected")], [])

        # 档位 1：日期收到"数据最后一天"、按时间倒序；档位 3 的排序只写一个值 → 三层都用它
        okP2, resP2 = rh.check_browser_with(out, """
          applyPreset(PRESETS.filter(function(p){return p.id===1;})[0]);
          var a = {dims:S.dims.slice(), sortOf:S.sortOf.slice(), hot:[S.hotCost,S.hotPeak,S.hotLeafOnly],
                   fromTo:[S.from,S.to], latest:latestDay()};
          applyPreset(PRESETS.filter(function(p){return p.id===3;})[0]);
          DATA.__probe = {a:a, b:{dims:S.dims.slice(), sortOf:S.sortOf.slice(),
                                  hot:[S.hotCost,S.hotPeak,S.hotLeafOnly],
                                  fromTo:[S.from,S.to], lo:DATA.range[0]}};
        """)
        if okP2:
            a, b = resP2["probe"]["a"], resP2["probe"]["b"]
            check("preset.1_dims", a["dims"], ["workspace", "session", "turn"])
            check("preset.1_hot", a["hot"], [0.5, 0, True])
            check("preset.1_today_only", a["fromTo"], [a["latest"], a["latest"]])
            check("preset.1_sorts_time_desc", a["sortOf"], ["ts_desc", "ts_desc", "ts_desc"])
            check("preset.3_one_sort_means_all", b["sortOf"], ["cost", "cost", "cost"])
            check("preset.3_dims", b["dims"], ["workspace", "session", "day"])
            check("preset.3_full_range", b["fromTo"], [b["lo"], a["latest"]])

        # 预设 0 = 回到"刚打开时"：状态回退，且渲染结果与打开时逐字相同
        okP3, resP3 = rh.check_browser_with(out, """
          var open = {dims:(S.dims||[]).slice(), hot:[S.hotCost,S.hotPeak,S.hotLeafOnly],
                      view:S.view, showRaw:!!S.showRaw, rows:$('treeRows').innerHTML};
          S.dims=['workspace','turn','none']; S.hotCost=5; S.hotPeak=5; S.hotLeafOnly=true;
          S.view='sun'; S.showRaw=true; render();
          applyPreset(PRESETS.filter(function(p){return p.id===0;})[0]);
          DATA.__probe = {dims:S.dims.slice(), hot:[S.hotCost,S.hotPeak,S.hotLeafOnly],
            view:S.view, showRaw:!!S.showRaw,
            same_rows: $('treeRows').innerHTML === open.rows,
            open_dims: open.dims, open_hot: open.hot, open_view: open.view,
            snap_keys: Object.keys(P0_SNAPSHOT)};
        """)
        if okP3:
            p = resP3["probe"]
            check("preset.0_back_to_open", [p["dims"], p["hot"], p["view"], p["showRaw"]],
                  [p["open_dims"], p["open_hot"], p["open_view"], False])
            check("preset.0_rows_identical", p["same_rows"], True)
            check("preset.0_snapshot_has_no_date",
                  [k for k in p["snap_keys"] if k in ("from", "to", "kw")], [])

        # 脏数据（旧版本/手改）不许把页面搞崩：坏的键丢掉，好的照用
        okP4, resP4 = rh.check_browser_with(out, """
          var threw=false, before=[S.hotCost,S.view];
          try { applyState({dims:['nope','day'], hotCost:'abc', hotPeak:-3, view:'weird',
                            sortOf:['nope','cost','ts'], workspaces:'oops'}); }
          catch(e){ threw=true; }
          DATA.__probe = {threw:threw, dims:S.dims.slice(), hot:[S.hotCost,S.hotPeak],
                          view:S.view, sortOf:S.sortOf.slice(),
                          ws_is_set:(S.workspaces instanceof Set), before:before};
        """)
        if okP4:
            p = resP4["probe"]
            check("state.dirty_does_not_throw", p["threw"], False)
            check("state.dirty_dims_salvaged", p["dims"], ["day", "none", "none"])
            check("state.dirty_numbers_kept_when_invalid", p["hot"], [p["before"][0], 0])
            check("state.dirty_view_ignored", p["view"], p["before"][1])
            check("state.dirty_sort_partially_kept", p["sortOf"][1:], ["cost", "ts"])

        # ---- 中断空壳：默认不显示、勾「含异常」才计入（黄标 ⚠）----
        okA1, resA1 = rh.check_browser_with(out, """
          DATA.anomaly = {n:1, cost:0.5, tokens:1000, days:[S.from], hours:[S.from+' 10时']};
          DATA.msgs.push({day:S.from, hour:10, ts:DATA.msgs[0].ts+3600000, turn_no:9,
            model:"deepseek-flash", workspace:DATA.msgs[0].workspace, depth:1,
            session_id:"session-anom", parent_id:null, in_tokens:0, out_tokens:0, cache_read:0,
            total_tokens:0, cost_est:0, cost_cache:0, cost_in:0, cost_out:0, peak:0,
            replay:1, start_ts:0, interrupted:1, est_cost:0.5, est_tokens:1000});
          S.dims=['day','workspace','session'];
          S.withAnomaly=false; render();
          var offN = currentRows().filter(function(m){return m.interrupted;}).length;
          var offC = currentRows().reduce(function(a,m){return a+(m.cost_est||0);},0);
          var offMark = $('treeRows').innerHTML.indexOf('anom-mark')>=0;
          S.withAnomaly=true; render();
          var onN = currentRows().filter(function(m){return m.interrupted;}).length;
          var onC = currentRows().reduce(function(a,m){
            return a+(m.cost_est||0)+((S.withAnomaly&&m.interrupted)?(m.est_cost||0):0);},0);
          var html = $('treeRows').innerHTML;
          DATA.__probe = {offN:offN, onN:onN, delta:+(onC-offC).toFixed(6),
            mark: html.indexOf('anom-mark')>=0, anomRow: html.indexOf('data-anom="1"')>=0,
            kpi: $('kpi').innerHTML.indexOf('中断空壳')>=0, offMark: offMark};
          S.withAnomaly=false; render();
          DATA.__probe.afterOff = $('treeRows').innerHTML.indexOf('anom-mark')>=0;
        """)
        if okA1:
            p = resA1["probe"]
            check("anomaly.hidden_by_default", [p["offN"], p["offMark"]], [0, False])
            check("anomaly.included_when_on", p["onN"], 1)
            check("anomaly.estimate_enters_total", p["delta"], 0.5)
            check("anomaly.yellow_mark", p["mark"], True)
            check("anomaly.ancestor_marked", p["anomRow"], True)
            check("anomaly.kpi_line", p["kpi"], True)
            check("anomaly.off_again_clears", p["afterOff"], False)

        # ---- 排序语义：每层独立、时间可升降序 ----
        def sib_ok(kids, key, desc):
            vals = [k[key] for k in kids]
            return all((vals[i] >= vals[i+1]) if desc else (vals[i] <= vals[i+1])
                       for i in range(len(vals)-1))

        def by_depth(sbd, depth):
            return [g["kids"] for g in sbd.values() if g["depth"] == depth]

        sb = res["sortByDepth"]
        check("sort.has_sibling_groups", len(sb) > 0, True)
        check("sort.default_all_time_asc",
              all(sib_ok(k, "ts", False) for g in sb.values() for k in [g["kids"]]), True)
        check("sort.sortOf_applied",
              rh.check_browser_with(out, "S.sortOf=['ts','cost','ts'];render();")[1]["sortOf"],
              ["ts", "cost", "ts"])

        # 顶层（天）按被指定那一档排序：夹具只有 2 天，正好验"时间可升可降 + 第 1 层生效"
        ok3, res3 = rh.check_browser_with(out, "S.sortOf=['ts_desc','ts','ts'];render();")
        if ok3:
            tops = [t["ts"] for t in res3["top"]]
            check("sort.time_desc", all(tops[i] >= tops[i+1] for i in range(len(tops)-1)), True)
        ok4, res4 = rh.check_browser_with(out, "S.sortOf=['cost','ts','ts'];render();")
        if ok4:
            tops = [t["cost"] for t in res4["top"]]
            check("sort.level1_cost_desc", all(tops[i] >= tops[i+1] for i in range(len(tops)-1)), True)

        # ---- 三个下拉各管一层（这里用「天 › 工作区 › 模型」：三层都有多个兄弟，
        #      才验得出"串位"。默认的 天›工作区›会话 里，会话是叶子、第 3 层看不出效果）----
        D3 = "S.dims=['day','workspace','model'];"
        okE, resE = rh.check_browser_with(out, D3 + "S.sortOf=['ts','ts','ts'];render();")
        if okE:
            def lvl_sorted(snap, level, key="cost", desc=True):
                """level 从 1 开始。第 1 层的父是根，只能看快照的 top；
                第 2/3 层看 sortByDepth 里 depth == level-1 的多子节点组。"""
                if level == 1:
                    vals = [t[key] for t in snap["top"]]
                    if len(vals) < 2:
                        return None
                else:
                    gs = [g["kids"] for g in snap["sortByDepth"].values()
                          if g["depth"] == level - 1]
                    if not gs:
                        return None
                    return all(
                        all((g[i][key] >= g[i+1][key]) if desc else (g[i][key] <= g[i+1][key])
                            for i in range(len(g)-1)) for g in gs)
                return all((vals[i] >= vals[i+1]) if desc else (vals[i] <= vals[i+1])
                           for i in range(len(vals)-1))
            # 每个下拉单独改成花费↓，只有它对应的那一层该变降序
            patterns = [(0, 1), (1, 2), (2, 3)]
            for idx, level in patterns:
                cfg = ["ts", "ts", "ts"]
                cfg[idx] = "cost"
                okF, resF = rh.check_browser_with(
                    out, D3 + "S.sortOf=['%s','%s','%s'];render();" % tuple(cfg))
                if okF:
                    check("levels.dropdown%d_sorts_level%d" % (idx + 1, level),
                          lvl_sorted(resF, level), True)
                    # 其它两层必须还是时间升序（按结束时间）
                    for other in (1, 2, 3):
                        if other == level:
                            continue
                        check("levels.dropdown%d_leaves_level%d_alone" % (idx + 1, other),
                              lvl_sorted(resF, other, key="end", desc=False), True)
        # ---- 折叠状态必须在换排序后依然有效（用路径而不是数字 id）----
        okG, resG = rh.check_browser_with(
            out, ("var t=getTree();var p=nodesOf(t).find(function(n){return n.children.length>1;});"
                  "S.collapsed=new Set([p.path]);render();"))
        if okG:
            check("collapse.state_kept", resG["collapsedList"], [p for p in resG["collapsedList"]])
            check("collapse.nonempty", len(resG["collapsedList"]) > 0, True)
            path_before = resG["collapsedList"][0]
            okH, resH = rh.check_browser_with(
                out, ("var t=getTree();var p=nodesOf(t).find(function(n){return n.children.length>1;});"
                      "S.collapsed=new Set([p.path]);S.sortOf=['cost','cost','cost'];render();"))
            if okH:
                check("collapse.survives_sort_change", resH["collapsedList"], [path_before])
                # 折叠的那个节点仍然在渲染出的行里（只是子节点被藏了）
                check("collapse.node_still_visible", ('data-path="%s"' % path_before) in resH["rows"], True)

        # treemap / sun 仍能跑
        ok6, res6 = rh.check_browser_with(out, "S.view='map';S.sortOf=['ts','cost','cost'];render();")
        check("map.run_ok", bool(ok6), True)
        ok7, res7 = rh.check_browser_with(out, "S.view='sun';render();")
        check("sun.run_ok", bool(ok7), True)
        # 空间筛选
        ok8, res8 = rh.check_browser_with(
            out, "S.workspaces=new Set([DATA.msgs[0].workspace]);render();")
        check("filter.run_ok", bool(ok8), True)

        # ---- 折叠：折叠一个带子节点的父节点后，可见行数应当变少 ----
        ok9, res9 = rh.check_browser_with(
            out, ("var t=getTree();"
                  "var p=nodesOf(t).find(function(n){return n.children.length>1;});"
                  "S.collapsed=new Set([p.path]);render();"))
        if ok9:
            rows9 = len(_re.findall(r"<tr data-path=", res9["rows"]))
            check("collapse.reduces_rows", rows9 < res["nNodes"], True)
        ok10, res10 = rh.check_browser_with(
            out, ("var t=getTree();"
                  "S.collapsed=new Set(nodesOf(t).filter(function(n){return n.children.length;})"
                  ".map(function(n){return n.path;}));render();"))
        if ok10:
            rows10 = len(_re.findall(r"<tr data-path=", res10["rows"]))
            check("collapse.all_folded_shows_top", rows10, res["rootChildren"])

        # ---- 显示名：有名字显示名字，没名字回落到 id，且数字绝不能变 ----
        check("labels.embedded", isinstance(res.get("labels"), dict), True)
        check("labels.has_workspace", bool(res.get("labels", {}).get("workspace")), True)
        check("labels.has_session", bool(res.get("labels", {}).get("session")), True)
        check("labels.has_turn", bool(res.get("labels", {}).get("turn")), True)
        n_cost_before = _re.findall(r'data-cost="([\d.]+)"', res["rows"])
        check("labels.name_shown", "测试会话甲" in res["rows"] or "甲工作区" in res["rows"], True)
        ok11, res11 = rh.check_browser_with(
            out, "DATA.labels={workspace:{},session:{},turn:{}};render();")
        if ok11:
            n_cost_after = _re.findall(r'data-cost="([\d.]+)"', res11["rows"])
            check("labels.removal_keeps_numbers", n_cost_after, n_cost_before)
            check("labels.removal_falls_back_id", len(res11["rows"]) > 0, True)
        # 标签表里绝不能有参与统计的字段（只有 kind/key/label/name/src/seen_at）
        cols = [r[1] for r in con().execute("PRAGMA table_info(labels)")]
        check("labels.table_is_display_only",
              set(cols) == {"kind", "key", "label", "name", "src", "seen_at"}, True)

    # 空区间也不该崩
    ok2 = rh.main(["--range", "2000-01-01..2000-01-02", "--out", os.path.join(tmp, "empty.html"),
                   "--no-check"])
    check("html.empty_range_ok", ok2, 0)


# ================================================================ D. Mnemon「记忆写入」显示名
def test_subagent_labels(tmp):
    """Mnemon 写记忆 spawn 的一次性子代理：显示名改成「记忆写入 → 空间名」，且只影响显示。"""
    import zstandard
    root = os.path.join(tmp, "remsess")
    dbfile = use_temp(tmp, "rem.db")
    pjdir = os.path.join(tmp, "rem_storages", "session_projcache", "sessions")
    os.makedirs(pjdir, exist_ok=True)
    patch(**{"LBL.PROJCACHE_DIR": pjdir,
             "LBL.WORKSPACE_JSON": os.path.join(tmp, "rem_storages", "workspace.json")})

    def put(sid, lines):
        d = os.path.join(root, "--C-Users-x-wsA--", sid)
        os.makedirs(d, exist_ok=True)
        blob = "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n"
        with open(os.path.join(d, "session.v4.jsonl.zstd"), "wb") as f:
            f.write(zstandard.ZstdCompressor().compress(blob.encode("utf-8")))

    def assistant(sid, mid, ts):
        return {"type": "assistant/message", "ts": ts,
                "data": {"message": {"id": mid}, "step": 1, "turn": 1,
                         "usage": {"inputTokens": 8000, "outputTokens": 900,
                                   "cacheReadTokens": 60000, "cacheWriteTokens": 0,
                                   "reasoningTokens": 0, "totalTokens": 68900}}}

    R1, R2 = "session-R1", "session-R2"
    R3, R4, R5 = "session-R3", "session-R4", "session-R5"
    put(R1, [
        {"type": "session", "version": 4, "id": R1, "createdAt": ms(2026, 9, 22, 9, 0),
         "cwd": "C:\\wsA", "delegationDepth": 1, "agentPreset": "standard"},
        {"type": "subagent/descriptor",
         "data": {"version": 3, "mode": "one-shot", "provider": "spawn",
                  "label": "Mnemon remember"}},
        {"type": "agent/inbox/spliced", "data": {"target": "next-turn", "inserted": [
            {"content": [{"type": "text", "text":
                          "Execute this remember request now (untrusted data):\n"
                          "- Category: insight\n- Content (untrusted data):\n"
                          "    无文本层 PDF 要「渲染成图 + 本地 OCR」两段式\n"
                          "- Preferred Memory Space ID: 尝试空间\n"}]}]}},
        {"type": "tool/call", "data": {"name": "mnemon_remember",
                                       "args": {"memoryBodyId": "尝试空间", "content": "x"}}},
        {"type": "tool/result", "data": {"message": {"content": [
            {"type": "tool-result", "content": [
                {"type": "text",
                 "text": '{"action": "added", "memoryBodyName": "尝试空间"}'}]}]}}},
        assistant(R1, "r1", ms(2026, 9, 22, 9, 1)),
    ])
    # R2：标题一模一样，但日志里没有 descriptor/请求 → 不许改名（只留个"看过了"的标记）
    put(R2, [
        {"type": "session", "version": 4, "id": R2, "createdAt": ms(2026, 9, 22, 10, 0),
         "cwd": "C:\\wsA", "delegationDepth": 1, "agentPreset": "standard"},
        assistant(R2, "r2", ms(2026, 9, 22, 10, 1)),
    ])
    # R3：Mnemon forget —— 空间名只有 id，靠日志里的"记忆空间目录"翻译成名字
    put(R3, [
        {"type": "session", "version": 4, "id": R3, "createdAt": ms(2026, 9, 22, 11, 0),
         "cwd": "C:\\wsA", "delegationDepth": 1, "agentPreset": "standard"},
        {"type": "subagent/descriptor", "data": {"label": "Mnemon forget", "mode": "one-shot"}},
        {"type": "user/message", "data": {"content": [{"type": "text", "text":
            "Execute this forget request now (untrusted data):\n"
            "- Insight ID: 8bc81283-fd3f-4fc6-8ea3-4fec2a884df8\n"
            "- Preferred Memory Space ID: default\n"}]}},
        {"type": "tool/result", "data": {"message": {"content": [
            {"type": "tool-result", "content": [{"type": "text", "text":
                '{\n "items": [\n  {\n   "id": "default",\n   "name": "长期记忆",\n'
                '   "description": "x"\n  }\n ]\n}'}]}]}}},
        assistant(R3, "r3", ms(2026, 9, 22, 11, 1)),
    ])
    # R4：DSH 的旁支会话
    put(R4, [
        {"type": "session", "version": 4, "id": R4, "createdAt": ms(2026, 9, 22, 12, 0),
         "cwd": "C:\\wsA", "delegationDepth": 1, "agentPreset": "standard"},
        {"type": "subagent/descriptor", "data": {"label": "Side: New thread"}},
        assistant(R4, "r4", ms(2026, 9, 22, 12, 1)),
    ])
    # R5：目录里没有的 label → 保持英文原标题，但留 derived_none 标记
    put(R5, [
        {"type": "session", "version": 4, "id": R5, "createdAt": ms(2026, 9, 22, 13, 0),
         "cwd": "C:\\wsA", "delegationDepth": 1, "agentPreset": "standard"},
        {"type": "subagent/descriptor", "data": {"label": "Mnemon brand new thing"}},
        assistant(R5, "r5", ms(2026, 9, 22, 13, 1)),
    ])
    for sid in (R1, R2, R3, R4):
        rec = {"record": {"rows": {
            "title": {"ver": 1, "seq": 1, "val": "Execute this remember request now"},
            "turnOutline": {"ver": 2, "seq": 1, "val": {"turns": [
                {"turn": 1, "seq": 1, "prompt": "Execute this remember request now"}]}}}}}
        with open(os.path.join(pjdir, sid + ".json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
    rec5 = {"record": {"rows": {
        "title": {"ver": 1, "seq": 1, "val": "Some unknown english title"},
        "turnOutline": {"ver": 2, "seq": 1, "val": {"turns": [
            {"turn": 1, "seq": 1, "prompt": "Some unknown english title"}]}}}}}
    with open(os.path.join(pjdir, R5 + ".json"), "w", encoding="utf-8") as f:
        json.dump(rec5, f, ensure_ascii=False)

    ss.scan(full=True, quiet=True, root=root)
    con = db.connect(dbfile)

    def lab(kind, key):
        r = con.execute("SELECT name, src FROM labels WHERE kind=? AND key=?",
                        (kind, key)).fetchone()
        return (r["name"], r["src"]) if r else None

    check("derived.session_name", lab("session", R1), ("记忆写入 → 尝试空间", "derived"))
    t = lab("turn", R1 + "|1")
    check("derived.turn_name",
          bool(t and t[1] == "derived" and t[0].startswith("记忆写入：")), True)
    check("derived.not_remember_untouched", lab("session", R2),
          ("Execute this remember request now", "projcache"))
    check("derived.forget_resolves_space", lab("session", R3),
          ("记忆删除 → 长期记忆", "derived"))
    check("derived.forget_insight_short", (lab("turn", R3 + "|1") or ("",))[0],
          "记忆删除：8bc81283")
    check("derived.side_thread", (lab("session", R4) or ("",))[0], "侧线对话")
    check("derived.unknown_label_kept", (lab("session", R5) or ("",))[0],
          "Some unknown english title")
    check("derived.unknown_marked", one(
        "SELECT COUNT(*) FROM labels WHERE kind='derived_none' AND key='%s'" % R5, con), 1)
    # projcache 又会把英文标题写回来 → 派生名必须活下来
    ss.scan(full=False, quiet=True, root=root)
    check("derived.survives_rescan", (lab("session", R1) or (None,))[0], "记忆写入 → 尝试空间")
    # 纯展示：改名前后的数字一字不动
    before = one("SELECT SUM(cost_est) FROM messages", con)
    ss.scan(full=True, quiet=True, root=root, with_labels=True)
    check("derived.numbers_untouched", one("SELECT SUM(cost_est) FROM messages", con), before)
    con.close()


# ================================================================ E. 中断空壳
def test_interrupts(tmp):
    """中断空壳：采集（keep=0、带标签与正文字数）、按官方小时桶分配、绝不进统计口径。"""
    import zstandard
    root = os.path.join(tmp, "gapsess")
    dbfile = use_temp(tmp, "gap.db")
    T10, T12, T14, T15, T16 = (ms(2026, 9, 22, 10, 0), ms(2026, 9, 22, 12, 0),
                               ms(2026, 9, 22, 14, 0), ms(2026, 9, 22, 15, 0),
                               ms(2026, 9, 22, 16, 0))
    D = "2026-09-22"

    def put(sid, lines):
        d = os.path.join(root, "--C-Users-x-wsA--", sid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "session.v4.jsonl.zstd"), "wb") as f:
            f.write(zstandard.ZstdCompressor().compress(
                ("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n").encode("utf-8")))

    def normal(mid, ts, tokens=1000000):
        return {"type": "assistant/message", "ts": ts,
                "data": {"message": {"id": mid}, "step": 1, "turn": 1,
                         "usage": {"inputTokens": tokens, "outputTokens": 0, "cacheReadTokens": 0,
                                   "cacheWriteTokens": 0, "reasoningTokens": 0,
                                   "totalTokens": tokens}}}

    def shell(mid, t, chars=0):
        # 中断空壳：没有 usage，但有 interrupted 标记 + time（不是 ts）+ 中断前正文
        return {"type": "assistant/message", "time": t,
                "data": {"message": {"id": mid, "content": [{"type": "text", "text": "x" * chars}]},
                         "step": 2, "turn": 1, "interrupted": True, "stream": []}}

    def sess(sid, t, msgs):
        return (sid, [{"type": "session", "version": 4, "id": sid, "createdAt": t,
                       "cwd": "C:\\wsA", "delegationDepth": 0, "agentPreset": "standard"}] + msgs)

    cases = [sess("session-G1", T10, [normal("g1", T10 + 60000), shell("g1-shell", T10 + 120000)]),
             sess("session-G2", T14, [normal("g2", T14 + 60000), shell("g2-shell", T14 + 120000)]),
             # 15 点：两个空壳，正文长度 300 / 100 → 应按长度加权
             sess("session-G3", T15, [shell("g3-shell", T15 + 60000, 300)]),
             sess("session-G4", T15, [shell("g4-shell", T15 + 90000, 100)]),
             # 16 点：两个空壳都没有正文 → 只能均分
             sess("session-G5", T16, [shell("g5-shell", T16 + 60000)]),
             sess("session-G6", T16, [shell("g6-shell", T16 + 90000)])]
    for sid, lines in cases:
        put(sid, lines)

    con = db.connect(dbfile)
    now = 1790000000          # fetched_at 只是记录用，测试里给个定值

    def off(hour, cost, tokens, key="harness", req=10):
        con.execute("INSERT INTO official_hour (day,hour,api_key_id,api_key_name,model,cache_hit,"
                    "cache_miss,output,requests,cost,currency,fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (D, hour, key + "-id", key, "deepseek-flash", 0, 0, tokens, req, cost, "CNY", now))

    off(10, 9.0, 9000000)          # 差额 +7.0 → 该给 10 点那个空壳
    off(10, 100.0, 0, key="codex")  # 别的 key（codex）的钱 → 必须被排除
    off(12, 5.0, 5000000, req=0)   # 次数对得上(0)、只有钱对不上 → 报出来，不当空转
    off(14, 0.1, 100)              # 差额为负 → 留空
    off(15, 4.0, 4000000)          # 两个空壳 → 按正文长度加权 3:1
    off(16, 2.0, 2000000)          # 两个空壳、无正文 → 均分
    con.commit()

    ss.scan(full=True, recon=True, quiet=True, root=root)
    q = lambda sql, *a: con.execute(sql, a).fetchone()

    def cell(mid, col):
        return q("SELECT %s FROM messages WHERE msg_id=?" % col, mid)[0]

    check("gap.shell_stored", q("SELECT COUNT(*) FROM messages WHERE interrupted=1")[0], 6)
    check("gap.shell_flags",
          (cell("g1-shell", "keep"), cell("g1-shell", "interrupted"),
           cell("g1-shell", "total_tokens"), cell("g1-shell", "cost_est")), (0, 1, 0, 0.0))
    check("gap.shell_uses_time_field", (cell("g1-shell", "day"), cell("g1-shell", "hour")),
          ("2026-09-22", 10))
    check("gap.shell_chars", cell("g3-shell", "chars"), 300)
    # 只认 harness：codex 那 100 元绝不能进来（9.0−2.0 = 7.0）
    check("gap.estimate_cost", round(cell("g1-shell", "est_cost"), 6), 7.0)
    check("gap.estimate_tokens", cell("g1-shell", "est_tokens"), 8000000)
    check("gap.note_single", cell("g1-shell", "est_note"), "同一小时差额（唯一中断）")
    check("gap.negative_gap_not_estimated",
          (cell("g2-shell", "est_cost"), cell("g2-shell", "est_tokens")), (0.0, 0))
    # 15 点：按正文长度加权（300:100 → 3:1）
    check("gap.weighted_cost", (round(cell("g3-shell", "est_cost"), 6),
                                round(cell("g4-shell", "est_cost"), 6)), (3.0, 1.0))
    check("gap.weighted_tokens", (cell("g3-shell", "est_tokens"), cell("g4-shell", "est_tokens")),
          (3000000, 1000000))
    check("gap.note_weighted", "按正文长度加权" in (cell("g3-shell", "est_note") or ""), True)
    # 16 点：没有正文 → 均分，并把方式写清楚
    check("gap.even_split", (round(cell("g5-shell", "est_cost"), 6),
                             round(cell("g6-shell", "est_cost"), 6)), (1.0, 1.0))
    check("gap.note_even", "均分" in (cell("g5-shell", "est_note") or ""), True)
    # 12 点那 5 元没有中断可对应 → 只报出来（不平账）
    gs = db.gap_summary(con)
    check("gap.unmatched_reported", round(gs["unmatched"], 6), 5.0)
    check("gap.official_total", round(gs["cost"], 6), 20.1)     # 9.0+5.0+0.1+4.0+2.0（codex 不算）
    rows12 = [r for r in gs["rows"] if r["hour"] == 12]
    check("gap.row_has_no_shell", (rows12[0]["shells"], rows12[0]["allocated"]), (0, 0.0))
    # 口径：空壳永远不进 keep=1
    check("gap.kept_sum_ignores_shells",
          round(q("SELECT SUM(cost_est) FROM messages WHERE keep=1")[0], 6), 4.0)
    check("gap.no_shell_in_kept",
          q("SELECT COUNT(*) FROM messages WHERE keep=1 AND interrupted=1")[0], 0)
    # 对账日志（给人看的那种）确实写出来了
    log = ss.write_recon_log(con, os.path.join(tmp, "recon.log"), quiet=True)
    check("gap.log_written", os.path.exists(log["path"]), True)
    txt = open(log["path"], encoding="utf-8").read()
    check("gap.log_has_rows", ("  12h" in txt and "  15h" in txt), True)
    check("gap.log_has_unmatched", "对不上" in txt, True)
    # 估算可重放：再跑一次增量，值不变
    ss.scan(full=False, recon=True, quiet=True, root=root)
    check("gap.estimate_stable",
          (round(cell("g1-shell", "est_cost"), 6), cell("g1-shell", "est_tokens")), (7.0, 8000000))
    # ---- 同一次中断的 fork 副本：只算一份、只在最早那份上恢复 ----
    T17 = ms(2026, 9, 22, 17, 0)
    put("session-G7", [{"type": "session", "version": 4, "id": "session-G7", "createdAt": T17,
                        "cwd": "C:\\wsA", "delegationDepth": 0, "agentPreset": "standard"},
                       shell("shared-shell", T17 + 10000, 100)])
    put("session-G8", [{"type": "session", "version": 4, "id": "session-G8", "createdAt": T17 + 60000,
                        "cwd": "C:\\wsA", "delegationDepth": 0, "agentPreset": "standard"},
                       shell("shared-shell", T17 + 70000, 100)])
    off(17, 1.0, 1000000)
    con.commit()
    ss.scan(full=True, recon=True, quiet=True, root=root)
    rows17 = list(con.execute("SELECT session_id, replayed, ROUND(est_cost,6) c, est_note n "
                              "FROM messages WHERE msg_id='shared-shell' ORDER BY ts"))
    check("gap.copy_counted_once", len(rows17), 2)
    check("gap.copy_earliest_gets_it", (rows17[0]["replayed"], rows17[0]["c"]), (0, 1.0))
    check("gap.copy_skipped", (rows17[1]["replayed"], rows17[1]["c"]), (1, 0.0))
    check("gap.copy_note", "重复副本" in (rows17[1]["n"] or ""), True)
    gs2 = db.gap_summary(con)
    row17 = [r for r in gs2["rows"] if r["hour"] == 17]
    check("gap.copy_hour_unique", row17[0]["shells"], 1)
    check("gap.copy_total_once", round(row17[0]["allocated"], 6), 1.0)
    con.close()


# ================================================================ 节假日表
def test_holidays(tmp):
    """节假日表：放假 / 调休上班 / 超有效期回落 / 坏行与缺文件容错"""
    HOL.reset()
    real = HOL.load(force=True)
    check("hol.real_off_0925", real.kind("2026-09-25"), "off")
    check("hol.real_work_0920", real.kind("2026-09-20"), "work")
    check("hol.real_range_from", real.valid_from, "2026-01-01")
    check("hol.real_range_to", real.valid_to, "2026-12-31")
    check("hol.real_counts", (len(real.off), len(real.work)), (33, 6))
    check("hol.off_holiday_morning", real.is_peak(HOL.from_datestr("2026-09-25", 10)), False)
    check("hol.work_day_morning", real.is_peak(HOL.from_datestr("2026-09-21", 10)), True)
    # 调休上班日：DS 不认调休 → 照普通周末算（非高峰）
    check("hol.work_extra_sunday_off", real.is_peak(HOL.from_datestr("2026-09-20", 10)), False)
    check("hol.weekend", real.is_peak(HOL.from_datestr("2026-09-19", 10)), False)
    check("hol.window_close_12", real.is_peak(HOL.from_datestr("2026-09-21", 12)), False)
    check("hol.window_open_14", real.is_peak(HOL.from_datestr("2026-09-21", 14)), True)
    check("hol.national_day", real.is_peak(HOL.from_datestr("2026-10-01", 10)), False)
    check("hol.spring_work_sat_off", real.is_peak(HOL.from_datestr("2026-02-14", 10)), False)
    check("hol.national_work_sat_off", real.is_peak(HOL.from_datestr("2026-10-10", 10)), False)
    check("hol.no_stale_hint", real.update_hint(), "")

    # 超有效期 → 回落"只看周末"，并给"该更新表了"的提示
    p = os.path.join(tmp, "holidays_fixture.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 【有效期】2026-01-01 ~ 2026-03-31\n"
                "2026-01-05 | off  | 测试放假（周一）\n"
                "2026-01-10 | work | 测试调休（周六）\n")
    HOL.reset()
    t = HOL.load(p)
    check("hol.fx_off", t.is_peak(HOL.from_datestr("2026-01-05", 10)), False)
    check("hol.fx_work_sat_off", t.is_peak(HOL.from_datestr("2026-01-10", 10)), False)
    check("hol.fx_covers_edge", t.covers("2026-03-31"), True)
    check("hol.fx_out_covers", t.covers("2026-04-01"), False)
    check("hol.fx_out_friday_peak", t.is_peak(HOL.from_datestr("2026-04-03", 10)), True)
    check("hol.fx_out_sat_off", t.is_peak(HOL.from_datestr("2026-04-04", 10)), False)
    check("hol.fx_hint_after_out", bool(t.update_hint()), True)

    # 坏行 / 没写有效期 / 文件不存在：只 warning，不崩、回落到老规则
    p2 = os.path.join(tmp, "holidays_bad.txt")
    with open(p2, "w", encoding="utf-8") as f:
        f.write("# 【有效期】2026-01-01 ~ 2026-12-31\n"
                "这不是一行合法的记录\n"
                "2026-13-45 | off | 日期不合法\n")
    HOL.reset()
    t2 = HOL.load(p2)
    check("hol.bad_rows_warn", len(t2.warnings), 2)
    check("hol.bad_empty", len(t2.off), 0)
    check("hol.bad_fallback", t2.is_peak(HOL.from_datestr("2026-01-05", 10)), True)
    p3 = os.path.join(tmp, "holidays_norange.txt")
    with open(p3, "w", encoding="utf-8") as f:
        f.write("# 忘了写有效期\n2026-01-05 | off | 测试放假\n")
    HOL.reset()
    t3 = HOL.load(p3)
    check("hol.norange_warn", len(t3.warnings), 1)
    check("hol.norange_covers_any", t3.covers("2030-01-01"), True)
    HOL.reset()
    t4 = HOL.load(os.path.join(tmp, "nope_holidays.txt"))
    check("hol.missing_warn", len(t4.warnings), 1)
    check("hol.missing_fallback", t4.is_peak(HOL.from_datestr("2026-01-05", 10)), True)
    HOL.reset()


# ================================================================ F. 高峰倍率自校准
def test_official_peak(tmp):
    """自校准（**实际开销以校验为准**）：两个候选取更近 → 改 messages.peak / cost_est；
    判不出来、没有官方数据、**未知价目**、中断空壳一律不动；别的 key 必须排除。"""
    dbfile = use_temp(tmp, "cal.db")
    con = db.connect(dbfile)

    def put(sid, day, hour, peak, cost, interrupted=0, price_row=3, model="deepseek-flash"):
        ts = ms(*[int(x) for x in day.split("-")], hour, 0)
        con.execute(
            "INSERT INTO messages (session_id,msg_id,seq,ts,day,hour,model,provider,in_tokens,"
            "out_tokens,cache_read,cache_write,reasoning,total_tokens,peak,cost_est,price_row,"
            "keep,replayed,interrupted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, sid + "-m", 1, ts, day, hour, model, "deepseek", 1000000, 0, 0, 0,
             0, 1000000, peak, cost, price_row, 1, 0, interrupted))

    def off(day, hour, cost, tokens=1000000, key="harness"):
        con.execute("INSERT INTO official_hour (day,hour,api_key_id,api_key_name,model,cache_hit,"
                    "cache_miss,output,requests,cost,currency,fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (day, hour, key + "-id", key, "deepseek-flash", 0, tokens, 0, 5, cost,
                     "CNY", 1790000000))

    # 3 号价：1e6 未命中 → 谷 1 元 / 峰 2 元
    put("s1", "2026-09-25", 15, 1, 2.0)      # 规则说高峰（周五 15 点）
    off("2026-09-25", 15, 1.0)               # 官方 1.0 → 更接近谷价 → 应按非高峰
    put("s2", "2026-09-26", 10, 0, 1.0)      # 规则说非高峰（周六）
    off("2026-09-26", 10, 2.0)               # 官方 2.0 → 更接近峰价 → 应按高峰
    put("s3", "2026-09-26", 11, 0, 1.0)      # 官方 0.5：两个候选都差 0.5 → 不判，不许动
    off("2026-09-26", 11, 0.5)
    put("s4", "2026-09-26", 12, 0, 1.0)      # 没有官方数据 → 不许动
    put("s6", "2026-09-26", 13, 0, -1, price_row=-1)   # 未知价目 → 不参与判定、也不许动
    off("2026-09-26", 13, 1.0)
    put("s5", "2026-09-25", 15, 0, 0.0, interrupted=1)   # 中断空壳 → 不参与
    off("2026-09-25", 15, 1.0, key="codex")  # 别的 key（codex）→ **必须被排除**
    con.commit()

    res = db.apply_official_peak(con, quiet=True)

    def cell(mid, col):
        return con.execute("SELECT %s FROM messages WHERE msg_id=?" % col, (mid,)).fetchone()[0]

    check("cal.hours", res["hours"], 4)
    check("cal.decided", res["decided"], 2)
    check("cal.abstain", res["abstain"], 2)
    check("cal.changed_rows", res["changed_rows"], 2)
    check("cal.peak_to_off", (cell("s1-m", "peak"), cell("s1-m", "cost_est")), (0, 1.0))
    check("cal.off_to_peak", (cell("s2-m", "peak"), cell("s2-m", "cost_est")), (1, 2.0))
    check("cal.abstain_kept", (cell("s3-m", "peak"), cell("s3-m", "cost_est")), (0, 1.0))
    check("cal.no_official_kept", (cell("s4-m", "peak"), cell("s4-m", "cost_est")), (0, 1.0))
    check("cal.unknown_row_kept", (cell("s6-m", "peak"), cell("s6-m", "cost_est")), (0, -1))
    check("cal.interrupted_kept", cell("s5-m", "peak"), 0)
    row = con.execute("SELECT * FROM official_peak WHERE day='2026-09-25' AND hour=15").fetchone()
    check("cal.table_actual", row["peak_actual"], 0)
    check("cal.table_rule", row["rule_peak"], 1)
    check("cal.table_row_id", row["row_id"], 3)
    check("cal.table_abstain_null", con.execute(
        "SELECT peak_actual FROM official_peak WHERE day='2026-09-26' AND hour=11").fetchone()[0],
        None)
    res2 = db.apply_official_peak(con, quiet=True)
    check("cal.idempotent", res2["changed_rows"], 0)
    check("cal.rows_written", con.execute("SELECT COUNT(*) FROM official_peak").fetchone()[0], 4)
    con.close()


def test_price_nearest(tmp):
    """峰价**不是谷价两倍**时也要能判：两个候选取更近（不再硬编码 ×2）。"""
    tbl = PRICES.parse("7 | test-model | | | yes | 1,1,1 | 3,3,3 | 峰价是谷价的 3 倍\n", "inline")
    dbfile = use_temp(tmp, "near.db")
    con = db.connect(dbfile)
    ts = ms(2026, 9, 26, 10, 0)
    con.execute(
        "INSERT INTO messages (session_id,msg_id,seq,ts,day,hour,model,provider,in_tokens,"
        "out_tokens,cache_read,cache_write,total_tokens,peak,cost_est,price_row,keep,replayed) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("s", "m", 1, ts, "2026-09-26", 10, "test-model", "", 1000000, 0, 0, 0, 1000000,
         0, 1.0, 7, 1, 0))
    con.execute("INSERT INTO official_hour (day,hour,api_key_id,api_key_name,model,cache_hit,"
                "cache_miss,output,requests,cost,currency,fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("2026-09-26", 10, "h-id", "harness", "test-model", 0, 1000000, 0, 3, 3.0,
                 "CNY", 1790000000))
    con.commit()
    res = db.apply_official_peak(con, quiet=True, table=tbl)
    row = con.execute("SELECT peak, cost_est FROM messages WHERE msg_id='m'").fetchone()
    check("near.decided", res["decided"], 1)
    check("near.picked_peak", (row["peak"], round(row["cost_est"], 6)), (1, 3.0))
    check("near.row_id", con.execute("SELECT row_id FROM official_peak").fetchone()[0], 7)
    con.close()


def test_unknown_price(tmp):
    """未知价目（price_row=-1 / cost_est=-1）：**钱一律不计**，token 照常统计。"""
    day = "2026-09-26"
    dbfile = use_temp(tmp, "unk.db")
    con = db.connect(dbfile)

    def put(sid, hour, cost, price_row, tokens=1000000):
        ts = ms(2026, 9, 26, hour, 0)
        con.execute(
            "INSERT INTO messages (session_id,msg_id,seq,ts,day,hour,model,provider,in_tokens,"
            "out_tokens,cache_read,cache_write,total_tokens,peak,cost_est,price_row,keep,replayed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, sid + "-m", 1, ts, day, hour, "deepseek-something-new", "", tokens, 0, 0, 0,
             tokens, 0, cost, price_row, 1, 0))

    put("sA", 10, 1.0, 3)            # 正常：按 3 号价，1 元
    put("sB", 11, -1, -1)            # 未知价目：钱记 -1
    con.execute("INSERT INTO sessions (session_id,workspace,cwd,depth,created_at) "
                "VALUES ('sA','w','c',0,0),('sB','w','c',0,0)")
    con.commit()

    db.rebuild_session_aggs(con)
    check("unk.session_cost", con.execute(
        "SELECT cost_est FROM sessions WHERE session_id='sA'").fetchone()[0], 1.0)
    check("unk.session_cost_unknown", con.execute(
        "SELECT cost_est FROM sessions WHERE session_id='sB'").fetchone()[0], 0.0)
    check("unk.session_calls", con.execute(
        "SELECT calls FROM sessions WHERE session_id='sB'").fetchone()[0], 1)
    db.sync_ledger(con)
    db.rebuild_day_recon(con)
    check("unk.day_recon_cost", con.execute(
        "SELECT scan_cost FROM day_recon WHERE day=?", (day,)).fetchone()[0], 1.0)
    rows = {r["hour"]: r["ours"] for r in db.gap_summary(con)["rows"]}
    check("unk.gap_ours_known", rows.get(10), 1.0)
    check("unk.gap_ours_unknown_absent", rows.get(11), None)
    check("unk.summary", db.unknown_price_summary(con), {"calls": 1, "tokens": 1000000})
    data = rh.build_data(day, day)
    check("unk.build_daily_cost", data["daily"][0]["cost"], 1.0)
    check("unk.build_daily_unknown", data["daily"][0]["unknown"], 1)
    check("unk.build_counter", data["unknown_price"]["calls"], 1)
    check("unk.build_price_rows", len(data["prices"]["rows"]) >= 1, True)
    # 界面：KPI 里要点出"未知价目"，金额那一格要带问号（不能被当成 0 元混过去）
    out = os.path.join(tmp, "unk.html")
    rh.render(day, day, out, data=data)
    ok, res = rh.check_browser_with(out, "")
    check("unk.html_runs", ok, True)
    if ok:
        check("unk.html_kpi_mentions", "未知价目" in res["kpi"], True)
        check("unk.html_row_mark", 'class="unk-mark"' in res["rows"], True)
        check("unk.html_row_title", "未知价目：金额未计入" in res["rows"], True)
    con.close()


def test_prices(tmp):
    """价目表：(模型, 时刻) 匹配 / 生效区间 / 未知 / 重叠不猜 / 坏行 / 缺文件兜底"""
    PRICES.reset()
    real = PRICES.load(force=True)
    check("pr.real_rows", len(real.rows), 5)
    check("pr.real_checked", real.checked, "2026-09-28")
    check("pr.flash_before", PRICES.lookup("deepseek-flash", PRICES._ts("2026-09-10 10:00"))[1], 1)
    check("pr.flash_after", PRICES.lookup("deepseek-flash", PRICES._ts("2026-09-28 10:00"))[1], 3)
    check("pr.pro_before", PRICES.lookup("deepseek-v4-pro", PRICES._ts("2026-09-10 10:00"))[1], 2)
    check("pr.pro_after", PRICES.lookup("deepseek-v4-pro", PRICES._ts("2026-09-28 10:00"))[1], 3)
    check("pr.unknown_model",
          PRICES.lookup("deepseek-brand-new", PRICES._ts("2026-09-28 10:00"))[1], -1)
    # 生效止**不含**边界那一刻（和 DSH 的计价切换点一致）
    edge = PRICES._ts("2026-09-14 12:00")
    check("pr.boundary_is_new_row", PRICES.lookup("deepseek-flash", edge)[1], 3)
    check("pr.boundary_minus_1ms", PRICES.lookup("deepseek-flash", edge - 1)[1], 1)
    # 价格：谷 / 峰 / 缓存命中单价
    r3 = real.by_id(3)
    check("pr.cost_offpeak", round(real.cost(r3, out_tok=1000000), 6), 4.0)
    check("pr.cost_peak", round(real.cost(r3, out_tok=1000000, peak=True), 6), 8.0)
    check("pr.cost_cache", round(real.cost(r3, cache_read=1000000), 6), 0.02)

    # 重叠 = 不许猜（判未知）；坏行 / 没给峰价的档 -> warning
    p = os.path.join(tmp, "prices_bad.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 【本表核对到】2026-01-01\n"
                "5 | m1 | | | yes | 0.02,1,4 | 0.04,2,8 | 第一档\n"
                "5 | m1 | | | yes | 0.02,1,4 | 0.04,2,8 | 与上面重叠\n"
                "6 | m2 | | | no  | 1,2,3 | | 不分峰谷\n"
                "乱七八糟的一行\n"
                "7 | m3 | | | yes | 1,2,3 | | 说了有峰谷却没给峰价\n")
    PRICES.reset()
    t = PRICES.load(p)
    check("pr.bad_warns", len(t.warnings), 2)      # 乱行 1 条 + "说了有峰谷却没给峰价" 1 条
    check("pr.overlap_unknown", t.match("m1", PRICES._ts("2026-05-01")), None)
    check("pr.no_peak_row_off_only",
          round(t.cost(t.by_id(6), out_tok=1000000, peak=True), 6), 3.0)
    check("pr.peak_missing_falls_back",
          round(t.cost(t.by_id(7), out_tok=1000000, peak=True), 6), 3.0)
    # 缺文件 → 内置兜底档（全记 1 号），保证看板不会空
    PRICES.reset()
    t2 = PRICES.load(os.path.join(tmp, "no_such_prices.txt"))
    check("pr.missing_fallback", t2.fallback, True)
    check("pr.missing_row",
          PRICES.lookup("deepseek-flash", PRICES._ts("2026-09-28 10:00"), table=t2)[1], 1)
    PRICES.reset()


# ================================================================ G. 具体 token 数三列
def test_rawcols(tmp):
    """「显示具体 token 数」开关真的切得到类 + 空壳那三列显示横杠（不是 0）。"""
    day = "2026-09-27"
    dbfile = use_temp(tmp, "raw.db")
    con = db.connect(dbfile)

    def put(sid, turn, in_tok, out_tok, cache, cost, interrupted=0):
        ts = ms(2026, 9, 27, 10, turn)
        con.execute(
            "INSERT INTO messages (session_id,msg_id,seq,ts,day,hour,turn_no,step_no,model,"
            "provider,in_tokens,out_tokens,cache_read,cache_write,total_tokens,peak,cost_est,"
            "price_row,keep,replayed,interrupted) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "%s-%d" % (sid, turn), turn, ts, day, 10, turn, 1, "deepseek-flash", "",
             in_tok, out_tok, cache, 0, in_tok + out_tok + cache, 0, cost, 3, 1, 0, interrupted))

    put("sA", 1, 1000, 100, 5000, 0.02)          # 正常调用
    put("sA", 2, 0, 0, 0, 0.0, interrupted=1)    # 空壳，且没分配（0 元）
    con.execute("INSERT INTO sessions (session_id,workspace,cwd,depth,created_at) "
                "VALUES ('sA','--C-Users-x-wsZ--','C:\\\\wsZ',0,0)")
    con.commit()

    data = rh.build_data(day, day)
    out = os.path.join(tmp, "raw.html")
    rh.render(day, day, out, data=data)
    ok, res = rh.check_browser_with(
        out, 'S.dims=["workspace","session","turn"];S.withAnomaly=true;render();')
    check("raw.html_runs", ok, True)
    if ok:
        dash = ('<td class="rawcol">—</td><td class="rawcol">—</td>'
                '<td class="rawcol">—</td>')
        check("raw.shell_three_dashes", dash in res["rows"], True)
        check("raw.normal_numbers", '<td class="rawcol">5,000</td>' in res["rows"], True)
        # 开关必须落在 render() 里（曾经的 bug：只在 refreshControls 里切类，点复选框没反应）
        ok2, res2 = rh.check_browser_with(out, """
            S.showRaw = true;  render();
            DATA.labels.__off_show = document.querySelector("table.t").classList.contains("rawcol-off");
            S.showRaw = false; render();
            DATA.labels.__off_hide = document.querySelector("table.t").classList.contains("rawcol-off");
        """)
        check("raw.toggle_js_ok", ok2, True)
        if ok2:
            check("raw.toggle_class_on", res2["labels"]["__off_show"], False)
            check("raw.toggle_class_off", res2["labels"]["__off_hide"], True)
    con.close()


# ================================================================ H. 分钟级时间范围
def test_minutes(tmp):
    """时间细分到分钟：默认 00:00~23:59（整天）、结束分钟整分钟算进去、
    改分钟会重画树（treeKey 别漏）、时间/空间各占一行。"""
    d1, d2 = "2026-09-27", "2026-09-28"
    dbfile = use_temp(tmp, "min.db")
    con = db.connect(dbfile)

    def put(sid, day, hh, mm, ss=0):
        ts = ms(*[int(x) for x in day.split("-")], hh, mm) + ss * 1000
        con.execute(
            "INSERT INTO messages (session_id,msg_id,seq,ts,day,hour,turn_no,step_no,model,"
            "provider,in_tokens,out_tokens,cache_read,cache_write,total_tokens,peak,cost_est,"
            "price_row,keep,replayed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "%s-%d%d" % (sid, hh, mm), 1, ts, day, hh, 1, 1, "deepseek-flash", "",
             1000, 100, 5000, 0, 6100, 0, 0.02, 3, 1, 0))

    put("sA", d1, 10, 15)          # 10:15
    put("sB", d1, 9, 0)            # 09:00
    put("sB", d1, 23, 59, 30)      # 23:59:30（考"结束那一分钟整分钟都算进去"）
    put("sC", d2, 0, 30)           # 次日 00:30
    con.execute("INSERT INTO sessions (session_id,workspace,cwd,depth,created_at) "
                "VALUES ('sA','--C-Users-x-wsM--','C:\\\\wsM',0,0),"
                "('sB','--C-Users-x-wsM--','C:\\\\wsM',0,0),"
                "('sC','--C-Users-x-wsM--','C:\\\\wsM',0,0)")
    con.commit()

    data = rh.build_data(d1, d2)
    out = os.path.join(tmp, "min.html")
    rh.render(d1, d2, out, data=data)          # 注意：render() 返回的是路径，不是 HTML
    with open(out, encoding="utf-8") as f:
        html = f.read()
    # 布局：时间、空间各占一行；两个分钟输入框在
    i, j = html.index("<span>时间：</span>"), html.index("<span>空间：</span>")
    seg = html[i:j]
    check("min.layout_two_rows", ("</div>" in seg) and ('<div class="row"' in seg), True)
    check("min.has_from_time", 'id="fromTime"' in html, True)
    check("min.has_to_time", 'id="toTime"' in html, True)
    check("min.default_from_0000", 'id="fromTime"' in html and 'value="00:00"' in html, True)
    check("min.default_to_2359", 'id="toTime"' in html and 'value="23:59"' in html, True)

    ok, res = rh.check_browser_with(out, """
        DATA.labels.__n_all = currentRows().length;
        DATA.labels.__head = $("head").innerHTML;
        // 全走真实的 onchange 通路：日期 + 分钟
        $("from").onchange({target:{value:"2026-09-27"}});
        $("to").onchange({target:{value:"2026-09-27"}});
        $("fromTime").onchange({target:{value:"10:00"}});
        $("toTime").onchange({target:{value:"10:20"}});
        DATA.labels.__n_narrow = currentRows().length;
        DATA.labels.__rows_narrow = $("treeRows").innerHTML;
        DATA.labels.__times = S.fromTime + "~" + S.toTime;
        DATA.labels.__dates = S.from + "~" + S.to;
        // 结束分钟：23:59 要把 23:59:30 那一条收进来
        $("fromTime").onchange({target:{value:"23:59"}});
        $("toTime").onchange({target:{value:"23:59"}});
        DATA.labels.__n_end = currentRows().length;
        // 点「最新一天」应该把分钟复位成默认
        $("btnToday").onclick();
        DATA.labels.__after_btn = S.fromTime + "~" + S.toTime;
    """)
    check("min.js_ok", ok, True)
    if ok:
        L = res["labels"]
        check("min.default_all", L["__n_all"], 4)          # 两天全收（含次日 00:30）
        check("min.head_has_times", "00:00" in L["__head"], True)
        check("min.dates_kept_days", L["__dates"], "2026-09-27~2026-09-27")
        check("min.narrow_count", L["__n_narrow"], 1)      # 只有 10:15 那条
        check("min.narrow_times", L["__times"], "10:00~10:20")
        check("min.narrow_tree_refreshed",
              ("sA" in L["__rows_narrow"]) and ("sB" not in L["__rows_narrow"])
              and ("sC" not in L["__rows_narrow"]), True)
        check("min.end_minute_inclusive", L["__n_end"], 1)  # 23:59:30 被收进来
        check("min.btn_today_resets_times", L["__after_btn"], "00:00~23:59")
    con.close()


# ================================================================ I. 深色模式
def test_dark(tmp):
    """深色模式：勾选切 body.dark；中性色走变量、图里也跟随；
    **标红/标黄那条通道在深色下必须一字不改**。"""
    day = "2026-09-27"
    dbfile = use_temp(tmp, "dark.db")
    con = db.connect(dbfile)
    ts = ms(2026, 9, 27, 10, 0)
    con.execute(
        "INSERT INTO messages (session_id,msg_id,seq,ts,day,hour,turn_no,step_no,model,provider,"
        "in_tokens,out_tokens,cache_read,cache_write,total_tokens,peak,cost_est,price_row,keep,"
        "replayed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sA", "m1", 1, ts, day, 10, 1, 1, "deepseek-flash", "", 1000, 100, 5000, 0, 6100,
         0, 0.02, 3, 1, 0))
    con.execute("INSERT INTO sessions (session_id,workspace,cwd,depth,created_at) "
                "VALUES ('sA','--C-Users-x-wsK--','C:\\\\wsK',0,0)")
    con.commit()

    data = rh.build_data(day, day)
    out = os.path.join(tmp, "dark.html")
    rh.render(day, day, out, data=data)
    with open(out, encoding="utf-8") as f:
        html = f.read()

    check("dark.checkbox", 'id="dark"' in html, True)
    check("dark.css_block", "body.dark{" in html, True)
    check("dark.bg_is_dark", "--bg:#14161a" in html, True)
    # 告警色：浅色那套（亮底）保留在 :root；深色块里换成"黑底混出来的暗红/暗琥珀"，
    # 但**语义通道不变**（红=超标、黄=异常），且都走变量
    root_seg = html[html.index(":root{"):html.index("body.dark{")]
    dark_seg = html[html.index("body.dark{"):]
    dark_seg = dark_seg[:dark_seg.index("}") + 1]
    check("dark.light_signals_kept",
          ("--hot-bg:#fdecea" in root_seg) and ("--hot-fg:#c62828" in root_seg)
          and ("--anom-bg:#fff8e6" in root_seg) and ("--anom-fg:#b26a00" in root_seg), True)
    check("dark.hot_mixed_with_bg",
          ("--hot-bg:#331d1c" in dark_seg) and ("#fdecea" not in dark_seg)
          and ("#c62828" not in dark_seg), True)
    check("dark.anom_mixed_with_bg",
          ("--anom-bg:#332b19" in dark_seg) and ("#fff8e6" not in dark_seg)
          and ("#b26a00" not in dark_seg), True)
    check("dark.signals_use_vars",
          (".hotcost,.hotpeak{color:var(--hot-fg)" in html)
          and (".tree tr.anom{background:var(--anom-bg)}" in html)
          and (".tree tr.hot{background:var(--hot-bg)}" in html), True)
    # 原生控件（日期/时间选择器的小图标与弹出面板、下拉弹层）只认 color-scheme
    check("dark.color_scheme_light", ":root{ color-scheme:light" in html, True)
    check("dark.color_scheme_dark", "body.dark{ color-scheme:dark" in html, True)
    check("dark.date_time_inputs_themed",
          "input[type=date],input[type=time]{background:var(--input);color:var(--fg)}" in html, True)

    ok, res = rh.check_browser_with(out, """
        $("dark").onchange({target:{checked:true}});
        DATA.labels.__on = document.body.classList.contains("dark");
        DATA.labels.__persist = snapshot().dark;
        DATA.labels.__donut = $("donutCost").innerHTML;
        $("dark").onchange({target:{checked:false}});
        DATA.labels.__off = document.body.classList.contains("dark");
    """)
    check("dark.js_ok", ok, True)
    if ok:
        L = res["labels"]
        check("dark.class_on", L["__on"], True)
        check("dark.class_off", L["__off"], False)
        check("dark.persisted_in_snapshot", L["__persist"], True)
        check("dark.svg_follows_theme",
              ('class="track"' in L["__donut"]) and ("#eceef1" not in L["__donut"]), True)
    con.close()


# ================================================================ J. 原生搜索账单（旁路）
def test_search(tmp):
    """原生搜索：吸进 search_calls → 按 ts 归到轮次 → **默认不区分来源**并进总账
    （KPI/树/对账/日聚合都算上，另给一行"其中原生搜索"）。"""
    day = "2026-10-19"            # 周一：12:30 非高峰，14:45 高峰
    dbfile = use_temp(tmp, "srch.db")
    con = db.connect(dbfile)

    def put(sid, turn, hh, mm):
        ts = ms(2026, 10, 19, hh, mm)
        con.execute(
            "INSERT INTO messages (session_id,msg_id,seq,ts,day,hour,turn_no,step_no,model,"
            "provider,in_tokens,out_tokens,cache_read,cache_write,total_tokens,peak,cost_est,"
            "price_row,keep,replayed,interrupted,start_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "%s-%d" % (sid, turn), 1, ts, day, hh, turn, 1, "deepseek-flash", "",
             1000, 100, 5000, 0, 6100, 0, 0.02, 3, 1, 0, 0, ts))

    put("sA", 1, 12, 0)      # 轮次 1（12:00 起）
    put("sA", 2, 14, 40)     # 轮次 2（14:40 起）
    con.execute("INSERT INTO sessions (session_id,workspace,cwd,depth,created_at) "
                "VALUES ('sA','--C-Users-x-wsS--','C:\\\\wsS',0,0)")
    con.commit()

    # 搜索账单：实测（12:30，落轮次 1）+ estimate（N=3，14:45，落轮次 2，高峰）
    #            + 一条**没有 sessionId** 的（12:01，靠时间邻近推定到 sA/轮1）
    p = os.path.join(tmp, "search-usage.jsonl")
    lines = [
        {"ts": ms(2026, 10, 19, 12, 1), "model": "deepseek-v4-flash",
         "source": "dsh-search-audit",
         "query": "DeepSeek Harness 插件系统 是什么",
         "usage": {"inputTokens": 9000, "outputTokens": 1000, "cacheReadTokens": 0,
                   "cacheWriteTokens": 0}},
        {"ts": ms(2026, 10, 19, 12, 30), "sessionId": "sA", "model": "deepseek-v4-flash",
         "source": "dsh-search-audit",
         "usage": {"inputTokens": 9000, "outputTokens": 1000, "cacheReadTokens": 0,
                   "cacheWriteTokens": 0}},
        {"ts": ms(2026, 10, 19, 14, 45), "sessionId": "sA", "model": "deepseek-v4-flash",
         "source": "estimate", "query": "（估算：3 次原生搜索请求，turn 2）",
         "usage": {"inputTokens": 27000, "outputTokens": 2280}},
    ]
    with open(p, "w", encoding="utf-8") as f:
        for r in lines:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.write('{"ts": 123, "usage": {"inputTokens": 1')     # 末行写了一半
    old = os.environ.get("DSH_SEARCH_AUDIT_LOG")
    os.environ["DSH_SEARCH_AUDIT_LOG"] = p
    old_fix = db.SEARCH_FIX_PATH
    fixp = os.path.join(tmp, "search-fix.txt")
    db.SEARCH_FIX_PATH = fixp
    with open(fixp, "w", encoding="utf-8") as f:
        f.write("# 测试用\n%d | sA | 首轮测试时插件没写 sessionId\n" % ms(2026, 10, 19, 12, 1))
    try:
        res = db.sync_search_usage(con)
        check("srch.rows", res["rows"], 3)
        check("srch.bad_rows_skipped", res["bad"], 1)
        check("srch.calls_total", res["calls"], 5)            # 1 + 1 + 3
        # 12:30 非高峰：9000×1 + 1000×4 = 0.013；14:45 高峰×2：(27000×2 + 2280×8)/1e6 = 0.07224
        check("srch.cost_total", res["cost"], 0.08524 + 0.013, 1e-9)   # 12:01 那条也算进去了
        check("srch.tokens_total", res["tokens"], 49280)
        rows = {r["ts"]: dict(r) for r in con.execute("SELECT * FROM search_calls")}
        r1 = rows[ms(2026, 10, 19, 12, 30)]
        r2 = rows[ms(2026, 10, 19, 14, 45)]
        r0 = rows[ms(2026, 10, 19, 12, 1)]
        check("srch.turn_by_ts", (r0["turn_no"], r1["turn_no"], r2["turn_no"]), (1, 1, 2))
        # 账单没写 sessionId 的那条：靠 config/search-fix.txt 人工补（**不自动猜**）
        check("srch.fix_session", r0["session_id"], "sA")
        check("srch.fix_marked", "人工补" in (r0["note"] or ""), True)
        check("srch.peak_rule", (r1["peak"], r2["peak"]), (0, 1))
        check("srch.price_row_v4flash", (r1["price_row"], r2["price_row"]), (3, 3))
        check("srch.calls_n_estimate", r2["calls_n"], 3)
        check("srch.cost_per_row", (round(r1["cost_est"], 6), round(r2["cost_est"], 6)),
              (0.013, 0.07224))

        # 对账：搜索并进"我们"这一侧；日折算也算上它
        db.sync_ledger(con)
        db.rebuild_day_recon(con)
        check("srch.day_recon_scan_cost", con.execute(
            "SELECT scan_cost FROM day_recon WHERE day=?", (day,)).fetchone()[0],
              0.04 + 0.09824, 1e-9)
        ours = {r["hour"]: r["ours"] for r in db.gap_summary(con)["rows"]}
        check("srch.gap_hour12", ours.get(12), 0.02 + 0.013 + 0.013)   # 12:00 调用 + 12:01/12:30 搜索
        check("srch.gap_hour14", ours.get(14), 0.02 + 0.07224)

        # 界面数据：摊成普通原子行、默认不区分（kind=search 只给 KPI 用）
        data = rh.build_data(day, day)
        cols = {c: i for i, c in enumerate(data["mcols"])}
        check("srch.mrows_include_search", len(data["mrows"]), 2 + 1 + 1 + 3)
        kinds = [r[data["mcols"].index("kind")] for r in data["mrows"]]
        check("srch.kind_count", kinds.count("search"), 5)
        check("srch.build_summary", data["search"]["calls"], 5)
        check("srch.build_daily_cost", data["daily"][0]["cost"], 0.04 + 0.09824, 1e-9)
        check("srch.build_daily_calls", data["daily"][0]["calls"], 2 + 5)
        # 「搜索开销」列：每行只记"其中搜索那部分"，合计必须等于搜索总花费（不能重复计）
        check("srch.cols_exist", ("srch" in cols) and ("srchN" in cols), True)
        si, ni = cols["srch"], cols["srchN"]
        check("srch.col_total", round(sum(r[si] or 0 for r in data["mrows"]), 6), 0.09824, 1e-9)
        check("srch.col_calls", sum(r[ni] or 0 for r in data["mrows"]), 5)

        out = os.path.join(tmp, "srch.html")
        rh.render(day, day, out, data=data)
        with open(out, encoding="utf-8") as f:
            html = f.read()
        ok, snap = rh.check_browser_with(out, 'S.dims=["turn"];render();')
        check("srch.html_ok", ok, True)
        if ok:
            check("srch.kpi_mentions", "其中原生搜索" in snap["kpi"], True)
            check("srch.kpi_counts", "5 次" in snap["kpi"], True)
            check("srch.column_present", ">搜索开销</th>" in html, True)
            check("srch.column_turn1", "0.026" in snap["rows"], True)   # 12:01 + 12:30 两次
            check("srch.column_turn2", "0.072" in snap["rows"], True)   # estimate 那 3 次
            check("srch.column_total", "0.098" in snap["tot"], True)

        # 把人工补的那条去掉 → 该记录就该变成"没归属"，并且能被查出来报警（不自动猜）
        os.remove(fixp)
        db.sync_search_usage(con)
        rows = {r["ts"]: dict(r) for r in con.execute("SELECT * FROM search_calls")}
        check("srch.no_fix_unattributed", rows[ms(2026, 10, 19, 12, 1)]["session_id"], None)
        check("srch.unattributed_reported", db.search_unattributed(con)["rows"], 1)
    finally:
        if old is None:
            os.environ.pop("DSH_SEARCH_AUDIT_LOG", None)
        else:
            os.environ["DSH_SEARCH_AUDIT_LOG"] = old
        db.SEARCH_FIX_PATH = old_fix
    con.close()


# ================================================================ K. 疑似缓存重建（参考列）
def test_rebuild(tmp):
    """重建参考：宽判=黄字、严判=整行标黄；没嫌疑画横杠；**金额只展示、不进任何统计**。"""
    day = "2026-10-19"
    dbfile = use_temp(tmp, "rb.db")
    con = db.connect(dbfile)

    def put(turn, step, hh, mm, in_tok, cache, out_tok, cost):
        ts = ms(2026, 10, 19, hh, mm)
        con.execute(
            "INSERT INTO messages (session_id,msg_id,seq,ts,day,hour,turn_no,step_no,model,"
            "provider,in_tokens,out_tokens,cache_read,cache_write,total_tokens,peak,cost_est,"
            "price_row,keep,replayed,interrupted,start_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sA", "m%d-%d" % (turn, step), step, ts, day, hh, turn, step, "deepseek-flash",
             "", in_tok, out_tok, cache, 0, in_tok + out_tok + cache, 0, cost, 3, 1, 0, 0, ts))

    put(1, 1, 12, 0, 1000, 99000, 1000, 0.01)    # 正常：上下文 100k、未命中 1%
    put(2, 1, 12, 10, 60000, 0, 1000, 0.06)      # 严判：60k 全未命中
    put(2, 2, 12, 11, 1000, 60000, 500, 0.01)    #   次条立刻回落 → 确认线过
    put(3, 1, 12, 20, 30000, 20000, 1000, 0.03)  # 宽判：50k 里未命中 60%
    put(3, 2, 12, 21, 30000, 20000, 1000, 0.03)  #   次条仍 60% → 过不了确认线
    put(4, 1, 12, 30, 9000, 0, 1000, 0.01)       # 上下文 9k，太小 → 不判
    con.execute("INSERT INTO sessions (session_id,workspace,cwd,depth,created_at) "
                "VALUES ('sA','w','c',0,0)")
    con.commit()

    fl = db.turn_rebuild_flags(con)
    check("rb.level", [fl[("sA", t)]["level"] for t in (1, 2, 3, 4)], [0, 2, 1, 0])
    check("rb.cost_strict", round(fl[("sA", 2)]["cost"], 6), 60000 * 0.98 / 1e6, 1e-9)
    check("rb.cost_loose", round(fl[("sA", 3)]["cost"], 6), 30000 * 0.98 / 1e6, 1e-9)
    check("rb.no_ctx_no_flag", fl[("sA", 4)]["cost"], 0.0)

    data = rh.build_data(day, day)
    cols = {c: i for i, c in enumerate(data["mcols"])}
    check("rb.cols_exist", ("rb" in cols) and ("rbLvl" in cols), True)
    rbrows = [r for r in data["mrows"] if r[cols["rb"]]]
    check("rb.only_first_call", len(rbrows), 2)
    check("rb.values", sorted((round(r[cols["rb"]], 6), r[cols["rbLvl"]]) for r in rbrows),
          [(0.0294, 1), (0.0588, 2)])
    # 关键：这些钱**不许**再进统计（本来就含在 cost_est 里）
    check("rb.not_in_daily_cost", data["daily"][0]["cost"], 0.15, 1e-9)
    check("rb.not_in_daily_calls", data["daily"][0]["calls"], 6)

    out = os.path.join(tmp, "rb.html")
    rh.render(day, day, out, data=data)
    ok, res = rh.check_browser_with(out, 'S.dims=["workspace","session","turn"];render();')
    with open(out, encoding="utf-8") as f:
        html = f.read()
    check("rb.html_ok", ok, True)
    if ok:
        rows = res["rows"]
        check("rb.cell_amounts", ("0.059" in rows) and ("0.029" in rows), True)
        check("rb.cell_none_dash", '<td class="rbcell none">—</td>' in rows, True)
        check("rb.cls_strict", 'class="rbcell strict"' in rows, True)
        check("rb.cls_loose", 'class="rbcell loose"' in rows, True)
        check("rb.cell_only_not_row", "rbrow" in rows, False)   # 严判只标那一格，不标整行
        check("rb.strict_is_red",
              ".rbcell.strict{color:var(--hot-fg);font-weight:700}" in html, True)
        check("rb.loose_is_yellow",
              ".rbcell.loose{color:var(--rb-fg);font-weight:600}" in html, True)
        check("rb.no_cell_bg", "background:var(--rb-bg)" in html, False)
        check("rb.total_cell", "0.088" in res["tot"], True)   # 0.0588 + 0.0294
        # 开关：默认显示；关掉 → 表格加 rbcol-off 类（整列隐藏），再开回来
        check("rb.switch_in_html", 'id="showRb"' in html, True)
        ok2, res2 = rh.check_browser_with(out, """
            DATA.labels.__def = S.showRb;
            $("showRb").onchange({target:{checked:false}});
            DATA.labels.__off = document.querySelector("table.t").classList.contains("rbcol-off");
            $("showRb").onchange({target:{checked:true}});
            DATA.labels.__on = document.querySelector("table.t").classList.contains("rbcol-off");
        """)
        check("rb.switch_js_ok", ok2, True)
        if ok2:
            check("rb.switch_default_on", res2["labels"]["__def"], True)
            check("rb.switch_off_class", res2["labels"]["__off"], True)
            check("rb.switch_on_class", res2["labels"]["__on"], False)
    con.close()


# ================================================================ L. 旁路调用的增量扫描
def test_side_calls(tmp):
    """旁路调用：增量扫描（没变的日志不再解压），汇总必须等于逐会话明细之和。"""
    root = os.path.join(tmp, "sessions")
    build_session_tree(root)
    dbfile = use_temp(tmp, "side.db")
    r1 = sc.scan(quiet=True)
    check("side.first_parses_all", r1["changed"], 8)
    check("side.rows_exist", bool(r1["rows"]), True)
    r2 = sc.scan(quiet=True)
    check("side.second_parses_none", r2["changed"], 0)
    check("side.idempotent",
          [(x["day"], x["kind"], x["calls"], x["tokens"]) for x in r2["rows"]],
          [(x["day"], x["kind"], x["calls"], x["tokens"]) for x in r1["rows"]])
    con = db.connect(dbfile)
    bad = []
    for x in r2["rows"]:
        agg = con.execute("SELECT SUM(calls) c, SUM(tokens) t FROM side_session "
                          "WHERE day=? AND kind=?", (x["day"], x["kind"])).fetchone()
        if (agg["c"], agg["t"]) != (x["calls"], x["tokens"]):
            bad.append(x["kind"])
    check("side.sum_matches_session_rows", bad, [])
    con.close()
    # 只改一个日志 → 只重解压它
    make_log(os.path.join(root, "--C-Users-x-0-wsB--", "session-B1", "session.v3.jsonl.zstd"),
             "session-B1",
             [{"ts": ms(2026, 9, 21, 14, 0), "in": 9000, "out": 900, "cache": 90000, "id": "b1"}],
             extra_unknown=True)
    r3 = sc.scan(quiet=True)
    check("side.only_one_reparsed", r3["changed"], 1)


# ================================================================ M. 分叉副本同时刻（ts 相同）的归属
def test_fork_tie(tmp):
    """分叉副本和原件的 ts 一模一样时：原创会话那份必须 keep=1。

    2026-10-19 的 bug：只比 ts（严格小于），原创会话重扫时它的行刚被删掉、
    库里只剩副本 → 原件也被判"重放" → 两边都 keep=0，**整轮凭空消失**。
    """
    root = os.path.join(tmp, "sessions")
    w = os.path.join(root, "--C-Users-x-0-wsT--")
    s1 = os.path.join(w, "session-T1")      # 原创（创建更早）
    s2 = os.path.join(w, "session-T2")      # 分叉副本
    ts = ms(2026, 9, 22, 10, 0)
    msg = {"ts": ts, "in": 1000, "out": 100, "cache": 5000, "id": "tie-1"}
    os.makedirs(s1, exist_ok=True)
    os.makedirs(s2, exist_ok=True)
    o1 = os.path.join(s1, "session.v4.jsonl.zstd")
    o2 = os.path.join(s2, "session.v4.jsonl.zstd")
    make_log(o1, "session-T1", [msg], cwd="C:\\wsT", created_at=ts - 600000)
    make_log(o2, "session-T2", [msg], cwd="C:\\wsT", created_at=ts + 600000)
    dbfile = use_temp(tmp, "tie.db")

    ss.scan(full=True, quiet=True, root=root)
    con = db.connect(dbfile)
    check("tie.original_kept", one("SELECT keep FROM messages WHERE session_id='session-T1'", con), 1)
    check("tie.fork_replay", one("SELECT keep FROM messages WHERE session_id='session-T2'", con), 0)
    check("tie.only_one_kept", one(
        "SELECT COUNT(*) FROM messages WHERE msg_id='tie-1' AND keep=1", con), 1)

    # 原创会话被重扫（旧行先删掉、库里只剩副本）→ 必须仍然是它 keep=1
    make_log(o1, "session-T1", [msg], cwd="C:\\wsT", created_at=ts - 600000)
    os.utime(o1, (time.time() + 2, time.time() + 2))
    ss.scan(full=False, quiet=True, root=root)
    check("tie.after_rescan_original", one(
        "SELECT keep FROM messages WHERE session_id='session-T1'", con), 1)
    check("tie.after_rescan_fork", one(
        "SELECT keep FROM messages WHERE session_id='session-T2'", con), 0)
    check("tie.still_one_kept", one(
        "SELECT COUNT(*) FROM messages WHERE msg_id='tie-1' AND keep=1", con), 1)

    # 历史遗留：两份都被标成 keep=1（09-23 就是 5 份都在算）→ 再扫一次应当自动收敛成一份
    con.execute("UPDATE messages SET keep=1, replayed=0 WHERE msg_id='tie-1'")
    con.commit()          # 先落盘，否则 scan 另开连接会 database is locked
    ss.scan(full=False, quiet=True, root=root)
    check("tie.repair_single_keep", one(
        "SELECT COUNT(*) FROM messages WHERE msg_id='tie-1' AND keep=1", con), 1)
    check("tie.repair_keeps_original", one(
        "SELECT keep FROM messages WHERE session_id='session-T1'", con), 1)
    check("tie.repair_demotes_fork", one(
        "SELECT keep FROM messages WHERE session_id='session-T2'", con), 0)
    con.close()


# ================================================================ N. 压缩摘要（compaction/summary）
def test_compaction(tmp):
    """压缩摘要：逐事件入库（分叉副本按 ts 去重）、算钱、挂到"第 N 轮前/中/后"。"""
    root = os.path.join(tmp, "sessions")
    w = os.path.join(root, "--C-Users-x-0-wsC--")
    s1 = os.path.join(w, "session-C1")      # 原创（创建更早）
    s2 = os.path.join(w, "session-C2")      # 分叉副本
    t_msg = ms(2026, 9, 22, 21, 0)
    t_sum = ms(2026, 9, 22, 21, 10)
    msg = {"ts": t_msg, "in": 1000, "out": 100, "cache": 5000, "id": "c-1",
           "model": "deepseek-flash"}
    summ = {"ts": t_sum, "in": 10000, "out": 500, "hit": 0}
    os.makedirs(s1, exist_ok=True)
    os.makedirs(s2, exist_ok=True)
    make_log(os.path.join(s1, "session.v4.jsonl.zstd"), "session-C1", [msg],
             cwd="C:\\wsC", created_at=t_msg - 600000, summary=summ)
    make_log(os.path.join(s2, "session.v4.jsonl.zstd"), "session-C2", [msg],
             cwd="C:\\wsC", created_at=t_msg + 600000, summary=summ)
    dbfile = use_temp(tmp, "comp.db")
    ss.scan(full=True, quiet=True, root=root)      # 生产顺序：先主扫描，再旁路扫描

    r = sc.scan(quiet=True)
    check("comp.calls", r["compactions"]["calls"], 1)          # 两份副本只算一次
    con = db.connect(dbfile)
    rows = [dict(x) for x in con.execute("SELECT * FROM compaction_events")]
    check("comp.rows", len(rows), 1)
    check("comp.session_is_original", rows[0]["session_id"], "session-C1")
    check("comp.turn", (rows[0]["turn_no"], rows[0]["pos"]), (1, "后"))
    # 10000×1 + 500×4 = 0.012 元（21 点非高峰）
    check("comp.cost", round(rows[0]["cost_est"], 6), 0.012, 1e-9)
    check("comp.tokens", rows[0]["total_tokens"], 10500)

    # 界面：作为额外原子行，轮次键 1.5，标签"第 1 轮后 · 压缩摘要"
    day = "2026-09-22"
    data = rh.build_data(day, day)
    cols = {x: i for i, x in enumerate(data["mcols"])}
    comp = [x for x in data["mrows"] if x[cols["kind"]] == "compaction"]
    check("comp.in_mrows", len(comp), 1)
    check("comp.turn_key", comp[0][cols["turn_no"]], 1.5)
    check("comp.label", data["labels"]["turn"].get("session-C1|1.5"),
          "第 1 轮后 · 压缩摘要")
    # 日聚合必须把它算进去：日合计 == 所有非重放行的花费之和（含压缩摘要那 0.012）
    tot = sum(max(0.0, x[cols["cost_est"]] or 0) for x in data["mrows"]
              if not x[cols["replay"]])
    check("comp.in_daily_cost", data["daily"][0]["cost"], tot, 1e-9)
    check("comp.in_daily_has_summary", data["daily"][0]["cost"] >= 0.012, True)

    # 标签只能出现一次：不能是「第 1.5 轮 第 1 轮后 · 压缩摘要」
    out = os.path.join(tmp, "comp.html")
    rh.render(day, day, out, data=data)
    ok, res = rh.check_browser_with(out, 'S.dims=["turn"];render();')
    check("comp.render_ok", ok, True)
    if ok:
        check("comp.row_label", "第 1 轮后 · 压缩摘要" in res["rows"], True)
        check("comp.no_fraction_label", "第 1.5 轮" in res["rows"], False)
    con.close()


# ================================================================ M2. 全量扫描里"后来居上"要能降级前面的
def test_fork_tie_order(tmp):
    """全量扫描时，若**创建更早**的会话排在后面被处理，它上位后必须把前面那份的 keep 改回 0。

    否则同一调用会被算两遍（2026-10-20 实测 09-23 有 5 份都在算、那天 249%）。
    文件名故意让"晚创建的 session-U1"排在"早创建的 session-U8"前面，制造这个顺序。
    """
    root = os.path.join(tmp, "sessions")
    w = os.path.join(root, "--C-Users-x-0-wsU--")
    s1 = os.path.join(w, "session-U1")      # 后创建，但目录名排前面（先被处理）
    s2 = os.path.join(w, "session-U8")      # 先创建，后处理 → 要"后来居上"
    ts = ms(2026, 9, 22, 9, 0)
    msg = {"ts": ts, "in": 1000, "out": 100, "cache": 5000, "id": "tie2-1"}
    os.makedirs(s1, exist_ok=True)
    os.makedirs(s2, exist_ok=True)
    make_log(os.path.join(s1, "session.v4.jsonl.zstd"), "session-U1", [msg],
             cwd="C:\\wsU", created_at=ts + 600000)
    make_log(os.path.join(s2, "session.v4.jsonl.zstd"), "session-U8", [msg],
             cwd="C:\\wsU", created_at=ts - 600000)
    dbfile = use_temp(tmp, "tie2.db")

    ss.scan(full=True, quiet=True, root=root)
    con = db.connect(dbfile)
    check("tie2.one_kept", one(
        "SELECT COUNT(*) FROM messages WHERE msg_id='tie2-1' AND keep=1", con), 1)
    check("tie2.earlier_created_wins", one(
        "SELECT keep FROM messages WHERE session_id='session-U8'", con), 1)
    check("tie2.later_created_demoted", one(
        "SELECT keep FROM messages WHERE session_id='session-U1'", con), 0)
    con.close()


# ================================================================ O. 空转/重试的自动记账
def test_lost_calls(tmp):
    """官方 requests 比我们数得出来的多 → 差额按小时记成"未记录调用"（次数 + 金额）。"""
    root = os.path.join(tmp, "sessions")
    dbfile = use_temp(tmp, "lost.db")
    con = db.connect(dbfile)
    D = "2026-09-23"
    ts = ms(2026, 9, 23, 14, 10)          # 高峰时段：官方 10 元是我们算不出来的
    con.execute("INSERT INTO sessions (session_id,workspace,cwd,depth,created_at) "
                "VALUES ('sL','--W--','c',0,0)")
    for i in range(2):
        con.execute(
            "INSERT INTO messages (session_id,msg_id,seq,ts,day,hour,turn_no,step_no,model,"
            "provider,in_tokens,out_tokens,cache_read,cache_write,total_tokens,peak,cost_est,"
            "price_row,keep,replayed,interrupted,start_ts) "
            "VALUES ('sL',?,?,?,?,?,1,?, 'deepseek-flash','',1000,100,5000,0,6100,1,0.01,3,1,0,0,?)",
            ("m%d" % i, i + 1, ts + i, D, 14, i + 1, ts))
    con.execute("INSERT INTO official_hour (day,hour,api_key_id,api_key_name,model,cache_hit,"
                "cache_miss,output,requests,cost,currency,fetched_at) "
                "VALUES (?,?,?,?,?,0,0,1000,10,10.0,'CNY',1790000000)", (D, 14, "h-id", "harness",
                                                                        "deepseek-flash"))
    con.commit()

    r = db.sync_lost_calls(con)
    check("lost.hours", r["hours"], 1)
    check("lost.calls", r["calls"], 8)                 # 10 次请求 - 我们 2 次调用
    check("lost.cost", round(r["cost"], 6), 10.0 - 0.02, 1e-9)   # 官方 10 - 我们已折 0.02
    row = con.execute("SELECT n, cost FROM lost_calls").fetchone()
    check("lost.stored", (row["n"], round(row["cost"], 6)), (8, 9.98))

    # 对账/日折算都算上它
    db.rebuild_day_recon(con)
    sc = con.execute("SELECT scan_cost, scan_calls FROM day_recon WHERE day=?", (D,)).fetchone()
    check("lost.in_recon_cost", round(sc["scan_cost"], 6), 0.02 + 9.98, 1e-9)
    check("lost.in_recon_calls", sc["scan_calls"], 2 + 8)
    check("lost.gap_closed", db.gap_summary(con)["unmatched"], 0.0)

    # 次数对得上时不制造空转行
    con.execute("UPDATE official_hour SET requests=2, cost=0.02")
    con.commit()
    r2 = db.sync_lost_calls(con)
    check("lost.none_when_count_matches", r2["hours"], 0)
    check("lost.cleared", con.execute("SELECT COUNT(*) FROM lost_calls").fetchone()[0], 0)

    # 界面：单独一行「(未记录 · 空转)」，次数按调用数摊
    con.execute("UPDATE official_hour SET requests=10, cost=10.0")
    con.commit()
    db.sync_lost_calls(con)
    data = rh.build_data(D, D)
    cols = {x: i for i, x in enumerate(data["mcols"])}
    lostrows = [x for x in data["mrows"] if x[cols["kind"]] == "lost"]
    check("lost.in_mrows", len(lostrows), 8)
    check("lost.rows_sum", round(sum(x[cols["cost_est"]] for x in lostrows), 6), 9.98, 1e-9)
    check("lost.label", data["labels"]["turn"].get("(未记录)|0"), "(未记录 · 空转)")
    con.close()


# ================================================================ P. 步级摘要 + 步维度
def test_step_snippet(tmp):
    """每条调用（assistant/message = 一步）要存一小段返回内容当摘要，且「步」能当维度。"""
    root = os.path.join(tmp, "sessions")
    dbfile = use_temp(tmp, "step.db")
    con = db.connect(dbfile)

    def put(sname, evs):
        d = os.path.join(root, "wsS", sname)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "session.v4.jsonl.zstd")
        with open(p, "wb") as f:
            import zstandard as _zs
            cctx = _zs.ZstdCompressor()
            for e in evs:
                f.write(cctx.compress((json.dumps(e, ensure_ascii=False) + chr(10)).encode()))

    T = ms(2026, 9, 24, 10, 0)
    sess = {"type": "session", "version": 4, "id": "session-STEP", "createdAt": T,
            "cwd": "C:\wsS", "delegationDepth": 0, "agentPreset": "standard"}
    usage = {"inputTokens": 1000, "outputTokens": 100, "cacheReadTokens": 5000,
             "cacheWriteTokens": 0, "totalTokens": 6100}
    # 一步里同时有 reasoning 和 text：摘要应该取 reasoning（思考链），不是正式输出
    step1 = {"type": "assistant/message", "time": T + 1000,
             "data": {"turn": 1, "step": 1, "usage": usage,
                      "message": {"role": "assistant", "content": [
                          {"type": "reasoning",
                           "text": "先看看用户要什么，" + chr(10) + "然后决定用哪个工具"},
                          {"type": "text", "text": "好的，我来处理。"}]}}}
    step2 = {"type": "assistant/message", "time": T + 2000,
             "data": {"turn": 1, "step": 2, "usage": usage,
                      "message": {"role": "assistant", "content": [
                          {"type": "text", "text": "第二步的正式输出"}]}}}
    put("session-STEP", [sess, step1, step2])
    con.commit()
    ss.scan(full=True, recon=False, quiet=True, root=root)

    rows = [dict(r) for r in con.execute(
        "SELECT turn_no, step_no, snippet FROM messages WHERE session_id='session-STEP' "
        "ORDER BY step_no")]
    check("step.rows", len(rows), 2)
    check("step.snippet_from_reasoning", rows[0]["snippet"].startswith("先看看用户要什么"), True)
    check("step.snippet_flat", (chr(10) not in rows[0]["snippet"]), True)   # 换行被压平
    check("step.snippet_fallback_text", rows[1]["snippet"], "第二步的正式输出")

    # 看板数据里：snippet 有列、步维度在排序键里
    data = rh.build_data("2026-09-24", "2026-09-24")
    cols = data["mcols"]
    check("step.dim_registered", ("step" in data["dim_order"]), True)
    check("step.in_mcols", ("snippet" in cols and "step_no" in cols), True)
    idx = cols.index("snippet")
    vals = [r[idx] for r in data["mrows"]
            if r[cols.index("session_id")] == "session-STEP"]
    check("step.snippets_in_rows", sorted(v[:4] for v in vals), sorted(["先看看用", "第二步的"]))
    con.close()


# ================================================================ Q. 工具调用
def test_tool_calls(tmp):
    """tool/call + tool/result 入库：名字/次数/成败，并且挂到对应那一「步」上（只展示，不算钱）。"""
    root = os.path.join(tmp, "sessions")
    dbfile = use_temp(tmp, "tools.db")
    con = db.connect(dbfile)

    d = os.path.join(root, "wsT", "session-TOOL")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "session.v4.jsonl.zstd")
    T = ms(2026, 9, 25, 9, 0)
    usage = {"inputTokens": 1000, "outputTokens": 100, "cacheReadTokens": 5000,
             "cacheWriteTokens": 0, "totalTokens": 6100}
    evs = [{"type": "session", "version": 4, "id": "session-TOOL", "createdAt": T,
            "cwd": "C:" + chr(92) + "wsT", "delegationDepth": 0, "agentPreset": "standard"},
           {"type": "assistant/message", "time": T + 1000,
            "data": {"turn": 1, "step": 1, "usage": usage,
                     "message": {"role": "assistant", "content": [{"type": "text", "text": "先看文件"}]}}}]
    for i, (nm, bad) in enumerate([("read", False), ("grep", False), ("pwsh", True)]):
        evs.append({"type": "tool/call", "time": T + 2000 + i,
                    "data": {"turn": 1, "step": 1, "callId": "c%d" % i, "name": nm, "arguments": "{}"}})
        evs.append({"type": "tool/result", "time": T + 2500 + i,
                    "data": {"turn": 1, "step": 1,
                             "message": {"role": "tool", "toolCallId": "c%d" % i,
                                         "isError": bad, "content": [{"type": "text", "text": "ok"}]}}})
    import zstandard as _zs
    with open(p, "wb") as f:
        f.write(_zs.ZstdCompressor().compress(
            (chr(10).join(json.dumps(x, ensure_ascii=False) for x in evs) + chr(10)).encode("utf-8")))

    ss.scan(full=True, recon=False, quiet=True, root=root)
    rows = [dict(r) for r in con.execute(
        "SELECT turn_no, step_no, name, ok FROM tool_calls WHERE session_id='session-TOOL' "
        "ORDER BY call_id")]
    check("tool.count", len(rows), 3)
    check("tool.names", [r["name"] for r in rows], ["read", "grep", "pwsh"])
    check("tool.error_flag", [r["ok"] for r in rows], [1, 1, 0])
    check("tool.turn_step", (rows[0]["turn_no"], rows[0]["step_no"]), (1, 1))
    # 工具调用**不进**金额/次数统计
    check("tool.not_counted_in_calls",
          con.execute("SELECT COUNT(*) FROM messages WHERE session_id='session-TOOL'").fetchone()[0], 1)

    data = rh.build_data("2026-09-25", "2026-09-25")
    cols = data["mcols"]
    i_sid, i_tn, i_tnm = cols.index("session_id"), cols.index("tn"), cols.index("tnm")
    hit = [r for r in data["mrows"] if r[i_sid] == "session-TOOL"]
    check("tool.in_mrows", (len(hit), hit[0][i_tn]), (1, 3))
    check("tool.names_in_row", ("read×1" in hit[0][i_tnm] and "pwsh×1" in hit[0][i_tnm]), True)
    con.close()


# ================================================================ 主流程
def main():
    tmp = tempfile.mkdtemp(prefix="tokenstats_test_")
    try:
        use_temp(tmp)
        write_ledger_fixture(tmp)
        for fn in (lambda: test_ledger_side(tmp), test_pricing_and_peak,
                   lambda: test_session_scan(tmp), lambda: test_hier_and_html(tmp),
                   lambda: test_subagent_labels(tmp), lambda: test_interrupts(tmp),
                   lambda: test_holidays(tmp), lambda: test_official_peak(tmp),
                   lambda: test_price_nearest(tmp), lambda: test_unknown_price(tmp),
                   lambda: test_prices(tmp), lambda: test_rawcols(tmp),
                   lambda: test_minutes(tmp), lambda: test_dark(tmp),
                   lambda: test_search(tmp), lambda: test_rebuild(tmp), lambda: test_side_calls(tmp), lambda: test_fork_tie(tmp),
                   lambda: test_compaction(tmp), lambda: test_fork_tie_order(tmp),
                   lambda: test_lost_calls(tmp), lambda: test_step_snippet(tmp),
                   lambda: test_tool_calls(tmp)):
            guard(fn)
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)

    os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
    with open(LOGFILE, "w", encoding="utf-8") as f:
        f.write("checks=%d fails=%d warns=%d\n" % (CHECKS[0], len(FAILS), len(WARN)))
        for x in FAILS:
            f.write("  FAIL %s\n" % x)
        for x in WARN:
            f.write("  WARN %s\n" % x)
        for e in ERRORS:
            f.write("\n--- traceback ---\n%s\n" % e)
    print("tests: %d checks, %d fail, %d warn -> %s" % (CHECKS[0], len(FAILS), len(WARN),
                                                        "PASS" if not FAILS else "FAIL"))
    for x in FAILS[:12]:
        print("  FAIL %s" % x)
    for x in WARN[:4]:
        print("  WARN %s" % x)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
