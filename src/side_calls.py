#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""side_calls.py —— 记录「旁路调用」的 token（title 生成 / deepseek 搜索等）

这些调用也走 API、也烧钱，但不写成 assistant/message，所以拿不到逐次成本；
本模块只把它们按天归拢到库里的 side_calls 表，界面上单独列一行「未归因」，
避免这部分被悄悄算进某个会话头上。

用法：
  python side_calls.py scan      # 扫描会话日志，按天统计旁路 token
  python side_calls.py show      # 打印结果
"""
from __future__ import annotations

import collections
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db            # noqa: E402
import scan_sessions as ss  # noqa: E402

# 表结构在 db.SCHEMA 里统一定义，这里不再重复建表


def scan(quiet=False, full=False):
    """按**变动过的会话**增量扫；side_calls 由 side_session 汇总生成。

    以前每次都把所有会话日志解压一遍（438 份 / 60MB ≈ 14 秒），其实没变的日志
    结果也不会变 —— 现在跟主扫描一样按 (size, mtime) 跳，没变动时 0.1 秒级。
    """
    con = db.connect()
    dirs = ss.list_session_dirs()
    changed = 0
    events = []                      # compaction/summary 事件（最后统一去重入账）
    for ws_dir, sid, version, path in dirs:
        try:
            st = os.stat(path)
        except OSError:
            continue
        prev = con.execute("SELECT file_size, file_mtime FROM side_log WHERE session_id=?",
                           (sid,)).fetchone()
        if (not full and prev and prev["file_size"] == st.st_size
                and abs((prev["file_mtime"] or 0) - st.st_mtime) < 1e-6):
            continue
        by_day = collections.defaultdict(lambda: collections.Counter())
        try:
            for o in ss.read_lines(path):
                t = o.get("type") or "?"
                d = o.get("data") if isinstance(o.get("data"), dict) else {}
                u = d.get("usage") if isinstance(d, dict) else None
                if not (isinstance(u, dict) and "inputTokens" in u and t != "assistant/message"):
                    continue
                ts = None
                for k in ("ts", "timestamp", "at", "time"):
                    v = o.get(k)
                    if isinstance(v, (int, float)) and v > 1e12:
                        ts = int(v)
                        break
                if t == "compaction/summary":
                    # 压缩上下文那次"写摘要"的调用：单独记（按 ts 去重，见 db.sync_compactions）
                    if ts:
                        events.append({"ts": ts, "session_id": sid,
                                       "in": ss._num(u, "inputTokens"),
                                       "out": ss._num(u, "outputTokens"),
                                       "hit": ss._num(u, "cacheReadTokens"),
                                       "write": ss._num(u, "cacheWriteTokens"),
                                       "model": str(u.get("model") or d.get("model") or "")})
                    continue
                day = db.local_day(ts) if ts else ""
                tok = (ss._num(u, "totalTokens") or (ss._num(u, "inputTokens")
                       + ss._num(u, "outputTokens") + ss._num(u, "cacheReadTokens")))
                by_day[day][t] += tok
                by_day[day][t + "::calls"] += 1
        except Exception as e:                     # 坏日志不该拖垮整个刷新
            if not quiet:
                print("  ! 旁路扫描跳过 %s: %s" % (sid, e))
            continue
        rows = []
        for day, kinds in by_day.items():
            for k, v in kinds.items():
                if not k.endswith("::calls"):
                    rows.append((sid, day, k, kinds[k + "::calls"], v))
        con.execute("DELETE FROM side_session WHERE session_id=?", (sid,))
        con.executemany("INSERT INTO side_session (session_id,day,kind,calls,tokens) "
                        "VALUES (?,?,?,?,?)", rows)
        con.execute("INSERT INTO side_log (session_id,file,file_size,file_mtime) "
                    "VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                    "file=excluded.file, file_size=excluded.file_size, "
                    "file_mtime=excluded.file_mtime",
                    (sid, path, st.st_size, st.st_mtime))
        changed += 1
    # 压缩摘要在自己的表里（compaction_events）单独入账，**不能**再算进旁路一次
    # （历史上 side_session 里存过这些行，必须清掉，否则"旁路token"那列会重复计）
    con.execute("DELETE FROM side_session WHERE kind='compaction/summary'")
    # 汇总表整个重建（源头是 side_session，很小）
    con.execute("DELETE FROM side_calls")
    con.execute("INSERT INTO side_calls (day,kind,calls,tokens) "
                "SELECT day, kind, SUM(calls), SUM(tokens) FROM side_session "
                "GROUP BY day, kind")
    con.commit()
    # 压缩摘要：按 ts 去重后单独入账（它不走 assistant/message，也不进 side_calls）
    comp = db.sync_compactions(con, events)
    out = [dict(r) for r in con.execute("SELECT * FROM side_calls ORDER BY day, kind")]
    if not quiet:
        tot = sum(r["tokens"] or 0 for r in out)
        print("side_calls: %d 天 %d 类，合计 token=%s（本次重解压 %d 个会话日志）"
              % (len({r["day"] for r in out}), len(out), "{:,}".format(tot), changed))
        if comp.get("rows"):
            print("压缩摘要: %d 次 / %.3f 元 / %s token"
                  % (comp["rows"], comp["cost"], "{:,}".format(
                      db.compaction_summary(con)["tokens"])))
    con.close()
    return {"rows": out, "changed": changed, "compactions": comp}


def show():
    con = db.connect()
    print("%-12s %-34s %8s %14s" % ("日期", "类型", "调用", "token"))
    for r in con.execute("SELECT * FROM side_calls ORDER BY day, tokens DESC"):
        print("%-12s %-34s %8d %14s" % (r["day"], r["kind"], r["calls"], "{:,}".format(r["tokens"])))
    con.close()


if __name__ == "__main__":
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    full = "--full" in sys.argv
    cmd = args[0] if args else "scan"
    if cmd == "show":
        show()
    else:
        scan(full=full)
