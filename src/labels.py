#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""labels.py —— 收集「显示名」（纯展示用，不参与任何统计计算）

数据来源都是 DSH 自己维护的映射，不去猜、不去编：

  ① 工作区名：`~/.dsh/storages/workspace.json`
     tables.workspaces[<uuid>] = { path: "C:\\...\\0-ds_harness_2", title: "2-兴趣代码",
                                   sessionIds: [...] }
     → 这是 Web UI 里显示的那个名字，改名后这里会变。

  ② 会话名：`~/.dsh/storages/session_projcache/sessions/<session-id>.json`
     record.rows.title.val = "开销统计"
     → 同样是 UI 显示的那个名字；取不到就退回会话 id。

  ③ 轮次摘要：同一个 projcache 文件里的
     record.rows.turnOutline.val.turns = [{turn, seq, prompt, response}, ...]
     → 取 prompt（用户当时问的话）的开头若干字，方便回 DSH 里对号入座。

设计原则：**标签只影响显示，绝不影响统计**。取不到就回落显示 id，
所以永远不会因为"缺标题"破坏自下而上的累加一致性。
"""
from __future__ import annotations

import json
import os

HOME = os.path.expanduser("~")
DSH_DIR = os.path.join(HOME, ".dsh")
WORKSPACE_JSON = os.path.join(DSH_DIR, "storages", "workspace.json")
PROJCACHE_DIR = os.path.join(DSH_DIR, "storages", "session_projcache", "sessions")

TURN_SUMMARY_CHARS = 14      # 轮次摘要取开头多少字


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _short(text, n=TURN_SUMMARY_CHARS):
    s = " ".join(str(text or "").split())      # 压掉换行和多余空白
    return s[:n] + ("…" if len(s) > n else "")


def read_workspace_titles(path: str = None):
    """返回 {(路径小写归一): 工作区名}。path 给 None 时用 DSH 的默认位置。"""
    p = path or WORKSPACE_JSON
    j = _load_json(p)
    out = {}
    if not isinstance(j, dict):
        return out
    ws = (((j.get("tables") or {}).get("workspaces")) or {})
    if isinstance(ws, dict):
        for _wid, rec in ws.items():
            if not isinstance(rec, dict):
                continue
            cwd = str(rec.get("path") or "")
            title = str(rec.get("title") or "").strip()
            if cwd and title:
                out[cwd.rstrip("\\/").lower()] = title
    return out


def read_session_labels(projdir: str = None, limit_turns: int = 0):
    """扫 projcache，返回 (会话名表, 轮次摘要表)。

    会话名表： {session_id: 标题}
    轮次摘要表：{(session_id, turn_no): 摘要}
    limit_turns > 0 时只取前 N 轮（默认全取）。
    """
    d = projdir or PROJCACHE_DIR
    titles, turns = {}, {}
    if not os.path.isdir(d):
        return titles, turns
    for name in os.listdir(d):
        if not name.endswith(".json"):
            continue
        j = _load_json(os.path.join(d, name))
        if not isinstance(j, dict):
            continue
        rec = j.get("record")
        if not isinstance(rec, dict):
            continue
        rows = rec.get("rows") or {}
        sid = name[:-5]                              # 文件名就是会话 id
        t = rows.get("title")
        if isinstance(t, dict) and t.get("val"):
            titles[sid] = str(t["val"]).strip()
        to = rows.get("turnOutline")
        val = to.get("val") if isinstance(to, dict) else None
        arr = (val or {}).get("turns") if isinstance(val, dict) else None
        if isinstance(arr, list):
            for i, item in enumerate(arr):
                if not isinstance(item, dict):
                    continue
                no = item.get("turn")
                if no is None:
                    continue
                prompt = _short(item.get("prompt"))
                if prompt:
                    turns[(sid, int(no))] = prompt
                if limit_turns and i + 1 >= limit_turns:
                    break
    return titles, turns


def collect(workspace_json: str = None, projdir: str = None):
    """汇总成三张显示表，返回 (ws_by_path, session_titles, turn_titles)。"""
    return read_workspace_titles(workspace_json), *read_session_labels(projdir)
