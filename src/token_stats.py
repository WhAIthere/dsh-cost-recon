#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""token_stats.py —— DSH 本机 token 用量 / 花费统计（命令行 + PNG 图 + CSV）

数据来源（全部只读，绝不写回 DSH 的文件）：
  1) ~/.dsh/.dshw-usage.json     逐次计费事件 + 每日合计 + 余额   ← 最接近真实账单
  2) ~/.dsh/dsh-usage/usage-ledger.json  按天×模型的 token 明细    ← 口径偏低，只当明细参考

本地累积库：<脚本目录>/token_stats.db （SQLite，幂等 upsert，跑多少次都不会重复计数）

用法：
  python token_stats.py sync                     把当前数据吸入本地库
  python token_stats.py show                     总览 + 每日柱状（终端 ASCII）
  python token_stats.py show 7d                  指定区间（7d / 30d / today / 2026-09-17..2026-09-23）
  python token_stats.py hours 2026-09-23         某天按小时的花费分布
  python token_stats.py export out/              导出 CSV（daily / events / models）
  python token_stats.py chart out/ [range]       生成 PNG 图（存到 out/）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "temp", "token_stats.db")

HOME = os.path.expanduser("~")
DSH_DIR = os.path.join(HOME, ".dsh")
USAGE_JSON = os.path.join(DSH_DIR, ".dshw-usage.json")
LEDGER_JSON = os.path.join(DSH_DIR, "dsh-usage", "usage-ledger.json")

# ---------------------------------------------------------------- 价目表
# 来源：~/.dsh/profiles/web/node_modules/@linxin666/dsh-usage/lib/index.js
# FLASH_PRICE：cacheHit 0.02 / inputMiss 1 / output 4（CNY 每百万 token，offPeak）
# peak＝工作日（周一~周五）北京时间 09:00-12:00 与 14:00-18:00，价格 ×2
FLASH_PRICE = {"cacheHit": 0.02, "inputMiss": 1.0, "output": 4.0}
PEAK_MULTIPLIER = 2.0
PEAK_WINDOWS = ((9, 12), (14, 18))


def is_peak(dt: datetime) -> bool:
    """dt 用本地时间（本机即北京时间）。"""
    if dt.weekday() >= 5:          # 周六周日
        return False
    h = dt.hour
    return any(a <= h < b for a, b in PEAK_WINDOWS)


def unit_price(peak: bool) -> dict:
    m = PEAK_MULTIPLIER if peak else 1.0
    return {k: v * m for k, v in FLASH_PRICE.items()}


# ---------------------------------------------------------------- 读源文件
def read_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def local_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0).strftime("%Y-%m-%d")


def load_sources():
    """返回 (events, days, snapshots)；缺文件不报错，返回空。"""
    events, days, snaps = [], [], []
    if os.path.exists(USAGE_JSON):
        j = read_json(USAGE_JSON)
        for e in j.get("events") or []:
            ts = int(e.get("ts") or 0)
            events.append({
                "ts": ts,
                "day": local_day(ts) if ts else str(e.get("day") or ""),
                "file_day": str(e.get("day") or ""),
                "model": str(e.get("model") or "unknown"),
                "tokens": int(e.get("tokens") or 0),
                "cost": float(e.get("cost") or 0.0),
            })
        snaps.append({
            "captured_at": int(datetime.now().timestamp()),
            "file_date": str(j.get("date") or ""),
            "balance": j.get("lastBalance"),
            "today_usage": j.get("todayUsage"),
            "day_start": j.get("dayStart"),
        })
    if os.path.exists(LEDGER_JSON):
        j = read_json(LEDGER_JSON)
        for day, providers in (j.get("days") or {}).items():
            for provider, models in (providers or {}).items():
                for model, u in (models or {}).items():
                    days.append({
                        "day": day, "provider": provider, "model": model,
                        "input": int(u.get("inputTokens") or 0),
                        "output": int(u.get("outputTokens") or 0),
                        "cache_read": int(u.get("cacheReadTokens") or 0),
                        "cache_write": int(u.get("cacheWriteTokens") or 0),
                        "reasoning": int(u.get("reasoningTokens") or 0),
                        "calls": int(u.get("calls") or 0),
                        "cost": float(u.get("cost") or 0.0),
                    })
    return events, days, snaps


# ---------------------------------------------------------------- 本地累积库
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  ts INTEGER PRIMARY KEY, day TEXT, file_day TEXT, model TEXT,
  tokens INTEGER, cost REAL, seen_at INTEGER
);
CREATE TABLE IF NOT EXISTS day_model (
  day TEXT, provider TEXT, model TEXT,
  input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER,
  reasoning INTEGER, calls INTEGER, cost REAL,
  PRIMARY KEY (day, provider, model)
);
CREATE TABLE IF NOT EXISTS snapshots (
  captured_at INTEGER PRIMARY KEY, file_date TEXT,
  balance REAL, today_usage REAL, day_start REAL
);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def sync(con: sqlite3.Connection) -> dict:
    events, days, snaps = load_sources()
    now = int(datetime.now().timestamp())
    n_ev = n_dm = n_sn = 0

    def fnum(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    con.executemany(
        "INSERT INTO events (ts,day,file_day,model,tokens,cost,seen_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(ts) DO UPDATE SET day=excluded.day, model=excluded.model, "
        "tokens=excluded.tokens, cost=excluded.cost",
        [(e["ts"], e["day"], e["file_day"], e["model"], e["tokens"], e["cost"], now) for e in events])
    n_ev = len(events)

    cur = con.executemany(
        "INSERT INTO day_model (day,provider,model,input,output,cache_read,cache_write,"
        "reasoning,calls,cost) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(day,provider,model) DO UPDATE SET input=excluded.input, output=excluded.output, "
        "cache_read=excluded.cache_read, cache_write=excluded.cache_write, "
        "reasoning=excluded.reasoning, calls=excluded.calls, cost=excluded.cost",
        [(d["day"], d["provider"], d["model"], d["input"], d["output"], d["cache_read"],
          d["cache_write"], d["reasoning"], d["calls"], d["cost"]) for d in days])
    n_dm = len(days)

    for s in snaps:
        con.execute(
            "INSERT INTO snapshots (captured_at,file_date,balance,today_usage,day_start) "
            "VALUES (?,?,?,?,?) ON CONFLICT(captured_at) DO NOTHING",
            (s["captured_at"], s["file_date"], fnum(s["balance"]),
             fnum(s["today_usage"]), fnum(s["day_start"])))
        n_sn += 1
    con.commit()
    return {"events": n_ev, "day_model": n_dm, "snapshots": n_sn}


# ---------------------------------------------------------------- 区间解析
def parse_range(spec: str, con: sqlite3.Connection):
    """返回 (start_day, end_day)，闭区间，'YYYY-MM-DD'。"""
    today = date.today()
    row = con.execute("SELECT MAX(day) d FROM (SELECT day FROM events UNION SELECT day FROM day_model)").fetchone()
    last = row["d"] if row and row["d"] else today.isoformat()

    s = (spec or "all").strip().lower()
    if s in ("all", ""):
        row = con.execute("SELECT MIN(day) d FROM (SELECT day FROM events UNION SELECT day FROM day_model)").fetchone()
        return (row["d"] or last, last)
    if s == "today":
        return (today.isoformat(), today.isoformat())
    if s == "yesterday":
        y = today - timedelta(days=1)
        return (y.isoformat(), y.isoformat())
    if s in ("week", "7d", "month", "30d", "year", "365d", "90d"):
        n = {"week": 7, "7d": 7, "month": 30, "30d": 30, "90d": 90, "year": 365, "365d": 365}[s]
        return ((today - timedelta(days=n - 1)).isoformat(), today.isoformat())
    if re.fullmatch(r"\d{1,3}d", s):     # 任意天数：3d / 14d / 100d
        n = max(1, int(s[:-1]))
        return ((today - timedelta(days=n - 1)).isoformat(), today.isoformat())
    if ".." in s:
        a, b = s.split("..", 1)
        return (a or last, b or last)
    # 单日或单月前缀
    if len(s) == 10:
        return (s, s)
    if len(s) == 7:                      # YYYY-MM
        return (s + "-01", s + "-31")
    raise SystemExit("无法识别的区间: %r（用 all/today/yesterday/7d/30d/90d/2026-09-17..2026-09-23）" % spec)


# ---------------------------------------------------------------- 统计
def day_costs(con, start, end):
    """按天花费。以事件表为准；某天没事件时回落到 ledger 合计。"""
    q = """SELECT d.day AS day,
                  COALESCE(ev.cost, dm.cost, 0) AS cost,
                  COALESCE(ev.tokens, 0) AS tokens,
                  COALESCE(ev.calls, 0) AS calls,
                  dm.cost AS ledger_cost,
                  (COALESCE(dm.input,0)+COALESCE(dm.output,0)+COALESCE(dm.cache_read,0)
                   +COALESCE(dm.cache_write,0)) AS ledger_tokens
           FROM (SELECT day FROM events WHERE day BETWEEN ? AND ?
                 UNION SELECT day FROM day_model WHERE day BETWEEN ? AND ?) d
           LEFT JOIN (SELECT day, SUM(cost) cost, SUM(tokens) tokens, COUNT(*) calls
                      FROM events WHERE day BETWEEN ? AND ? GROUP BY day) ev ON ev.day = d.day
           LEFT JOIN (SELECT day, SUM(cost) cost, SUM(input) input, SUM(output) output,
                             SUM(cache_read) cache_read, SUM(cache_write) cache_write
                      FROM day_model WHERE day BETWEEN ? AND ? GROUP BY day) dm ON dm.day = d.day
           ORDER BY d.day"""
    args = (start, end) * 4
    return [dict(r) for r in con.execute(q, args)]


def model_costs(con, start, end):
    return [dict(r) for r in con.execute(
        "SELECT model, SUM(cost) cost, SUM(tokens) tokens, COUNT(*) calls FROM events "
        "WHERE day BETWEEN ? AND ? GROUP BY model ORDER BY cost DESC", (start, end))]


def hour_costs(con, day):
    rows = [dict(r) for r in con.execute(
        "SELECT ts, cost, tokens FROM events WHERE day = ? ORDER BY ts", (day,))]
    buckets = {h: {"cost": 0.0, "tokens": 0, "calls": 0} for h in range(24)}
    for r in rows:
        h = datetime.fromtimestamp(r["ts"] / 1000.0).hour
        buckets[h]["cost"] += r["cost"]
        buckets[h]["tokens"] += r["tokens"]
        buckets[h]["calls"] += 1
    return buckets


def ledger_days(con, start, end):
    return [dict(r) for r in con.execute(
        "SELECT day, provider, model, input, output, cache_read, cache_write, reasoning, calls, cost "
        "FROM day_model WHERE day BETWEEN ? AND ? ORDER BY day, cost DESC", (start, end))]


def ledger_daily(con, start, end):
    return [dict(r) for r in con.execute(
        "SELECT day, SUM(input) input, SUM(output) output, SUM(cache_read) cache_read, "
        "SUM(cache_write) cache_write, SUM(reasoning) reasoning, SUM(calls) calls, SUM(cost) cost "
        "FROM day_model WHERE day BETWEEN ? AND ? GROUP BY day ORDER BY day", (start, end))]


def summary(con, start, end):
    dc = day_costs(con, start, end)
    total = sum(d["cost"] for d in dc)
    used = [d for d in dc if d["cost"] > 0]
    best = max(dc, key=lambda d: d["cost"]) if dc else None
    worst = min(used, key=lambda d: d["cost"]) if used else None
    snap = con.execute("SELECT * FROM snapshots ORDER BY captured_at DESC LIMIT 1").fetchone()
    return {
        "range": (start, end),
        "days": len(dc),
        "total": total,
        "avg": (total / len(used)) if used else 0.0,
        "max": best,
        "min": worst,
        "events": con.execute("SELECT COUNT(*) c FROM events WHERE day BETWEEN ? AND ?",
                              (start, end)).fetchone()["c"],
        "tokens": con.execute("SELECT COALESCE(SUM(tokens),0) t FROM events WHERE day BETWEEN ? AND ?",
                              (start, end)).fetchone()["t"],
        "balance": snap["balance"] if snap else None,
        "today_usage": snap["today_usage"] if snap else None,
        "daily": dc,
        "models": model_costs(con, start, end),
    }


# ---------------------------------------------------------------- 终端输出
def _bar(frac: float, width: int = 34) -> str:
    frac = max(0.0, min(1.0, frac))
    return "#" * int(round(frac * width))


def print_summary(s: dict) -> None:
    a, b = s["range"]
    print("=" * 62)
    print("DSH 用量 / 花费总览   %s .. %s" % (a, b))
    print("=" * 62)
    print("合计花费      %.2f 元" % s["total"])
    print("有花费天数    %d 天（区间内 %d 天），日均 %.2f 元" % (
        len([d for d in s["daily"] if d["cost"] > 0]), s["days"], s["avg"]))
    if s["max"]:
        print("最贵一天      %s  %.2f 元" % (s["max"]["day"], s["max"]["cost"]))
    if s["min"]:
        print("最省一天      %s  %.2f 元" % (s["min"]["day"], s["min"]["cost"]))
    print("计费调用      %d 次，上报 token %s" % (s["events"], "{:,}".format(s["tokens"])))
    if s["balance"] is not None:
        print("当前余额      %.2f 元" % s["balance"])
    print("-" * 62)
    if s["daily"]:
        mx = max(d["cost"] for d in s["daily"]) or 1.0
        print("每日花费（# = %.3f 元）" % (mx / 34.0))
        for d in s["daily"]:
            print("  %s  %7.3f 元 |%s" % (d["day"], d["cost"], _bar(d["cost"] / mx)))
    if s["models"]:
        print("-" * 62)
        print("按模型")
        for m in s["models"]:
            print("  %-22s %8.3f 元  %6d 次  %s token" % (
                m["model"], m["cost"], m["calls"], "{:,}".format(m["tokens"])))
    print("=" * 62)


def print_hours(day: str, buckets: dict) -> None:
    print("=" * 62)
    print("%s 按小时花费" % day)
    print("=" * 62)
    mx = max((v["cost"] for v in buckets.values()), default=0) or 1.0
    for h in range(24):
        v = buckets[h]
        if v["calls"] == 0:
            continue
        mark = "peak" if is_peak(datetime.strptime(day, "%Y-%m-%d").replace(hour=h)) else "    "
        print("  %02d时 %s %7.3f 元 |%s  %d 次" % (
            h, mark, v["cost"], _bar(v["cost"] / mx), v["calls"]))
    print("=" * 62)


# ---------------------------------------------------------------- CSV
def export_csv(con, outdir: str, start: str, end: str) -> list:
    os.makedirs(outdir, exist_ok=True)
    files = []

    p = os.path.join(outdir, "daily_%s_%s.csv" % (start, end))
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["日期", "花费_元", "上报token", "计费调用次数", "ledger花费_元", "ledger_tokens"])
        for d in day_costs(con, start, end):
            w.writerow([d["day"], "%.6f" % d["cost"], d["tokens"], d["calls"],
                        "%.6f" % (d["ledger_cost"] or 0), d["ledger_tokens"] or 0])
    files.append(p)

    p = os.path.join(outdir, "events_%s_%s.csv" % (start, end))
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["时间", "日期", "模型", "花费_元", "token"])
        for r in con.execute("SELECT ts,day,model,cost,tokens FROM events WHERE day BETWEEN ? AND ? "
                             "ORDER BY ts", (start, end)):
            w.writerow([datetime.fromtimestamp(r["ts"] / 1000.0).strftime("%Y-%m-%d %H:%M:%S"),
                        r["day"], r["model"], "%.6f" % r["cost"], r["tokens"]])
    files.append(p)

    p = os.path.join(outdir, "models_%s_%s.csv" % (start, end))
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["日期", "服务商", "模型", "输入token", "输出token", "缓存命中token",
                    "缓存写入token", "推理token", "调用次数", "ledger花费_元"])
        for r in ledger_days(con, start, end):
            w.writerow([r["day"], r["provider"], r["model"], r["input"], r["output"],
                        r["cache_read"], r["cache_write"], r["reasoning"], r["calls"],
                        "%.6f" % r["cost"]])
    files.append(p)
    return files


# ---------------------------------------------------------------- PNG 图
def make_charts(con, outdir: str, start: str, end: str) -> list:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    for name in ("Microsoft YaHei", "SimHei", "DengXian"):
        try:
            font_manager.findfont(font_manager.FontProperties(family=name), fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [name]
            break
        except Exception:
            continue
    plt.rcParams["axes.unicode_minus"] = False

    os.makedirs(outdir, exist_ok=True)
    made = []
    dc = day_costs(con, start, end)
    lc = ledger_daily(con, start, end)
    days = [d["day"] for d in dc]
    costs = [d["cost"] for d in dc]

    # 1) 每日花费 + 累计
    fig, ax = plt.subplots(figsize=(11, 4.6), dpi=130)
    ax.bar(days, costs, color="#4c8bf5", label="每日花费")
    ax.set_ylabel("花费（元）")
    ax.set_title("每日花费  %s .. %s" % (start, end))
    ax.tick_params(axis="x", rotation=45)
    ax2 = ax.twinx()
    cum, run = [], 0.0
    for c in costs:
        run += c
        cum.append(run)
    ax2.plot(days, cum, color="#e8710a", marker="o", ms=4, lw=1.6, label="累计")
    ax2.set_ylabel("累计（元）")
    for i, c in enumerate(costs):
        ax.text(i, c, "%.2f" % c, ha="center", va="bottom", fontsize=8)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=9)
    fig.tight_layout()
    p = os.path.join(outdir, "01_daily_cost.png")
    fig.savefig(p); plt.close(fig); made.append(p)

    # 2) token 构成环形图（输入 / 缓存命中 / 输出，来自 ledger 明细）
    ti = sum(r["input"] for r in lc); to = sum(r["output"] for r in lc)
    tc = sum(r["cache_read"] for r in lc); tw = sum(r["cache_write"] for r in lc)
    labels = ["缓存命中输入", "未命中输入", "输出", "缓存写入"]
    vals = [tc, ti, to, tw]
    pairs = [(l, v) for l, v in zip(labels, vals) if v > 0]
    if pairs:
        fig, ax = plt.subplots(figsize=(6.6, 5.4), dpi=130)
        colors = ["#4c8bf5", "#f5a623", "#34a853", "#ea4335"]
        ax.pie([v for _, v in pairs], labels=None, colors=colors[:len(pairs)],
               autopct=lambda pct: "%.1f%%" % pct,
               wedgeprops=dict(width=0.42, edgecolor="w"))
        total = sum(v for _, v in pairs)
        ax.text(0, 0, "总计\n%s\n(token)" % "{:,}".format(total), ha="center", va="center", fontsize=11)
        ax.legend([l for l, _ in pairs], loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=9)
        ax.set_title("token 构成  %s .. %s" % (start, end))
        fig.tight_layout()
        p = os.path.join(outdir, "02_token_mix.png")
        fig.savefig(p); plt.close(fig); made.append(p)

    # 3) 花费构成环形图（按 token 类型用刊例价折算，仅 offPeak 口径，示意用）
    if pairs:
        up = unit_price(False)
        cost_pairs = [("缓存命中输入", tc * up["cacheHit"] / 1e6),
                      ("未命中输入", ti * up["inputMiss"] / 1e6),
                      ("输出", to * up["output"] / 1e6)]
        cost_pairs = [(l, v) for l, v in cost_pairs if v > 0]
        fig, ax = plt.subplots(figsize=(6.6, 5.4), dpi=130)
        colors = ["#4c8bf5", "#f5a623", "#34a853"]
        ax.pie([v for _, v in cost_pairs], labels=None, colors=colors[:len(cost_pairs)],
               autopct=lambda pct: "%.1f%%" % pct,
               wedgeprops=dict(width=0.42, edgecolor="w"))
        tot = sum(v for _, v in cost_pairs)
        ax.text(0, 0, "折算\n%.2f 元" % tot, ha="center", va="center", fontsize=11)
        ax.legend(["%s  %.2f 元" % (l, v) for l, v in cost_pairs],
                  loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=9)
        ax.set_title("token 折算花费构成（按刊例价，非实付）")
        fig.tight_layout()
        p = os.path.join(outdir, "03_cost_mix.png")
        fig.savefig(p); plt.close(fig); made.append(p)

    # 3b) 实付花费构成（事件表逐次真实 cost，按天聚合，取前几名 + 其他）
    ev_rows = [dict(r) for r in con.execute(
        "SELECT day, SUM(cost) cost FROM events WHERE day BETWEEN ? AND ? GROUP BY day ORDER BY cost DESC",
        (start, end))]
    if ev_rows:
        top = ev_rows[:8]
        rest = sum(r["cost"] for r in ev_rows[8:])
        items = [(r["day"], r["cost"]) for r in top]
        if rest > 0:
            items.append(("其它 %d 天" % (len(ev_rows) - 8), rest))
        fig, ax = plt.subplots(figsize=(6.8, 5.4), dpi=130)
        cmap = plt.get_cmap("tab20")
        colors = [cmap(i % 20) for i in range(len(items))]
        ax.pie([v for _, v in items], labels=None, colors=colors,
               autopct=lambda pct: "%.1f%%" % pct, wedgeprops=dict(width=0.42, edgecolor="w"))
        ax.text(0, 0, "实付合计\n%.2f 元" % sum(v for _, v in items), ha="center", va="center", fontsize=11)
        ax.legend(["%s  %.2f 元" % (l, v) for l, v in items],
                  loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=8.5)
        ax.set_title("实付花费构成（按天）  %s .. %s" % (start, end))
        fig.tight_layout()
        p = os.path.join(outdir, "03b_paid_mix.png")
        fig.savefig(p); plt.close(fig); made.append(p)

    # 4) 模型占比
    mc = [m for m in model_costs(con, start, end) if m["cost"] > 0]
    if mc:
        fig, ax = plt.subplots(figsize=(6.6, 5.4), dpi=130)
        cmap = plt.get_cmap("tab20")
        colors = [cmap(i % 20) for i in range(len(mc))]
        ax.pie([m["cost"] for m in mc], labels=None, colors=colors,
               autopct=lambda pct: "%.1f%%" % pct, wedgeprops=dict(width=0.42, edgecolor="w"))
        ax.text(0, 0, "合计\n%.2f 元" % sum(m["cost"] for m in mc), ha="center", va="center", fontsize=11)
        ax.legend(["%s  %.2f 元" % (m["model"], m["cost"]) for m in mc],
                  loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=9)
        ax.set_title("按模型花费占比  %s .. %s" % (start, end))
        fig.tight_layout()
        p = os.path.join(outdir, "04_model_cost.png")
        fig.savefig(p); plt.close(fig); made.append(p)

    # 5) 日历热力图（周 × 星期）
    if dc:
        d0 = datetime.strptime(days[0], "%Y-%m-%d").date()
        d1 = datetime.strptime(days[-1], "%Y-%m-%d").date()
        by_day = {d["day"]: d["cost"] for d in dc}
        start_mon = d0 - timedelta(days=d0.weekday())
        nweeks = ((d1 - start_mon).days // 7) + 1
        grid = [[0.0] * nweeks for _ in range(7)]
        for i in range((d1 - start_mon).days + 1):
            cur = start_mon + timedelta(days=i)
            grid[cur.weekday()][i // 7] = by_day.get(cur.isoformat(), 0.0)
        fig, ax = plt.subplots(figsize=(max(6.0, nweeks * 0.55), 3.4), dpi=130)
        vmax = max((v for row in grid for v in row), default=1.0) or 1.0
        im = ax.imshow(grid, cmap="YlOrRd", aspect="auto", vmin=0, vmax=vmax)
        ax.set_yticks(range(7)); ax.set_yticklabels(["周一", "周二", "周三", "周四", "周五", "周六", "周日"])
        ax.set_xticks(range(nweeks))
        ax.set_xticklabels([(start_mon + timedelta(weeks=w)).strftime("%m-%d") for w in range(nweeks)],
                           fontsize=8, rotation=45)
        for r in range(7):
            for c in range(nweeks):
                if grid[r][c] > 0:
                    ax.text(c, r, "%.1f" % grid[r][c], ha="center", va="center", fontsize=7)
        ax.set_title("日历热力图（每天花费，元）")
        fig.colorbar(im, ax=ax, shrink=0.8)
        fig.tight_layout()
        p = os.path.join(outdir, "05_heatmap.png")
        fig.savefig(p); plt.close(fig); made.append(p)

    # 6) token 与费用双轴（看单价漂移）
    if lc:
        fig, ax = plt.subplots(figsize=(11, 4.6), dpi=130)
        d2 = [r["day"] for r in lc]
        bill = [r["input"] + r["output"] + r["cache_read"] + r["cache_write"] for r in lc]
        ax.bar(d2, bill, color="#9aa0a6", label="计费 token")
        ax.set_ylabel("token")
        ax2 = ax.twinx()
        ax2.plot(d2, [r["cost"] for r in lc], color="#ea4335", marker="o", ms=4, label="ledger 记录花费")
        ax2.set_ylabel("元")
        ax.set_title("token 体量 vs 记录花费（%s .. %s）" % (start, end))
        ax.tick_params(axis="x", rotation=45)
        h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=9)
        fig.tight_layout()
        p = os.path.join(outdir, "06_tokens_vs_cost.png")
        fig.savefig(p); plt.close(fig); made.append(p)

    return made


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="DSH token 用量 / 花费统计")
    ap.add_argument("--db", default=DB_PATH, help="SQLite 路径（默认脚本目录 token_stats.db）")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("sync", help="读取 DSH 数据并写入本地库（幂等）")

    p = sub.add_parser("show", help="总览 + 每日 ASCII 柱状")
    p.add_argument("range", nargs="?", default="all")

    p = sub.add_parser("hours", help="某天按小时分布")
    p.add_argument("day", nargs="?")

    p = sub.add_parser("table", help="明细表")
    p.add_argument("what", choices=["daily", "ledger", "models"], nargs="?", default="daily")
    p.add_argument("range", nargs="?", default="all")

    p = sub.add_parser("export", help="导出 CSV")
    p.add_argument("outdir")
    p.add_argument("range", nargs="?", default="all")

    p = sub.add_parser("chart", help="生成 PNG 图")
    p.add_argument("outdir")
    p.add_argument("range", nargs="?", default="all")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2

    con = connect(args.db)
    try:
        if args.cmd == "sync":
            r = sync(con)
            print("sync ok: events=%d day_model=%d snapshots=%d -> %s" % (
                r["events"], r["day_model"], r["snapshots"], args.db))
            return 0

        if args.cmd == "show":
            print_summary(summary(con, *parse_range(args.range, con)))
            return 0

        if args.cmd == "hours":
            day = args.day
            if not day:
                row = con.execute("SELECT MAX(day) d FROM events").fetchone()
                day = row["d"]
            if not day:
                print("库里还没有事件数据，先跑 sync")
                return 1
            print_hours(str(day), hour_costs(con, str(day)))
            return 0

        if args.cmd == "table":
            a, b = parse_range(args.range, con)
            if args.what == "daily":
                print("%-12s %10s %14s %8s" % ("日期", "花费_元", "token", "调用"))
                for d in day_costs(con, a, b):
                    print("%-12s %10.3f %14s %8d" % (d["day"], d["cost"],
                                                     "{:,}".format(d["tokens"]), d["calls"]))
            elif args.what == "ledger":
                print("%-12s %10s %12s %12s %14s %8s" % (
                    "日期", "花费_元", "输入", "输出", "缓存命中", "调用"))
                for r in ledger_daily(con, a, b):
                    print("%-12s %10.3f %12s %12s %14s %8d" % (
                        r["day"], r["cost"] or 0, "{:,}".format(r["input"]),
                        "{:,}".format(r["output"]), "{:,}".format(r["cache_read"]), r["calls"]))
            else:
                print("%-12s %-22s %10s %8s" % ("日期", "模型", "花费_元", "调用"))
                for r in ledger_days(con, a, b):
                    print("%-12s %-22s %10.3f %8d" % (r["day"], r["model"], r["cost"] or 0, r["calls"]))
            return 0

        if args.cmd == "export":
            a, b = parse_range(args.range, con)
            for f in export_csv(con, args.outdir, a, b):
                print("wrote: %s" % f)
            return 0

        if args.cmd == "chart":
            a, b = parse_range(args.range, con)
            made = make_charts(con, args.outdir, a, b)
            for f in made:
                print("wrote: %s" % f)
            print("charts=%d" % len(made))
            return 0
    finally:
        con.close()

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
