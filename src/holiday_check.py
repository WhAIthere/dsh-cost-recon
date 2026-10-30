#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""holiday_check.py —— 用官方小时桶"自校准"高峰倍率，并和节假日表对照

原理（一小时一行）：
    倍率 = 官方金额 ÷ 官方这一小时的 token 按【纯刊例价】折出来的钱
    ≈ 1.000 → 这小时官方按【非高峰】收
    ≈ 2.000 → 这小时官方按【高峰】收
用**官方自己的 token**去折价，所以"官方比我们多算了几笔（旁路/缺账）"不会污染这个比值 ——
它读出来的就是官方实际用的那个倍率。我们这一侧的 token 只用来做旁证（差得多就提示缺账）。

然后拿观测结果和两种判定对比：
    · 旧规则   = db.is_peak（只看周末 + 两个高峰窗口）
    · 节假日表 = holidays.txt（放假日全天非高峰、调休上班日按工作日算）

**只读**：不改库、不改判定、不写任何 DSH 文件。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db                    # noqa: E402
import holidays as HOL       # noqa: E402

TOL = 0.08                   # 倍率容差：±8%
TOK_TOL = 0.03               # 我们/官方 token 差多少算"缺账"


def near(x: float, target: float, tol: float = TOL) -> bool:
    return abs(x - target) <= tol * target


def official_hours(con, key: str, day_from=None, day_to=None):
    """官方小时桶（含 token 组成）→ {(day, hour): {...}}"""
    q = ("SELECT day, hour, SUM(cache_hit) ch, SUM(cache_miss) cm, SUM(output) om, "
         "SUM(cost) cost, SUM(requests) req FROM official_hour WHERE api_key_name=?")
    a = [key]
    if day_from:
        q += " AND day>=?"
        a.append(day_from)
    if day_to:
        q += " AND day<=?"
        a.append(day_to)
    q += " GROUP BY day, hour"
    out = {}
    for r in con.execute(q, a):
        ch, cm, om = r["ch"] or 0, r["cm"] or 0, r["om"] or 0
        out[(r["day"], r["hour"])] = {
            "cost": r["cost"] or 0.0, "ch": ch, "cm": cm, "om": om, "req": r["req"] or 0,
            "tokens": ch + cm + om,
            "base": (ch * db.PRICE["cacheHit"] + cm * db.PRICE["inputMiss"]
                     + om * db.PRICE["output"]) / 1e6}
    return out


def our_hours(con, day_from=None, day_to=None):
    """我们这一侧：逐小时的已计入金额、token，以及**库里实际用于算钱的 peak 标记**"""
    q = ("SELECT day, hour, SUM(cache_read) cr, SUM(in_tokens + cache_write) cm, "
         "SUM(out_tokens) om, SUM(cost_est) charged, SUM(est_cost) alloc "
         "FROM messages WHERE keep=1")
    a = []
    if day_from:
        q += " AND day>=?"
        a.append(day_from)
    if day_to:
        q += " AND day<=?"
        a.append(day_to)
    q += " GROUP BY day, hour"
    out = {}
    for r in con.execute(q, a):
        cr, cm, om = r["cr"] or 0, r["cm"] or 0, r["om"] or 0
        out[(r["day"], r["hour"])] = {
            "charged": r["charged"] or 0.0, "alloc": r["alloc"] or 0.0,
            "tokens": cr + cm + om, "used": None}
    # 库里实际的 peak（自校准之后应该 = 官方实测倍率）
    q2 = ("SELECT day, hour, SUM(peak) sp, COUNT(*) n FROM messages "
          "WHERE keep=1 AND interrupted=0")
    a2 = []
    if day_from:
        q2 += " AND day>=?"
        a2.append(day_from)
    if day_to:
        q2 += " AND day<=?"
        a2.append(day_to)
    q2 += " GROUP BY day, hour"
    for r in con.execute(q2, a2):
        k = (r["day"], r["hour"])
        sp, n = r["sp"] or 0, r["n"] or 0
        out.setdefault(k, {"charged": 0.0, "alloc": 0.0, "tokens": 0})
        out[k]["used"] = 1 if sp == n and n else (0 if sp == 0 else -1)   # -1 = 同一小时里混了
    return out


def infer(base: float, ratio: float):
    """→ (观测, 备注)：观测 ∈ {'peak','offpeak','倍率异常','样本太小'}"""
    if base < 0.005:
        return "样本太小", "金额(%.4f元)太小，比值没意义" % base
    if near(ratio, 1.0):
        return "offpeak", ""
    if near(ratio, 2.0):
        return "peak", ""
    return "倍率异常", "官方实际倍率 %.3f，既不像 1 也不像 2" % ratio


def ts_of(day: str, hour: int) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(hour=hour).timestamp() * 1000)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="官方小时桶自校准 vs 节假日表")
    ap.add_argument("--from", dest="day_from", default=None, help="起始日期 YYYY-MM-DD")
    ap.add_argument("--to", dest="day_to", default=None, help="结束日期 YYYY-MM-DD")
    ap.add_argument("--key", default="", help="只认这把 key（留空 = 自动：库里花钱最多的那把）")
    ap.add_argument("--all", action="store_true", help="把每一小时都列出来（默认只列有问题的）")
    ap.add_argument("--db", default=None, help="库路径（默认 temp/token_stats.db）")
    args = ap.parse_args(argv)

    con = db.connect(args.db)          # connect() 自带 row_factory=sqlite3.Row
    table = HOL.load(force=True)
    off = official_hours(con, args.key, args.day_from, args.day_to)
    ours = our_hours(con, args.day_from, args.day_to)

    print("== 节假日高峰倍率自校准 ==")
    if off:
        days = sorted({k[0] for k in off})
        print("官方小时桶：%s ~ %s，%d 小时（key=%s）" % (days[0], days[-1], len(off), args.key))
    else:
        print("官方小时桶：空 —— 先跑 `python src\\ds_usage.py --days 14`")
    print(table.describe())
    for w in table.warnings:
        print("  WARN", w)
    print("口径：倍率 = 官方金额 ÷ 官方token按纯刊例价的折算 → 1.000=非高峰 / 2.000=高峰")
    print("      （用官方自己的 token 折价，所以官方多算的那几笔不会污染倍率）")
    print()

    rows, stat = [], {"hol_ok": 0, "hol_bad": 0, "old_ok": 0, "old_bad": 0, "na": 0,
                      "use_ok": 0, "use_bad": 0}
    for k in sorted(set(off) | set(ours)):
        day, hour = k
        o = off.get(k)
        u = ours.get(k) or {"charged": 0.0, "alloc": 0.0, "tokens": 0}
        if o is None:
            continue                    # 我们有、官方没有这小时 → 不参与校准
        ratio = o["cost"] / o["base"] if o["base"] else 0.0
        obs, why = infer(o["base"], ratio)
        ts = ts_of(day, hour)
        p_old = db.is_peak(ts)
        p_hol = table.is_peak(ts)
        decidable = obs in ("peak", "offpeak")
        tok_ratio = (u["tokens"] / o["tokens"]) if o["tokens"] else None
        used = u.get("used")
        r = {"day": day, "hour": hour, "official": o["cost"], "base": o["base"],
             "ratio": ratio, "req": o["req"], "our_charged": u["charged"],
             "our_alloc": u["alloc"], "tok_ratio": tok_ratio, "used": used,
             "obs": obs, "why": why, "old": p_old, "hol": p_hol,
             "old_ok": (p_old == (obs == "peak")) if decidable else None,
             "hol_ok": (p_hol == (obs == "peak")) if decidable else None,
             "used_ok": (used == (1 if obs == "peak" else 0)) if decidable and used in (0, 1)
             else None}
        rows.append(r)
        if not decidable:
            stat["na"] += 1
            continue
        stat["hol_ok" if r["hol_ok"] else "hol_bad"] += 1
        stat["old_ok" if r["old_ok"] else "old_bad"] += 1
        if r["used_ok"] is not None:
            stat["use_ok" if r["used_ok"] else "use_bad"] += 1

    # ---------------------------------------------------------------- 逐日小计
    print("[逐日小计]")
    print("  天         官方(元)  官方/刊例 观测(峰/谷/难)  旧规则错 节假日表错 库内错  结论")
    for d in sorted({r["day"] for r in rows}):
        rs = [r for r in rows if r["day"] == d]
        oc = sum(r["official"] for r in rs)
        ob = sum(r["base"] for r in rs)
        pk = sum(1 for r in rs if r["obs"] == "peak")
        op = sum(1 for r in rs if r["obs"] == "offpeak")
        na = sum(1 for r in rs if r["obs"] not in ("peak", "offpeak"))
        bad_h = [r for r in rs if r["hol_ok"] is False]
        bad_o = [r for r in rs if r["old_ok"] is False]
        bad_u = [r for r in rs if r["used_ok"] is False]
        verdict = "✅ 表与观测一致" if not bad_h else "❌ 表错 %d 小时" % len(bad_h)
        if not bad_u and bad_o:
            verdict += "（旧规则错 %d 小时，库内已按校验校正）" % len(bad_o)
        if bad_u:
            verdict += " ❌ 库内还有 %d 小时没校正" % len(bad_u)
        if na == len(rs):
            verdict = "— 无法判定（样本小）"
        print("  %s  %8.3f  %7.3f  峰%d 谷%d 难%d    %3d      %3d      %3d   %s"
              % (d, oc, oc / ob if ob else 0.0, pk, op, na, len(bad_o), len(bad_h),
                 len(bad_u), verdict))

    # ---------------------------------------------------------------- 明细
    sel = [r for r in rows if args.all or (r["hol_ok"] is False) or (r["old_ok"] is False)
           or (r["used_ok"] is False) or r["obs"] in ("倍率异常", "样本太小")]
    if sel:
        print()
        print("[%s]" % ("全部小时" if args.all else "有问题的 / 被修正的小时"))
        print("  日期 小时 | 官方    官方/刊例 | 观测     我们token/官方token | "
              "旧规则  节假日表  库内peak | 判定(旧/表/库)")
        for r in sel:
            def mk(x):
                return "✅" if x else ("❌" if x is False else "·")
            mark = mk(r["old_ok"]) + mk(r["hol_ok"]) + mk(r["used_ok"])
            obs = {"peak": "高峰", "offpeak": "非高峰"}.get(r["obs"], r["obs"])
            tr = "%.3f" % r["tok_ratio"] if r["tok_ratio"] is not None else "  -  "
            used_txt = {1: "高峰", 0: "非高峰", -1: "混合", None: "  -  "}[r["used"]]
            extra = ("  ← " + r["why"]) if r["why"] else ""
            print("  %s %2dh | %7.4f %8.3f | %-8s %-10s | %-6s %-6s %-6s | %s%s"
                  % (r["day"], r["hour"], r["official"], r["ratio"], obs, tr,
                     "高峰" if r["old"] else "非高峰", "高峰" if r["hol"] else "非高峰",
                     used_txt, mark, extra))

    print()
    n_use = stat["use_ok"] + stat["use_bad"]
    print("[合计] 可判定 %d 小时：节假日表 对 %d / 错 %d；规则判定 对 %d / 错 %d；"
          "**库内实际算钱的标记 对 %d / 错 %d**（%d 小时没对上官方数据）；无法判定 %d"
          % (stat["hol_ok"] + stat["hol_bad"], stat["hol_ok"], stat["hol_bad"],
             stat["old_ok"], stat["old_bad"], stat["use_ok"], stat["use_bad"],
             (stat["hol_ok"] + stat["hol_bad"]) - n_use, stat["na"]))
    hint = table.update_hint()
    print("[提示] " + (hint if hint else "数据都在节假日表有效期内（%s）" % table.range_text()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
