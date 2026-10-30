#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ds_usage.py —— 抓 platform.deepseek.com 的**官方小时桶**（用量 + 费用）

为什么要有它：`events`（插件自己算的）和我们同源、余额差账本只有天级且混着同账户下
别的 API 的花费，都不能当"外部观察层"。官方页面能看到的**小时级 token/费用**才是基准
（端点与参数来自社区项目 dboycht/ds-usage-export 的调研：单日窗口 → `bucket=3600`）。

规矩（都是踩过的）：
- **只认一把 key**（默认自动：库里花钱最多的那把，可用 `--key-name` 指定）→ 别的 key（如 codex）的钱绝不混进来；
- **请求要慢**：一天两次请求（amount + cost），每次之间 sleep（默认 1.5 秒），
  429 指数退避并尊重 `Retry-After`；
- **只补缺的天**：已经抓过的天不重抓，最近 1 天默认重抓（当天的数据还在长）；
- **失败不阻塞看板**：抓不到就跳过，看板照旧出，只是这次没有官方对账数据。

token 从哪来：`token统计/usertoken.txt`（也支持 `--token-file` 或环境变量
`DS_USER_TOKEN`）。**它只在本机读取、绝不写进任何产物、绝不外传。**
取值方式：在 platform.deepseek.com 按 F12 → Console：
    copy(JSON.parse(localStorage.getItem('userToken')).value)

用法：
    python src\\ds_usage.py --check                 # 只验证登录态
    python src\\ds_usage.py --days 14               # 抓最近 14 天里"缺的"
    python src\\ds_usage.py --start 2026-09-17 --end 2026-09-28 --force
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db            # noqa: E402

BASE = "https://platform.deepseek.com/api/v0"
TZ_SEC = 8 * 3600                      # 本机 UTC+8（平台参数 tz 用秒）
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_TOKEN = os.path.join(ROOT, "config", "usertoken.txt")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36"


class FetchError(Exception):
    pass


# ---------------------------------------------------------------- 基础
def _ctx():
    """本机有 TLS 劫持：优先正常验证，失败再降级（只影响本机到官方的这条请求）。"""
    ctx = ssl.create_default_context()
    try:
        urllib.request.urlopen(urllib.request.Request(BASE + "/users/get_user_summary",
                                                      headers={"Accept": "application/json"}),
                               timeout=10, context=ctx)
        return ctx, True
    except urllib.error.HTTPError:
        return ctx, True                   # 能收到 HTTP 应答 = 证书没问题
    except Exception:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx, False


def read_token(path: str = None) -> str:
    p = path or os.environ.get("DS_USER_TOKEN_FILE") or DEFAULT_TOKEN
    if not os.path.exists(p):
        return os.environ.get("DS_USER_TOKEN", "").strip()
    raw = open(p, "rb").read().decode("utf-8-sig", "replace").strip()
    if raw.startswith("{"):
        try:
            j = json.loads(raw)
            for k in ("value", "token", "userToken"):
                if isinstance(j.get(k), str):
                    return j[k].strip()
        except Exception:
            pass
    return raw.splitlines()[0].strip() if raw else ""


def api_get(path: str, params: dict, token: str, ctx, retries: int = 4, quiet: bool = False):
    """带 429/5xx 退避的 GET；返回 biz_data。"""
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    delay = 2.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json",
            "x-app-version": "1.0.0", "Origin": "https://platform.deepseek.com",
            "Referer": "https://platform.deepseek.com/usage", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
            data = j.get("data") or {}
            if j.get("code") == 0 and data.get("biz_code") in (0, None):
                return data.get("biz_data")
            raise FetchError("业务失败 code=%s biz_code=%s msg=%s"
                             % (j.get("code"), data.get("biz_code"), str(data.get("biz_msg"))[:80]))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                ra = e.headers.get("Retry-After") if e.headers else None
                wait = float(ra) if (ra and str(ra).isdigit()) else delay
                if not quiet:
                    print("    %d → 等 %.0fs 重试（第 %d 次）" % (e.code, wait, attempt + 1))
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            if e.code in (401, 403):
                raise FetchError("登录态失效（HTTP %d）：去平台重新登录后更新 usertoken.txt" % e.code)
            raise FetchError("HTTP %d" % e.code)
        except FetchError:
            raise
        except Exception as e:
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise FetchError("%s: %s" % (type(e).__name__, str(e)[:120]))
    raise FetchError("重试都用完了")


def _ids_of(api_key) -> list:
    """series 里的 api_key 可能是字符串，也可能是 {"trackingId":..., "name":...} 这类对象。"""
    out = []
    if isinstance(api_key, str):
        out.append(api_key)
    elif isinstance(api_key, dict):
        for k in ("trackingId", "tracking_id", "id", "sensitiveId", "name"):
            if isinstance(api_key.get(k), str):
                out.append(api_key[k])
    return out


def _day_window(d: date):
    s = int(datetime(d.year, d.month, d.day, tzinfo=timezone(timedelta(seconds=TZ_SEC))).timestamp())
    return s, s + 86400


def api_keys(token, ctx):
    """→ [(id, name)]（平台上的 API Key 列表）"""
    bd = api_get("/users/get_api_keys", {}, token, ctx)
    items = bd if isinstance(bd, list) else (bd or {}).get("api_keys") or (bd or {}).get("items") or []
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        kid = it.get("trackingId") or it.get("tracking_id") or it.get("id") or ""
        out.append((str(kid), str(it.get("name") or "")))
    return out


def fetch_day(d: date, token, ctx, key_name: str = None, key_ids=None, quiet=False):
    """抓一天：amount（小时桶）+ cost → [(hour, key_id, key_name, model, hit, miss, out, req, cost)]"""
    s, e = _day_window(d)
    p = {"start": s, "end": e, "tz": TZ_SEC}
    amt = api_get("/usage/by_api_key/amount", p, token, ctx, quiet=quiet) or {}
    cost = api_get("/usage/by_api_key/cost", p, token, ctx, quiet=quiet) or {}
    want = set(key_ids or []) if key_ids else None

    def keep(ids):
        if not want:
            # 没有 key 列表时退回"名字里含 harness"这种宽松判断（宁可漏也别混进别人的钱）
            return any(key_name and key_name.lower() in (i or "").lower() for i in ids) if key_name else True
        return any(i in want for i in ids)

    # 费用先建索引：(key_id, model) -> hour -> cost
    cmap = {}
    for cur in (cost.get("data") or []):
        if str(cur.get("currency")) != "CNY":
            continue
        for ser in (cur.get("series") or []):
            ids = _ids_of(ser.get("api_key"))
            if not keep(ids):
                continue
            kid = ids[0] if ids else ""
            for b in (ser.get("buckets") or []):
                try:
                    hr = datetime.fromtimestamp(int(b.get("time"))).hour
                except Exception:
                    continue
                try:
                    v = float(b.get("cost") or 0)
                except Exception:
                    v = 0.0
                cmap.setdefault((kid, ser.get("model")), {})[hr] = cmap.setdefault(
                    (kid, ser.get("model")), {}).get(hr, 0.0) + v

    rows = []
    for ser in (amt.get("series") or []):
        ids = _ids_of(ser.get("api_key"))
        if not keep(ids):
            continue
        kid = ids[0] if ids else ""
        model = ser.get("model") or "unknown"
        for b in (ser.get("buckets") or []):
            u = b.get("usage") or {}
            hit = int(u.get("PROMPT_CACHE_HIT_TOKEN") or 0)
            miss = int(u.get("PROMPT_CACHE_MISS_TOKEN") or 0)
            out = int(u.get("RESPONSE_TOKEN") or 0)
            req = int(u.get("REQUEST") or 0)
            try:
                hr = datetime.fromtimestamp(int(b.get("time"))).hour
            except Exception:
                continue
            c = cmap.get((kid, model), {}).get(hr, 0.0)
            if not any((hit, miss, out, req, c)):
                continue                   # 空桶不存
            rows.append((hr, kid, key_name or "", model, hit, miss, out, req, c))
    return rows


def store_day(con, d: date, rows, note: str = ""):
    con.execute("DELETE FROM official_hour WHERE day=?", (d.isoformat(),))
    now = int(time.time())
    con.executemany(
        "INSERT INTO official_hour (day,hour,api_key_id,api_key_name,model,cache_hit,cache_miss,"
        "output,requests,cost,currency,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(d.isoformat(),) + tuple(r[:8]) + (r[8], "CNY", now) for r in rows])
    con.execute("INSERT INTO official_fetch (day,fetched_at,rows,note) VALUES (?,?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET fetched_at=excluded.fetched_at, "
                "rows=excluded.rows, note=excluded.note", (d.isoformat(), now, len(rows), note))
    con.commit()


def fetch_range(start: date, end: date, token, ctx, con, key_name="harness", key_ids=None,
                sleep=1.5, force=False, refetch_recent=1, max_age_min=3.0, quiet=False):
    """按天抓（**每请求间隔 sleep 秒**）。只补缺的天；最近 refetch_recent 天默认重抓，
    但刚抓过不到 max_age_min 分钟的天会跳过（连续点刷新不用每次都等网络）。"""
    done = {}
    for r in con.execute("SELECT day, rows FROM official_fetch"):
        done[r["day"]] = r["rows"]
    # 冷却：刚抓过的天先别重抓（不然连着点两次刷新，每次都要等 5 秒去拉同一份数据）
    fetched_at = {r["day"]: (r["fetched_at"] or 0)
                  for r in con.execute("SELECT day, fetched_at FROM official_fetch")}
    cutoff = time.time() - max_age_min * 60 if max_age_min > 0 else 0
    today = date.today()
    total_rows, fetched, skipped, failed = 0, 0, 0, 0
    d = start
    while d <= end:
        ds = d.isoformat()
        recent_day = (today - d).days <= refetch_recent
        need = (force or ds not in done
                or (recent_day and fetched_at.get(ds, 0) < cutoff))
        if not need:
            skipped += 1
            d += timedelta(days=1)
            continue
        try:
            rows = fetch_day(d, token, ctx, key_name=key_name, key_ids=key_ids, quiet=quiet)
            store_day(con, d, rows)
            total_rows += len(rows)
            fetched += 1
            if not quiet:
                c = sum(r[8] for r in rows)
                print("  %s ✓ %2d 行 · %6.3f 元%s" % (ds, len(rows), c,
                                                     "" if rows else "（该天无用量）"))
        except FetchError as e:
            failed += 1
            print("  %s ✗ %s" % (ds, e))
            if "登录态失效" in str(e):
                break
        d += timedelta(days=1)
        time.sleep(sleep)                  # ← 限流：每次请求之间都歇一下
    return {"fetched": fetched, "skipped": skipped, "failed": failed, "rows": total_rows}


def main(argv=None):
    ap = argparse.ArgumentParser(description="抓官方小时级用量/费用（只认一把 key）")
    ap.add_argument("--start", default="", help="起始日 YYYY-MM-DD")
    ap.add_argument("--end", default="", help="结束日 YYYY-MM-DD（含）")
    ap.add_argument("--days", type=int, default=0, help="最近 N 天（与 --start 二选一）")
    ap.add_argument("--token-file", default="", help="userToken 文件（默认 工具根目录/usertoken.txt）")
    ap.add_argument("--key-name", default="",
                    help="只抓这把 key（留空=自动：库里花钱最多的那把，否则 harness）")
    ap.add_argument("--sleep", type=float, default=1.5, help="每次请求之间的间隔秒")
    ap.add_argument("--force", action="store_true", help="连同已抓过的天一起重抓")
    ap.add_argument("--max-age-min", type=float, default=3.0,
                    help="最近 N 天里，刚抓过不到这么多分钟的天先跳过（0 = 不限）")
    ap.add_argument("--check", action="store_true", help="只验证登录态，不抓数据")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    token = read_token(a.token_file or None)
    if not token:
        print("没有 token：把 userToken 写进 %s（或 --token-file / 环境变量 DS_USER_TOKEN）"
              % DEFAULT_TOKEN)
        return 1
    ctx, verified = _ctx()
    if not verified and not a.quiet:
        print("提示：本机 TLS 证书验证过不去，已降级为不验证（只影响这台机器到官方这条请求）")

    con = db.connect()
    try:
        key_name = a.key_name or db.resolve_key_name(con)
        keys = api_keys(token, ctx)
        ids = [k for k, n in keys if key_name.lower() == (n or "").lower()]
        if not a.quiet:
            print("平台上的 key:", ", ".join("%s(%s)" % (n or "?", (k or "")[:8] + "…")
                                            for k, n in keys) or "（没取到）")
            print("锁定 key   : %s → %s" % (key_name, ("id " + ids[0][:8] + "…") if ids else "按名字匹配"))
        if not ids:
            print("!! 没找到叫 %s 的 key：将按名字做宽松匹配（仍只统计它的量）" % key_name)
        if a.check:
            print("登录态 OK")
            return 0

        if a.start:
            start = date.fromisoformat(a.start)
        else:
            n = a.days or 14
            start = date.today() - timedelta(days=n - 1)
        end = date.fromisoformat(a.end) if a.end else date.today()
        if not a.quiet:
            print("抓取区间   : %s ~ %s（每天 2 个请求，间隔 %.1fs）" % (start, end, a.sleep))
        r = fetch_range(start, end, token, ctx, con, key_name=key_name, key_ids=ids,
                        sleep=a.sleep, force=a.force, max_age_min=a.max_age_min, quiet=a.quiet)
        print("完成：抓 %d 天 / 跳过 %d 天 / 失败 %d 天，共 %d 行" %
              (r["fetched"], r["skipped"], r["failed"], r["rows"]))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
