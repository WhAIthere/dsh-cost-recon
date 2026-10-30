#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""req_check.py —— 对账的"次数维"：官方请求数 vs 我们日志里的调用数

为什么要有这个：钱差一点点的时候，光看金额分不清是"我们价格算错"还是"根本没记到这笔"。
**次数**最直白：官方小时桶里有 requests，我们这边能把每次调用数出来 ——

    我们调用数 = 正常 message + 中断空壳 + 原生搜索（calls_n）+ 压缩摘要

对不上的那些小时，基本就是"钱花了、日志里没有"的调用：请求发出去了但我们这边没留下
assistant/message（重试、被中止、turn 被硬删、或者像 10-17 那样对面没收到）。这类**补不了**
（没有 token 账可算），只能手工补金额（见 MAINTAIN §2.11 之后的补记做法）。

用法（只读，不动库）：
  python src\\req_check.py              # 只看差 ≥2 次的小时
  python src\\req_check.py --all        # 看所有有差异的小时
  python src\\req_check.py --days 7     # 只看最近 7 天
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402


def collect(con, day_from=None, day_to=None):
    """{(day,hour): (官方请求, 我们的调用, 空壳数, 官方金额, 我们的金额)}"""
    where, args = " WHERE api_key_name=?", [db.resolve_key_name(con)]
    if day_from:
        where += " AND day>=?"
        args.append(day_from)
    if day_to:
        where += " AND day<=?"
        args.append(day_to)
    rows = {}
    for r in con.execute("SELECT day, hour, requests, cost FROM official_hour" + where +
                         " ORDER BY day, hour", args):
        rows[(r["day"], r["hour"])] = [r["requests"] or 0, 0, 0, r["cost"] or 0.0, 0.0]

    def bump(day, hour, n, cost, shell=0):
        k = (day, hour)
        if k not in rows:
            return                      # 官方没有这一小时：不参与次数对账
        rows[k][1] += n
        rows[k][2] += shell
        rows[k][4] += cost or 0.0

    for r in con.execute("SELECT day, hour, COUNT(*) n, SUM(cost_est) c FROM messages "
                         "WHERE keep=1 AND interrupted=0 AND day<>'' GROUP BY day, hour"):
        bump(r["day"], r["hour"], r["n"], r["c"])
    for r in con.execute("SELECT day, hour, COUNT(*) n FROM messages "
                         "WHERE interrupted=1 AND day<>'' GROUP BY day, hour"):
        bump(r["day"], r["hour"], 0, 0, shell=r["n"])
    for r in con.execute("SELECT day, hour, SUM(calls_n) n, SUM(cost_est) c FROM search_calls "
                         "GROUP BY day, hour"):
        bump(r["day"], r["hour"], r["n"] or 0, r["c"])
    for r in con.execute("SELECT day, hour, COUNT(*) n, SUM(cost_est) c FROM compaction_events "
                         "GROUP BY day, hour"):
        bump(r["day"], r["hour"], r["n"], r["c"])
    for r in con.execute("SELECT day, hour, n FROM lost_calls"):
        bump(r["day"], r["hour"], r["n"], None)   # 空转：次数补上（钱已单独记在 lost_calls）
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="官方请求数 vs 我们调用数（对账的次数维）")
    ap.add_argument("--all", action="store_true", help="显示所有有差异的小时（默认只看差 ≥2 次）")
    ap.add_argument("--days", type=int, default=0, help="只看最近 N 天")
    a = ap.parse_args(argv)

    con = db.connect()
    try:
        key = db.resolve_key_name(con)
        day_to = con.execute("SELECT MAX(day) d FROM official_hour WHERE api_key_name=?",
                             (key,)).fetchone()["d"]
        day_from = None
        if a.days and day_to:
            day_from = (date.fromisoformat(day_to) - timedelta(days=a.days - 1)).isoformat()
        rows = collect(con, day_from, day_to)
    finally:
        con.close()

    if not rows:
        print("没有官方小时桶数据（先跑 refresh.py）")
        return 1
    tot_off = sum(v[0] for v in rows.values())
    tot_our = sum(v[1] for v in rows.values())
    diff_hours = [(k, v) for k, v in rows.items() if v[0] != v[1]]
    print("时间范围: %s ~ %s（%d 个小时）" % (min(k[0] for k in rows), max(k[0] for k in rows), len(rows)))
    print("官方请求合计 %d ｜ 我们调用合计 %d ｜ 差 %+d（%.2f%%）"
          % (tot_off, tot_our, tot_off - tot_our, 100.0 * (tot_off - tot_our) / max(1, tot_off)))
    print("有差异的小时: %d / %d（差 ≥3 次的 %d 个）"
          % (len(diff_hours), len(rows), sum(1 for _k, v in diff_hours if abs(v[0] - v[1]) >= 3)))
    print()
    show = [(k, v) for k, v in diff_hours
            if a.all or abs(v[0] - v[1]) >= 3]
    if not show:
        print("没有差 ≥3 次的小时（--all 可看全部差异）")
        return 0
    print("日期          时 官方  我们  差   空壳  官方金额  我们金额")
    for (day, hour), v in sorted(show, key=lambda x: (-abs(x[1][0] - x[1][1]), x[0])):
        print("%s %2dh %5d %5d %+4d %4d  %8.4f  %8.4f"
              % (day, hour, v[0], v[1], v[0] - v[1], v[2], v[3], v[4]))
    print()
    print("差 = 官方多出来的请求：钱花了但日志里没有这一次调用（重试 / 中止 / turn 被硬删 /")
    print("     对面没收到）。没有 token 账可算，只能手工补金额。空壳数 >0 的小时是另一种情况：")
    print("     那次调用留下了 message 但没 usage，按小时差额估过（详见 MAINTAIN §2.6）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
