#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scan_sessions.py —— 把 ~/.dsh/sessions 的会话日志抽成「原子行」写进库（源文件只读）

一条原子行 = 一次模型调用 = 一条 assistant/message 的 usage。
子代理有自己的会话目录，所以天然独立成行，不会互相污染。

规则（都是实测出来的，见 DESIGN.md）：
  1. 一个会话目录里同时有 session.v3/v4.jsonl.zstd 时，只读版本号最大的那份（v4 恒 ⊇ v3）。
  2. 同一文件内部没有重复；重复来自 fork/resume 重放——同一 msg_id 会在多个目录出现。
  3. 归属规则：同 msg_id 且 token 完全相同 → 归最早那次（时间戳最小）的会话；
     其余同名但 token 不同的（真重复生成）单独成行，按 seq 区分。

用法：
  python scan_sessions.py             # 增量（文件没变就跳过）
  python scan_sessions.py --full      # 全量重扫（会清空 messages/sessions 重建）
  python scan_sessions.py --recon     # 顺便刷新 day_recon 对账表
  python scan_sessions.py --probe     # 只看结构，不写库
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db            # noqa: E402
import labels as L   # noqa: E402
import prices as PRICES   # noqa: E402
import subagent_names as SN   # noqa: E402  （子代理显示名目录，纯展示）

VER_RE = re.compile(r"^session\.v(\d+)\.jsonl\.zstd$")


# ---------------------------------------------------------------- 解压 / 解析
def _zstd():
    import zstandard
    return zstandard


def read_lines(path: str):
    with open(path, "rb") as f:
        r = _zstd().ZstdDecompressor().stream_reader(f)
        raw = r.read().decode("utf-8", errors="replace")
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue



def _snippet(msg, limit=100):
    """从一条 assistant 消息里抽一小段文字当"这次调用在干嘛"的摘要。

    优先 **reasoning（思考链）**，其次第一个非空 text —— 正式输出一般在最后，
    前面这段才是模型"想什么"，认调用最划算。
    """
    for want in ("reasoning", "text"):
        for blk in (msg.get("content") or []):
            if not isinstance(blk, dict) or blk.get("type") != want:
                continue
            t = blk.get("text")
            if not isinstance(t, str) or not t.strip():
                continue
            flat = " ".join(t.split())          # 合并换行 / 连续空白
            return flat[:limit] + ("…" if len(flat) > limit else "")
    return ""

def _num(d, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def pick_newest_version(files):
    """从目录内文件名里挑版本号最大的；返回 (version, filename) 或 None。"""
    best = None
    for f in files:
        m = VER_RE.match(f)
        if not m:
            continue
        v = int(m.group(1))
        if best is None or v > best[0]:
            best = (v, f)
    return best


def list_session_dirs(root=None):
    root = root or db.SESSIONS_DIR
    out = []
    if not os.path.isdir(root):
        return out
    for ws_dir in sorted(os.listdir(root)):
        ws_path = os.path.join(root, ws_dir)
        if not os.path.isdir(ws_path):
            continue
        for sid in sorted(os.listdir(ws_path)):
            sdir = os.path.join(ws_path, sid)
            if not os.path.isdir(sdir):
                continue
            try:
                files = os.listdir(sdir)
            except OSError:
                continue
            best = pick_newest_version(files)
            if best:
                out.append((ws_dir, sid, best[0], os.path.join(sdir, best[1])))
    return out


def parse_session(path: str, ts_hint=None):
    """解析一个会话文件 → (meta, messages, unknowns)。

    每条消息：{msg_id, ts, start_ts, turn_no, step_no, model, provider, in/out/cache_read/
              cache_write/reasoning/total_tokens, sig}

    start_ts = 该轮"用户提问时间"：user/message 里没有 turn 号，只能按出现顺序配 —
    在遇到某轮的 assistant 消息之前，最近一条 user/message 的时间就当作该轮的起点；
    找不到就退回用 assistant 自己的时间（保证 start_ts 永远有值）。
    """
    meta = None
    messages = []
    unknowns = collections.Counter()
    last_user_ts = None
    for o in read_lines(path):
        t = o.get("type")
        ts = None
        for k in ("ts", "timestamp", "at", "time"):
            v = o.get(k)
            if isinstance(v, (int, float)) and v > 1e12:
                ts = int(v)
                break
        if t == "session" and meta is None:
            meta = o
            continue
        if t == "user/message":
            if ts:
                last_user_ts = ts
            continue
        d = o.get("data") if isinstance(o.get("data"), dict) else {}
        u = d.get("usage") if isinstance(d, dict) else None
        if t == "assistant/message" and isinstance(u, dict) and "inputTokens" in u:
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            tok = {
                "in_tokens": _num(u, "inputTokens", "promptTokens"),
                "out_tokens": _num(u, "outputTokens", "completionTokens"),
                "cache_read": _num(u, "cacheReadTokens", "cachedTokens"),
                "cache_write": _num(u, "cacheWriteTokens"),
                "reasoning": _num(u, "reasoningTokens"),
                "total_tokens": _num(u, "totalTokens"),
            }
            start = last_user_ts if (last_user_ts and ts and 0 <= ts - last_user_ts < 6 * 3600 * 1000) \
                else (ts or 0)
            messages.append({
                "msg_id": str(msg.get("id") or msg.get("messageId") or ""),
                "ts": ts or 0,
                "start_ts": start,
                "turn_no": _num(d, "turn") or None,
                "step_no": _num(d, "step") or None,
                "model": str(u.get("model") or (meta or {}).get("model") or "deepseek-flash"),
                "provider": str(u.get("provider") or ""),
                "snippet": _snippet(msg),
                "sig": (tok["in_tokens"], tok["out_tokens"], tok["cache_read"], tok["cache_write"]),
                **tok,
            })
        elif isinstance(u, dict):                     # 旁路调用（title / search 等）
            unknowns[t] += _num(u, "totalTokens") or (
                _num(u, "inputTokens") + _num(u, "outputTokens") + _num(u, "cacheReadTokens"))
        elif t == "assistant/message" and d.get("interrupted"):
            # 中断空壳：DSH 罢工/被中断的那一步，记录在（还带毫秒 time、正文、reasoning），
            # 但 **usage 没落盘** → token 只能全 0，并且强制 keep=0（不进任何统计）。
            # 它是"有记录、没钱"：金额只能靠 db.sync_gap_estimates 从官方小时桶的差额里补，
            # 补的时候多笔中断按正文长度加权（所以这里把 chars 记下来）。
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            chars = 0
            for blk in (msg.get("content") or []):
                if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                    chars += len(blk["text"])
            start = last_user_ts if (last_user_ts and ts and 0 <= ts - last_user_ts < 6 * 3600 * 1000) \
                else (ts or 0)
            messages.append({
                "msg_id": str(msg.get("id") or msg.get("messageId") or ""),
                "ts": ts or 0, "start_ts": start,
                "turn_no": _num(d, "turn") or None, "step_no": _num(d, "step") or None,
                "model": str((meta or {}).get("model") or "deepseek-flash"), "provider": "",
                "sig": (0, 0, 0, 0),
                "in_tokens": 0, "out_tokens": 0, "cache_read": 0, "cache_write": 0,
                "reasoning": 0, "total_tokens": 0, "interrupted": 1, "chars": chars,
                "snippet": _snippet(msg),
            })
    return meta, messages, unknowns


def sync_derived_labels(con, quiet=True):
    """给机器生成标题的子代理会话打上中文显示名（src='derived'）。

    名字目录在 `subagent_names.py`（label → 中文名），这里只负责挑候选、读日志、写表：

    * 候选＝labels 里**还是 projcache 原标题**、且标题是纯 ASCII 的会话
      （英文标题＝基本可以断定是注入指令生成的；中文标题的人看着不难受，不动它）；
    * 已经有 derived / derived_none 标记的不再读文件 → 增量友好；
    * 目录里查不到的 label 写一条 `derived_none` 标记（**不改名**，保持 DSH 原标题），
      免得每次扫描都把它重读一遍；想知道有哪些没收录就跑
      `python src\\subagent_names.py --unmapped`。

    纯展示：这些名字只进 labels 表，一个字都不进统计。
    """
    done = {r["key"] for r in con.execute(
        "SELECT key FROM labels WHERE (kind='session' AND src='derived') OR kind='derived_none'")}
    cands = [(r["key"], r["name"]) for r in con.execute(
        "SELECT key, name FROM labels WHERE kind='session' AND src='projcache' AND name<>''")
        if SN.is_ascii_title(r["name"]) and r["key"] not in done]
    if not cands:
        return {"derived": 0, "derived_none": 0}
    files = {r["session_id"]: r["source_file"] for r in
             con.execute("SELECT session_id, source_file FROM sessions")}
    now = int(time.time())
    rows = []
    n_ok = n_none = 0
    for sid, _title in cands:
        p = files.get(sid)
        if not p or not os.path.exists(p):
            continue
        info = SN.probe(p)
        got = SN.display_names(info)
        if not got:
            # 目录里没收录 → 不改名，但留个标记免得下次再读一遍这个文件
            rows.append(("derived_none", sid, "", "", info.get("label") or "", now))
            n_none += 1
            continue
        sess, turn = got
        rows.append(("session", sid, "", sess, "derived", now))
        rows.append(("turn", "%s|1" % sid, "", turn, "derived", now))
        n_ok += 1
    if rows:
        con.executemany(
            "INSERT INTO labels (kind,key,label,name,src,seen_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(kind,key) DO UPDATE SET label=excluded.label, name=excluded.name, "
            "src=excluded.src, seen_at=excluded.seen_at", rows)
        con.commit()
    if not quiet and (n_ok or n_none):
        print("labels: 子代理改名 %d 个（%d 个标签没收录，保持原标题）" % (n_ok, n_none))
    return {"derived": n_ok, "derived_none": n_none}


def write_recon_log(con, path: str = None, quiet=True):
    """把「官方小时桶 vs 我们 vs 已分摊」逐小时写成一份日志（**对账用，不是平账**）。

    每行：天 时 | 官方 | 我们 | 差额 | 空壳数 | 已分摊 | 补后残差 | 分配方式
    """
    import db as _db
    g = _db.gap_summary(con)
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "temp", "official_recon.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    day_tot = {}
    with open(path, "w", encoding="utf-8-sig") as f:   # 带 BOM：记事本/Excel 打开不乱码
        f.write("# 官方小时桶对账（只认 key=%s）\n" % _db.resolve_key_name(con))
        f.write("# 补前 = 官方 − 我们；补后 = 补前 − 已分摊给该小时中断的估算\n")
        f.write("# 生成时间 %s\n\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        f.write("%-11s %-5s %10s %10s %9s %5s %10s %11s  %s\n"
                % ("日期", "小时", "官方", "我们", "差额", "中断", "已分摊", "补后残差", "分配方式"))
        for r in g["rows"]:
            d = day_tot.setdefault(r["day"], [0.0, 0.0, 0.0])
            d[0] += r["official"]; d[1] += r["ours"]; d[2] += r["allocated"]
            note = r["note"] or ("—" if not r["shells"] else "留空（差额≤0 或对不上）")
            f.write("%-11s %4dh %10.4f %10.4f %+9.4f %5d %10.4f %+11.4f  %s\n"
                    % (r["day"], r["hour"], r["official"], r["ours"], r["gap"],
                       r["shells"], r["allocated"], r["residual"], note))
        f.write("\n# 按天小计\n")
        for day in sorted(day_tot):
            o, m, a = day_tot[day]
            f.write("%-11s 官方 %8.4f | 我们 %8.4f | 已分摊 %7.4f | 补后残差 %+8.4f\n"
                    % (day, o, m, a, o - m - a))
        f.write("\n官方合计 %.4f 元 / %d token；有官方开销却没有任何中断可对应的：%.4f 元\n"
                % (g["cost"], g["tokens"], g["unmatched"]))
    if not quiet:
        print("对账日志 → %s" % path)
    return {"path": path, "rows": len(g["rows"]), "unmatched": g["unmatched"]}


# ---------------------------------------------------------------- 扫描
def sync_labels(con, workspace_json=None, projdir=None, quiet=True):
    """把显示名（工作区/会话/轮次）吸进 labels 表。

    纯展示数据：失败或缺失都不影响统计，只是界面上会回落到显示 id。
    """
    ws_by_path, sess_titles, turn_titles = L.read_workspace_titles(workspace_json), \
        *L.read_session_labels(projdir)
    now = int(time.time())
    rows = []
    seen_ws = set()
    for r in con.execute("SELECT session_id, workspace, cwd FROM sessions"):
        ws, cwd, sid = r["workspace"] or "", r["cwd"] or "", r["session_id"]
        if ws and ws not in seen_ws:
            seen_ws.add(ws)
            name = ws_by_path.get(cwd.rstrip("\\/").lower(), "")
            if name:
                rows.append(("workspace", ws, cwd, name, "workspace.json", now))
        if sid in sess_titles:
            rows.append(("session", sid, "", sess_titles[sid], "projcache", now))
    # 轮次摘要的键是 会话id|轮次号
    for (sid, turn), prompt in turn_titles.items():
        rows.append(("turn", "%s|%d" % (sid, turn), "", prompt, "projcache", now))
    con.executemany(
        "INSERT INTO labels (kind,key,label,name,src,seen_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(kind,key) DO UPDATE SET label=excluded.label, name=excluded.name, "
        "src=excluded.src, seen_at=excluded.seen_at WHERE labels.src <> 'derived'", rows)
    con.commit()
    n_ws = sum(1 for r in rows if r[0] == "workspace")
    n_sess = sum(1 for r in rows if r[0] == "session")
    if not quiet:
        print("labels: 工作区名 %d / 会话名 %d / 轮次摘要 %d"
              % (n_ws, n_sess, len(turn_titles)))
    return {"workspace": n_ws, "session": n_sess, "turn": len(turn_titles)}


def scan(full=False, recon=False, quiet=False, root=None, with_labels=True):
    root = root or db.SESSIONS_DIR
    t0 = time.time()
    con = db.connect()
    now = int(time.time())
    # 库结构升级（比如 v3 新加的"中断空壳"列）：老库的列是刚补上的、里面还没有值，
    # 而增量扫描只会重扫"变动过"的会话 → 必须自动来一次全量重扫，否则标签永远填不上。
    if not full:
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None or str(row[0]) != str(db.SCHEMA_VERSION):
            full = True
            if not quiet:
                print("scan: 库结构版本变了（→ v%s），自动全量重扫一次" % db.SCHEMA_VERSION)
    dirs = list_session_dirs(root)
    if full:
        con.execute("DELETE FROM messages")
        con.execute("DELETE FROM sessions")
        con.execute("DELETE FROM sync_log")
        con.commit()

    # 已入库的（未变动的会话）消息，先取出来一起参与全局去重；
    # 变动过的会话会重新解析，旧的先删掉，避免和新结果混在一起。
    stats = collections.Counter()
    pending = []          # 所有参与方：dict(ws_dir,sid,depth,cwd,created_path,version,st,meta,messages,unknowns,is_new)
    # 水位一次全读进来：436 个会话逐个查库白花 0.2 秒
    prev_map = {r["session_id"]: r for r in
                con.execute("SELECT session_id, file_size, file_mtime, version FROM sync_log")}
    for ws_dir, sid, version, path in dirs:
        st = os.stat(path)
        prev = prev_map.get(sid)
        unchanged = (not full and prev and prev["file_size"] == st.st_size
                     and abs((prev["file_mtime"] or 0) - st.st_mtime) < 1e-6
                     and prev["version"] == version)
        if unchanged:
            stats["skipped"] += 1
            pending.append({"ws_dir": ws_dir, "sid": sid, "version": version, "path": path,
                            "st": st, "meta": None, "messages": None, "unknowns": None,
                            "is_new": False})
            continue
        try:
            meta, messages, unknowns = parse_session(path)
        except Exception as e:
            stats["failed"] += 1
            if not quiet:
                print("  ! 解析失败 %s: %s" % (sid, e))
            continue
        if meta is None:
            stats["failed"] += 1
            continue
        messages.sort(key=lambda m: (m["ts"] or 1 << 62))
        con.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        # 工作区名兜底：正常是从目录名拿（DSH 用 --C-...-- 编码），异常目录拿不到时
        # 用 cwd 末级目录顶，避免这个会话在工作区维度变成空值、直接挂到"天"下面。
        if not ws_dir or ws_dir in (".", ".."):
            ws_dir = os.path.basename(str(meta.get("cwd") or "").rstrip("\\/")) or "(未知工作区)"
        pending.append({"ws_dir": ws_dir, "sid": sid, "version": version, "path": path,
                        "st": st, "meta": meta, "messages": messages, "unknowns": unknowns,
                        "is_new": True})
        stats["updated"] += 1

    # ---- 全局按时间排序后再定归属：同一调用（msg_id + token 全同）只算「最早那次」 ----
    # ⚠️ 2026-10-19 修：**分叉副本的 ts 和原件一模一样**，只比 ts（严格小于）分不出胜负。
    # 原创会话一被重扫，它的旧行先被删掉、库里只剩副本 → 原创那份反而被判成"重放"，
    # 于是原件/副本**两边都 keep=0、整轮凭空消失**。现在按 (ts, 会话创建时间, sid) 决胜负：
    # 同一时刻先认"创建更早的会话"（也就是原创那一边），稳定且可重放。
    created = {r["session_id"]: (r["created_at"] or 0) for r in
               con.execute("SELECT session_id, created_at FROM sessions")}
    for p in pending:
        created.setdefault(p["sid"], int(((p.get("meta") or {}).get("createdAt")) or 0))

    def order_key(sid, ts):
        return (ts or 0, created.get(sid, 0), sid)

    # ⚠️ 2026-10-20 补：**裁决必须是全局的**。以前只给"当前扫到的行"下结论：
    # 某个副本所在会话后来一重扫，它的 (ts,创建时间) 更靠前 → 它自己判 keep=1，
    # 而原来那个赢家**没被重扫、还保持着 keep=1** → 同一调用被算两遍（实测 09-23 有 5 份都在算，
    # 那天算出 249%）。现在谁赢就把**库里那个旧赢家降级**（keep=0/replayed=1），一张表只留一份。
    owner = {}            # key -> (排序键, 旧行的 rowid 或 None, 它现在是不是 keep=1)
    demote = []           # (session_id, rowid)：规则说它不该再算的旧行
    if not full:
        for r in con.execute("SELECT rowid rid, msg_id, in_tokens, out_tokens, cache_read, "
                             "cache_write, ts, session_id, keep FROM messages "
                             "WHERE msg_id<>'' AND interrupted=0"):
            k = (r["msg_id"], r["in_tokens"], r["out_tokens"], r["cache_read"], r["cache_write"])
            v = order_key(r["session_id"], r["ts"])
            cur = owner.get(k)
            if cur is None or v < cur[0]:
                if cur is not None and cur[2]:
                    demote.append((cur[3], cur[1]))      # 旧赢家让位
                owner[k] = (v, r["rid"], r["keep"], r["session_id"])
            elif r["keep"]:
                demote.append((r["session_id"], r["rid"]))   # 平手/更晚却又在算 → 降级

    # 只处理**变动过**的会话：未变动的会话 keep 标记早就定好了，上面那张 owner 表
    # （从库里一次查出）已经包含它们，再逐个把它们的消息捞出来排一遍毫无用处。
    all_items = []
    for p in pending:
        if p["messages"] is None:
            continue                       # 未变动的会话：跳过（以前这里白跑了 436 次查询）
        for m in p["messages"]:
            all_items.append((m["ts"], p["sid"], (m["msg_id"], *m["sig"]), m))
    all_items.sort(key=lambda x: (x[0] or 1 << 62))

    # 库里已有 keep 标记的（未变动会话）保持原样，不重新插入
    new_keep = {}

    def take_over(first, sid, m):
        """新的一份赢了：把上一个赢家降级。

        上位可能是**库里已有的行**（没被重扫 → 直接 UPDATE），也可能是**本轮另一份待插入的行**
        （→ 改它的 new_keep）。后者不管就会两边都 keep=1（2026-10-20 实测 5 份都在算）。
        """
        if first is None or not first[2]:
            return
        if first[1] is not None:
            demote.append((first[3], first[1]))
        elif first[4] is not None:
            new_keep[(first[3], id(first[4]))] = (0, 1)
            stats["replayed"] += 1

    for ts, sid, key, m in all_items:
        if m is None:
            continue                       # 未变动会话的归属沿用库里已有值
        mid = key[0]
        cur = order_key(sid, m["ts"])
        first = owner.get(key)
        # 有 ts 就按 (ts, 创建时间, sid) 比；没 ts 的老规矩：只有"之前没人占"才算它的
        earliest = (first is None) or (bool(m["ts"]) and cur < first[0])
        if m.get("interrupted"):
            # 空壳永远 keep=0（不进统计），但**仍然要判"哪一份最早"**：
            # fork 兄弟会话里会有同一次中断的副本（msg_id 相同），只有最早那份参与分配，
            # 副本标 replayed=1（次数才不会虚高）。
            if earliest:
                take_over(first, sid, m)
                owner[key] = (cur, None, 0, sid, m)
            new_keep[(sid, id(m))] = (0, 0 if earliest else 1)
            continue
        if not mid:
            new_keep[(sid, id(m))] = (1, 0)
            continue
        if earliest:
            take_over(first, sid, m)
            owner[key] = (cur, None, 1, sid, m)
            new_keep[(sid, id(m))] = (1, 0)
        else:
            new_keep[(sid, id(m))] = (0, 1)
            stats["replayed"] += 1

    # 把"不该再算"的旧行降级（**跳过本轮要重写的会话**：它们的行已经删了/会用 new_keep 重插，
    # 而且 SQLite 会复用 rowid，误伤就糟了）
    changed_sids = {p["sid"] for p in pending if p["messages"] is not None}
    demote = [x for x in demote if x[0] not in changed_sids and x[1] is not None]
    if demote:
        con.executemany("UPDATE messages SET keep=0, replayed=1 WHERE rowid=?", [(x[1],) for x in demote])
        stats["demoted"] += len(demote)

    unknown_tokens = 0
    for p in pending:
        sid, ws_dir = p["sid"], p["ws_dir"]
        st, meta = p["st"], p["meta"]
        if p["messages"] is not None:
            rows = []
            for i, m in enumerate(p["messages"], 1):
                keep, replay = new_keep.get((sid, id(m)), (1, 0))
                peak = 1 if db.is_peak(m["ts"]) else 0
                if m.get("interrupted"):
                    # 空壳：有记录、没有 usage（钱靠官方差额补，见 db.sync_gap_estimates）
                    cost, prow = 0.0, PRICES.UNKNOWN
                else:
                    # 价目表按 (模型, 时刻) 命中哪一档就按哪一档算；
                    # 没命中 = 未知价目：cost 记 -1（界面横杠），只报 token 不计钱
                    cost, prow = PRICES.cost_of(
                        m["model"], m["ts"], m["in_tokens"], m["out_tokens"],
                        m["cache_read"], m["cache_write"], bool(peak))
                day = db.local_day(m["ts"]) if m["ts"] else ""
                rows.append((
                    sid, m["msg_id"] or "seq-%d" % i, i,
                    m["ts"], day,
                    datetime.fromtimestamp(m["ts"] / 1000.0).hour if m["ts"] else -1,
                    m["turn_no"], m["step_no"], m["model"], m["provider"],
                    m["in_tokens"], m["out_tokens"], m["cache_read"], m["cache_write"],
                    m["reasoning"], m["total_tokens"], peak, cost, prow, keep, replay,
                    m.get("start_ts") or m["ts"], m.get("interrupted", 0), m.get("chars", 0),
                    m.get("snippet") or ""))
            con.executemany(
                "INSERT INTO messages (session_id,msg_id,seq,ts,day,hour,turn_no,step_no,model,"
                "provider,in_tokens,out_tokens,cache_read,cache_write,reasoning,total_tokens,peak,"
                "cost_est,price_row,keep,replayed,start_ts,interrupted,chars,snippet) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            unknown_tokens += sum((p["unknowns"] or {}).values())
            stats["messages"] += len(rows)
        # 主会话 / 子代理归属：同工作区里「创建时间不晚于它」的最近主会话（排除自己）
        cwd = str((meta or {}).get("cwd") or "")
        depth = int((meta or {}).get("delegationDepth") or 0)
        created = int((meta or {}).get("createdAt") or 0)
        if meta is not None:
            con.execute(
                "INSERT INTO sessions (session_id,workspace,cwd,depth,parent_id,agent_preset,created_at,"
                "source_file,source_version,file_size,file_mtime,scanned_at) "
                "VALUES (?,?,?,?,NULL,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET workspace=excluded.workspace, cwd=excluded.cwd, "
                "depth=excluded.depth, agent_preset=excluded.agent_preset, "
                "created_at=excluded.created_at, source_file=excluded.source_file, "
                "source_version=excluded.source_version, file_size=excluded.file_size, "
                "file_mtime=excluded.file_mtime, scanned_at=excluded.scanned_at",
                (sid, ws_dir, cwd, depth, str(meta.get("agentPreset") or ""), created,
                 p["path"], p["version"], st.st_size, st.st_mtime, now))
            con.execute("INSERT INTO sync_log (session_id,file,file_size,file_mtime,version,last_scan) "
                        "VALUES (?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                        "file=excluded.file, file_size=excluded.file_size, "
                        "file_mtime=excluded.file_mtime, version=excluded.version, "
                        "last_scan=excluded.last_scan",
                        (sid, p["path"], st.st_size, st.st_mtime, p["version"], now))

    # 子代理归属统一在最后解析：确保父会话已经入库，并且绝不把自己当父
    con.execute("""
      UPDATE sessions SET parent_id = (
        SELECT p.session_id FROM sessions p
        WHERE p.workspace = sessions.workspace AND p.depth = 0
          AND p.session_id <> sessions.session_id
          AND p.created_at <= sessions.created_at
        ORDER BY p.created_at DESC LIMIT 1)
      WHERE depth > 0
    """)
    replayed = stats["replayed"]

    # 原生搜索账单（旁路）：单独一份 JSONL，不写会话日志，官方小时桶却照收它的钱。
    # 必须在 apply_official_peak 之前吸进来 —— 这样它也能按官方实测的峰谷/价目重算。
    srch = db.sync_search_usage(con)

    # 高峰倍率校验：实际开销以官方小时桶的实测倍率为准（没官方数据的小时保持规则判定）。
    # 必须在汇总之前跑 —— 它改的是 messages.peak / cost_est，sessions 的聚合要跟着一致。
    cal = db.apply_official_peak(con, quiet=True)
    unk = db.unknown_price_summary(con)
    # 搜索账单理论上每条都带 sessionId；查不出来 = 插件/账单出问题了（**不自动猜**）
    unatt = db.search_unattributed(con)
    n_sess = db.rebuild_session_aggs(con)
    # 显示名是从 DSH 的映射表/轮次摘要缓存里读的（440 个 JSON，约 0.8 秒）。
    # 没有会话变动时它不可能变 → 跳过；表还是空的（首次跑）就照读。
    need_labels = with_labels and (full or stats["updated"] > 0
                                   or not con.execute("SELECT 1 FROM labels LIMIT 1").fetchone())
    lab = sync_labels(con, quiet=True) if need_labels else {}
    if need_labels:
        # 派生显示名（"记忆写入 → 记忆空间"）走的是同一张 labels 表，纯展示
        lab.update(sync_derived_labels(con, quiet=True))
    if recon:
        db.sync_ledger(con)
        db.rebuild_day_recon(con)
    # 中断空壳的估算：必须在官方小时桶（official_hour）抓完之后跑
    gap = db.sync_gap_estimates(con)
    # 空转/重试：官方有请求、我们连壳都没有的，按小时把次数和钱补上
    lost = db.sync_lost_calls(con)
    if recon:
        # 逐小时对账日志（补前/补后残差），方便人眼看"哪小时对不上、补了多少"
        write_recon_log(con, quiet=quiet)
    con.execute("INSERT INTO meta (key,value) VALUES ('last_scan',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
    con.execute("INSERT INTO meta (key,value) VALUES ('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(db.SCHEMA_VERSION),))
    con.commit()
    con.close()
    if not quiet:
        print("scan: 目录=%d 更新=%d 跳过=%d 失败=%d 原子行=%d 重放跳过=%d 旁路token=%d 用时=%.1fs"
              % (len(dirs), stats["updated"], stats["skipped"], stats["failed"],
                 stats["messages"], replayed, unknown_tokens, time.time() - t0))
        if lab:
            print("labels: 工作区名 %d / 会话名 %d / 轮次摘要 %d / 记忆写入改名 %d"
                  % (lab.get("workspace", 0), lab.get("session", 0), lab.get("turn", 0),
                     lab.get("derived", 0)))
        if gap.get("shells"):
            print("异常: 中断空壳 %d 个，其中 %d 个按小时对上了账（补回 %.3f 元 / %s token），其余留空"
                  % (gap["shells"], gap["estimated"], gap["cost"], gap.get("tokens", 0)))
        if cal.get("hours"):
            print("校验: 官方小时 %d 个 → 能定倍率 %d 个（改 %d 小时 / %d 行），判不了 %d 个"
                  % (cal["hours"], cal["decided"], cal["changed_hours"], cal["changed_rows"],
                     cal["abstain"]))
        pt = PRICES.load()
        for w in pt.warnings:
            print("价目: " + w)
        if unk.get("calls"):
            print("价目: **未知价目 %d 次调用 / %d token**（钱一律不计，界面上显示横杠）"
                  % (unk["calls"], unk["tokens"]))
        if srch.get("rows"):
            print("搜索: 原生搜索账单 %d 行 → %d 次调用 / %.3f 元 / %s token%s"
                  % (srch["rows"], srch["calls"], srch["cost"], srch["tokens"],
                     ("（跳过坏行 %d）" % srch["bad"]) if srch.get("bad") else ""))
        if unatt.get("rows"):
            print("搜索: ⚠ %d 条没有会话归属（%d 次 / %.3f 元）—— 不该出现；"
                  "查插件是否还在写 sessionId，或补 config/search-fix.txt"
                  % (unatt["rows"], unatt["calls"], unatt["cost"]))

    return {"dirs": len(dirs), "updated": stats["updated"], "skipped": stats["skipped"],
            "failed": stats["failed"], "messages": stats["messages"], "replayed": replayed,
            "sessions": n_sess, "labels": lab, "gap": gap, "cal": cal, "unknown": unk,
            "search": srch, "unattributed": unatt, "lost": lost,
            "seconds": round(time.time() - t0, 1)}


def probe(root=None):
    dirs = list_session_dirs(root)
    ver = collections.Counter(d[2] for d in dirs)
    print("会话目录 = %d" % len(dirs))
    print("版本分布 = %s" % dict(sorted(ver.items())))
    if not dirs:
        return
    ws_dir, sid, version, path = dirs[0]
    meta, messages, unknowns = parse_session(path)
    print("样例 = %s (v%d)" % (sid, version))
    print("  meta keys = %s" % sorted((meta or {}).keys()))
    print("  原子行 = %d，旁路 = %s" % (len(messages), dict(unknowns)))
    if messages:
        m = messages[0]
        print("  首条原子行 = ts=%s turn=%s step=%s in/out/cache=%s/%s/%s total=%s"
              % (m["ts"], m["turn_no"], m["step_no"], m["in_tokens"], m["out_tokens"],
                 m["cache_read"], m["total_tokens"]))
        turns = sorted({m["turn_no"] for m in messages if m["turn_no"]})
        print("  轮次范围 = %s..%s（共 %d 轮）" % (turns[0] if turns else "-",
                                                 turns[-1] if turns else "-", len(turns)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="扫描 DSH 会话日志 → 原子行")
    ap.add_argument("--full", action="store_true", help="全量重扫")
    ap.add_argument("--recon", action="store_true", help="同时刷新对账表")
    ap.add_argument("--probe", action="store_true", help="只看结构，不写库")
    ap.add_argument("--no-labels", action="store_true", help="不收集显示名（工作区/会话/轮次）")
    a = ap.parse_args(argv)
    if a.probe:
        probe()
        return 0
    scan(full=a.full, recon=a.recon, with_labels=not a.no_labels)
    return 0


if __name__ == "__main__":
    sys.exit(main())