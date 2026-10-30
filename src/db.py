#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""db.py —— token统计 的共享数据层

职责：
  * 定义全部表结构（SQLite，单文件 token_stats.db）
  * 从 DSH 的账本文件抽「按天 / 按天×模型」的钱与 token
  * 汇总 messages 表（会话日志抽出来的原子行）
  * 三源对账（余额账本 / events / 会话折算）
  * 定价与 peak 判定

设计要点（踩过的坑见 DESIGN.md）：
  * 原子行 = 一次 assistant/message 的 usage（一次模型调用）。子代理有自己的会话目录，天然独立。
  * 主键 (session_id, msg_id, seq)：msg_id 是 DSH 生成的 message id，seq 是该会话内第几条用量记录。
  * 会话目录里同时存在 session.v3/v4.jsonl.zstd 时只读版本号最大的那份（实测 v4 恒 ⊇ v3）。
  * 跨目录重复的 message.id（fork/resume 会重放历史）按「最早时间戳」归属到原会话，只保留一条。
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import time
from datetime import datetime, date, timedelta

import prices as PRICES

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # 工具根目录（看板.html 在这一层）
DB_PATH = os.path.join(ROOT, "temp", "token_stats.db")

HOME = os.path.expanduser("~")
DSH_DIR = os.path.join(HOME, ".dsh")
USAGE_JSON = os.path.join(DSH_DIR, ".dshw-usage.json")
LEDGER_JSON = os.path.join(DSH_DIR, "dsh-usage", "usage-ledger.json")
SESSIONS_DIR = os.path.join(DSH_DIR, "sessions")

SCHEMA_VERSION = 6

# ---------------------------------------------------------------- 价目表
# 来源：~/.dsh/profiles/web/node_modules/@linxin666/dsh-usage/lib/index.js 的 FLASH_PRICE
# 单位 CNY / 百万 token（offPeak）；peak＝工作日 09:00-12:00、14:00-18:00，×2
# ⚠ 这是**内置兜底档**（= prices.txt 的 1 号）；实际算钱走 prices.txt（见 prices.py）。
PRICE = {"cacheHit": 0.02, "inputMiss": 1.0, "output": 4.0}

# 只认这一个 API Key 名字（platform.deepseek.com 上那把）：对账时其它 key（比如 codex）
# 的花费一概不算 —— 它们在同一个账户下，混进来就会把"别的 API"的钱摊到我们的中断上。
KEY_NAME = "harness"     # 兜底默认值；真正用哪把 key 见 resolve_key_name()


def resolve_key_name(con: sqlite3.Connection = None) -> str:
    """用哪把 key 的官方小时桶来对账。

    优先级：环境变量 `DSH_API_KEY_NAME` → 库里花钱最多的那把 key → 默认 "harness"。
    （别人的 key 不叫 harness 时，以前会静默抓不到/对不上；现在自动认。）
    """
    env = os.environ.get("DSH_API_KEY_NAME")
    if env:
        return env
    if con is not None:
        try:
            # 按**请求数**选（比金额稳：金额会被贵模型/长会话带偏，次数才是"哪把 key 在干活"）
            row = con.execute("SELECT api_key_name, SUM(requests) r FROM official_hour "
                              "WHERE requests>0 GROUP BY api_key_name ORDER BY r DESC LIMIT 1"
                              ).fetchone()
            if row and row[0]:
                return row[0]
        except sqlite3.OperationalError:
            pass
    return KEY_NAME
PEAK_MULTIPLIER = 2.0
PEAK_WINDOWS = ((9, 12), (14, 18))

# 价目表（prices.txt / prices.py）：**实际算钱用它**，不是上面这行 PRICE。
# PRICE 现在只当"内置兜底档"（= 1 号 flash 价），留给旧 CLI 与自测；messages.price_row
# 记的是命中的档号，-1 = 未知价目（cost_est 也记 -1，界面显示横杠、不进任何金额统计）。
UNKNOWN_PRICE = PRICES.UNKNOWN

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS sessions (
  session_id   TEXT PRIMARY KEY,
  workspace    TEXT,            -- 目录名（DSH 的编码形式）
  cwd          TEXT,            -- 真实工作区路径
  depth        INTEGER,         -- delegationDepth：0=主会话 1+=子代理
  parent_id    TEXT,            -- 子代理所属主会话（能认出来就填）
  agent_preset TEXT,
  created_at   INTEGER,
  first_ts     INTEGER, last_ts INTEGER,
  messages     INTEGER DEFAULT 0,          -- 原子行数
  calls        INTEGER DEFAULT 0,
  in_tokens    INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0,
  cache_read   INTEGER DEFAULT 0, cache_write INTEGER DEFAULT 0,
  reasoning    INTEGER DEFAULT 0,
  cost_est     REAL DEFAULT 0,             -- 按刊例价推算
  peak_cost    REAL DEFAULT 0, offpeak_cost REAL DEFAULT 0,
  turns        INTEGER DEFAULT 0,          -- 用户轮数（data.turn 的最大值）
  source_file  TEXT, source_version INTEGER,
  file_size    INTEGER, file_mtime REAL, scanned_at INTEGER
);

-- 原子行：一次模型调用（一次 assistant/message 的 usage）
CREATE TABLE IF NOT EXISTS messages (
  session_id TEXT, msg_id TEXT, seq INTEGER,
  ts INTEGER, day TEXT, hour INTEGER,
  start_ts INTEGER,             -- 该轮"用户提问时间"（找不到时回落=ts）
  turn_no INTEGER, step_no INTEGER,
  model TEXT, provider TEXT,
  in_tokens INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0,
  cache_read INTEGER DEFAULT 0, cache_write INTEGER DEFAULT 0,
  reasoning INTEGER DEFAULT 0,
  total_tokens INTEGER DEFAULT 0,
  peak INTEGER DEFAULT 0,
  cost_est REAL DEFAULT 0,
  price_row INTEGER DEFAULT -1,  -- 命中的价目档号（prices.txt 的第一列）；-1 = 未知价目
  keep INTEGER DEFAULT 1,        -- 1 = 归属本会话（口径正确）；0 = 同内容已在更早的会话记过
  replayed INTEGER DEFAULT 0,    -- 1 = 这条是从别的目录重放来的（归属仍算原会话）
  interrupted INTEGER DEFAULT 0, -- 1 = 中断空壳：有记录、没有 usage（token 全 0，keep 恒 0）
  est_cost REAL DEFAULT 0,       -- 估算补回的金额（只给中断空壳用；见 sync_gap_estimates）
  est_tokens INTEGER DEFAULT 0,  -- 估算补回的 token 数（同上）
  est_note TEXT DEFAULT '',      -- 这一笔是怎么分的（"均分（3 个）" / "按正文长度加权" / ""）
  chars INTEGER DEFAULT 0,       -- 中断空壳的正文长度（用来做加权分配）
  snippet TEXT DEFAULT '',      -- 这次调用返回内容的一小段（优先思考链），只给界面显示
  PRIMARY KEY (session_id, msg_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_msg_day      ON messages(day);
CREATE INDEX IF NOT EXISTS idx_msg_ws       ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_msg_mid      ON messages(msg_id);
CREATE INDEX IF NOT EXISTS idx_msg_turn     ON messages(session_id, turn_no);
CREATE INDEX IF NOT EXISTS idx_msg_keep     ON messages(keep);

-- ledger 明细（按天×服务商×模型）
CREATE TABLE IF NOT EXISTS day_model (
  day TEXT, provider TEXT, model TEXT,
  input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER,
  reasoning INTEGER, calls INTEGER, cost REAL,
  PRIMARY KEY (day, provider, model)
);

-- 逐次实付事件（.dshw-usage.json 的 events）
CREATE TABLE IF NOT EXISTS events (
  ts INTEGER PRIMARY KEY, day TEXT, file_day TEXT, model TEXT,
  tokens INTEGER, cost REAL, seen_at INTEGER
);

-- 官方用量（platform.deepseek.com 的小时桶；唯一可信的"外部观察层"）
-- 一天一个 (api_key, model) 一条；cost 只有币种对得上才写
CREATE TABLE IF NOT EXISTS official_hour (
  day TEXT, hour INTEGER,
  api_key_id TEXT, api_key_name TEXT, model TEXT,
  cache_hit INTEGER DEFAULT 0, cache_miss INTEGER DEFAULT 0,
  output INTEGER DEFAULT 0, requests INTEGER DEFAULT 0,
  cost REAL DEFAULT 0, currency TEXT DEFAULT 'CNY',
  fetched_at INTEGER,
  PRIMARY KEY (day, hour, api_key_id, model)
);
CREATE INDEX IF NOT EXISTS idx_off_day ON official_hour(day);
-- 抓过哪些天（用来"只补缺的天"）
CREATE TABLE IF NOT EXISTS official_fetch (
  day TEXT PRIMARY KEY, fetched_at INTEGER, rows INTEGER, note TEXT
);

-- 高峰倍率校验（自校准）：用官方小时桶反推"官方这一小时实际按高峰还是非高峰收"
-- peak_actual: 1=高峰 / 0=非高峰 / NULL=判不出来（金额太小或倍率不像 1/2）
-- rule_peak:   我们原来的规则判定（只看周末+窗口）给的是什么，用来报告"改了几小时"
CREATE TABLE IF NOT EXISTS official_peak (
  day TEXT, hour INTEGER,
  peak_actual INTEGER, ratio REAL, base_cost REAL, rule_peak INTEGER,
  row_id INTEGER,                -- 这一小时按哪一档价算的（未判定/未知 = -1）
  decided_at INTEGER,
  PRIMARY KEY (day, hour)
);

-- 原生搜索账单（旁路）：**不在会话日志、也不在 DSH 账本里**，只有 dsh-search-audit
-- 插件写的那份 JSONL（`~/.dsh/search-usage.jsonl`）。官方小时桶却照收它的钱，
-- 不补这一块，对账就永远差一截。一行 = 一次搜索调用；`src='estimate'` 的行是
-- N 次调用的聚合（`calls_n` 记着 N，token/金额按 N 摊到每次调用上）。
CREATE TABLE IF NOT EXISTS search_calls (
  ts INTEGER PRIMARY KEY,          -- 响应时刻（ms）
  day TEXT, hour INTEGER,
  session_id TEXT, turn_no INTEGER, -- sessionId 可能为 NULL；turn 由 ts 落进哪一轮推出来
  model TEXT, calls_n INTEGER DEFAULT 1,
  in_tokens INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0,
  cache_read INTEGER DEFAULT 0, cache_write INTEGER DEFAULT 0,
  total_tokens INTEGER DEFAULT 0,
  peak INTEGER DEFAULT 0,
  cost_est REAL DEFAULT -1,        -- -1 = 未知价目（同 messages 的口径）
  price_row INTEGER DEFAULT -1,
  src TEXT, note TEXT, query TEXT, synced_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_srch_day  ON search_calls(day);
CREATE INDEX IF NOT EXISTS idx_srch_sess ON search_calls(session_id);

-- 每次 sync 的余额快照
CREATE TABLE IF NOT EXISTS snapshots (
  captured_at INTEGER PRIMARY KEY, file_date TEXT,
  balance REAL, today_usage REAL, day_start REAL
);

-- 三源对账（每天一行，随 sync 更新）
CREATE TABLE IF NOT EXISTS day_recon (
  day TEXT PRIMARY KEY,
  book_cost    REAL,   -- accounting.books 的 debitUnits（权威，精确到分）
  history_cost REAL,   -- .dshw-usage.json 的 history
  events_cost  REAL,   -- events 逐次求和
  ledger_cost  REAL,   -- usage-ledger 求和
  scan_cost    REAL,   -- 会话日志折算
  scan_calls   INTEGER,
  factor       REAL,   -- 校准系数 = book_cost / scan_cost
  note         TEXT
);

-- 旁路调用（title/搜索/压缩摘要等，不写 assistant/message，单独记「未归因」）
CREATE TABLE IF NOT EXISTS side_calls (
  day TEXT, kind TEXT, calls INTEGER, tokens INTEGER,
  PRIMARY KEY (day, kind)
);

-- 旁路调用的**逐会话明细**：side_calls 只是它的按天汇总。
-- 这样可以用增量扫描（只重解压变动过的会话日志，其余不动）——
-- 以前每次刷新都要把所有会话日志解压一遍（438 份 / 60MB，约 14 秒）。
CREATE TABLE IF NOT EXISTS side_session (
  session_id TEXT, day TEXT, kind TEXT, calls INTEGER, tokens INTEGER,
  PRIMARY KEY (session_id, day, kind)
);

-- 旁路扫描自己的水位（不能复用 sync_log：side_calls 跑在 scan 之后，那时 sync_log 已经更新了）
CREATE TABLE IF NOT EXISTS side_log (
  session_id TEXT PRIMARY KEY, file TEXT, file_size INTEGER, file_mtime REAL
);

-- 空转/重试留下的"没记到"的调用：官方有 requests、我们连壳都没有。
-- 只按小时记（没有会话、没有正文、没有 token），**次数和金额**都能对账。
CREATE TABLE IF NOT EXISTS lost_calls (
  day TEXT, hour INTEGER, n INTEGER, cost REAL, note TEXT, computed_at INTEGER,
  PRIMARY KEY (day, hour)
);

-- 上下文压缩的「摘要调用」（compaction/summary）：它也是一次完整的模型调用、也烧钱，
-- 但**不写成 assistant/message**（只在会话日志里留一条事件）。按 ts 去重（分叉副本），
-- 并按 ts 定位到某一轮的前/中/后，界面上显示成「第 N 轮后 · 压缩摘要」。
CREATE TABLE IF NOT EXISTS compaction_events (
  ts INTEGER PRIMARY KEY,
  day TEXT, hour INTEGER, session_id TEXT, turn_no INTEGER, pos TEXT,
  in_tokens INTEGER, out_tokens INTEGER, cache_read INTEGER, cache_write INTEGER,
  total_tokens INTEGER, peak INTEGER, cost_est REAL, price_row INTEGER, synced_at INTEGER
);

-- 显示名（纯展示用，绝不参与统计；取不到就回落显示 id）
--   kind='workspace' : key=工作区目录名, label=cwd, name=UI 里那个名字
--   kind='session'   : key=会话 id,       label='',  name=会话标题
--   kind='turn'      : key=会话id|轮次号,  label='',  name=该轮提问的开头
CREATE TABLE IF NOT EXISTS labels (
  kind TEXT, key TEXT, label TEXT, name TEXT, src TEXT, seen_at INTEGER,
  PRIMARY KEY (kind, key)
);

-- 文件扫描记录（增量用）
CREATE TABLE IF NOT EXISTS sync_log (
  session_id TEXT PRIMARY KEY, file TEXT, file_size INTEGER, file_mtime REAL,
  version INTEGER, last_scan INTEGER
);
"""


# ---------------------------------------------------------------- 基础
def connect(path: str = None) -> sqlite3.Connection:
    """打开库。path 为 None 时每次调用都重新读模块级 DB_PATH（不要在签名里写默认值，
    否则默认参数会在定义时绑定，测试/切库时改了 DB_PATH 也不生效）。"""
    con = sqlite3.connect(path or DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    return con


# 建表语句对**已经存在**的表无能为力（CREATE TABLE IF NOT EXISTS 不会加列），
# 所以老库要靠这里补列。补完还缺数据的话，scan 会发现 schema 版本变了 → 自动全量重扫。
MSG_COLUMNS = {
    "interrupted": "INTEGER DEFAULT 0",   # v3：中断空壳标记
    "est_cost": "REAL DEFAULT 0",         # v3：估算补回的金额
    "est_tokens": "INTEGER DEFAULT 0",    # v3：估算补回的 token
    "chars": "INTEGER DEFAULT 0",         # v4：空壳的正文长度（加权分配用）
    "est_note": "TEXT DEFAULT ''",        # v4：这一笔怎么分的（均分 / 按长度加权）
    "price_row": "INTEGER DEFAULT -1",    # v5：命中的价目档号（-1 = 未知价目）
    "snippet": "TEXT DEFAULT ''",         # v6：这次调用返回内容的一小段（优先思考链）
}

# 其它表的补列（{表: {列: 定义}}）
OTHER_COLUMNS = {
    "official_peak": {"row_id": "INTEGER DEFAULT -1"},   # v5：这一小时按哪一档价算的
}


def _migrate(con: sqlite3.Connection):
    added = []
    have = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    for c in MSG_COLUMNS:
        if c not in have:
            con.execute("ALTER TABLE messages ADD COLUMN %s %s" % (c, MSG_COLUMNS[c]))
            added.append("messages." + c)
    for table, cols in OTHER_COLUMNS.items():
        has = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
        if not has:
            continue                      # 表还没建（executescript 会建全的）
        for c, ddl in cols.items():
            if c not in has:
                con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, c, ddl))
                added.append("%s.%s" % (table, c))
    if added:
        con.commit()
    return added


def sync_gap_estimates(con: sqlite3.Connection) -> dict:
    """把**能按时间对上**的差额分摊给中断空壳（interrupted=1）；**对不上的一律留空**。

    原则（2026-09-28 定）：这是"对账"，不是"平账"。

    - **观察侧只认官方小时桶**（`official_hour` 表，platform.deepseek.com 拉的，
      且只认 `KEY_NAME` 那把 key）—— 之前的 `events`（插件自己算的）和"当天账本"
      都**已弃用**：前者和我们同源、后者只有天级且混着别的 API。
    - 只认**同一个小时**：`官方小时合计 − 我们 keep=1 合计`；差额 ≤ 0 或那小时没有空壳
      → **不分**（空壳保持 0，界面上照旧标黄）。
    - 金额与 token 同进同出；**分配方式写进 `est_note`**：
      多笔且都有正文长度 → 按正文长度加权；否则均分（唯一一笔就写"唯一中断"）。
    - 对不上的部分不塞给任何人，由 `gap_summary()` 逐小时报出来。

    结果只写 `est_cost` / `est_tokens` / `est_note`（先清零再写），**绝不动 cost_est**；
    要不要把估算计入总数是界面开关（app.js 的「含异常」）。
    """
    ev_h = {}
    for r in con.execute("SELECT day, hour, SUM(cost) c, "
                         "SUM(cache_hit + cache_miss + output) t "
                         "FROM official_hour WHERE api_key_name=? GROUP BY day, hour",
                         [resolve_key_name(con)]):
        ev_h[(r["day"], r["hour"])] = (r["c"] or 0.0, r["t"] or 0)
    ours_h = {}
    for r in con.execute("SELECT day, hour, SUM(cost_est) c, SUM(total_tokens) t "
                         "FROM messages WHERE keep=1 AND cost_est>=0 GROUP BY day, hour"):
        ours_h[(r["day"], r["hour"])] = (r["c"] or 0.0, r["t"] or 0)
    shells = {}
    for r in con.execute("SELECT rowid, day, hour, chars, replayed FROM messages WHERE interrupted=1"):
        shells.setdefault((r["day"], r["hour"]), []).append((r["rowid"], r["chars"] or 0, r["replayed"]))

    rows, n_all, n_est, tot_c, tot_t = [], 0, 0, 0.0, 0
    for key, items in shells.items():
        n_all += len([1 for _r, _c, rep in items if not rep])   # 只数"唯一的中断"
        # fork 副本不参与分配（同一次中断只在最早那个会话里恢复）
        uniq = [(r, c) for r, c, rep in items if not rep]
        for rid, _c, rep in items:
            if rep:
                rows.append((0.0, 0, "重复副本（同一次中断在更早的会话里恢复）", rid))
        if not uniq:
            continue
        e = ev_h.get(key)
        if not e:
            continue                       # 这个小时官方没数据 → 对不上，留空
        mine = ours_h.get(key, (0.0, 0))
        gap_c, gap_t = e[0] - mine[0], e[1] - mine[1]
        if gap_c <= 0 and gap_t <= 0:
            continue                       # 这个小时我们没少算 → 留空（可能只是结算跨小时）
        gap_c, gap_t = max(0.0, gap_c), max(0, gap_t)
        # 分配：能按"中断时已生成的正文长度"加权就加权，否则均分（并把方式写进 est_note）
        chars = [c for _, c in uniq]
        if len(uniq) > 1 and sum(chars) > 0:
            w = [max(1, c) for c in chars]
            note = "按正文长度加权（%d 个）" % len(uniq)
        else:
            w = [1] * len(uniq)
            note = ("同一小时差额（唯一中断）" if len(uniq) == 1 else "均分（%d 个）" % len(uniq))
        tw = float(sum(w))
        for (rid, _c), wi in zip(uniq, w):
            pc = gap_c * wi / tw
            pt = int(gap_t * wi / tw)
            rows.append((pc, pt, note, rid))
        n_est += len(uniq)
        tot_c += gap_c
        tot_t += gap_t
    # 先清零再写：上一次的估算不能残留
    con.execute("UPDATE messages SET est_cost=0, est_tokens=0, est_note='' WHERE interrupted=1")
    if rows:
        con.executemany("UPDATE messages SET est_cost=?, est_tokens=?, est_note=? WHERE rowid=?", rows)
    con.commit()
    return {"shells": n_all, "estimated": n_est, "cost": round(tot_c, 6), "tokens": tot_t}


def official_hours(con: sqlite3.Connection, key_name: str = None, day_from: str = None,
                   day_to: str = None) -> dict:
    """官方小时桶（默认只认 harness 那把 key）→ {(day, hour): (cost, tokens, requests)}"""
    q = ("SELECT day, hour, SUM(cost) c, SUM(cache_hit + cache_miss + output) t, "
         "SUM(requests) r FROM official_hour WHERE 1=1")
    a = []
    if key_name:
        q += " AND api_key_name=?"
        a.append(key_name)
    if day_from:
        q += " AND day>=?"
        a.append(day_from)
    if day_to:
        q += " AND day<=?"
        a.append(day_to)
    q += " GROUP BY day, hour"
    return {(r["day"], r["hour"]): (r["c"] or 0.0, r["t"] or 0, r["r"] or 0)
            for r in con.execute(q, a)}


def sync_lost_calls(con: sqlite3.Connection) -> dict:
    """把"官方有请求、我们连壳都没有"的调用按小时记账（空转 / 重试 / 对面没收到）。

    判据只用**次数**：官方 requests > 我们数得出来的调用数（正常 message + 中断空壳 +
    原生搜索 + 压缩摘要）。次数对得上、只是钱差一点的小时**不动** —— 那是价格/峰谷问题，
    留在对账里报出来；次数对不上才是真的"这几次调用我们没记录"。

    金额取"官方这一小时的钱 - 我们已经折算出来的钱"（在 sync_gap_estimates 之后跑，
    空壳已经先分走了它那部分）。**没有 token、没有正文、没有会话**，所以界面上它是单独一行
    「(未记录 · 空转)」，不会被算进任何会话。
    """
    now = int(time.time())
    our = {}
    for r in con.execute("SELECT day, hour, COUNT(*) n, "
                         "COALESCE(SUM(CASE WHEN cost_est>=0 THEN cost_est ELSE 0 END),0) c "
                         "FROM messages WHERE keep=1 AND interrupted=0 AND day<>'' "
                         "GROUP BY day, hour"):
        k = (r["day"], r["hour"])
        n, c0 = our.get(k, (0, 0.0))
        our[k] = (n + r["n"], c0 + (r["c"] or 0))
    for r in con.execute("SELECT day, hour, COUNT(*) n FROM messages "
                         "WHERE interrupted=1 AND day<>'' GROUP BY day, hour"):
        k = (r["day"], r["hour"])
        n, c0 = our.get(k, (0, 0.0))
        our[k] = (n, c0)                      # 空壳只算次数（钱走 est_cost，见下面）
    for r in con.execute("SELECT day, hour, SUM(calls_n) n, "
                         "COALESCE(SUM(CASE WHEN cost_est>=0 THEN cost_est ELSE 0 END),0) c "
                         "FROM search_calls GROUP BY day, hour"):
        k = (r["day"], r["hour"])
        n, c0 = our.get(k, (0, 0.0))
        our[k] = (n + (r["n"] or 0), c0 + (r["c"] or 0))
    for r in con.execute("SELECT day, hour, COUNT(*) n, "
                         "COALESCE(SUM(CASE WHEN cost_est>=0 THEN cost_est ELSE 0 END),0) c "
                         "FROM compaction_events GROUP BY day, hour"):
        k = (r["day"], r["hour"])
        n, c0 = our.get(k, (0, 0.0))
        our[k] = (n + r["n"], c0 + (r["c"] or 0))

    con.execute("DELETE FROM lost_calls")
    rows = []
    key = resolve_key_name(con)
    for r in con.execute("SELECT day, hour, requests, cost FROM official_hour "
                         "WHERE api_key_name=? ORDER BY day, hour", (key,)):
        k = (r["day"], r["hour"])
        have_n, have_c = our.get(k, (0, 0.0))
        missing = (r["requests"] or 0) - have_n
        if missing <= 0:
            continue
        # 空壳吃掉的差额也算"已经折到"的：估算值挂在 messages.est_cost 上
        est = con.execute("SELECT COALESCE(SUM(est_cost),0) c FROM messages "
                          "WHERE interrupted=1 AND day=? AND hour=?", k).fetchone()["c"]
        gap = (r["cost"] or 0) - have_c - (est or 0)
        if gap <= 0:
            continue
        rows.append((k[0], k[1], missing, round(gap, 6),
                     "官方 %d 次请求 / 我们 %d 次调用" % (r["requests"], have_n), now))
    if rows:
        con.executemany("INSERT INTO lost_calls (day,hour,n,cost,note,computed_at) "
                        "VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    return {"hours": len(rows), "calls": sum(x[2] for x in rows),
            "cost": round(sum(x[3] for x in rows), 6)}


def gap_summary(con: sqlite3.Connection, day_from: str = None, day_to: str = None) -> dict:
    """逐小时对账（**这是"对账"不是"平账"**）：官方 vs 我们 vs 已分摊，补前/补后残差都报出来。

    返回：
      rows      每小时一行：official / ours / gap / shells / allocated / residual_after / note
      unmatched 正残差合计（有官方开销但没有任何中断可对应）
      cost/tokens 官方合计
    """
    off = official_hours(con, resolve_key_name(con), day_from, day_to)
    ours = {(r["day"], r["hour"]): (r["c"] or 0.0, r["t"] or 0) for r in con.execute(
        "SELECT day, hour, SUM(cost_est) c, SUM(total_tokens) t FROM messages "
        "WHERE keep=1 AND cost_est>=0 GROUP BY day, hour")}
    # 原生搜索（旁路账）也算"我们"：官方小时桶**含**它的钱，不并进来就永远差一截
    for (d, h), (c, t2, _n) in search_hour_totals(con, day_from, day_to).items():
        c0, t0 = ours.get((d, h), (0.0, 0))
        ours[(d, h)] = (c0 + c, t0 + t2)
    # 压缩摘要同理（它不写 assistant/message，但官方照收）
    for r in con.execute("SELECT day, hour, SUM(CASE WHEN cost_est>=0 THEN cost_est ELSE 0 END) c, "
                         "SUM(total_tokens) t FROM compaction_events GROUP BY day, hour"):
        c0, t0 = ours.get((r["day"], r["hour"]), (0.0, 0))
        ours[(r["day"], r["hour"])] = (c0 + (r["c"] or 0), t0 + (r["t"] or 0))
    # 空转/重试（官方有请求、我们没记录）：同样算"我们"的，钱已经在 lost_calls 里补过
    for r in con.execute("SELECT day, hour, SUM(cost) c FROM lost_calls GROUP BY day, hour"):
        c0, t0 = ours.get((r["day"], r["hour"]), (0.0, 0))
        ours[(r["day"], r["hour"])] = (c0 + (r["c"] or 0), t0)
    alloc = {(r["day"], r["hour"]): (r["c"] or 0.0, r["t"] or 0, r["n"]) for r in con.execute(
        "SELECT day, hour, SUM(est_cost) c, SUM(est_tokens) t, COUNT(*) n FROM messages "
        "WHERE interrupted=1 GROUP BY day, hour")}
    shells = {(r["day"], r["hour"]): r["n"] for r in con.execute(
        "SELECT day, hour, COUNT(*) n FROM messages WHERE interrupted=1 AND replayed=0 "
        "GROUP BY day, hour")}
    notes = {(r["day"], r["hour"]): (r["note"] or "") for r in con.execute(
        "SELECT day, hour, MAX(est_note) note FROM messages WHERE interrupted=1 AND est_note<>'' "
        "GROUP BY day, hour")}
    keys = set(off) | set(ours) | set(shells)
    rows, unmatched, tot_c, tot_t = [], 0.0, 0.0, 0
    for k in sorted(keys):
        o = off.get(k, (0.0, 0, 0))
        m = ours.get(k, (0.0, 0))
        a = alloc.get(k, (0.0, 0, 0))
        res = o[0] - m[0] - a[0]
        if o[0] == 0 and m[0] == 0 and not shells.get(k):
            continue
        tot_c += o[0]
        tot_t += o[1]
        if res > 0 and not shells.get(k):
            unmatched += res              # 有钱、但这一小时没有任何中断 → 明确"对不上"
        elif res > 0 and shells.get(k) and a[0] == 0:
            unmatched += res              # 有中断但差额非正→留空，也算"对不上"
        rows.append({"day": k[0], "hour": k[1], "official": round(o[0], 6),
                     "ours": round(m[0], 6), "gap": round(o[0] - m[0], 6),
                     "shells": shells.get(k, 0), "allocated": round(a[0], 6),
                     "residual": round(res, 6), "note": notes.get(k, "")})
    return {"rows": rows, "unmatched": round(unmatched, 6),
            "cost": round(tot_c, 6), "tokens": tot_t}


def is_peak(ts_ms: int) -> bool:
    """**规则判定**（只看周末 + 两个高峰窗口）。自校准之后，实际算钱用的是
    official_peak 里官方实测出来的倍率，见 apply_official_peak()。

    注意：法定节假日表（holidays.txt）**不参与这里的判定**，它只是对照用的工具
    （src/holiday_check.py）——实测口径以官方小时桶为准。"""
    dt = datetime.fromtimestamp(ts_ms / 1000.0)
    if dt.weekday() >= 5:
        return False
    return any(a <= dt.hour < b for a, b in PEAK_WINDOWS)


# ---------------------------------------------------------------- 高峰倍率校验（自校准）
CAL_TOL = 0.08          # 倍率容差：±8%
CAL_MIN_COST = 0.005    # 这一小时官方金额小于这个数（元）就不判倍率，比值没意义


def _ts_of(day: str, hour: int) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(hour=hour).timestamp() * 1000)


def official_peak_decide(con: sqlite3.Connection, key_name: str = None, tol: float = CAL_TOL,
                         table=None) -> dict:
    """用官方小时桶反推"官方这一小时到底按高峰还是非高峰收"（**两个候选取更近**）。

    对每一小时：
      1. 这一小时用的是哪一档价（`messages.price_row`）；混了多档 / 未知价目 → 不判
      2. 拿**官方的 token** 分别按这一档的【谷价】和【峰价】折一次钱
      3. 谁更接近官方金额就用谁（差值还要在容差 `tol` 之内，否则不判）

    这样不依赖"峰价正好是谷价两倍"这种假设 —— 价目表里怎么写就按怎么写判。
    返回 {(day, hour): {"peak_actual", "ratio", "base", "cand_peak", "row_id", "note"}}
    """
    t = table or PRICES.load()
    key_name = key_name or resolve_key_name(con)
    off = {}
    q = ("SELECT day, hour, SUM(cache_hit) ch, SUM(cache_miss) cm, SUM(output) om, "
         "SUM(cost) cost FROM official_hour WHERE 1=1")
    a = []
    if key_name:
        q += " AND api_key_name=?"
        a.append(key_name)
    q += " GROUP BY day, hour"
    for r in con.execute(q, a):
        off[(r["day"], r["hour"])] = {"ch": r["ch"] or 0, "cm": r["cm"] or 0,
                                      "om": r["om"] or 0, "cost": r["cost"] or 0.0}
    # 这一小时我们用的是哪一档（keep=1 且非空壳的行）
    used = {}
    for r in con.execute("SELECT day, hour, price_row, COUNT(*) n FROM messages "
                         "WHERE keep=1 AND interrupted=0 GROUP BY day, hour, price_row"):
        used.setdefault((r["day"], r["hour"]), []).append((r["price_row"], r["n"]))
    rule = {k: (1 if is_peak(_ts_of(*k)) else 0) for k in off}

    out = {}
    for k, o in off.items():
        info = {"peak_actual": None, "ratio": 0.0, "base": 0.0, "cand_peak": None,
                "row_id": -1, "note": "", "cost": o["cost"]}
        cands = used.get(k) or []
        if not cands:
            info["note"] = "这一小时我们没数据"
            out[k] = info
            continue
        if len(cands) > 1:
            info["note"] = "这一小时混了多档价（%s）" % ",".join(str(c[0]) for c in cands)
            out[k] = info
            continue
        rid = cands[0][0]
        info["row_id"] = rid
        row = t.by_id(rid) if rid is not None else None
        if rid is None or rid < 0 or row is None:
            info["note"] = "未知价目（第 %s 档）" % rid
            out[k] = info
            continue
        c_off = t.cost(row, cache_read=o["ch"], in_tok=o["cm"], out_tok=o["om"], peak=False)
        c_peak = (t.cost(row, cache_read=o["ch"], in_tok=o["cm"], out_tok=o["om"], peak=True)
                  if row.peak_ok else None)
        info["base"], info["cand_peak"] = c_off, c_peak
        if c_off <= 0:
            info["note"] = "官方 token 为 0，判不了"
            out[k] = info
            continue
        info["ratio"] = o["cost"] / c_off
        e_off = abs(o["cost"] - c_off)
        e_peak = abs(o["cost"] - c_peak) if c_peak is not None else None
        if e_peak is None or e_off <= e_peak:
            win, err, cand = 0, e_off, c_off
        else:
            win, err, cand = 1, e_peak, c_peak
        if cand > 0 and err <= tol * cand:
            info["peak_actual"] = win
        else:
            info["note"] = ("两个候选都对不上（谷差 %.4f / 峰差 %s）"
                            % (e_off, "%.4f" % e_peak if e_peak is not None else "—"))
        out[k] = info
    return out


def official_peak_info(con: sqlite3.Connection) -> dict:
    """给界面/日志用：官方小时桶里"能定倍率 / 判不了"的汇总"""
    rows = [dict(r) for r in con.execute("SELECT * FROM official_peak ORDER BY day, hour")]
    return {"hours": len(rows),
            "decided": sum(1 for r in rows if r["peak_actual"] is not None),
            "abstain": sum(1 for r in rows if r["peak_actual"] is None)}


def apply_official_peak(con: sqlite3.Connection, key_name: str = None, quiet: bool = True,
                        table=None) -> dict:
    """**实际开销计算以校验为准**：官方有定论的小时，把 messages 的 peak / cost_est
    改成官方实测出来的那个倍率（按这一行自己的价目档重算）；没官方数据（或判不出来）
    的小时**保持原样**；未知价目（price_row<0）的行不动。

    每次扫描末尾跑一遍（幂等）；--full 重扫后也由它把新插进来的行校正回来。
    结果整体重写进 official_peak 表，供看板/对账日志引用。
    """
    t = table or PRICES.load()
    key_name = key_name or resolve_key_name(con)
    dec = official_peak_decide(con, key_name, table=t)
    now = int(time.time())
    rows, changed_rows, changed_hours = [], 0, 0
    for (day, hour), d in sorted(dec.items()):
        rows.append((day, hour, d["peak_actual"], round(d["ratio"], 6), round(d["base"], 6),
                     1 if is_peak(_ts_of(day, hour)) else 0, d["row_id"], now))
        if d["peak_actual"] is None:
            continue
        upd, n = [], 0
        for m in con.execute(
                "SELECT rowid, peak, price_row, in_tokens, out_tokens, cache_read, cache_write "
                "FROM messages WHERE day=? AND hour=? AND interrupted=0 AND price_row>=0",
                (day, hour)).fetchall():
            row = t.by_id(m["price_row"])
            if row is None:
                continue
            if m["peak"] == d["peak_actual"]:
                continue
            upd.append((d["peak_actual"],
                        t.cost(row, m["in_tokens"], m["out_tokens"], m["cache_read"],
                               m["cache_write"], bool(d["peak_actual"])), m["rowid"]))
            n += 1
        if upd:
            con.executemany("UPDATE messages SET peak=?, cost_est=? WHERE rowid=?", upd)
            changed_rows += n
        # 原生搜索那几行也在这一小时里，倍率同样要跟着官方实测走
        supd = []
        for s in con.execute(
                "SELECT ts, peak, price_row, in_tokens, out_tokens, cache_read, cache_write "
                "FROM search_calls WHERE day=? AND hour=? AND price_row>=0",
                (day, hour)).fetchall():
            srow = t.by_id(s["price_row"])
            if srow is None or s["peak"] == d["peak_actual"]:
                continue
            supd.append((d["peak_actual"],
                         t.cost(srow, s["in_tokens"], s["out_tokens"], s["cache_read"],
                                s["cache_write"], bool(d["peak_actual"])), s["ts"]))
        if supd:
            con.executemany("UPDATE search_calls SET peak=?, cost_est=? WHERE ts=?", supd)
            changed_rows += len(supd)
        # 压缩摘要（compaction/summary）也在这一个小时里，同样跟着官方实测走
        cupd = []
        for s in con.execute(
                "SELECT ts, peak, price_row, in_tokens, out_tokens, cache_read, cache_write "
                "FROM compaction_events WHERE day=? AND hour=? AND price_row>=0",
                (day, hour)).fetchall():
            srow = t.by_id(s["price_row"])
            if srow is None or s["peak"] == d["peak_actual"]:
                continue
            cupd.append((d["peak_actual"],
                         t.cost(srow, s["in_tokens"], s["out_tokens"], s["cache_read"],
                                s["cache_write"], bool(d["peak_actual"])), s["ts"]))
        if cupd:
            con.executemany("UPDATE compaction_events SET peak=?, cost_est=? WHERE ts=?", cupd)
            changed_rows += len(cupd)
        if upd or supd or cupd:
            changed_hours += 1
    con.execute("DELETE FROM official_peak")
    if rows:
        con.executemany("INSERT INTO official_peak (day,hour,peak_actual,ratio,base_cost,"
                        "rule_peak,row_id,decided_at) VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    res = {"hours": len(dec),
           "decided": sum(1 for d in dec.values() if d["peak_actual"] is not None),
           "abstain": sum(1 for d in dec.values() if d["peak_actual"] is None),
           "changed_hours": changed_hours, "changed_rows": changed_rows}
    if not quiet and changed_hours:
        print("校验: 官方小时 %d 个，能定倍率 %d 个（改 %d 小时 / %d 行），判不了 %d 个"
              % (res["hours"], res["decided"], changed_hours, changed_rows, res["abstain"]))
    return res


def unknown_price_summary(con: sqlite3.Connection) -> dict:
    """未知价目（price_row<0）的调用数与 token —— **钱一律不计**，只在界面提示"""
    r = con.execute("SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) t FROM messages "
                    "WHERE keep=1 AND price_row<0").fetchone()
    return {"calls": r["n"] or 0, "tokens": r["t"] or 0}


# ---------------------------------------------------------------- 原生搜索账单（旁路）
def search_log_path() -> str:
    """`~/.dsh/search-usage.jsonl`（可用环境变量 DSH_SEARCH_AUDIT_LOG 覆盖）"""
    return os.environ.get("DSH_SEARCH_AUDIT_LOG") or os.path.join(DSH_DIR, "search-usage.jsonl")


def read_search_rows(path: str = None) -> tuple:
    """读搜索账单 JSONL → (rows, bad)。

    只追加的文件，**末行可能正在写**（半截 JSON）→ 坏行直接跳过，不算错。
    """
    path = path or search_log_path()
    rows, bad = [], 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(o, dict) or not o.get("ts"):
                    bad += 1
                    continue
                rows.append(o)
    except OSError:
        pass
    return rows, bad


def _search_calls_n(o) -> int:
    """estimate 行是"N 次调用的聚合"：从 query 里把 N 抠出来（认不出就当 1 次）"""
    if str(o.get("source") or "") != "estimate":
        return 1
    m = re.search(r"(\d+)\s*次", str(o.get("query") or ""))
    return int(m.group(1)) if m and int(m.group(1)) > 0 else 1


def _turn_windows(con):
    """{会话: [(轮起点 ts, turn_no) 升序]} —— 每轮最早的 start_ts（= 用户提问时刻）"""
    out = {}
    for r in con.execute(
            "SELECT session_id, turn_no, MIN(COALESCE(start_ts, ts)) s FROM messages "
            "WHERE keep=1 AND turn_no IS NOT NULL GROUP BY session_id, turn_no"):
        out.setdefault(r["session_id"], []).append((r["s"] or 0, r["turn_no"]))
    for v in out.values():
        v.sort()
    return out


def _turn_of(windows, sid, ts):
    """搜索落在哪一轮：取「轮起点 ≤ ts」的最后一轮（参考文档 §5 的口径）；找不到就 None"""
    if not sid:
        return None
    best = None
    for start, turn in windows.get(sid, ()):
        if start and start <= ts:
            best = turn
        else:
            break
    return best


# 搜索账单理论上**一定带 sessionId**。第一轮测试时插件还没写这个字段，留了一条没有的 ——
# 这种不肯猜（猜错了钱会落进别人的轮次）。要补就在 `config/search-fix.txt` 里人工指定：
#     <ts> | <session_id> | 备注
# **以后再有查不出来的，就是插件/账单出问题了**：扫描时会打警告，界面上明摆着"未归属搜索"。
SEARCH_FIX_PATH = os.path.join(ROOT, "config", "search-fix.txt")


def load_search_fix(path: str = None) -> dict:
    """读人工补的会话归属 → {ts: (session_id, 备注)}"""
    out = {}
    path = path or SEARCH_FIX_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [x.strip() for x in line.split("|")]
                if len(parts) < 2 or not parts[0].isdigit() or not parts[1]:
                    continue
                out[int(parts[0])] = (parts[1], parts[2] if len(parts) > 2 else "")
    except OSError:
        pass
    return out


def search_unattributed(con: sqlite3.Connection) -> dict:
    """没有会话归属的搜索（不该出现；出现就是账单缺字段）"""
    r = con.execute("SELECT COUNT(*) n, COALESCE(SUM(calls_n),0) c, "
                    "COALESCE(SUM(CASE WHEN cost_est>=0 THEN cost_est ELSE 0 END),0) cost "
                    "FROM search_calls WHERE session_id IS NULL").fetchone()
    return {"rows": r["n"] or 0, "calls": r["c"] or 0, "cost": round(r["cost"] or 0.0, 6)}


def sync_search_usage(con: sqlite3.Connection, path: str = None) -> dict:
    """把搜索账单吸进 `search_calls`（幂等：按 ts 覆盖，整份重读）。

    `load` 完顺手算好价目与峰谷：模型走 `prices.txt`（`deepseek-v4-flash` 已登记同 flash 价），
    峰谷先用规则判，之后 `apply_official_peak()` 会按官方小时桶实测再校一遍。
    """
    raw, bad = read_search_rows(path)
    if not raw:
        return {"rows": 0, "bad": bad, "calls": 0, "cost": 0.0, "tokens": 0}
    t = PRICES.load()
    windows = _turn_windows(con)
    fix = load_search_fix()
    now = int(time.time())
    out, tot_c, tot_t, tot_n = [], 0.0, 0, 0
    for o in raw:
        try:
            ts = int(o.get("ts") or 0)
        except (TypeError, ValueError):
            bad += 1
            continue
        u = o.get("usage") or {}
        n = _search_calls_n(o)
        inp = int(u.get("inputTokens") or 0)
        outp = int(u.get("outputTokens") or 0)
        hit = int(u.get("cacheReadTokens") or 0)
        wrt = int(u.get("cacheWriteTokens") or 0)
        model = str(o.get("model") or "")
        sid = o.get("sessionId") or None
        note = str(o.get("note") or "")
        if ts in fix and (not sid or fix[ts][0] != sid):
            # 账单缺字段/写错：用 config/search-fix.txt 里人工补的（**不自动猜**）
            sid = fix[ts][0]
            mark = "（会话是人工补的：%s）" % (fix[ts][1] or "config/search-fix.txt")
            note = (note + mark) if note else mark
        peak = 1 if is_peak(ts) else 0
        row, rid = PRICES.lookup(model, ts, table=t)
        cost = t.cost(row, inp, outp, hit, wrt, bool(peak)) if row is not None else -1.0
        out.append((ts, local_day(ts), datetime.fromtimestamp(ts / 1000.0).hour, sid,
                    _turn_of(windows, sid, ts), model, n, inp, outp, hit, wrt,
                    inp + outp + hit + wrt, peak, cost, rid, str(o.get("source") or ""),
                    note, str(o.get("query") or "")[:200], now))
        tot_c += max(0.0, cost)
        tot_t += inp + outp + hit + wrt
        tot_n += n
    con.executemany(
        "INSERT INTO search_calls (ts,day,hour,session_id,turn_no,model,calls_n,in_tokens,"
        "out_tokens,cache_read,cache_write,total_tokens,peak,cost_est,price_row,src,note,"
        "query,synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ts) DO UPDATE SET day=excluded.day, hour=excluded.hour, "
        "session_id=excluded.session_id, turn_no=excluded.turn_no, model=excluded.model, "
        "calls_n=excluded.calls_n, in_tokens=excluded.in_tokens, out_tokens=excluded.out_tokens, "
        "cache_read=excluded.cache_read, cache_write=excluded.cache_write, "
        "total_tokens=excluded.total_tokens, peak=excluded.peak, cost_est=excluded.cost_est, "
        "price_row=excluded.price_row, src=excluded.src, note=excluded.note, "
        "query=excluded.query, synced_at=excluded.synced_at", out)
    con.commit()
    return {"rows": len(out), "bad": bad, "calls": tot_n,
            "cost": round(tot_c, 6), "tokens": tot_t}


def search_hour_totals(con: sqlite3.Connection, day_from: str = None, day_to: str = None) -> dict:
    """{(day, hour): (cost, tokens, calls)} —— 只算有价的（未知价目不计钱）"""
    q = ("SELECT day, hour, SUM(CASE WHEN cost_est>=0 THEN cost_est ELSE 0 END) c, "
         "SUM(total_tokens) t, SUM(calls_n) n FROM search_calls WHERE 1=1")
    a = []
    if day_from:
        q += " AND day>=?"
        a.append(day_from)
    if day_to:
        q += " AND day<=?"
        a.append(day_to)
    q += " GROUP BY day, hour"
    return {(r["day"], r["hour"]): (r["c"] or 0.0, r["t"] or 0, r["n"] or 0)
            for r in con.execute(q, a)}


def search_summary(con: sqlite3.Connection, day_from: str = None, day_to: str = None) -> dict:
    """搜索账单的汇总（给界面 KPI 用）"""
    q = ("SELECT COALESCE(SUM(calls_n),0) n, COALESCE(SUM(total_tokens),0) t, "
         "COALESCE(SUM(CASE WHEN cost_est>=0 THEN cost_est ELSE 0 END),0) c, "
         "COALESCE(SUM(CASE WHEN price_row<0 THEN calls_n ELSE 0 END),0) unk "
         "FROM search_calls WHERE 1=1")
    a = []
    if day_from:
        q += " AND day>=?"
        a.append(day_from)
    if day_to:
        q += " AND day<=?"
        a.append(day_to)
    r = con.execute(q, a).fetchone()
    return {"calls": r["n"] or 0, "tokens": r["t"] or 0, "cost": round(r["c"] or 0.0, 6),
            "unknown": r["unk"] or 0}


# ---------------------------------------------------------------- 疑似缓存重建（只作参考）
# 判据（2026-10-19 实测标定：上下文 ≥20k 的首调用里，未命中率中位数 0%，90% 分位才 6%；
# 重建的轮则是"首调用整段未命中 → 次调用立刻回到 0%"）：
#   ① 硬线一：该轮**第一条调用**的未命中率 ≥ RB_LOOSE_MISS（宽）/ ≥ RB_STRICT_MISS（严）
#   ② 硬线二：这条调用的输入上下文 ≥ RB_MIN_CTX（太小的重建也就几分钱，不看）
#   ③ 确认线（只严判要求）：同一轮**第二条**调用的未命中率 < RB_CONFIRM_MISS
#      —— 区分"真重建"（爆一次就回落）和"这轮本来塞了很多新内容"（次条还带着新内容）
# **只做展示**：不进 cost_est、不进 KPI、不进任何筛选。
RB_MIN_CTX = 50000        # 上下文规模下限（token）
RB_LOOSE_MISS = 0.50      # 宽判：首调用未命中率
RB_STRICT_MISS = 0.70     # 严判：首调用未命中率
RB_CONFIRM_MISS = 0.10    # 严判的确认线：次调用未命中率要低于它


def _miss_ratio(m) -> tuple:
    """(输入总量, 未命中率) —— 未命中 = 未命中输入 + 写缓存（与计价同口径）"""
    miss = (m["in_tokens"] or 0) + (m["cache_write"] or 0)
    total = miss + (m["cache_read"] or 0)
    return total, (miss / total if total else 0.0)


def turn_rebuild_flags(con: sqlite3.Connection, day_from: str = None,
                       day_to: str = None, table=None) -> dict:
    """逐轮判断"疑似缓存重建" → {(session_id, turn_no): {"level", "cost", "ts"}}

    level: 0=没嫌疑 / 1=宽判命中 / 2=严判命中
    cost : 估算**多花**的钱 = 未命中 token ×（未命中价 − 命中价），按这一行的价目档与峰谷算
    ts   : 该轮第一条调用的时间（界面按它把那笔钱挂到这一行上）
    """
    t = table or PRICES.load()
    q = ("SELECT session_id, turn_no, ts, step_no, in_tokens, cache_read, cache_write, "
         "price_row, peak FROM messages WHERE keep=1 AND interrupted=0 "
         "AND turn_no IS NOT NULL")
    a = []
    if day_from:
        q += " AND day>=?"
        a.append(day_from)
    if day_to:
        q += " AND day<=?"
        a.append(day_to)
    q += " ORDER BY session_id, turn_no, ts, step_no"
    turns = {}
    for r in con.execute(q, a):
        turns.setdefault((r["session_id"], r["turn_no"]), []).append(r)

    out = {}
    for key, seq in turns.items():
        first = seq[0]
        second = seq[1] if len(seq) > 1 else None
        ctx, ratio = _miss_ratio(first)
        level = 0
        if ctx >= RB_MIN_CTX and ratio >= RB_LOOSE_MISS:
            if ratio >= RB_STRICT_MISS and (second is None
                                            or _miss_ratio(second)[1] < RB_CONFIRM_MISS):
                level = 2
            else:
                level = 1
        cost = 0.0
        if level:
            row = t.by_id(first["price_row"]) if first["price_row"] is not None else None
            if row is not None:
                p = row.price_of(bool(first["peak"]))
                miss = (first["in_tokens"] or 0) + (first["cache_write"] or 0)
                cost = miss * max(0.0, p[1] - p[0]) / 1e6
        out[key] = {"level": level, "cost": round(cost, 6), "ts": first["ts"],
                    "step_no": first["step_no"]}
    return out


# ---------------------------------------------------------------- 压缩摘要（compaction/summary）
def sync_compactions(con: sqlite3.Connection, events, table=None) -> dict:
    """上下文压缩的「写摘要」调用：逐条入库，按 ts 去重，按 ts 定位到轮次的前/中/后。

    它跟普通调用一样是一次完整的模型调用（还可能把整段上下文按未命中价重发一遍），
    但**不写成 assistant/message**，所以必须单独记 —— 否则对账永远差这 0.5 元级的一笔。

    events: [{"ts": ms, "session_id": sid, "in": n, "out": n, "hit": n, "write": n, "model": ...}]
    """
    t = table or PRICES.load()
    windows = _turn_windows(con)
    created = {r["session_id"]: (r["created_at"] or 0) for r in
               con.execute("SELECT session_id, created_at FROM sessions")}
    existing = {r["ts"]: dict(r) for r in con.execute("SELECT * FROM compaction_events")}
    if not events and not existing:
        return {"rows": 0, "cost": 0.0, "calls": 0, "new": 0}

    # 同一份摘要在分叉会话的日志里各记一份 → 只留一份（认创建更早的会话，跟消息去重同规矩）
    best = {}
    for e in events:
        try:
            ts = int(e["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        sid = str(e.get("session_id") or "")
        k = (created.get(sid, 0), sid)
        if ts not in best or k < best[ts][0]:
            best[ts] = (k, e)
    # 再把库里已有的逐条重算一遍（钱/轮次/前后位置）。
    # 原因：side_calls 可能先于主扫描跑（那时 messages 还是空的），算出来的轮次/金额会是错的；
    # 反正事件很少，每次都整表重算最省心，也让"先跑谁"不影响结果。
    for ts, old in existing.items():
        if ts in best:
            continue
        sid = old["session_id"] or ""
        k = (created.get(sid, 0), sid)
        if ts not in best or k < best[ts][0]:
            best[ts] = (k, {"ts": ts, "session_id": sid,
                            "in": old["in_tokens"], "out": old["out_tokens"],
                            "hit": old["cache_read"], "write": old["cache_write"],
                            "model": ""})

    out, tot_c = [], 0.0
    new_rows = 0
    for ts, (k, e) in best.items():
        old = existing.get(ts)
        if old:
            old_k = (created.get(old["session_id"], 0), old["session_id"] or "")
            if old_k < k:                    # 库里那份会话更早：沿用它的 token
                sid = old["session_id"] or ""
                e = {"ts": ts, "session_id": sid, "in": old["in_tokens"],
                     "out": old["out_tokens"], "hit": old["cache_read"],
                     "write": old["cache_write"], "model": ""}
        sid = str(e.get("session_id") or "")
        inp, outp = int(e.get("in") or 0), int(e.get("out") or 0)
        hit, wrt = int(e.get("hit") or 0), int(e.get("write") or 0)
        model = str(e.get("model") or "")
        if not model:
            # 事件里没带模型：退一步用它所在会话最近一次调用的模型（实测就是这个口径）
            r = con.execute("SELECT model FROM messages WHERE session_id=? AND ts<=? "
                            "ORDER BY ts DESC LIMIT 1", (sid, ts)).fetchone()
            model = (r["model"] if r else "") or ""
        peak = 1 if is_peak(ts) else 0
        row, rid = PRICES.lookup(model, ts, table=t)
        cost = t.cost(row, inp, outp, hit, wrt, bool(peak)) if row is not None else -1.0
        # 落在哪一轮的前/中/后
        turn = _turn_of(windows, sid, ts)
        pos = "中"
        if turn is not None:
            b = con.execute("SELECT MIN(ts) a, MAX(ts) z FROM messages WHERE session_id=? "
                            "AND turn_no=? AND keep=1", (sid, turn)).fetchone()
            if b and b["a"] is not None:
                if ts < b["a"]:
                    pos = "前"
                elif ts > (b["z"] or b["a"]):
                    pos = "后"
        if ts not in existing:
            new_rows += 1
        out.append((ts, local_day(ts), datetime.fromtimestamp(ts / 1000.0).hour, sid,
                    turn if turn is not None else 0, pos, inp, outp, hit, wrt,
                    inp + outp + hit + wrt, peak, cost, rid, int(time.time())))
        tot_c += max(0.0, cost)
    con.executemany(
        "INSERT INTO compaction_events (ts,day,hour,session_id,turn_no,pos,in_tokens,"
        "out_tokens,cache_read,cache_write,total_tokens,peak,cost_est,price_row,synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ts) DO UPDATE SET day=excluded.day, hour=excluded.hour, "
        "session_id=excluded.session_id, turn_no=excluded.turn_no, pos=excluded.pos, "
        "in_tokens=excluded.in_tokens, out_tokens=excluded.out_tokens, "
        "cache_read=excluded.cache_read, cache_write=excluded.cache_write, "
        "total_tokens=excluded.total_tokens, peak=excluded.peak, cost_est=excluded.cost_est, "
        "price_row=excluded.price_row, synced_at=excluded.synced_at", out)
    con.commit()
    return {"rows": len(out), "cost": round(tot_c, 6), "calls": len(out), "new": new_rows}


def compaction_summary(con: sqlite3.Connection, day_from: str = None,
                       day_to: str = None) -> dict:
    q = ("SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) t, "
         "COALESCE(SUM(CASE WHEN cost_est>=0 THEN cost_est ELSE 0 END),0) c "
         "FROM compaction_events WHERE 1=1")
    a = []
    if day_from:
        q += " AND day>=?"
        a.append(day_from)
    if day_to:
        q += " AND day<=?"
        a.append(day_to)
    r = con.execute(q, a).fetchone()
    return {"n": r["n"] or 0, "tokens": r["t"] or 0, "cost": round(r["c"] or 0.0, 6)}


def call_cost(in_tok=0, out_tok=0, cache_read=0, cache_write=0, peak=False) -> float:
    m = PEAK_MULTIPLIER if peak else 1.0
    return (cache_read * PRICE["cacheHit"] * m
            + (in_tok + cache_write) * PRICE["inputMiss"] * m
            + out_tok * PRICE["output"] * m) / 1e6


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 账本侧抽数
def local_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0).strftime("%Y-%m-%d")


def load_usage_file():
    """返回 (events, history, book_days, snapshot)。缺文件返回空。"""
    if not os.path.exists(USAGE_JSON):
        return [], {}, {}, None
    j = read_json(USAGE_JSON)
    events = []
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
    book_days = {}
    try:
        acc = j.get("accounting") or {}
        books = acc.get("books") or {}
        for _bid, book in books.items():
            for day, rec in (book.get("days") or {}).items():
                book_days[day] = float(rec.get("debitUnits") or 0) / 1e8
    except Exception:
        pass
    snap = {
        "captured_at": int(time.time()),
        "file_date": str(j.get("date") or ""),
        "balance": j.get("lastBalance"),
        "today_usage": j.get("todayUsage"),
        "day_start": j.get("dayStart"),
    }
    return events, {k: float(v) for k, v in (j.get("history") or {}).items()}, book_days, snap


def load_ledger():
    if not os.path.exists(LEDGER_JSON):
        return []
    j = read_json(LEDGER_JSON)
    out = []
    for day, providers in (j.get("days") or {}).items():
        for provider, models in (providers or {}).items():
            for model, u in (models or {}).items():
                out.append({
                    "day": day, "provider": provider, "model": model,
                    "input": int(u.get("inputTokens") or 0),
                    "output": int(u.get("outputTokens") or 0),
                    "cache_read": int(u.get("cacheReadTokens") or 0),
                    "cache_write": int(u.get("cacheWriteTokens") or 0),
                    "reasoning": int(u.get("reasoningTokens") or 0),
                    "calls": int(u.get("calls") or 0),
                    "cost": float(u.get("cost") or 0.0),
                })
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def sync_ledger(con: sqlite3.Connection) -> dict:
    """把账本文件（events / ledger / 余额快照）吸进库。会话日志由 scan_sessions.py 负责。"""
    events, history, book_days, snap = load_usage_file()
    ledger = load_ledger()
    now = int(time.time())
    con.executemany(
        "INSERT INTO events (ts,day,file_day,model,tokens,cost,seen_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(ts) DO UPDATE SET day=excluded.day, model=excluded.model, "
        "tokens=excluded.tokens, cost=excluded.cost",
        [(e["ts"], e["day"], e["file_day"], e["model"], e["tokens"], e["cost"], now) for e in events])
    con.executemany(
        "INSERT INTO day_model (day,provider,model,input,output,cache_read,cache_write,"
        "reasoning,calls,cost) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(day,provider,model) DO UPDATE SET input=excluded.input, output=excluded.output, "
        "cache_read=excluded.cache_read, cache_write=excluded.cache_write, "
        "reasoning=excluded.reasoning, calls=excluded.calls, cost=excluded.cost",
        [(d["day"], d["provider"], d["model"], d["input"], d["output"], d["cache_read"],
          d["cache_write"], d["reasoning"], d["calls"], d["cost"]) for d in ledger])
    if snap:
        con.execute(
            "INSERT INTO snapshots (captured_at,file_date,balance,today_usage,day_start) "
            "VALUES (?,?,?,?,?) ON CONFLICT(captured_at) DO NOTHING",
            (snap["captured_at"], snap["file_date"], _f(snap["balance"]),
             _f(snap["today_usage"]), _f(snap["day_start"])))
    con.commit()
    return {"events": len(events), "day_model": len(ledger), "history": len(history),
            "book_days": len(book_days), "snapshot": bool(snap)}


# ---------------------------------------------------------------- 汇总 / 对账
def rebuild_session_aggs(con: sqlite3.Connection) -> int:
    """从 messages 重算 sessions 的聚合字段。

    只统计 keep=1 的行：keep=0 是 fork/resume 重放的历史，钱和 token 已经算在更早的
    会话头上了，再统计一次就会双份。

    ⚠️ 2026-10 改法：以前是**一个 UPDATE 带 11 个相关子查询**（每个会话再扫一遍 messages），
    436 个会话 × 11 × 全表 ≈ 几千万行 → 单这一条 SQL 要 **34 秒**。现在先把全表
    `GROUP BY session_id` 算一遍（一趟），再按会话 UPDATE —— 同一份数据，毫秒级。
    """
    rows = con.execute("""
      SELECT session_id,
             COUNT(*)                                        AS messages,
             COUNT(*)                                        AS calls,
             COALESCE(SUM(in_tokens),0)                      AS in_tokens,
             COALESCE(SUM(out_tokens),0)                     AS out_tokens,
             COALESCE(SUM(cache_read),0)                     AS cache_read,
             COALESCE(SUM(cache_write),0)                    AS cache_write,
             COALESCE(SUM(reasoning),0)                      AS reasoning,
             COALESCE(SUM(CASE WHEN cost_est>=0 THEN cost_est ELSE 0 END),0)         AS cost_est,
             COALESCE(SUM(CASE WHEN cost_est>=0 AND peak=1 THEN cost_est ELSE 0 END),0) AS peak_cost,
             COALESCE(SUM(CASE WHEN cost_est>=0 AND peak=0 THEN cost_est ELSE 0 END),0) AS offpeak_cost,
             COALESCE(MAX(turn_no),0)                        AS turns,
             MIN(ts)                                         AS first_ts,
             MAX(ts)                                         AS last_ts
      FROM messages WHERE keep=1 GROUP BY session_id
    """).fetchall()
    # 没有任何 keep=1 行的会话（全是重放副本）要归零，否则会留着上一次的旧值
    con.execute("UPDATE sessions SET messages=0, calls=0, in_tokens=0, out_tokens=0, "
                "cache_read=0, cache_write=0, reasoning=0, cost_est=0, peak_cost=0, "
                "offpeak_cost=0, turns=0, first_ts=NULL, last_ts=NULL")
    con.executemany(
        "UPDATE sessions SET messages=?, calls=?, in_tokens=?, out_tokens=?, cache_read=?, "
        "cache_write=?, reasoning=?, cost_est=?, peak_cost=?, offpeak_cost=?, turns=?, "
        "first_ts=?, last_ts=? WHERE session_id=?",
        [(r["messages"], r["calls"], r["in_tokens"], r["out_tokens"], r["cache_read"],
          r["cache_write"], r["reasoning"], r["cost_est"], r["peak_cost"], r["offpeak_cost"],
          r["turns"], r["first_ts"], r["last_ts"], r["session_id"]) for r in rows])
    con.commit()
    return con.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]


# ---------------------------------------------------------------- 显示名
def load_labels(con: sqlite3.Connection):
    """读显示名表，返回 (ws_names, session_names, turn_names)。

    这些**只用于显示**，不参与任何统计；缺了就由调用方回落显示 id。
    """
    ws, sess, turns = {}, {}, {}
    try:
        rows = con.execute("SELECT kind, key, label, name FROM labels")
    except sqlite3.OperationalError:
        return ws, sess, turns
    for r in rows:
        k, key, label, name = r["kind"], r["key"], r["label"] or "", r["name"] or ""
        if k == "workspace":
            ws[key] = name
            if label:
                ws[label.rstrip("\\/").lower()] = name
        elif k == "session":
            sess[key] = name
        elif k == "turn":
            turns[key] = name
    return ws, sess, turns


def rebuild_day_recon(con: sqlite3.Connection) -> int:
    events, history, book_days, _snap = load_usage_file()
    ledger = load_ledger()
    ev_by_day, led_by_day = {}, {}
    for e in events:
        ev_by_day[e["day"]] = ev_by_day.get(e["day"], 0.0) + e["cost"]
    for d in ledger:
        led_by_day[d["day"]] = led_by_day.get(d["day"], 0.0) + d["cost"]
    scan = {r["day"]: (r["cost"], r["calls"]) for r in con.execute(
        "SELECT day, SUM(cost_est) cost, COUNT(*) calls FROM messages "
        "WHERE day<>'' AND keep=1 AND cost_est>=0 GROUP BY day")}
    # 原生搜索（旁路账）也进"会话折算"这一列：它同样是我们的花费，只是不写日志
    for r in con.execute("SELECT day, SUM(CASE WHEN cost_est>=0 THEN cost_est ELSE 0 END) c, "
                         "SUM(calls_n) n FROM search_calls WHERE day<>'' GROUP BY day"):
        c0, n0 = scan.get(r["day"], (0.0, 0))
        scan[r["day"]] = ((c0 or 0) + (r["c"] or 0), (n0 or 0) + (r["n"] or 0))
    # 压缩摘要（compaction/summary）也进这一列：它也是我们的花费，只是不写会话日志
    for r in con.execute("SELECT day, SUM(CASE WHEN cost_est>=0 THEN cost_est ELSE 0 END) c, "
                         "COUNT(*) n FROM compaction_events WHERE day<>'' GROUP BY day"):
        c0, n0 = scan.get(r["day"], (0.0, 0))
        scan[r["day"]] = ((c0 or 0) + (r["c"] or 0), (n0 or 0) + (r["n"] or 0))
    # 空转/重试（官方有 requests、我们没记录）：按小时补的次数和金额
    for r in con.execute("SELECT day, SUM(cost) c, SUM(n) n FROM lost_calls WHERE day<>'' GROUP BY day"):
        c0, n0 = scan.get(r["day"], (0.0, 0))
        scan[r["day"]] = ((c0 or 0) + (r["c"] or 0), (n0 or 0) + (r["n"] or 0))
    # 官方小时桶按天合计（备注要和表格里的「折算/官方」用同一个数）
    off_day = {r["day"]: (r["c"] or 0.0) for r in con.execute(
        "SELECT day, SUM(cost) c FROM official_hour WHERE api_key_name=? GROUP BY day",
        (resolve_key_name(con),))}
    days = sorted(set(list(book_days) + list(history) + list(ev_by_day) + list(led_by_day) + list(scan)))
    today = date.today().isoformat()
    rows = []
    for day in days:
        book = book_days.get(day)
        sc, calls = scan.get(day, (None, 0))
        off_c = off_day.get(day, 0.0)
        factor = (book / sc) if (book and sc and sc > 0) else None
        # 备注跟**表格里那一列**（折算/官方）用同一个口径，否则会出现"比率 100% 却写着偏差>15%"
        ratio = (sc / off_c) if (off_c > 0 and sc > 0) else None
        note = ""
        if not sc:
            note = "无会话记录"
        elif day >= today:
            note = "当日数据未结算（会话日志还在写）"
        elif off_c <= 0:
            note = "无官方数据"
        elif ratio is not None and abs(ratio - 1) > 0.15:
            note = "折算/官方 %.0f%%（差 %.0f 个百分点，查是否有未记录/多记的开销）" % (
                ratio * 100, abs(ratio - 1) * 100)
        rows.append((day, book, history.get(day), ev_by_day.get(day), led_by_day.get(day),
                     sc, calls, factor, note))
    con.executemany(
        "INSERT INTO day_recon (day,book_cost,history_cost,events_cost,ledger_cost,scan_cost,"
        "scan_calls,factor,note) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET book_cost=excluded.book_cost, "
        "history_cost=excluded.history_cost, events_cost=excluded.events_cost, "
        "ledger_cost=excluded.ledger_cost, scan_cost=excluded.scan_cost, "
        "scan_calls=excluded.scan_calls, factor=excluded.factor, note=excluded.note", rows)
    con.commit()
    return len(rows)


# ---------------------------------------------------------------- 区间解析
def parse_range(spec: str, con: sqlite3.Connection):
    """返回闭区间 (start_day, end_day)。支持 all/today/yesterday/7d/30d/90d/YYYY-MM-DD/YYYY-MM/YYYY-MM-DD..YYYY-MM-DD"""
    today = date.today()
    row = con.execute("SELECT MAX(day) d FROM (SELECT day FROM messages UNION "
                      "SELECT day FROM events UNION SELECT day FROM day_model)").fetchone()
    last = row["d"] if row and row["d"] else today.isoformat()
    s = (spec or "all").strip().lower()
    if s in ("all", ""):
        row = con.execute("SELECT MIN(day) d FROM (SELECT day FROM messages UNION "
                          "SELECT day FROM events UNION SELECT day FROM day_model)").fetchone()
        return (row["d"] or last, last)
    if s == "today":
        return (today.isoformat(), today.isoformat())
    if s == "yesterday":
        y = today - timedelta(days=1)
        return (y.isoformat(), y.isoformat())
    if s in ("week", "7d", "month", "30d", "90d", "year", "365d"):
        n = {"week": 7, "7d": 7, "month": 30, "30d": 30, "90d": 90,
             "year": 365, "365d": 365}[s]
        return ((today - timedelta(days=n - 1)).isoformat(), today.isoformat())
    m = re.fullmatch(r"(\d{1,3})d", s)          # 任意天数：3d / 14d / 100d
    if m:
        n = max(1, int(m.group(1)))
        return ((today - timedelta(days=n - 1)).isoformat(), today.isoformat())
    if ".." in s:
        a, b = s.split("..", 1)
        return (a or last, b or last)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return (s, s)
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return (s + "-01", s + "-31")
    raise SystemExit("无法识别的区间: %r（all/today/yesterday/7d/30d/90d/A..B）" % spec)


if __name__ == "__main__":
    con = connect()
    for t in ("sessions", "messages", "day_model", "events", "snapshots", "day_recon", "sync_log"):
        n = con.execute("SELECT COUNT(*) c FROM %s" % t).fetchone()["c"]
        print("%-12s %d 行" % (t, n))
    con.close()
