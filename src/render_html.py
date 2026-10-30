#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""render_html.py —— 把库里的数据渲成一个自包含的 HTML 报表（单文件、离线、零依赖）

产出：工具根目录的 dashboard.html —— 双击即开，数据内联在文件里，不发任何网络请求。
（中文名 看板.html 是 refresh.py 写的跳转页；维护说明见根目录 MAINTAIN.md）
页面三种视图（同一棵树的三种画法）：
  * 缩进树表：层级可任排（如 天 › 工作区 › 会话），逐级缩进、可折叠，每层独立排序
  * 树图 treemap：面积 = 指标，一屏看完整层级的占比
  * 旭日图 sunburst：同心环 = 层级
点任意节点 → 下方显示该节点的花费组成 / token 组成 / 时间分布（最细一级也有）。

用法：
  python render_html.py                       # 全部历史
  python render_html.py --range 7d
  python render_html.py --csv out/messages.csv   # 顺便导原子行
  python render_html.py --shot out/shot.png      # headless 截图（自己看效果用）
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db                 # noqa: E402
import hier               # noqa: E402
import prices as PRICES   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(ROOT, "dashboard.html")   # 看板本体（ASCII 名，批处理好调用）
APP_HTML = os.path.join(HERE, "app.html")
APP_JS = os.path.join(HERE, "app.js")


def _assets():
    """界面外壳（app.html，用 <!--BODY--> 分两半）与前端脚本（app.js）。

    放成独立文件而不是嵌在 python 字符串里：改界面时不用在几百行模板里翻，
    编辑器也能正确高亮 HTML/JS。
    """
    with open(APP_HTML, "r", encoding="utf-8") as f:
        shell = f.read()
    with open(APP_JS, "r", encoding="utf-8") as f:
        js = f.read()
    # app.js 的头部注释要剥掉：它被直接拼到 body 里，留在开头会变成可见正文
    while js.lstrip().startswith("//"):
        js = js.lstrip().split("\n", 1)[1] if "\n" in js else ""
    if "<!--BODY-->" not in shell:
        raise SystemExit("app.html 里缺少 <!--BODY--> 分隔标记")
    head, body = shell.split("<!--BODY-->", 1)
    return head, body, js


def build_data(start: str, end: str) -> dict:
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT m.ts ts, m.day day, m.hour hour, m.turn_no turn_no, m.model model, "
            "       m.step_no step_no, s.workspace workspace, s.depth depth, m.session_id session_id, "
            "       m.in_tokens in_tokens, m.out_tokens out_tokens, m.cache_read cache_read, "
            "       m.total_tokens total_tokens, m.cost_est cost_est, m.peak peak, m.keep keep, "
            "       m.price_row price_row, "
            "       m.replayed replayed, "
            "       m.interrupted interrupted, m.est_cost est_cost, m.est_tokens est_tokens, "
            "       m.est_note est_note, "
            "       m.start_ts start_ts, s.parent_id parent_id, m.snippet snippet "
            "FROM messages m JOIN sessions s ON s.session_id=m.session_id "
            "WHERE m.day BETWEEN ? AND ? AND m.day<>'' ORDER BY ts", (start, end))
        msgs = [dict(r) for r in rows]
        # 原生搜索（旁路账）：不写会话日志，但钱是真的（官方小时桶含它）。
        # **默认不区分来源**：这里把它摊成"额外的原子行"，跟普通调用一起进同一棵树/同一张 KPI，
        # 按 ts 落到它所属的那一轮上。estimate 行是 N 次调用的聚合，按 N 摊开（见下）。
        srows = [dict(r) for r in con.execute(
            "SELECT ts, day, hour, session_id, turn_no, model, calls_n, in_tokens, out_tokens, "
            "       cache_read, cache_write, total_tokens, peak, cost_est, price_row, src "
            "FROM search_calls WHERE day BETWEEN ? AND ? AND day<>'' ORDER BY ts", (start, end))]
        sid_info = {r["session_id"]: dict(r) for r in con.execute(
            "SELECT session_id, workspace, depth, parent_id FROM sessions")}
        search_total = db.search_summary(con, start, end)
        # 压缩摘要（compaction/summary）：也是一次模型调用，但不在 messages 里 →
        # 挂成"额外的原子行"，键用 turn+0.5 让它排在那一轮后面（界面上显示"第 N 轮后"）。
        # 空转/重试：官方有请求、我们连壳都没有的（按小时补过次数和金额）
        lrows = [dict(r) for r in con.execute(
            "SELECT day, hour, n, cost FROM lost_calls WHERE day BETWEEN ? AND ? AND day<>'' "
            "ORDER BY day, hour", (start, end))]
        crows = [dict(r) for r in con.execute(
            "SELECT ts, day, hour, session_id, turn_no, pos, in_tokens, out_tokens, cache_read, "
            "       cache_write, total_tokens, peak, cost_est, price_row "
            "FROM compaction_events WHERE day BETWEEN ? AND ? AND day<>'' ORDER BY ts",
            (start, end))]
        sessions = [dict(r) for r in con.execute(
            "SELECT session_id, workspace, cwd, depth, parent_id, agent_preset, created_at, "
            "       first_ts, last_ts, messages, turns, cost_est, peak_cost, "
            "       in_tokens, out_tokens, cache_read "
            "FROM sessions WHERE messages>0")]
        recon = [dict(r) for r in con.execute("SELECT * FROM day_recon ORDER BY day")]
        sides = [dict(r) for r in con.execute("SELECT * FROM side_calls ORDER BY day")]
        snap = con.execute("SELECT * FROM snapshots ORDER BY captured_at DESC LIMIT 1").fetchone()
        meta = {r["key"]: r["value"] for r in con.execute("SELECT * FROM meta")}
        price = dict(db.PRICE)
        # 对不上的差额（不平账：只报出来，不塞给任何人）
        gap = db.gap_summary(con)
        # 未知价目（price_row<0）：钱不计，只在界面上提示
        unk_price = db.unknown_price_summary(con)
        # 官方小时桶按天合计（只认对账那把 key），给对账表加一列
        key_name = db.resolve_key_name(con)
        off_day = {r["day"]: (r["c"] or 0.0) for r in con.execute(
            "SELECT day, SUM(cost) c FROM official_hour WHERE api_key_name=? GROUP BY day",
            (key_name,))}
        has_official = bool(off_day)
        # 显示名（纯展示；缺了界面回落到显示 id，绝不影响统计）
        ws_names, sess_names, turn_names = db.load_labels(con)
        # 疑似缓存重建（只作参考：每轮首调用整段未命中 → 估算多花的钱）
        rbflags = db.turn_rebuild_flags(con, start, end)
        # 工具调用（纯展示）：按 (会话, 轮, 步) 汇总成"这一步发起了几次、都是什么工具"
        tmap = {}
        for r in con.execute(
                "SELECT session_id, turn_no, step_no, COUNT(*) n, GROUP_CONCAT(name) nm "
                "FROM tool_calls WHERE day BETWEEN ? AND ? AND day<>'' "
                "GROUP BY session_id, turn_no, step_no", (start, end)):
            cnt, names = {}, []
            for nm in (r["nm"] or "").split(","):
                if nm:
                    cnt[nm] = cnt.get(nm, 0) + 1
            for nm, k in sorted(cnt.items(), key=lambda kv: -kv[1])[:4]:
                names.append(nm + "×" + str(k))
            tmap[(r["session_id"], r["turn_no"], r["step_no"])] = (r["n"], " · ".join(names))
    finally:
        con.close()

    for s in sessions:
        s["ws_short"] = hier.pretty_workspace(s.get("workspace") or "")
    for r in recon:
        r["official"] = round(off_day.get(r["day"], 0.0), 6)

    # 每行预先算好三种 token 各自贡献的钱（按**这一行命中的价目档**），环形图才能精确
    pt = PRICES.load()
    for m in msgs:
        row = pt.by_id(m["price_row"]) if m.get("price_row") is not None else None
        if row is None:
            # 未知价目：钱一律算 0（cost_est 是 -1，界面显示横杠），token 照常统计
            m["cost_cache"] = m["cost_in"] = m["cost_out"] = 0.0
            m["unknown_price"] = 1
        else:
            p = row.price_of(bool(m["peak"]))
            m["cost_cache"] = m["cache_read"] * p[0] / 1e6
            m["cost_in"] = m["in_tokens"] * p[1] / 1e6
            m["cost_out"] = m["out_tokens"] * p[2] / 1e6
            m["unknown_price"] = 0
        m["replay"] = 0 if m["keep"] else 1
        m["kind"] = ""
        _t = tmap.get((m["session_id"], m.get("turn_no"), m.get("step_no"))) or (0, "")
        m["tn"], m["tnm"] = _t[0], _t[1]
        m["srch"], m["srchN"] = 0.0, 0        # 搜索开销（只给"搜索开销"列看，钱已含在 cost_est 里）
        # 疑似缓存重建：只挂在该轮**第一条调用**上（金额=估算多花的钱），父节点自己累加
        f = rbflags.get((m["session_id"], m["turn_no"]))
        m["rb"], m["rbLvl"] = 0.0, 0
        if f and f["level"] and f["ts"] == m["ts"] and f.get("step_no") == m.get("step_no"):
            m["rb"], m["rbLvl"] = f["cost"], f["level"]

    # 搜索行 → 摊成普通原子行（estimate 行是 N 次调用的聚合：token 按 N 摊平，钱按每份 token 重算）
    for s in srows:
        n = max(1, int(s.get("calls_n") or 1))
        info = sid_info.get(s.get("session_id")) or {}
        sid = s.get("session_id") or "(未归属搜索)"
        ws = info.get("workspace") or "(未归属)"
        toks = [int(s.get("in_tokens") or 0), int(s.get("out_tokens") or 0),
                int(s.get("cache_read") or 0), int(s.get("cache_write") or 0)]
        for i in range(n):
            part = [v // n + (1 if i < v % n else 0) for v in toks]
            peak = 1 if s.get("peak") else 0
            row = pt.by_id(s.get("price_row")) if s.get("price_row") is not None else None
            if row is None:
                cost, cc, ci, co = -1.0, 0.0, 0.0, 0.0
            else:
                p = row.price_of(bool(peak))
                cost = pt.cost(row, part[0], part[1], part[2], part[3], bool(peak))
                cc, ci, co = (part[2] * p[0] / 1e6, part[0] * p[1] / 1e6,
                              part[1] * p[2] / 1e6)
            msgs.append({
                "ts": s.get("ts"), "day": s.get("day"), "hour": s.get("hour"),
                "turn_no": s.get("turn_no") if s.get("turn_no") is not None else 0,
                "model": s.get("model") or "", "workspace": ws,
                "depth": info.get("depth") or 0, "session_id": sid,
                "parent_id": info.get("parent_id"),
                "in_tokens": part[0], "out_tokens": part[1], "cache_read": part[2],
                "total_tokens": sum(part), "cost_est": cost,
                "cost_cache": cc, "cost_in": ci, "cost_out": co,
                "peak": peak, "keep": 1, "price_row": s.get("price_row"),
                "replay": 0, "interrupted": 0, "est_cost": 0.0, "est_tokens": 0,
                "est_note": "", "start_ts": s.get("ts"), "replayed": 0,
                "unknown_price": 0 if row is not None else 1, "kind": "search",
                "srch": max(0.0, cost), "srchN": 1,
                "rb": 0.0, "rbLvl": 0})

    # 空转/重试 → 单独一行（没有会话、没有 token，只有次数和钱）。轮次键用 0，
    # 标签注进 turn_names 避免显示成"第0轮"。
    for s in lrows:
        hour = int(s.get("hour") or 0)
        y, mo, d = [int(x) for x in str(s.get("day")).split("-")]
        ts = int(datetime.datetime(y, mo, d, hour, 0).timestamp() * 1000)
        n = max(1, int(s.get("n") or 1))
        each = round((s.get("cost") or 0.0) / n, 9)
        turn_names["(未记录)|0"] = "(未记录 · 空转)"
        for _i in range(n):               # 一次调用一行：次数才对得上
            msgs.append({
                "ts": ts, "day": s.get("day"), "hour": hour, "turn_no": 0, "model": "",
                "workspace": "(未记录)", "depth": 0, "session_id": "(未记录)", "parent_id": None,
                "in_tokens": 0, "out_tokens": 0, "cache_read": 0, "total_tokens": 0,
                "cost_est": each, "cost_cache": 0.0, "cost_in": 0.0, "cost_out": 0.0,
                "peak": 0, "price_row": 3, "keep": 1, "replay": 0, "interrupted": 0,
                "est_cost": 0.0, "est_tokens": 0, "est_note": "", "start_ts": ts,
                "replayed": 0, "unknown_price": 0, "kind": "lost",
                "srch": 0.0, "srchN": 0, "rb": 0.0, "rbLvl": 0,
                "step_no": None, "snippet": ""})

    # 压缩摘要 → 也摊成原子行；轮次键用 turn+0.5（"第 12 轮后"），标签注进 turn_names
    for s in crows:
        info = sid_info.get(s.get("session_id")) or {}
        sid = s.get("session_id") or "(未归属)"
        ws = info.get("workspace") or "(未归属)"
        turn = int(s.get("turn_no") or 0)
        key = turn + 0.5
        pos = (s.get("pos") or "中") if turn else ""
        turn_names[sid + "|" + str(key)] = ("第 %d 轮%s · 压缩摘要" % (turn, pos)
                                            if turn else "会话开始前 · 压缩摘要")
        peak = 1 if s.get("peak") else 0
        row = pt.by_id(s.get("price_row")) if s.get("price_row") is not None else None
        if row is None:
            cost, cc, ci, co = -1.0, 0.0, 0.0, 0.0
        else:
            p = row.price_of(bool(peak))
            cost = pt.cost(row, s.get("in_tokens") or 0, s.get("out_tokens") or 0,
                           s.get("cache_read") or 0, s.get("cache_write") or 0, bool(peak))
            cc = (s.get("cache_read") or 0) * p[0] / 1e6
            ci = (s.get("in_tokens") or 0) * p[1] / 1e6
            co = (s.get("out_tokens") or 0) * p[2] / 1e6
        msgs.append({
            "ts": s.get("ts"), "day": s.get("day"), "hour": s.get("hour"),
            "turn_no": key, "model": "", "workspace": ws,
            "depth": info.get("depth") or 0, "session_id": sid,
            "parent_id": info.get("parent_id"),
            "in_tokens": s.get("in_tokens") or 0, "out_tokens": s.get("out_tokens") or 0,
            "cache_read": s.get("cache_read") or 0, "total_tokens": s.get("total_tokens") or 0,
            "cost_est": cost, "cost_cache": cc, "cost_in": ci, "cost_out": co,
            "peak": peak, "keep": 1, "price_row": s.get("price_row"),
            "replay": 0, "interrupted": 0, "est_cost": 0.0, "est_tokens": 0,
            "est_note": "", "start_ts": s.get("ts"), "replayed": 0,
            "unknown_price": 0 if row is not None else 1, "kind": "compaction",
            "srch": 0.0, "srchN": 0, "rb": 0.0, "rbLvl": 0,
                "step_no": None, "snippet": ""})

    # 日聚合只统计 keep=1（口径），由服务端算好给柱状图；未知价目的钱不计（单独计数）
    daily = {}
    for m in msgs:
        if m["replay"]:
            continue
        d = daily.setdefault(m["day"], {"day": m["day"], "cost": 0.0, "tokens": 0,
                                        "calls": 0, "peak_cost": 0.0, "in": 0, "out": 0,
                                        "cache": 0, "unknown": 0})
        if (m["cost_est"] or 0) >= 0:
            d["cost"] += m["cost_est"]
            if m["peak"]:
                d["peak_cost"] += m["cost_est"]
        else:
            d["unknown"] += 1
        d["tokens"] += m["total_tokens"]
        d["calls"] += 1
        d["in"] += m["in_tokens"]
        d["out"] += m["out_tokens"]
        d["cache"] += m["cache_read"]
    daily = [daily[k] for k in sorted(daily)]

    # 列式存储：减少内联体积
    mcols = ["ts", "day", "hour", "turn_no", "step_no", "snippet", "tn", "tnm",
             "model", "workspace", "depth", "session_id",
             "parent_id", "in_tokens", "out_tokens", "cache_read", "total_tokens",
             "cost_est", "cost_cache", "cost_in", "cost_out", "peak", "replay", "start_ts",
             "interrupted", "est_cost", "est_tokens", "est_note", "replayed", "price_row",
             "kind", "rb", "rbLvl", "srch", "srchN"]
    mrows = [[m.get(c) for c in mcols] for m in msgs]

    # 中断空壳的汇总（纯展示：界面上标黄 + 可选的"含异常"开关用它）
    anomaly = {"n": 0, "cost": 0.0, "tokens": 0, "hours": [],
               "unmatched": gap.get("unmatched", 0.0),
               "unmatched_rows": gap.get("rows", [])[:8],
               "days": sorted({m["day"] for m in msgs if m.get("interrupted")})}
    for m in msgs:
        if m.get("interrupted"):
            if m.get("replayed"):
                anomaly["copies"] = anomaly.get("copies", 0) + 1
                continue                   # fork 副本：同一次中断，只算一份
            anomaly["n"] += 1
            anomaly["cost"] += m.get("est_cost") or 0.0
            anomaly["tokens"] += m.get("est_tokens") or 0
            if not (m.get("est_cost") or 0):
                anomaly["unrecovered"] = anomaly.get("unrecovered", 0) + 1
            h = "%s %02d时" % (m.get("day"), m.get("hour"))
            if h not in anomaly["hours"]:
                anomaly["hours"].append(h)

    return {
        "generated_at": int(time.time()),
        "range": [start, end],
        "price": price,
        "peak_windows": [list(w) for w in db.PEAK_WINDOWS],
        "peak_multiplier": db.PEAK_MULTIPLIER,
        "balance": (snap["balance"] if snap else None),
        "today_usage": (snap["today_usage"] if snap else None),
        "balance_date": (snap["file_date"] if snap else ""),
        "last_scan": int(meta.get("last_scan") or 0),
        "dim_order": list(hier.DIMS.keys()),
        "mcols": mcols,
        "mrows": mrows,
        "anomaly": anomaly,
        # 官方口径（platform 小时桶，只认一把 key）：给 KPI 与对账用
        "official": {"key": key_name, "cost": round(gap.get("cost", 0.0), 6),
                     "tokens": gap.get("tokens", 0),
                     "unmatched": gap.get("unmatched", 0.0),
                     "has": has_official},
        # 价目表（prices.txt）：界面用它显示"这行按第几号价算的 / 有没有未知价目"
        "prices": {"rows": [{"id": r.id, "model": r.model, "span": r.span_text(),
                             "off": list(r.off), "peak": list(r.peak) if r.peak else None,
                             "peak_ok": r.peak_ok, "note": r.note} for r in pt.rows],
                   "checked": pt.checked, "fallback": pt.fallback,
                   "warnings": pt.warnings},
        "unknown_price": unk_price,
        # 原生搜索账单（旁路）：默认已经并进上面所有数字/所有维度，这里只是给 KPI 报个数
        "search": search_total,
        "labels": {"workspace": ws_names, "session": sess_names, "turn": turn_names},
        "sessions": sessions,
        "daily": daily,
        "recon": recon,
        "side_calls": sides,
    }


# ------------------------------------------------------------------ 预设查看模式
DEFAULT_PRESETS = os.path.join(ROOT, "config", "presets.txt")

PRESET_COLS = ("名字 | 层级 | 日期 | 排序 | 花费标红(元) | 峰值标红(%) | 只标最下层(yes/no)")


def load_presets(path: str = None):
    """读「预设查看模式」配置文件（每行一档，用 `|` 分隔）。返回 (presets, warnings)。

    这份文件是**给人改的**，所以格式故意做得土：`#` 开头是注释、某一格留空＝这项不动、
    日期只认 `today`（最新一天）/`full`（全部）/空。坏行跳过并记 warning，
    整份缺失就返回空表（前端用内置预设兜底）——**读不懂也绝不影响看板生成**。
    """
    p = path or DEFAULT_PRESETS
    out, warns = [], []
    if not os.path.exists(p):
        warns.append("没找到预设文件 %s，看板用内置预设" % os.path.basename(p))
        return out, warns
    with open(p, encoding="utf-8-sig") as f:
        for i, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            cells = [c.strip() for c in s.split("|")]
            if len(cells) < 2:
                warns.append("第 %d 行没有竖线分隔（应当有 7 格），已跳过：%s" % (i, s[:36]))
                continue

            def cell(j):
                return cells[j] if j < len(cells) else ""

            name = cell(0)
            if not name:
                warns.append("第 %d 行没有名字，已跳过" % i)
                continue
            d = {"name": name}
            dims = [x for x in cell(1).replace("，", ",").split(",") if x]
            if dims:
                d["dims"] = dims
            rng = cell(2).lower()
            if rng in ("today", "full"):
                d["range"] = rng
            elif rng:
                warns.append("「%s」的日期写法看不懂：%s（只认 today / full / 空）" % (name, rng))
            sorts = [x for x in cell(3).replace("，", ",").split(",") if x]
            if sorts:
                d["sortOf"] = sorts
            for j, key, label in ((4, "hotCost", "花费标红"), (5, "hotPeak", "峰值标红")):
                v = cell(j)
                if not v:
                    continue
                try:
                    d[key] = float(v)
                except ValueError:
                    warns.append("「%s」的%s不是数字：%s" % (name, label, v))
            leaf = cell(6).lower()
            if leaf in ("yes", "y", "1", "true", "是"):
                d["hotLeafOnly"] = True
            elif leaf in ("no", "n", "0", "false", "否"):
                d["hotLeafOnly"] = False
            elif leaf:
                warns.append("「%s」的只标最下层看不懂：%s（写 yes / no）" % (name, leaf))
            out.append(d)
    return out, warns


# ------------------------------------------------------------------ 渲染
def render(start: str, end: str, out: str, data=None) -> str:
    head, body, js = _assets()
    data = data or build_data(start, end)
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = (head + body + "\n<script>\n" + js.replace("__DATA__", blob)
            + "</script>\n</body>\n</html>\n")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


def export_csv(start: str, end: str, out: str, data=None) -> str:
    """把原子行导成 CSV（与看板同一口径：全部行都在，replay 列标 0/1）。UTF-8 BOM。"""
    import csv
    data = data or build_data(start, end)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(data["mcols"])
        for r in data["mrows"]:
            w.writerow(r)
    return out


# ------------------------------------------------------------------ 验证
NODE_HARNESS = r"""
// 假 DOM：让页面脚本能真跑起来，并把它渲染出的东西吐回来做断言
const __els = {};
const __all = [];
const __sel = {};
function __mk(id){
  if (__els[id]) return __els[id];      // 同一个 id 必须拿到同一个元素，否则模拟交互全是假的
  const el = {
    id, innerHTML:"", value:"", checked:false, textContent:"", style:{}, dataset:{},
    onclick:null, onchange:null, classList:{_s:new Set(),
      add(c){this._s.add(c);}, remove(c){this._s.delete(c);},
      toggle(c,on){ if(on===undefined){ on = !this._s.has(c); } on ? this._s.add(c) : this._s.delete(c); },
      contains(c){ return this._s.has(c); }},
    querySelectorAll(){ return []; },
  };
  __all.push(el);
  return el;
}
global.document = {
  getElementById: id => (__els[id] || (__els[id] = __mk(id))),
  querySelector: sel => (__sel[sel] || (__sel[sel] = __mk("__q" + sel))),
  querySelectorAll: () => [],
  addEventListener: () => {},
  body: __mk("__body"),        // app.js 会用 document.body.classList 切深色模式
};
function __LS(){
  const m = {};
  return {getItem: k => (k in m ? m[k] : null),
          setItem: (k, v) => { m[k] = String(v); },
          removeItem: k => { delete m[k]; },
          __dump: () => m};
}
const __STORE = __LS();          // 函数声明会提升，这里可以直接调
global.window = {addEventListener: () => {}, onerror: null, localStorage: __STORE};
global.localStorage = __STORE;
__SRC__
function __snap(){
  return {
    kpi: __el0("kpi"), head: __el0("head"), rows: __el0("treeRows"), tot: __el0("treeTot"),
    donutCost: __el0("donutCost"), timeline: __el0("timeline"), daily: __el0("dailyChart"),
    recon: __el0("recon"), mapBox: __el0("mapBox"), sunBox: __el0("sunBox"),
    crumb: __el0("crumb"), filterInfo: __el0("filterInfo"), preset: __el0("preset"),
    nCurrent: currentRows().length, nNodes: nodesOf(getTree()).length,
    rootChildren: getTree().children.length,
    labels: DATA.labels,
    paths: nodesOf(getTree()).map(n => n.path),
    parents: nodesOf(getTree()).map(n => ({path:n.path, parent:n.parent ? n.parent.path : null})),
    sortOf: (S.sortOf || []).slice(),
    top: getTree().children.map(c => ({key:String(c.key), label:String(c.label||""),
                                       ts:+c.maxTs, start:+c.minTs,
                                       end:+c.maxTs, cost:+c.cost.toFixed(6),
                                       tokens:c.tokens, calls:c.calls})),
    nids: nodesOf(getTree()).map(n => n.path),
    collapsedList: Array.from(S.collapsed || []),
    selected: S.selected || null,
    depthOfNid: (function(){ const o={}; for (const n of nodesOf(getTree())) o[n.nid]=n.depth; return o; })(),
    sortByDepth: (function(){
      // 每个父节点下，兄弟节点的指标序列 + 被排序那一层的层号
      const out = {};
      for (const n of nodesOf(getTree())){
        if (n.children.length > 1){
          out[n.path] = {depth: n.depth + 1,
                         kids: n.children.map(c => ({cost:+c.cost.toFixed(6), tokens:c.tokens,
                                                     calls:c.calls, start:+c.minTs, end:+c.maxTs,
                                                     ts:+c.maxTs,   // 时间排序用结束时间
                                                     key:String(c.key)}))};
        }
      }
      return out;
    })(),
    // 通用探针：注入的测试代码把要带出来的东西塞进 DATA.__probe 即可
    probe: (typeof DATA.__probe === "undefined" ? null : DATA.__probe),
    stored: (function(){ try { return __STORE.getItem("dshTokenStats.view") || ""; }
                         catch (e) { return ""; } })(),
  };
}
boot();
__EXTRA__
process.stdout.write("__JSON__" + JSON.stringify(__snap()));
function __el0(id){ return __els[id] ? __els[id].innerHTML : ""; }
"""


def _script_of(html_path: str):
    import re
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    m = re.findall(r"<script>(.*?)</script>", html, re.S)
    return m[-1] if m else ""


def check_js(html_path: str) -> tuple:
    """node --check 语法检查。"""
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    if not node:
        return (True, "node 不可用，跳过 JS 语法检查")
    src = _script_of(html_path)
    if not src:
        return (False, "没找到 <script> 块")
    tmp = os.path.join(tempfile.mkdtemp(prefix="tokenhtml_"), "check.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(src)
    p = subprocess.run([node, "--check", tmp], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return (p.returncode == 0, (p.stdout + p.stderr).strip()[:500])


def check_browser(html_path: str, extra_js: str = "") -> tuple:
    """在 node 假 DOM 里真跑一遍页面脚本，返回 (ok, 渲染结果 dict)。

    extra_js 会在 init() 之后、取结果之前执行（用来模拟改筛选/切视图等交互）。
    """
    import json as _json
    import re
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    if not node:
        return (True, None)
    src = _script_of(html_path)
    if not src:
        return (False, "没有 script")
    m = re.search(r"const DATA = (.*?);\s*\n", src)
    if not m:
        return (False, "没找到 DATA 定义")
    data_json = m.group(1)
    rest = src[:m.start()] + src[m.end():]
    # 去掉末尾的自动启动（boot() 或 init()），测试脚本自己控制启动时机
    rest = re.sub(r"\n(boot|init)\(\);\s*$", "\n", rest)
    tmpdir = tempfile.mkdtemp(prefix="tokenhtml_run_")
    path = os.path.join(tmpdir, "run.js")
    harness = NODE_HARNESS.replace("__SRC__", rest).replace("__EXTRA__", extra_js or "")
    with open(path, "w", encoding="utf-8") as f:
        f.write("const DATA = %s;\n" % data_json)
        f.write(harness)
    p = subprocess.run([node, path], capture_output=True, text=True, timeout=180,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        return (False, (p.stdout + p.stderr).strip()[:900])
    if "__JSON__" not in p.stdout:
        return (False, p.stdout[:400])
    try:
        return (True, _json.loads(p.stdout.split("__JSON__", 1)[1]))
    except Exception as e:
        return (False, "解析 node 输出失败: %s" % e)


def check_browser_with(html_path: str, extra_js: str):
    """按 html_path + 额外 JS 查缓存跑一遍（同样的交互重复问时不重复算）。"""
    key = (os.path.abspath(html_path), extra_js)
    if key not in _BROWSER_CACHE:
        _BROWSER_CACHE[key] = check_browser(html_path, extra_js)
    return _BROWSER_CACHE[key]


BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
_BROWSER_CACHE = {}


def screenshot(html_path: str, png_path: str, width=1500, height=2600):
    """用 headless Edge/Chrome 截图（自己看效果用，返回 (ok, msg)）。

    坑：不指定 --user-data-dir 时 Edge 会去用系统默认 profile，在沙箱里会报
    「Network: 拒绝访问 (0x5)」直接失败；用自己的临时 profile 就干净了。
    """
    import os as _os
    import shutil
    import subprocess
    import tempfile
    exe = next((b for b in BROWSERS if os.path.exists(b)), None)
    if not exe:
        return (False, "没找到 Edge/Chrome")
    png_abs = _os.path.abspath(png_path)
    _os.makedirs(_os.path.dirname(png_abs), exist_ok=True)
    if _os.path.exists(png_abs):
        _os.remove(png_abs)
    profile = tempfile.mkdtemp(prefix="htmlshot_")
    url = "file:///" + _os.path.abspath(html_path).replace("\\", "/")
    cmd = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
           "--disable-extensions", "--no-first-run", "--no-default-browser-check",
           "--disable-features=Translate,BackForwardCache",
           "--user-data-dir=" + profile,
           "--virtual-time-budget=8000",
           "--window-size=%d,%d" % (width, height),
           "--screenshot=" + png_abs, url]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                       encoding="utf-8", errors="replace")
    except Exception as e:
        return (False, "启动浏览器失败: %s" % e)
    ok = _os.path.exists(png_abs) and _os.path.getsize(png_abs) > 1000
    shutil.rmtree(profile, ignore_errors=True)
    return (ok, ("%s %.0fKB" % (_os.path.basename(png_abs),
                                _os.path.getsize(png_abs) / 1024)) if ok
            else "浏览器没产出截图（可能被沙箱拦了）")


def _template_sig() -> str:
    """app.html + app.js 的指纹：模板没变就不用每次刷新都跑 node 自检（省约 2 秒）"""
    h = hashlib.sha256()
    for name in ("app.html", "app.js"):
        with open(os.path.join(HERE, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def _get_meta(key: str) -> str:
    try:
        con = db.connect()
        try:
            row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return (row[0] if row else "") or ""
        finally:
            con.close()
    except Exception:
        return ""


def _set_meta(key: str, value: str) -> None:
    try:
        con = db.connect()
        try:
            con.execute("INSERT INTO meta (key,value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成单文件 HTML 报表")
    ap.add_argument("--range", default="all")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--csv", default="", help="同时导一份原子行 CSV 到这个路径")
    ap.add_argument("--shot", default="", help="headless 截图到这个 PNG（自己看效果用）")
    ap.add_argument("--presets", default=DEFAULT_PRESETS,
                    help="预设查看模式配置文件（每行一档，用 | 分隔；默认 presets.txt）")
    ap.add_argument("--no-check", action="store_true", help="跳过 node JS 自检")
    a = ap.parse_args(argv)
    con = db.connect()
    try:
        start, end = db.parse_range(a.range, con)
    finally:
        con.close()
    t0 = time.time()
    data = build_data(start, end)
    data["presets"], data["preset_warn"] = load_presets(a.presets)
    path = render(start, end, a.out, data)
    print("wrote: %s  (%.0f KB, 原子行=%d, 会话=%d, 区间 %s..%s, %.1fs)"
          % (path, os.path.getsize(path) / 1024, len(data["mrows"]), len(data["sessions"]),
             start, end, time.time() - t0))
    print("预设: %d 档（%s）" % (len(data["presets"]), os.path.basename(a.presets)))
    for w in data["preset_warn"]:
        print("  注意: %s" % w)
    if a.csv:
        c = export_csv(start, end, a.csv, data)
        print("wrote: %s  (%.0f KB)" % (c, os.path.getsize(c) / 1024))
    if not a.no_check and _template_sig() == _get_meta("js_check_sig"):
        print("JS 自检: 跳过（app.html / app.js 没改过，上次已通过）")
    elif not a.no_check:
        ok, msg = check_js(path)
        print("JS 语法自检: %s%s" % ("PASS" if ok else "FAIL", ("  " + msg) if msg else ""))
        if not ok:
            return 1
        ok2, res = check_browser(path)
        print("页面脚本运行: %s" % ("PASS" if ok2 else "FAIL  %s" % res))
        if not ok2:
            return 1
        _set_meta("js_check_sig", _template_sig())
    if a.shot:
        ok3, msg3 = screenshot(path, a.shot)
        print("截图: %s  %s" % ("OK" if ok3 else "FAIL", msg3))
        if not ok3:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
