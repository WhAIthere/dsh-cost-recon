# DESIGN —— token统计 内部设计

## 1. 目标与边界

要的是**分层下钻**：任意 1~3 个维度（天 / 小时 / 工作区 / 会话 / 轮次 / 模型 / 峰谷 / 主子）自由排列分组，
每行看 token 组成（未命中 / 输出 / 缓存命中）和花费（含峰谷）。

GUI 里已有 `dsh-usage-panel`（热力图、模型环形图、堆叠柱）和 `dsh-usage-chart`（实时指示器），
所以本工具**不复刻它们**，只做它们给不了的：下钻到会话/轮次、CSV 导出、离线 PNG、自建库。

## 2. 数据源（实测结论）

### 2.1 `~/.dsh/.dshw-usage.json`
```
date / lastBalance / todayUsage / dayStart
history   {day: 当日花费}
accounting{active:"<bookid>-CNY", books:{<bookid>:{days:{day:{openingUnits,lastUnits,
           debitUnits,creditUnits,...}}}}}
events    [{ts(ms), day, model, cost, tokens}]
```
- `debitUnits` / 1e8 = 当日实付，**权威口径**；units 最小 1e6 = 0.01 元，所以只精确到分。
- `history` 就是 `debitUnits` 的投影（实测逐日完全相等）。
- `events` 有逐次 cost，但**没有** input/output/cache 拆分。

### 2.2 `~/.dsh/dsh-usage/usage-ledger.json`
天×服务商×模型的 `inputTokens/outputTokens/cacheReadTokens/cacheWriteTokens/reasoningTokens/calls/cost`。
- 覆盖期与 2.1 相同；`cost` 比账本低约 6%，且当日可能还没收全（见过 ledger=8.78 而账本=9.80）。
- 只当天×模型明细用；旁边常见 `.tmp` 孤儿（原子写残留），不用管。

### 2.3 `~/.dsh/sessions/<ws>/<session>/session.vN.jsonl.zstd` ← 关键
- 一行一条 JSON 记录，`type` 有 `session` / `assistant/message` / `tool/call` / `turn/start` / …
- 用量在 `assistant/message` 的 `data.usage`：
  `{inputTokens, outputTokens, totalTokens, cacheReadTokens, reasoningTokens}`（**没有** cacheWrite）
- 会话元信息在首行 `type:"session"`：`id / createdAt / cwd / delegationDepth / agentPreset`
- 实测 269 个会话目录、28645 行、59.1 MB 压缩；python `zstandard` 全部解压成功（0 失败）。
- **文件版本**：`session.v3.jsonl.zstd` 与 `session.v4.jsonl.zstd`（升级到 v4 后新建的会写 v4）。
  实测 22 个目录两者并存：18 个内容完全相同、4 个 v4 是 v3 的超集，**没有一例 v4 更少**
  → 每个目录只读版本号最大的那份。

## 3. 三个致命坑（都踩过，按发现顺序）

### 坑 1：fork / resume 重放历史（重复计数）
同一批调用会以**新的 `message.id`** 复制进新会话文件；`message.id` 不同、token 却完全相同。
不去重时实测：09-23 那天 1890 行、444.8M token，而账本只有 134.5M token → 多算 3.2 倍，折算 28.45 元（应为 7.99 元）。
那天有 10+ 个会话互为 fork 分身（会话对之间重合 250+ 组）。

**去重规则**：键 = `(message_id, in, out, cache_read, cache_write)`；
全局按时间排序，**最早那次** `keep=1` 归属并计钱，其余同名同内容的标 `keep=0`（存着可查，不计）。
去重后逐日 token 与账本 ledger 精确吻合（09-23：138.8M vs 134.5M）。

⚠️ 中间踩过两个错：① 一开始用「全局指纹 + 文件 mtime」，结果**误合并 1742 条**（把不同会话里合法的同名记录也丢了）；
② 后来按目录名字母序处理，会让**后发生**的会话抢走归属（父会话名字靠后就吃亏）。正解是「先全部解析 → 全局按 ts 排序 → 最早者拥有」。

### 坑 2：`db.connect(path=DB_PATH)` 的默认参数
默认参数在**函数定义时**求值，所以之后改 `db.DB_PATH` 对 `db.connect()` 毫无影响。
表现：单测里所有扫描都写进了真实库，临时库永远 0 行。
正解：签名写 `path: str = None`，函数体内 `sqlite3.connect(path or DB_PATH)`。

### 坑 3：聚合忘了过滤去重
`rebuild_session_aggs()` 一开始把 `keep=0` 的行也 SUM 进 `sessions`，导致「会话维度」和「消息维度」对不上
（自测的 `agg.messages_eq_kept` 抓到：10 vs 8）。所有聚合都必须带 `AND keep=1`。

## 4. 库结构

```sql
sessions(session_id PK, workspace, cwd, depth, parent_id, agent_preset, created_at,
         first_ts, last_ts, messages, calls, in/out/cache_read/cache_write/reasoning,
         cost_est, peak_cost, offpeak_cost, turns, source_file, source_version,
         file_size, file_mtime, scanned_at)
messages(session_id, msg_id, seq PK(session_id,msg_id,seq), ts, day, hour,
         turn_no, step_no, model, provider, in/out/cache_read/cache_write/reasoning,
         total_tokens, peak, cost_est, keep, replayed)
day_model(day, provider, model PK, input, output, cache_read, cache_write, reasoning, calls, cost)
events(ts PK, day, file_day, model, tokens, cost, seen_at)
snapshots(captured_at PK, file_date, balance, today_usage, day_start)
day_recon(day PK, book_cost, history_cost, events_cost, ledger_cost, scan_cost,
          scan_calls, factor, note)
side_calls(day, kind PK, calls, tokens)
sync_log(session_id PK, file, file_size, file_mtime, version, last_scan)
meta(key PK, value)
```

- `keep=1` 是**唯一计入口径**；`keep=0` 只为留痕（能回答「这条为什么不算」）。
- `messages` 是原子行（一次模型调用）；`sessions` 是它的聚合（带 `keep=1` 过滤）。
- 增量靠 `sync_log` 的 `(file_size, file_mtime, version)`：三者都没变就跳过，实测增量 0.2~0.4 秒。

## 5. 归属与维度

| 维度 | 来源 |
|---|---|
| 天 / 小时 | `ts` 换算**本地时间**（本机=北京时间）；`day` 直接由 ts 算，不信文件里的 `day` 字段 |
| 工作区 | 目录名（如 `--C-Users-...-0-ds_harness_2--`）+ 首行的真实 `cwd` |
| 会话 | 目录名 = `session.id` |
| 轮次 | `assistant/message` 的 `data.turn`（会话内第几轮用户消息）、`data.step`（轮内第几步） |
| 主子 | `delegationDepth`；子代理的 `parent_id` = 同工作区里创建时间不晚于它的最近主会话（排除自己） |
| 峰谷 | 工作日 09:00-12:00、14:00-18:00 为 peak，价格 ×2 |

**无效组合自动退化**：`GROUP BY session, workspace` 里 workspace 由 session 唯一决定，内层只有一行，
表上就是「会话 → 空」，不会报错也不需要特判。这是选「动态 GROUP BY」而不是层级树的原因。

## 6. 钱怎么算

```
cost_est = (cache_read*0.02 + (in+cache_write)*1 + out*4)/1e6 × (peak?2:1)
```
价目来自 `~/.dsh/profiles/web/node_modules/@linxin666/dsh-usage/lib/index.js` 的 `FLASH_PRICE`。
**折算不是账单**：与账本逐日偏差实测 −2.5% ~ +14.5%（09-19/09-23 精确到 0，09-21 差 14.5%）。
`day_recon.factor = book_cost / scan_cost` 就是每天的校准系数；
要用「校准后花费」时乘它即可，token 不需要校准（是实测值）。

对账表（最近一次实测）：
```
日期        账本    events   ledger   会话折算   factor
2026-09-17  4.16   4.16    3.01     4.00      1.041
2026-09-18  7.09   7.09    6.62     6.64      1.068
2026-09-19  9.75   9.75    9.75     9.75      1.000
2026-09-20  5.10   5.10    5.10     5.10      1.001
2026-09-21  5.21   5.21    4.55     4.55      1.145   ← 差最多，疑有未记录的旁路调用
2026-09-22  2.91   2.91    2.99     2.99      0.975
2026-09-23  7.99   7.99    7.99     7.99      1.000
2026-09-24  11.19  11.19   9.52     9.76      1.171   ← 当日未结算
```

## 7. 旁路调用

`session/title-llm-request`、`web/deepseek-search-llm-request`、`compaction/summary` 等
也带 `usage`、也烧钱，但不写成 `assistant/message`，所以拿不到逐次成本。
`side_calls.py` 把它们按天×类型归到 `side_calls` 表，界面上应单独列「未归因」，不摊到任何会话。
实测目前只有 `compaction/summary` 3 次 / 1,376,364 token（09-24）。

## 8. 界面层（render_html.py）

**形态**：单文件 `dashboard.html`（工具根目录），数据内联，零外部请求、零第三方库（只用到 SVG 命名空间）。
双击即开；换维度/换区间全部在浏览器里算，不重新生成文件。

**数据内联格式**：为了控制体积，原子行用**列式**存——`{mcols:[列名...], mrows:[[值...],...]}`，
页面加载时还原成对象数组（3900 行 → 1.4 MB；列式比「每行一个对象」省约一半）。
JSON 里的 `<` `>` `&` 一律转成 `\u003c` 等形式，避免 `</script>` 提前截断文档。

**服务端算好的字段**：每行的 `cost_cache/cost_in/cost_out`（三种 token 各自贡献的钱，含 peak ×2 倍率）
——这样环形图的花费组成是**精确值**，不是「按平均倍率估」。
`daily`（每日聚合）也只在服务端按 `keep=1` 算一次。

**维度与指标**直接复用 `hier.DIMS` 的白名单，界面不做任何自己的口径判断。

**交互**：
- 三个下拉 = 1~3 级分组，重复选同一维度时后排自动清空；
- 点表格行 → 展开明细：把**剩下未选中的维度**各列前 6 名（等价于「自动再下钻一层」），
  并标注这组里有多少次是重放；
- 「含重放」勾选框才把 `keep=0` 的行算进来（默认不算）；「只算主会话」= `depth=0`。

**怎么验证一个 HTML 界面**（没浏览器也能验）：`render_html.check_browser()` 把内联 `<script>`
抠出来，在 node 里注入**假 DOM**（`getElementById` 返回带 `innerHTML` 的假元素）真跑一遍，
再读回 `#kpi` / `#rows` / 环形图 / 柱状图 / 对账表的 HTML，断言与库里口径一致。
自测里还模拟了「点行」「换维度」「切只算主会话」三种交互。

**踩坑**：
1. `src.replace("init();", "")` 会把 `function init(){...}` 里的 `init()` 也删掉，导致页面静默空白
   —— 必须用正则锚定末尾那次调用。
2. harness 里读 DOM 必须放在 `init()` **之后**，否则拿到空串（第一版就是这样，白高兴一场）。
3. `subprocess.run(text=True)` 在 Windows 上按 GBK 解码 node 输出，遇到中文就 UnicodeDecodeError
   —— 必须显式 `encoding="utf-8"`。

## 9. 验证方法

`python tests\run_tests.py` → 83 项，退出码 0 = PASS（明细写 `<工作区>/temp/token_stats_tests.txt`）。
自造样例覆盖：

- 定价与 peak 边界（12:00 收盘、18:00 收盘、周六不算 peak）
- 版本选择（v3/v4 挑新的、无版本文件返回 None）
- 跨会话重放去重（A2 fork A1，两条重放必须归 A1 且 `keep=0`）
- v4 超集只读一份、不重复
- 子代理 `parent_id` 正确、主会话 `parent_id` 为 None（防目标会话把自己当父）
- 增量：全跳过 → 改一个文件只重扫那一个 → 不产生重复行
- 聚合一致性：`SUM(sessions.messages) == COUNT(messages WHERE keep=1)`
- 对账表 `scan_cost` 只含 `keep=1`，且 `factor = book/scan`
- 分层查询：`day,workspace` 与 `workspace,day` 求和相等（换顺序不改总数）
- HTML：单文件无外链、`DATA` 已内联、blob 内无裸 `<`、**在 node 假 DOM 里真跑一遍**
  且首屏花费与库里 (keep=1) 合计一致、空区间不崩

真实数据交叉验证：`hier.py day,workspace` 与 `workspace,day` 必须给出同一组数字（只是顺序不同）。

## 9. 已知边界 / 没做的

- **拿不到逐次调用的 cacheWrite**（源数据里就没有）。
- 逐次 cost 只能折算，不能直接取实付（`events` 有实付但没有 token 拆分，两者对不上号）。
- 当日数据还在写：要么先关 DSH，要么接受「未结算」标记（`day_recon.note` 会自动标）。
- HTML 界面（透视表 + 下钻 + SVG 图）还没做；`hier.py` 的 `DIMS`/`METRICS` 白名单就是界面要复用的那层。
- `token_stats.py` 与 `db.py` 在区间解析/账本读取上有少量重复代码，暂时保留（旧功能不动）。
