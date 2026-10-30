#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""prices.py —— 价目表（只认 DeepSeek 自家的模型）

**为什么不能只看模型名**：日志里记的是 `deepseek-flash` 这种**稳定的别名**
（`data.model` / `data.message.source.model` 全是它，DSH 自己的账本也一样），
背后的模型和价格却会变 —— `deepseek-v4-pro` 就被并进 flash 计价过。
所以"这一笔按什么价"取决于 **(模型, 时刻)**，光看名字不够。

表在工具根目录 `prices.txt`（**给人改的**），每行一档：

    号 | 模型 | 生效起 | 生效止 | 峰谷 | 谷价:命中,未命中,输出 | 峰价:... | 备注

- **号**：命中哪一档会记进库（`messages.price_row`），多行可共用同一个号（= 同一档价）
- **生效起止**：留空 = 不限；一律按**北京时间**比；起含、止不含（`ts >= 起 and ts < 止`）
- **峰谷**：`yes` 分峰/谷价，`no` 只有一个价
- 匹配不到 → **未知价目**：`price_row = -1`、`cost_est = -1`（界面显示横杠），
  只报 token、不进任何金额统计

文件找不到时用**内置兜底档**（= 1 号 flash 价）并在 warnings 里说明，保证看板不会空。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_PATH = os.path.join(ROOT, "config", "prices.txt")

UNKNOWN = -1                     # price_row / cost_est 的"未知价目"哨兵值
TZ8 = timezone(timedelta(hours=8))   # DS 的计价时钟 = 北京时间（固定 +8，无夏令时）

ROW_MIN = 6                      # 一行至少要有：号|模型|起|止|峰谷|谷价
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
_CACHE = {}


class Row:
    """一档价：off = (命中, 未命中, 输出)，peak 可能为 None（不分峰谷时）"""

    __slots__ = ("id", "model", "start", "end", "peak_ok", "off", "peak", "note", "line")

    def __init__(self, rid, model, start, end, peak_ok, off, peak, note="", line=0):
        self.id, self.model = rid, model
        self.start, self.end = start, end
        self.peak_ok, self.off, self.peak = peak_ok, off, peak
        self.note, self.line = note, line

    def covers(self, ts_ms: int) -> bool:
        if ts_ms is None:
            return self.start is None and self.end is None
        if self.start is not None and ts_ms < self.start:
            return False
        if self.end is not None and ts_ms >= self.end:
            return False
        return True

    def span_text(self) -> str:
        fmt = lambda t: datetime.fromtimestamp(t / 1000.0, TZ8).strftime("%Y-%m-%d %H:%M") \
            if t else "…"
        return "%s ~ %s" % (fmt(self.start), fmt(self.end))

    def price_of(self, peak: bool):
        if peak and self.peak_ok and self.peak:
            return self.peak
        return self.off


def _ts(text: str):
    text = (text or "").strip()
    if not text or text in ("-", "—", "不限"):
        return None
    if not _TS_RE.match(text.replace("T", " ")):
        raise ValueError(text)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text.replace("T", " "), fmt)
                       .replace(tzinfo=TZ8).timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(text)


def _trio(text: str):
    """'0.02,1,4' → (0.02, 1.0, 4.0)；空 / '-' / '—' → None"""
    text = (text or "").strip()
    if not text or text in ("-", "—"):
        return None
    parts = [p for p in re.split(r"[,/\s]+", text) if p]
    if len(parts) != 3:
        raise ValueError(text)
    return tuple(float(p) for p in parts)


class Table:
    def __init__(self, rows, warnings=None, path=None, checked=None, fallback=False):
        self.rows = rows or []
        self.warnings = list(warnings or [])
        self.path = path
        self.checked = checked          # 【本表核对到】那一行写的日期
        self.fallback = fallback        # True = 文件没读到，用的是内置兜底档
        self.overlaps = []              # 匹配时发现的重叠（模型, 时刻）

    def by_id(self, rid):
        for r in self.rows:
            if r.id == rid:
                return r
        return None

    def match(self, model: str, ts_ms: int):
        """(模型, 时刻) → Row；匹配不到或**查出多档重叠** → None（= 未知价目）"""
        model = (model or "").strip()
        cands = [r for r in self.rows if r.model in (model, "*") and r.covers(ts_ms)]
        exact = [r for r in cands if r.model == model]
        star = [r for r in cands if r.model == "*"]
        pick = exact if exact else star
        if len(pick) == 1:
            return pick[0]
        if len(pick) > 1:
            # 重叠说明表写错了：宁可判"未知"，也不许瞎猜一个
            self.overlaps.append((model or "*", ts_ms,
                                  [r.id for r in pick]))
            return None
        return None

    def cost(self, row, in_tok=0, out_tok=0, cache_read=0, cache_write=0, peak=False) -> float:
        p = row.price_of(peak)
        return (cache_read * p[0] + (in_tok + cache_write) * p[1] + out_tok * p[2]) / 1e6

    def describe(self) -> str:
        src = "内置兜底" if self.fallback else os.path.basename(self.path or DEFAULT_PATH)
        return ("价目表：%s（%d 档，核对到 %s）"
                % (src, len(self.rows), self.checked or "未写"))


FALLBACK = Row(1, "*", None, None, True, (0.02, 1.0, 4.0), (0.04, 2.0, 8.0),
               "内置兜底 = flash 价", 0)


def parse(text: str, path: str = None) -> Table:
    rows, warns, checked = [], [], None
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            m = re.search(r"【本表核对到】\s*(\d{4}-\d{2}-\d{2})", line)
            if m:
                checked = m.group(1)
            continue
        f = [x.strip() for x in line.split("|")]
        if len(f) < ROW_MIN:
            warns.append("prices: 第 %d 行只有 %d 格（要 ≥%d 格），已跳过" % (i, len(f), ROW_MIN))
            continue
        try:
            rid = int(f[0])
            model = f[1]
            if not model:
                raise ValueError("模型为空")
            start, end = _ts(f[2]), _ts(f[3])
            peak_ok = f[4].strip().lower() not in ("no", "n", "0", "false", "否")
            off = _trio(f[5])
            if off is None:
                raise ValueError("没有谷价")
            peak = _trio(f[6]) if len(f) > 6 else None
            if peak_ok and peak is None:
                warns.append("prices: 第 %d 行写了峰谷=yes 但没给峰价 → 当 no 处理" % i)
                peak_ok = False
            rows.append(Row(rid, model, start, end, peak_ok, off, peak,
                            f[7] if len(f) > 7 else "", i))
        except ValueError as e:
            warns.append("prices: 第 %d 行读不懂（%s），已跳过：%s" % (i, e, line.strip()[:40]))
    if not rows:
        warns.append("prices: 一行都没读出来 → 全部按未知价目处理")
    return Table(rows, warns, path, checked)


def load(path: str = None, force: bool = False) -> Table:
    """读表（按 路径+mtime 缓存）。文件不在 → 内置兜底档 + 一条 warning。"""
    path = path or DEFAULT_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    key = (os.path.abspath(path), mtime)
    if force or key not in _CACHE:
        if mtime is None:
            _CACHE.clear()
            t = Table([FALLBACK], ["prices: 找不到 %s → 用内置兜底档（flash 价，全部记 1 号）" % path],
                      path, None, True)
            _CACHE[key] = t
            return t
        with open(path, "r", encoding="utf-8") as f:
            t = parse(f.read(), path)
        _CACHE.clear()
        _CACHE[key] = t
    return _CACHE[key]


def reset():
    """清缓存（测试用）"""
    _CACHE.clear()


def lookup(model: str, ts_ms: int, table: Table = None):
    """(模型, 时刻) → (Row 或 None, price_row)；None 时 price_row = -1"""
    t = table or load()
    r = t.match(model, ts_ms)
    return r, (r.id if r else UNKNOWN)


def cost_of(model: str, ts_ms: int, in_tok=0, out_tok=0, cache_read=0, cache_write=0,
            peak=False, table: Table = None):
    """→ (金额 或 -1, price_row)：命中就按那一档算，没命中就是未知价目(-1, -1)"""
    t = table or load()
    r = t.match(model, ts_ms)
    if r is None:
        return UNKNOWN * 1.0, UNKNOWN
    return t.cost(r, in_tok, out_tok, cache_read, cache_write, peak), r.id


if __name__ == "__main__":
    t = load(force=True)
    print(t.describe())
    for w in t.warnings:
        print("  WARN", w)
    for r in t.rows:
        print("  %2d  %-16s %-28s 谷 %-18s 峰 %s" %
              (r.id, r.model, r.span_text(), r.off, r.peak or "-"))
    for model, day in (("deepseek-flash", "2026-09-10"), ("deepseek-flash", "2026-09-28"),
                       ("deepseek-v4-pro", "2026-09-10"), ("deepseek-v4-pro", "2026-09-28"),
                       ("deepseek-something-new", "2026-09-28")):
        ts = _ts(day + " 10:00")
        row, rid = lookup(model, ts)
        print("  %-22s %s → 号 %s" % (model, day, rid if row else "未知(-1)"))
