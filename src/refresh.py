#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""refresh.py —— 一键刷新入口（ASCII 文件名：批处理里不能出现中文，否则 cmd 会乱码）

做四件事：
  1. 增量扫描会话日志 → 本地库
  2. 统计旁路调用（压缩摘要等未归因 token）
  3. 刷新对账表
  4. 生成 看板.html（有数据、双击可看的那份）

改名前叫 看板.py，为了让 刷新看板.bat 能稳定调用才换成 ASCII 名。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # src/
ROOT = os.path.dirname(HERE)                                # token统计/（产物、配置都在这一层）
SRC = HERE
OUT = os.path.join(ROOT, "dashboard.html")                 # 真正的看板（ASCII 名，批处理好调用）
ALIAS = os.path.join(ROOT, "看板.html")                     # 中文名跳转页，方便双击

REDIRECT = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0; url=dashboard.html">
<title>DSH 消耗看板</title></head>
<body style="font:14px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;padding:24px">
正在打开看板……如果没有自动跳转，请点这里 →
<a href="dashboard.html">dashboard.html</a>
<p style="color:#6b7280">（这个文件只是跳转页；内容都在 dashboard.html，由 refresh.py 生成）</p>
</body></html>
"""


def run(args, desc):
    # flush=True：否则子进程输出会插到这些标题前面，看起来像乱序
    print("== %s ==" % desc, flush=True)
    p = subprocess.run([sys.executable] + args, cwd=SRC, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print("!! 失败（退出码 %d）" % p.returncode)
        sys.exit(p.returncode)


def run_optional(args, desc):
    """可以失败的一步：抓官方用量（没 token / 被限流 / 平台改版）都不该拖垮整个刷新"""
    print("== %s ==" % desc, flush=True)
    p = subprocess.run([sys.executable] + args, cwd=SRC, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print("（这一步没成功，跳过；看板照常出，只是这次没有官方对账数据）")
    return p.returncode == 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="刷新 token统计 看板")
    ap.add_argument("--full", action="store_true",
                    help="全量：所有会话日志重扫一遍 + 官方小时桶强制重抓（正常刷新是增量的）")
    a = ap.parse_args(argv)
    print("工作目录:", HERE, "　模式:", "全量" if a.full else "增量")
    # 一次扫描就够：以前先扫一遍、抓完官方用量再扫第二遍（同样的增量工作干两次）。
    # 现在的顺序：抓官方（网络，失败不阻塞）→ 打包扫描 + 对账（含峰谷校准/中断补齐）→ 旁路 → 出图
    ds_args = [os.path.join(SRC, "ds_usage.py"), "--days", "14"]
    scan_args = [os.path.join(SRC, "scan_sessions.py"), "--recon"]
    if a.full:
        # 全量：官方数据也强制重抓（有些天平台只返回了部分小时，不重抓会一直少算）
        ds_args += ["--force"]
        scan_args += ["--full"]
    run_optional(ds_args, "抓官方用量（小时桶%s）" % ("，强制重抓" if a.full else "，只补缺"))
    run(scan_args, "扫描会话日志 + 对账（%s）" % ("全量" if a.full else "增量"))
    run([os.path.join(SRC, "side_calls.py"), "scan"] + (["--full"] if a.full else []),
        "统计旁路调用" + ("（全量）" if a.full else ""))
    run([os.path.join(SRC, "render_html.py"), "--out", OUT], "生成看板 HTML")
    # 中文名跳转页（内容只有几十行，不占地方）
    with open(ALIAS, "w", encoding="utf-8") as f:
        f.write(REDIRECT)
    print()
    print("完成 → 双击打开：%s" % OUT)
    print("       （中文名 %s 是跳转页，点它也一样）" % os.path.basename(ALIAS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
