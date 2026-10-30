#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""hier.py —— 任意维度的分层统计（动态 GROUP BY，1~3 级）

维度（随便排列组合，1~3 个）：
  day        天
  hour       小时（0-23）
  week       周（YYYY-Www）
  workspace  工作区
  session    会话
  turn       会话内第几轮用户消息
  model      模型
  peak       高峰 / 非高峰
  depth      主会话 / 子代理

指标：cost（折算花费，默认）/ tokens / in / out / cache / calls

例：
  python hier.py day,workspace              # 天 → 工作区
  python hier.py workspace,day              # 工作区 → 天（换个顺序看同一份数据）
  python hier.py day,workspace,session      # 三级下钻
  python hier.py day,hour --range 2026-09-23
  python hier.py session --range 7d --metric tokens --top 20
  python hier.py peak --range all

口径：默认只算 keep=1（fork/resume 重放的历史不重复计），花费是按刊例价折算。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402

# 维度白名单：名称 -> SQL 表达式（必须来自这里，避免拼接注入）
DIMS = {
    "day":       "m.day",
    "hour":      "m.hour",
    "week":      "strftime('%Y-W%W', m.ts/1000, 'unixepoch', 'localtime')",
    "workspace": "s.workspace",
    "session":   "m.session_id",
    "turn":      "m.turn_no",
    "step":      "printf('%d.%d', m.turn_no, COALESCE(m.step_no,1))",
    "model":     "m.model",
    "peak":      "CASE WHEN m.peak=1 THEN 'peak' ELSE 'offpeak' END",
    "depth":     "CASE WHEN s.depth=0 THEN 'main' ELSE 'sub' END",
}

METRICS = {
    # cost_est < 0 = 未知价目（价目表里没这一档）：钱一律不计，只报 token
    "cost":    "SUM(CASE WHEN m.cost_est >= 0 THEN m.cost_est ELSE 0 END)",
    "cost_peak": "SUM(CASE WHEN m.peak=1 AND m.cost_est >= 0 THEN m.cost_est ELSE 0 END)",
    "tokens":  "SUM(m.total_tokens)",
    "in":      "SUM(m.in_tokens)",
    "out":     "SUM(m.out_tokens)",
    "cache":   "SUM(m.cache_read)",
    "calls":   "COUNT(*)",
}

SHORT = {"workspace": "ws"}


def pretty_workspace(w: str) -> str:
    """把 --C-Users-...-0-ds_harness_2-- 这种目录名压成短名。"""
    if not w:
        return "(空)"
    if w.startswith("--") and w.endswith("--"):
        return w.strip("-").split("-")[-1]
    return w


def run(dims, start, end, metric="cost", top=None, main_only=False, include_replay=False,
        session_like=None, quiet=False):
    if not dims:
        raise SystemExit("至少给一个维度")
    for d in dims:
        if d not in DIMS:
            raise SystemExit("不认识的维度 %r，可选：%s" % (d, ", ".join(sorted(DIMS))))
    if metric not in METRICS:
        raise SystemExit("不认识的指标 %r，可选：%s" % (metric, ", ".join(sorted(METRICS))))

    sel = ", ".join("%s AS d%d" % (DIMS[d], i) for i, d in enumerate(dims))
    grp = ", ".join("d%d" % i for i in range(len(dims)))
    where = ["m.day BETWEEN ? AND ?", "m.day<>''"]
    if not include_replay:
        where.append("m.keep=1")
    if main_only:
        where.append("s.depth=0")
    if session_like:
        where.append("m.session_id LIKE ?")
    args = [start, end] + ([session_like] if session_like else [])

    # 主指标 + 同时带上 token 三件套和调用数，方便看组成
    sql = """
      SELECT {sel},
             {metric} AS metric,
             COUNT(*) AS calls,
             SUM(m.cost_est) AS cost,
             SUM(CASE WHEN m.peak=1 THEN m.cost_est ELSE 0 END) AS cost_peak,
             SUM(m.total_tokens) AS tokens,
             SUM(m.in_tokens) AS in_tok,
             SUM(m.out_tokens) AS out_tok,
             SUM(m.cache_read) AS cache_tok
      FROM messages m JOIN sessions s ON s.session_id = m.session_id
      WHERE {where}
      GROUP BY {grp}
      ORDER BY metric DESC
    """.format(sel=sel, metric=METRICS[metric],
               where=" AND ".join(where), grp=grp)

    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()
    if top:
        rows = rows[:top]
    if not quiet:
        print_rows(dims, metric, rows, start, end)
    return rows


def print_rows(dims, metric, rows, start, end):
    total = sum(r["metric"] for r in rows) or 1.0
    names = [SHORT.get(d, d) for d in dims]
    width = 26
    head = "  ".join("%-*s" % (width if d in ("workspace", "session") else 12, n) for d, n in zip(dims, names))
    print("区间 %s .. %s   指标=%s   共 %d 组" % (start, end, metric, len(rows)))
    print(head + "  %10s %7s  %12s %12s %12s %8s" % (
        {"cost": "折算元", "cost_peak": "peak元", "tokens": "token", "in": "未命中",
         "out": "输出", "cache": "缓存命中", "calls": "调用数"}[metric],
        "占比", "未命中", "输出", "缓存命中", "调用"))
    print("-" * (len(head) + 90))
    for r in rows:
        cells = []
        for i, d in enumerate(dims):
            v = r["d%d" % i]
            if d == "workspace":
                v = pretty_workspace(v)
            elif d == "session" and v:
                v = v[-12:]
            cells.append("%-*s" % (width if d in ("workspace", "session") else 12, str(v)))
        print("  ".join(cells) + "  %10.3f %6.1f%%  %12s %12s %12s %8d" % (
            r["metric"], r["metric"] / total * 100,
            "{:,}".format(r["in_tok"] or 0), "{:,}".format(r["out_tok"] or 0),
            "{:,}".format(r["cache_tok"] or 0), r["calls"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="分层统计（任意 1~3 个维度）")
    ap.add_argument("dims", help="逗号分隔的维度，如 day,workspace 或 workspace,day,session")
    ap.add_argument("--range", default="all", help="all/today/7d/30d/A..B（默认 all）")
    ap.add_argument("--metric", default="cost", choices=sorted(METRICS))
    ap.add_argument("--top", type=int, default=0, help="只看前 N 组")
    ap.add_argument("--main-only", action="store_true", help="只算主会话（不含子代理）")
    ap.add_argument("--with-replay", action="store_true", help="把 fork/resume 重放的也计入（默认不算）")
    ap.add_argument("--session", default=None, help="只看某个会话（id 片段即可）")
    a = ap.parse_args(argv)
    dims = [x.strip() for x in a.dims.split(",") if x.strip()]
    if len(dims) > 3:
        raise SystemExit("最多 3 级（你给了 %d 级）" % len(dims))
    con = db.connect()
    try:
        start, end = db.parse_range(a.range, con)
    finally:
        con.close()
    rows = run(dims, start, end, a.metric, a.top or None, a.main_only, a.with_replay, a.session)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
