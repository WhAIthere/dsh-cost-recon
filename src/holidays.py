#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""holidays.py —— 节假日表（判定"高峰 / 非高峰"用）

只干一件事：给定一个时刻，回答"算不算高峰"。

规则（与 db.is_peak 一致）：工作日的 09:00-12:00、14:00-18:00 是高峰（价格 ×2）；
- 法定放假日（表里 `off`）  → **全天按非高峰**算
- 调休上班日（表里 `work`） → **仍然按非高峰**算：DS 不认调休，周末就是周末
  （表里留着它们只是为了对照与备查，判定结果与"普通周末"相同）
- 其它日子                 → 周一~周五走两个高峰窗口，周末全天非高峰

**这张表只用于对照**（`src/holiday_check.py`），不参与实际开销计算：实际算钱用的是
官方小时桶实测出来的倍率（`db.apply_official_peak()`）。

表放在工具根目录 `holidays.txt`（**给人改的**），文件里带一行【有效期】。
**超出有效期**时：本表不再参与判定（回落成"只看周末"的老规则），并给出
"节假日表需要更新"的提示 —— 绝不因此崩掉或算错 token。
"""
from __future__ import annotations

import datetime as _dt
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_PATH = os.path.join(ROOT, "config", "holidays.txt")

# 与 db.py 保持一致的判定窗口（这里不 import db，免得 db → holidays 之后再成环）
PEAK_WINDOWS = ((9, 12), (14, 18))
PEAK_MULTIPLIER = 2.0

ROW_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(off|work)\s*\|?\s*(.*?)\s*$")
VALID_RE = re.compile(r"^\s*#.*?[【\[]?\s*有效期\s*[】\]]?\s*[:：]?\s*"
                      r"(\d{4}-\d{2}-\d{2})\s*[~～至\-—]+\s*(\d{4}-\d{2}-\d{2})")

_CACHE = {}


class Table:
    """一张节假日表：off = 放假日，work = 调休上班日，都按 'YYYY-MM-DD' 存。"""

    def __init__(self, off=None, work=None, note=None, valid_from=None, valid_to=None,
                 path=None, warnings=None):
        self.off = off or {}          # {'2026-09-25': '中秋节'}
        self.work = work or {}        # {'2026-09-20': '国庆节调休上班（周日）'}
        self.note = note or {}
        self.valid_from = valid_from  # 'YYYY-MM-DD' 或 None（= 不设下限）
        self.valid_to = valid_to
        self.path = path
        self.warnings = list(warnings or [])
        self.stale_days = set()       # 判定时遇过的、超出有效期的天

    # ------------------------------------------------------------ 基本查询
    def kind(self, day: str):
        """返回 'off' / 'work' / None（普通日子）"""
        if day in self.off:
            return "off"
        if day in self.work:
            return "work"
        return None

    def covers(self, day: str) -> bool:
        if self.valid_from is None and self.valid_to is None:
            return True
        if self.valid_from and day < self.valid_from:
            return False
        if self.valid_to and day > self.valid_to:
            return False
        return True

    def is_peak_day_hour(self, day: str, hour: int, weekday: int) -> bool:
        k = self.kind(day)
        if k == "off":
            return False
        if k == "work":
            # 调休上班：DS **不认**调休，照普通周末算（→ 非高峰）。留着这条只是备查。
            return False
        if weekday >= 5:
            return False
        return any(a <= hour < b for a, b in PEAK_WINDOWS)

    def is_peak(self, ts_ms: int) -> bool:
        dt = _dt.datetime.fromtimestamp(ts_ms / 1000.0)
        day = dt.strftime("%Y-%m-%d")
        if not self.covers(day):
            self.stale_days.add(day)          # 超范围：回落老规则，并记一笔
        return self.is_peak_day_hour(day, dt.hour, dt.weekday())

    # ------------------------------------------------------------ 提示
    def range_text(self) -> str:
        if self.valid_from and self.valid_to:
            return "%s ~ %s" % (self.valid_from, self.valid_to)
        return "（未写有效期）"

    def update_hint(self, day: str = None) -> str:
        """该不该去更新表；返回空串 = 不用更新。"""
        bad = None
        if day and not self.covers(day):
            bad = day
        elif self.stale_days:
            bad = sorted(self.stale_days)[-1]
        if not bad:
            return ""
        return ("节假日表需要更新：%s 超出行内有效期 %s（holidays.txt）→ 本次回落到"
                "\"只看周末\"的老规则" % (bad, self.range_text()))

    def describe(self) -> str:
        return ("节假日表：%s（放假 %d 天 / 调休上班 %d 天，有效期 %s）"
                % (os.path.basename(self.path or DEFAULT_PATH), len(self.off), len(self.work),
                   self.range_text()))


def parse(text: str, path: str = None) -> Table:
    off, work, note, vf, vt, warns = {}, {}, {}, None, None, []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            m = VALID_RE.match(line)
            if m:
                vf, vt = m.group(1), m.group(2)
            continue
        m = ROW_RE.match(line)
        if not m:
            warns.append("holidays: 第 %d 行看不懂，已跳过：%s" % (i, line.strip()[:40]))
            continue
        day, kind, label = m.group(1), m.group(2), m.group(3)
        try:
            _dt.date.fromisoformat(day)
        except ValueError:
            warns.append("holidays: 第 %d 行日期不合法，已跳过：%s" % (i, day))
            continue
        (off if kind == "off" else work)[day] = label
        note[day] = label
    if not vf or not vt:
        warns.append("holidays: 没读到【有效期】那一行（写成 `# 【有效期】2026-01-01 ~ 2026-12-31`）")
    return Table(off, work, note, vf, vt, path, warns)


def load(path: str = None, force: bool = False) -> Table:
    """读表（带缓存，按 路径+mtime 失效）。文件不在 → 空表 + 一条 warning，回落到老规则。"""
    path = path or DEFAULT_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    key = (os.path.abspath(path), mtime)
    if force or key not in _CACHE:
        if mtime is None:
            _CACHE.clear()
            t = Table(path=path, warnings=["holidays: 找不到 %s（回落到\"只看周末\"）" % path])
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


def is_peak(ts_ms: int, table: Table = None) -> bool:
    return (table or load()).is_peak(ts_ms)


def from_datestr(day: str, hour: int = 10) -> int:
    """'YYYY-MM-DD' + 小时 → 毫秒时间戳（本机时区），方便把表喂给 is_peak"""
    dt = _dt.datetime.strptime(day, "%Y-%m-%d").replace(hour=hour)
    return int(dt.timestamp() * 1000)


if __name__ == "__main__":
    import sys
    t = load(force=True)
    print(t.describe())
    for w in t.warnings:
        print("  WARN", w)
    for day in sys.argv[1:]:
        print("  %s -> %s" % (day, "高峰" if t.is_peak(from_datestr(day)) else "非高峰"))
    h = t.update_hint()
    if h:
        print(" ", h)
