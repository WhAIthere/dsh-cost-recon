#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""subagent_names.py —— 子代理会话的中文显示名目录（**纯展示层，不进任何统计**）

为什么有这份东西
----------------
Mnemon / DSH 会 spawn 各种一次性子代理，标题是 DSH 从注入指令生成的英文句子：
`Execute this remember request now`、`Bounded completed checkpoint (untrusted …`……
在树里既认不出谁是谁、看着也不舒服。每个子代理日志里都有一条**结构化的**
`subagent/descriptor.label`（枚举值，比标题可靠得多），这份目录就是「label → 中文名」的映射。

怎么加新条目
------------
1. 跑 `python src\\subagent_names.py --unmapped` —— 它会扫真实库，列出**没被收录**的 label
   （带出现次数、花费、样例标题），顺带报一条命令能省你多少英文名；
2. 在下面的 `RULES` 里加一行；
3. 跑 `python src\\tests\\run_tests.py`（有 `derived.*` 一组建模用例兜底）。

RULES 每行 = (label, prefix, 会话名模板, 轮次名模板)
  label   `subagent/descriptor.label` 的值；prefix=True 表示前缀匹配（label 里带批次号这类）
  模板    可用占位符：{space} {name} {insight} {batch} {event} {doc} {head}
          取不到的占位符会被"安全截断"（见 _fill），不会留下半截箭头或空冒号

**没收录的 label 一律不改名**（保持 DSH 原标题），宁可先看着，也不要瞎起名字。
数据来源只有会话日志本身：只读、只用来做显示名，永远不参与统计口径。
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import labels as L   # noqa: E402  （只为共用一个 _short）

TURN_CHARS = 16        # 轮次名截断长度
DOC_CHARS = 18         # 文档标题这类长文本截断长度

# (label, prefix, 会话名模板, 轮次名模板)
RULES = [
    ("Mnemon remember",                     False, "记忆写入 → {space}",       "记忆写入：{head}"),
    ("Mnemon forget",                       False, "记忆删除 → {space}",       "记忆删除：{insight}"),
    ("Mnemon create-memory-body",           False, "新建记忆空间：{name}",      "新建记忆空间：{name}"),
    ("Mnemon update-memory-body",           False, "更新记忆空间：{name}",      "更新记忆空间：{name}"),
    ("Mnemon idle checkpoint review",       False, "空闲自检",                 "空闲自检：事件 {event}"),
    ("Route runtime memory archive batch",  True,  "记忆归档 第{batch}批",      "记忆归档 第{batch}批"),
    ("Plan managed document archive",       False, "文档归档：{doc}",          "文档归档：{doc}"),
    ("Consolidate local user profile",      False, "整理个人档案",              "整理个人档案"),
    ("Side: New thread",                    False, "侧线对话",                 "侧线对话"),
]


# ---------------------------------------------------------------- 基础
def iter_records(path: str):
    """逐条读会话日志（zstd 流式解压）；坏了、缺了都当空。"""
    import zstandard
    try:
        with open(path, "rb") as f:
            with zstandard.ZstdDecompressor().stream_reader(f) as r:
                raw = r.read().decode("utf-8", errors="replace")
    except Exception:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _texts(node, acc=None):
    """把 tool/result、inbox 这类嵌套结构里的文本全抠出来。"""
    if acc is None:
        acc = []
    if isinstance(node, dict):
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            acc.append(node["text"])
        for v in node.values():
            _texts(v, acc)
    elif isinstance(node, list):
        for v in node:
            _texts(v, acc)
    return acc


def is_ascii_title(t: str) -> bool:
    """标题是不是"纯 ASCII"（＝基本可以断定是机器生成的英文指令）。"""
    return bool(t) and all(ord(ch) < 128 for ch in t)


def rule_for(label: str):
    """按 label 找规则；前缀规则排在后面也没关系（精确匹配优先）。"""
    if not label:
        return None
    for r in RULES:
        if not r[1] and r[0] == label:
            return r
    for r in RULES:
        if r[1] and label.startswith(r[0]):
            return r
    return None


def _fill(tpl: str, ctx: dict) -> str:
    """填模板；某个占位符没有值 → 连它前面的分隔符一起砍掉（不留"记忆写入 → "这种半截）。"""
    out = tpl
    for key in re.findall(r"\{(\w+)\}", tpl):
        v = str(ctx.get(key) or "").strip()
        if v:
            out = out.replace("{%s}" % key, v)
        else:
            out = re.sub(r"[\s→\-:：，,、]+$", "", out.split("{%s}" % key)[0]) or "子代理"
    return out


# ---------------------------------------------------------------- 抽字段
def _scan_request(text: str, info: dict):
    """从注入的请求体里抠字段（remember / forget / create / update / archive 都是这个格式）。"""
    if "remember request" in text:
        info["is_remember"] = True
    m = re.search(r"Preferred Memory Space ID:\s*([^\n\r]+)", text)
    if m and not info["want_space"]:
        info["want_space"] = m.group(1).strip()
    m = re.search(r"Insight ID:\s*([^\n\r]+)", text)
    if m and not info["insight"]:
        info["insight"] = m.group(1).strip().split("-")[0]      # uuid 只留前一段
    m = re.search(r"^\s*-\s*Name:\s*(.+)$", text, re.M)          # 行首的 - Name:
    if m and not info["name"]:
        info["name"] = " ".join(m.group(1).split())[:L.TURN_SUMMARY_CHARS + 4]
    m = re.search(r"Document title:\s*(.+)", text)
    if m and not info["doc"]:
        info["doc"] = L._short(m.group(1), DOC_CHARS)
    m = re.search(r"checkpoint event (\d+)", text)
    if m and not info["event"]:
        info["event"] = m.group(1)
    m = re.search(r"Content \(untrusted data\):\s*\n\s*(.+)", text)
    if m and not info["head"]:
        info["head"] = L._short(m.group(1), TURN_CHARS)


def probe(path: str) -> dict:
    """读一个子代理会话日志 → 字段字典（只读，不改任何东西）。"""
    info = {"label": "", "space": "", "body_name": "", "want_space": "", "name": "",
            "insight": "", "batch": "", "event": "", "doc": "", "head": "",
            "spaces": {}, "is_remember": False}
    for o in iter_records(path):
        t = o.get("type")
        d = o.get("data") if isinstance(o.get("data"), dict) else {}
        if t == "subagent/descriptor":
            info["label"] = info["label"] or str(d.get("label") or "")
        elif t in ("agent/inbox/spliced", "user/message", "system/message"):
            for tx in _texts(d):
                _scan_request(tx, info)
        elif t == "tool/result":
            blob = " ".join(_texts(d))
            m = re.search(r'"memoryBodyName"\s*:\s*"([^"]+)"', blob)
            if m:
                info["body_name"] = m.group(1)
            # 记忆空间目录：id → name（forget / update 只有 id，靠这张小表翻译）
            for m in re.finditer(r'"id"\s*:\s*"([^"]+)"\s*,\s*"name"\s*:\s*"([^"]+)"', blob):
                info["spaces"].setdefault(m.group(1), m.group(2))
    m = re.search(r"batch\s+(\d+/\d+)", info["label"])
    if m:
        info["batch"] = m.group(1)
    info["space"] = (info["body_name"] or info["spaces"].get(info["want_space"], "")
                     or info["want_space"])
    info["name"] = info["name"] or info["body_name"]
    if info["is_remember"] and "remember" not in info["label"]:
        info["label"] = info["label"] or "Mnemon remember"
    return info


def display_names(info: dict):
    """→ (会话名, 轮次名)；没收录这个 label 就返回 None（保持原标题）。"""
    rule = rule_for(info.get("label") or "")
    if not rule:
        return None
    sess = _fill(rule[2], info)
    turn = _fill(rule[3], info)
    return sess, L._short(turn, TURN_CHARS + 6)


# ---------------------------------------------------------------- 自检命令
def unmapped(db_path: str = None, limit: int = 0):
    """扫真实库里的子代理会话，报出没被 RULES 收录的 label（带次数、花费、样例标题）。"""
    import collections
    import sqlite3
    here = os.path.dirname(os.path.abspath(__file__))
    db_path = db_path or os.path.join(os.path.dirname(here), "temp", "token_stats.db")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    titles = {r["key"]: r["name"] for r in
              con.execute("SELECT key, name FROM labels WHERE kind='session'")}
    hits = collections.Counter()
    cost = collections.Counter()
    sample = {}
    renamed = 0
    for r in con.execute("SELECT session_id, source_file, cost_est FROM sessions WHERE depth > 0"):
        sid = r["session_id"]
        info = probe(r["source_file"]) if r["source_file"] else {"label": ""}
        label = info.get("label") or ""
        if display_names(info):
            renamed += 1
            continue
        key = label or "(没有 descriptor.label)"
        hits[key] += 1
        cost[key] += r["cost_est"] or 0
        sample.setdefault(key, titles.get(sid) or "")

    print("子代理会话里已收录并会改名的：%d 个" % renamed)
    print("没收录的 label（%d 种）：" % len(hits))
    for label, n in hits.most_common(limit or None):
        print("  %3d 个 | %6.2f 元 | %-42s | 样例标题: %s"
              % (n, cost[label], label[:42], sample[label][:46]))
    return hits


def _print_rules():
    print("label → 显示名目录（%d 条）" % len(RULES))
    for label, prefix, sess, turn in RULES:
        print("  %-38s %s→ %s  /  %s"
              % (label + ("*" if prefix else ""), "  " if prefix else "", sess, turn))
    print("\n（带 * 的是前缀匹配；占位符：{space} {name} {insight} {batch} {event} {doc} {head}）")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="子代理会话的中文显示名目录（纯展示层）")
    ap.add_argument("--list", action="store_true", help="打印当前映射目录")
    ap.add_argument("--unmapped", action="store_true", help="扫真实库，列出没收录的 label")
    ap.add_argument("--db", default=None, help="指定 token_stats.db（默认 temp/ 下那个）")
    ap.add_argument("--limit", type=int, default=0, help="--unmapped 只显示前 N 条")
    a = ap.parse_args(argv)
    if a.list or not a.unmapped:
        _print_rules()
    if a.unmapped:
        if a.list:
            print()
        unmapped(db_path=a.db, limit=a.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
