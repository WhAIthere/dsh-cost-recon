# DSH Cost Recon · DSH 花费对账看板

DSH（DeepSeek Harness）的**花费对账 + 分层透视**看板：读本地会话日志与官方小时桶，把每次模型调用还原成
原子行，生成一个**自带全部数据的单文件 HTML**（不用起服务、不装插件、离线可看）。License: MIT。

大多数用量工具回答"我花了多少"；这个还回答"我怎么知道这个数是对的"，并让你按自己的维度切开看。

- **自由分层、每层独立排序**：在 {天, 小时, 周, 工作区, 会话, 轮次, 步, 模型, 峰谷, 主/子} 里任选 1~3 个当层级，
  每一层都能单独换排序 —— 横向比会话、一个会话按天、一天里按小时/按轮次……
- **对账而不是估算**：抓官方小时桶（requests / token / 金额），逐小时和会话日志折算值对比；
  对账表里的 `折算/官方` 100% 就是严丝合缝，对不上的残差单独列（备注写出差多少）。
- **工具调用也能看见**（只计数、不算钱）：那一步发起了哪些工具、几次、有没有失败 —— 用来解释“为什么下一步突然贵”。
- **把日志漏掉的钱补回去**：fork/resume 重放去重、中断空壳、空转/重试（官方有请求、日志里没有）、
  原生搜索账单、上下文压缩摘要 —— 都补进同一套按轮次的统计里。
- **峰谷倍率用官方实测反推**（不写死 ×2）；价目表 / 节假日 / 预设都是可手改的 txt。
- **离线优先**：产物是一个 HTML，数据全部内联，没网也能看。
- **下层很多也不卡**：树表分页渲染（一次 400 行，滚到底自动加载），1.6 万行的视图也能开。

## English

An offline **cost-reconciliation + pivot dashboard** for [DeepSeek Harness](https://www.deepseek.com/harness/).
It reads local session logs plus the official hourly usage buckets, rebuilds every model call as an atomic row,
and renders **one self-contained HTML file** — no server, no plugin, works offline. License: MIT.

- **Free hierarchical grouping with per-level sorting** — pick any 1–3 of {day, hour, week, workspace, session,
  turn, model, peak, main/sub}; each level sorts independently (compare sessions, one session day-by-day,
  one day hour-by-hour, …).
- **Reconciliation, not guessing** — official hourly buckets are compared against the log-derived cost every hour;
  the table shows `recon / official` coverage (100% = exact) and reports whatever does not match.
- **Missing spend is folded back in** — fork/resume de-duplication, interrupted shells, lost/retried requests,
  the native-search audit log, and context-compaction summaries.
- **Peak multiplier self-calibrates** from the official hourly cost instead of assuming x2.
- **Offline by design** — one HTML with all data inlined.

### Quick start

```
pip install zstandard
python src/refresh.py            # scan logs -> dashboard.html
python src/tests/run_tests.py    # self-check (no network)
```

Optional: put a platform `userToken` in `config/usertoken.txt` to enable the official reconciliation;
without it the dashboard still works from session logs alone. Windows users can double-click
`刷新看板.bat` instead.

---

# 完整文档（中文）

DSH 本机 token 用量 / 花费的分层统计工具。数据来自三处：DSH 自己写的**账本文件**（钱最准）、`~/.dsh/sessions` 里的**会话日志**（能下钻到会话/轮次，还能拆 token 组成），以及 `~/.dsh/search-usage.jsonl` —— **原生搜索（deepseek-official）的旁路账单**（这部分不写会话日志，只能从这里拿）。攒进本地 SQLite，然后随便切维度看。

**源文件只读，绝不写回 DSH 的任何东西。**

## 入口（只看这个）

| 想干什么 | 干什么 |
|---|---|
| **刷新数据 + 打开看板** | 双击 **`刷新看板.bat`**（先关掉 DSH，跑完自动打开看板；**只算增量，通常几秒**） |
| 只想看、不刷新 | 双击 `看板.html`（或 `dashboard.html`，同一份） |
| **数字看着不对 / 改过统计逻辑** | 双击 **`全量刷新.bat`**（所有日志重扫 + 官方数据强制重抓，1~2 分钟） |
| **想要独立窗口**（不是浏览器标签页） | 双击 **`看板窗口.bat`**（用 Edge/Chrome 的 `--app` 模式打开） |

`README-FOR-AI.md` 是**写给接手方 AI 助手看的**接手说明（配置、命令、禁忌）。

⚠️ 三个 `.bat` 双击时如果弹「打开文件 - 安全警告」，那是文件被打上了“低完整性”标签
（不是文件内容的问题）——右键属性 → 解除锁定，或在 PowerShell 里跑
`icacls 刷新看板.bat /setintegritylevel Medium` 就再也不弹了。

⚠️ `src/app.html` 是**页面模板**（没有数据，直接打开是空的），别点它。
`src/` 是源码，`config/` 是**可以手改的几个 txt**，`doc/` 是维护文档，`temp/` 是本地库与自测日志。

```
token统计/
├── 刷新看板.bat     ← 日常就点这个：刷新 + 自动打开
├── dashboard.html   ← 看板本体（数据内联，离线可用）
├── 看板.html        ← 中文名跳转页，点它跟点 dashboard.html 一样
├── README.md        ← 这份说明
├── config/          ← 给人改的配置（都是 txt，改完重新刷新即可）
│   ├── presets.txt    预设查看模式
│   ├── prices.txt     价目表（模型 × 生效时段 → 单价）
│   ├── holidays.txt   节假日表（只作对照，不参与算钱）
│   ├── search-fix.txt 搜索账单缺 sessionId 时人工补归属
│   └── usertoken.txt  platform 的 userToken（**别外传**）
├── doc/             ← 维护文档
│   ├── MAINTAIN.md    维护手册（改代码前必读）
│   ├── HANDOFF.md     交接快照（这轮改了什么、下一步）
│   └── 参考.md        原生搜索账单的对接说明（外部给的）
├── temp/            ← 本地库 token_stats.db、自测日志（自动生成）
└── src/             ← 源码 + 刷新入口 src/refresh.py
                        （db / scan_sessions / labels / hier / render_html / side_calls /
                          prices / holidays / holiday_check / subagent_names / token_stats /
                          app.html / app.js / tests/）
```

**为什么 `refresh.py` 是英文名**：`.bat` 正文里不能出现非 ASCII 字符——cmd 按系统 ANSI(GBK)
读批处理，而脚本里 `chcp 65001` 之后 cmd 对批处理正文的解码会错位，中文路径直接变乱码。
所以批处理正文全用 ASCII，中文提示交给 Python 打印（控制台已切 UTF-8，显示正常）；
看板本体也顺手改成 ASCII 名，另留一个中文名跳转页方便双击。

## 看板能干什么

- **时间范围能精确到分钟**：`日期 [日历] 时间 [时:分] ~ 日期 [日历] 时间 [时:分]`（时间、空间各占一行）
  - 默认就是 **`00:00` ~ `23:59`**，也就是整天；结束那分钟**整分钟都算进去**，
    所以填 `23:59` 等价于 `24:00`，不用去凑 24 点
  - 例：`09-29 20:00 ~ 09-29 23:00` 只看那三个小时；跨天的窗口也支持（`09-29 22:00 ~ 09-30 02:00`）
  - 点 `全部 / 近 7 天 / 最新一天` 会把分钟复位回 `00:00 / 23:59`；改日期不会冲掉你填的分钟
  - 注意：底部**按天的柱状图**和**对账表**仍是天粒度（数据本身就是按天算好的），
    分钟窗只作用于上面的树 / 树图 / 旭日图 / KPI
- **三个层级下拉**任选 1~3 个维度自由排列（含 `步`：轮次再往下拆到单次调用，标签带那一步的一小段摘要；换顺序就是换视角：`天→工作区` 或 `工作区→天`）
- **缩进树**：子节点只挂在父节点下一次；点行首 `▸/▼` 折叠展开，另有 `展开全部 / 折叠全部`
- **每层独立排序**：三个排序下拉**各自只负责一层**，下拉前面直接标着 第1层·工作区
  这样的字样（层级下拉改了会跟着变），互相不干扰：
  可选 `时间 ↑ / 时间 ↓ / 花费 ↓ / token ↓ / 调用数 ↓`
  - 时间排序按**结束时间**（最后一次活动）比，不是起点——否则「很早就开始、但你刚刚还在用」
    的会话会一直沉在底部；花费/token/调用数排序并列时，也用结束时间兜底
- 每行（从左到右按"常用程度"排）：
  `层级` → `花费` → `占父级` → `总占比` → `总 token` → **`token 占比`** → **`开销占比`**
  → `峰值` → `调用` → `起止时间` → **`搜索开销`** → **`重建参考`**
  - **token 占比**：三种 token 按**数量**分（可以理解为"体量构成"）
  - **开销占比**：三种 token 按**各自贡献的钱**分 —— 和上一条对照着看，就是"量大 ≠ 贵"
    （实测缓存命中占 96%+ 的 token，却只贡献 25~40% 的钱）
  - 柱子上直接写了百分比，太窄的段自动不显示；悬停任一柱可看具体数字
  - 具体 token 数（缓存命中/未命中/输出）挪到最右边、默认隐藏，勾选「显示具体 token 数」才出现
- **空转/重试也会自动记账**：官方有请求、日志里连一条消息都没有的调用（对面没回、进程被关），
  按小时把**次数和金额**补上，界面上显示成一行 `(未记录 · 空转)`（没有会话、没有 token 明细）。
  只认「次数对不上」的小时；次数对得上只是钱差一点的，仍然留在对账表里让你看见。
- **「旁路token」列正常是 `-`**：真正的旁路调用（title 生成等）才会计；压缩摘要已经单独
  记账，不在这列里（以前这里堆了几百万，其实是同一份摘要在多个分叉会话里各记了一遍）。
- **对账表最后一列是「折算/官方」**：`100.0%` = 我们统计到的和官方一样多；
  `90%` = 只统计到九成（少算）；`110%` = 多算了一成。没有官方数据的天显示 `-`。
- **表格不出横向滚动条**：层级/会话名和起止时间太长会显示省略号（悬停看全文），
  窗口窄时自动缩得更短；占比条、内边距也压过一轮。
- **压缩摘要节点**：上下文快满时 DSH 会压缩上下文，那次"写摘要"的模型调用也是一次完整调用、also 花钱，
  但它不写会话日志 → 单独记一笔，在会话里多出一个节点「**第 N 轮中 · 压缩摘要**」（前/中/后 按它跟那一轮消息的先后）。
  默认按时间排序，所以它就夹在两轮之间，不用额外找位置。
- **重建参考**：判断"这一轮是不是碰上了**缓存被清掉、整段上下文重新算了一遍**"。
  - 没嫌疑 → `—`；有嫌疑 → 显示**估算多花的钱**（就是"缓存还在的话能省多少"）
  - **宽判**（首调用未命中 ≥50%、上下文 ≥50k）= 黄色数字；**严判**（未命中 ≥70% 且下一条
    立刻回落 <10%）= 黄字 + **只把那一格标黄**（不标整行）；
    旁边的「重建参考列」勾选框可以把整列收起（默认显示，会记住）
  - **纯参考**：这笔钱本来就含在「花费」里，这一列只是拆出来看，不影响任何统计/筛选
  - 实测（到 2026-10-19）：1279 轮里 38 次，估算多花 **¥10.38**，约占全部花费的 6.5%
- **深色模式**：操作区最后一行勾「深色模式」→ 黑底白字（图表也跟着变）。
  勾选状态会被记住（和层级/排序那些一样）。**标红、标黄也跟着换一版**：浅色模式是"浅红/浅黄底 + 深色字"，
  深色模式换成"和黑底混出来的暗红/暗琥珀底 + 亮红/亮黄字"——一眼能认出来，又不会在黑底上晃眼。
- **起止时间**：起点 = 该节点里最早那条消息的**用户提问时间**，终点 = 最晚一条 **API 回复时间**；
  轮次就是它自己的提问~回复，会话/工作区/天则是其下所有轮次的最小~最大
- **点节点** → 下方显示该节点的花费组成 / token 组成 / 时间分布（图表指标可单独切换）
- 三种视图：缩进树表 / **树图 treemap** / **旭日图**
- 筛选：时间区间 + **工作区多选** + 关键字 + 只算主会话 + 含重放
- **标红**（只上色、不过滤）：操作区可设「花费 > __ 元」（默认 0.1）和「峰值 > __ %」（默认 50），
  超过阈值的行会变红，但筛选结果、行数、KPI 数字都不受影响；填 `0` 关闭对应那项
  - 勾「**只标红最下层**」后，只标当前屏幕上最深的那一层：没有下级的行，以及**下级被折叠**起来的行。
    例：`工作区 › 会话 › 轮次` 全展开时只标轮次；折叠某个会话，那个会话就可以被标；折叠工作区同理
- 底部：每日柱状（▲=余额账本口径）、对账表（含旁路未归因 token）

### 异常（中断）标黄 + 「含异常」开关

DSH 有时候会**循环思考罢工、被你中断**——那一步在日志里**有记录、没有 usage**（所以它既没 token
也没钱）。这类行会被单独标出来：

- 行底色**琥珀黄**、名字前一个 **⚠**，悬停能看到"几条中断、估算多少钱、当前计没计入"
- 父层（会话/工作区/天）也会带 ⚠，所以**折叠着也能看到哪棵子树里有异常**
- 勾上「**含异常（中断）**」才把它们放进统计，并把**按天差额估算**出来的金额/token 一起算进
  节点和 KPI；**不勾就和以前一模一样**（默认不勾）

估算怎么来的：**只认同一个小时** —— 拿**官方小时桶**（platform.deepseek.com 拉的，且只算
`harness` 那把 key）减去"我们数到的"，多出来的部分给那一小时里的中断：
能按**中断时已生成的正文长度**加权就加权，否则**均分**（悬停会写明是哪一种）；该小时对不上、
或那一小时没有中断，就**留空**。所以它**不会**为了把账做平而搬家：官方余额是**账号级**的
（含网页版、其他设备、同一个账户下别的 API 的消费），对不上的部分只会显示成
"另有 X 元官方开销对不上任何中断（未分摊）"。

**逐小时对账日志**在 `temp\official_recon.log`（每次刷新重写），长这样：

```
日期        小时        官方       我们      差额   中断     已分摊    补后残差  分配方式
2026-09-24  19h     1.6079     0.9106   +0.6972    2     0.6972    +0.0000  按正文长度加权（2 个）
```

同一次中断在 fork 兄弟会话里会有副本，工具按 `msg_id` 只算一次（副本不重复计钱）。
2026-09 实测：11 条空壳里 **6 次是唯一中断**，全部按小时对上、共恢复 **1.394 元 / 355 万 token**。

### 预设（第二行）+ 记住上次看的样子

操作区第二行有个「**预设**」下拉，它是一组**按钮**（选完自动弹回，不表示当前状态）：

| 选它 | 会发生什么 |
|---|---|
| `0. 上次状态（刚打开时）` | 一键回到刚打开页面时的样子（试完别的预设想退回来就用它） |
| `1. 今天 · 工作区›会话›轮次` | 层级三项、日期收到**数据最后一天**、三个排序都改成**时间↓**、花费>0.5 元标红（不标峰值）、只标最下层 |
| `2. 天›工作区›会话` | 层级三项、日期**全部**、三个排序都改成**花费↓**、花费>1 元 / 峰值>50% 标红、只标最下层 |
| `3. 工作区›会话›天` | 同上，花费>2 元 |

**这份清单在 `config/presets.txt` 里，可以直接用记事本改**（改完跑一次「刷新看板.bat」生效）：

```
名字 | 层级 | 日期 | 排序 | 花费标红(元) | 峰值标红(%) | 只标最下层(yes/no)
今天 · 工作区›会话›轮次 | workspace,session,turn | today | ts_desc | 0.5 | 0 | yes
```

- 某一格留空 = 那项不动；`#` 开头是注释；写错了只会跳过那一行并在界面上提示，不会弄坏看板
- 日期：`today` = 数据最后一天，`full` = 全部（最早一天 ~ 数据最后一天），留空 = 不动
- 排序：`ts` 时间↑ / `ts_desc` 时间↓ / `cost` 花费↓ / `tokens` token↓ / `calls` 调用数↓；
  写一个 = 三层都用它

**每档只改它写的那些选项**——空间勾选、关键字、"只算主会话"、含重放、视角标签这些不写就不动，
套用之后你仍然可以随便手改。

另外，**下次打开会继承上次的选择**（层级、排序、标红三件、只算主会话、含重放、视角、
显示具体 token 数、空间勾选）。**日期不记**，每次都是 `最早一天 ~ 数据最后一天`，
关键字和折叠状态也不记。预设右边会写一句"（已记住）"；如果你的浏览器不让本地文件记住
（少数环境会这样），它会降级写进地址栏；还是记不住就会提示"本浏览器不让记住"——功能照常，只是不记。

### 显示名（工作区 / 会话 / 轮次）

| 显示什么 | 从哪来 | 例子 |
|---|---|---|
| 工作区名 | `~/.dsh/storages/workspace.json` → `tables.workspaces[*].title` | `2-兴趣代码` |
| 会话名 | `~/.dsh/storages/session_projcache/sessions/<id>.json` → `record.rows.title.val` | `分析工作区D题目Q4` |
| 轮次摘要 | 同一文件的 `record.rows.turnOutline.val.turns[*].prompt`（你问的话，取前 14 字） | `第33轮 刚才出问题了，你重新来一下…` |

还有一类是**推导出来**的名字：Mnemon / DSH 会 spawn 各种一次性子代理，标题是它自己按注入指令
生成的英文句子（`Execute this remember request now`、`Bounded completed checkpoint (untrusted …`、
`Side: New thread`…），几十上百个重名，在树里根本分不出谁是谁。工具会顺手从子代理日志里
读出结构化信息，翻成中文：

| 原本的英文标题 | 显示成 | 出现过的数量 |
|---|---|---|
| `Execute this remember request now` | `记忆写入 → 兴趣代码`（→ 后面是记忆空间名） | 74 |
| `Bounded completed checkpoint (untrusted` | `空闲自检`（轮次：`空闲自检：事件 228`） | 102 |
| `Execute this forget request now` | `记忆删除 → 长期记忆`（轮次：`记忆删除：8bc81283`） | 18 |
| `Route this bounded MEMORY.md archive` | `记忆归档 第7/7批` | 7 |
| `Execute this create-memory-body request` | `新建记忆空间：数阵` | 5 |
| `Archive this managed document now.` | `文档归档：回顾 DSH 历史会话的最轻路径…` | 2 |
| `Execute this update-memory-body request` | `更新记忆空间：月露&墨雨猫` | 2 |
| `Side: New thread` | `侧线对话` | 2 |
| `Run local USER.md compaction now.` | `整理个人档案` | 1 |

想找「某条记忆是哪个子会话写的」，按空间名一眼就能扫到；想只看主会话就勾「只算主会话」，
或把 **主/子** 维度插进层级里。映射表在 `src/subagent_names.py`（`python src\subagent_names.py --list`
看目录，`--unmapped` 找还没收录的标签）。

**这些属于纯显示层**，单独存在 `labels` 表，**不参与任何统计口径**；取不到就回落显示 id。
改名后重跑一次显示就跟着变，而且**永远不会因为缺标题让数字对不上**——
自测里有一条专门钉这个：把标签全清空后，所有行的数字必须逐字不变。
鼠标悬停在行上能看到完整名字 + 原始 id。

## 用之前先关 DSH ⚠️

最新几分钟的会话日志还在写盘，**先关掉 DSH 再跑本工具**，拿到的才是完整的一份。只关 agent 侧就行，Web 界面留着看结果无所谓。
（工具本身跑在终端里跟 DSH 无关，所以也可以让我在 DSH 里帮你跑来看数字。）

跑之前建议在 PowerShell 里设一下编码，否则中文打印到管道可能报编码错：

```powershell
$env:PYTHONIOENCODING="utf-8"
cd <token统计 所在目录>
```

## 三条主线

### 0. 看界面（`dashboard.html`，双击即开；中文名 `看板.html` 是跳转页）

```powershell
python src\refresh.py                          # 一键：扫描 → 旁路统计 → 搜索账单 → 对账 → 生成看板
python src\render_html.py                      # 只重新生成看板（用现有库，不重新扫描）
python src\render_html.py --range 7d           # 只带最近 7 天（文件更小）
python src\render_html.py --csv temp\messages.csv   # 顺手导一份原子行 CSV（WPS 直接开）
```

- 单文件、离线、不联网、零依赖，数据内联在 HTML 里。页面能干的：

- **三个层级下拉**任选 1~3 个维度自由排列（换顺序就是换视角：`天→工作区` 或 `工作区→天`）
- **缩进树**：子节点只挂在父节点下一次，点行首 `▸/▼` **折叠展开**，`展开全部 / 折叠全部` 一键处理；
  会话超过 260 个时会默认把最细一级折起来，先看骨架
- **每层独立排序**：三个排序下拉依次对应第 1/2/3 层（旁边有提示写明哪层是哪个维度），
  可选 `时间 ↑ / 时间 ↓ / 花费 ↓ / token ↓ / 调用数 ↓`
- 每行：花费 + 占父级 + 总占比、token 三件套 + 组成条、调用数、峰值时段占比
- **点节点** → 下方显示该节点的花费组成 / token 组成 / 时间分布（最细一级也有）
- 三种视图：缩进树表 / **树图 treemap**（面积=指标，蓝条=天、橙条=工作区、绿叶=会话）/ **旭日图**
- 筛选：时间区间（全部 / 近 7 天 / 最新一天 / 自选）+ **工作区多选**（空间分区）+ 关键字
  （会话 id / 模型，逗号分隔）+ 只算主会话 + 含重放
- 底部：每日柱状（▲=余额账本口径）、对账表（含旁路未归因 token）

**完整刷新一条龙**（先关 DSH）——等价于 `src/refresh.py` 里跑的几步：

```powershell
cd src
python src\\scan_sessions.py --recon ; python src\\side_calls.py scan ; python src\\scan_sessions.py --recon
python src\\render_html.py --out ..\dashboard.html
```

改界面时：外壳在 `src/app.html`、脚本在 `src/app.js`（用 `<!--BODY-->` 和 `__DATA__` 占位），
`src/render_html.py` 只负责抽数 + 拼接 + 自检。`python src\\render_html.py --shot temp\shot.png` 可截图看效果
（要 Edge/Chrome，且在本机沙箱里需提权）。

### 1. 分层查询（`src/hier.py`，终端版，同一条逻辑）

任意 **1~3 个维度**动态分组，换个顺序就是换个视角：

```powershell
python src\\hier.py day,workspace                  # 每天 → 各工作区花多少
python src\\hier.py workspace,day                  # 每个工作区 → 每天花多少（同一份数据换个看法）
python src\\hier.py day,workspace,session          # 三级下钻
python src\\hier.py day,hour --range 2026-09-23    # 时间挂在天下面：按小时
python src\\hier.py peak --range all               # 高峰 / 非高峰各花多少
python src\\hier.py session --range 7d --metric tokens --top 20
python src\\hier.py workspace --main-only          # 只算主会话，排掉子代理
```

维度：`day` `hour` `week` `workspace` `session` `turn` `model` `peak` `depth`
指标：`cost`（默认）`cost_peak` `tokens` `in` `out` `cache` `calls`
区间：`all` / `today` / `yesterday` / `7d` / `30d` / `90d` / `2026-09-17..2026-09-23`
输出固定带 token 三件套（未命中 / 输出 / 缓存命中），所以每行都能看组成。

### 2. 账本侧统计（`src/token_stats.py`，旧功能保留）

```powershell
python src\\token_stats.py sync                 # 吸账本文件（幂等）
python src\\token_stats.py show 30d             # 终端总览 + ASCII 柱状
python src\\token_stats.py hours                # 按小时（标出 peak 段）
python src\\token_stats.py table daily|ledger|models
python src\\token_stats.py export out all       # CSV（daily / events / models）
python src\\token_stats.py chart out all        # 7 张 PNG（柱状 / 环形 / 热力图）
```

### 3. 数据层维护（`src/scan_sessions.py` / `src/side_calls.py`）

```powershell
python src\\scan_sessions.py            # 增量扫会话日志（没变的文件秒过）
python src\\scan_sessions.py --full     # 全量重扫（约 8 秒 / 59MB 压缩）
python src\\scan_sessions.py --recon    # 顺手刷新对账表
python src\\side_calls.py scan|show     # 旁路调用（压缩摘要等）的 token
python src\\db.py                       # 看各表行数
```

自测：`python src\tests\run_tests.py`（114 项，退出码 0 = PASS）。
其中包含**页面脚本的真运行验证**：把内联 JS 抠出来丢进 node 的假 DOM 跑一遍，核对首屏数字与库里口径一致，并检查点行下钻、换维度、只算主会话这些交互不炸。

## 数据从哪来

| 源 | 路径 | 给什么 |
|---|---|---|
| 事件账本 | `~/.dsh/.dshw-usage.json` | 逐次调用实付 cost、每日合计、余额、`accounting` 日账（**权威**，精确到分） |
| 用量明细 | `~/.dsh/dsh-usage/usage-ledger.json` | 按天×模型的 token 拆分（口径偏低，只当明细） |
| **会话日志** | `~/.dsh/sessions/<工作区>/<会话>/session.vN.jsonl.zstd` | **原子行**：每次模型调用的 token 三件套 + 时间 + `turn`/`step`；工作区与子代理层级 |
| 官方小时桶 | platform.deepseek.com（`src/ds_usage.py` 抓） | 逐小时「官方实际收了多少」——用来校验我们的高峰/非高峰判定（唯一外部观察层） |
| **原生搜索账单** | `~/.dsh/search-usage.jsonl`（dsh-search-audit 插件写） | 原生搜索（`deepseek-official`）每次调用的 token —— **不写会话日志、官方桶却照收**，不补就永远差一截 |

会话日志是唯一能下钻的源；搜索账单**借**它的轮次时间线归位。

## 口径（重要）

- **原子行 = 一次模型调用**（一条 `assistant/message` 的 usage）。子代理有自己的会话目录，天然独立成行。
- **fork / resume 会重放历史**：同一个 `message.id` 会在多个会话目录里出现。工具按「同一 id + 同样 token → 归最早那次」去重，重放行标 `keep=0` 存着但**不计钱、不计 token**。不去重的后果实测过：09-23 会从 7.99 元算成 44 元（那天有 10+ 个 fork 分身）。
- **一个会话目录里只读版本号最大的日志**（`session.v3` vs `v4`）：实测 v4 恒为 v3 的超集，读新的就够，这是去重的第一道闸。
- **钱有两个口径**：`cost_est` 是按刊例价（命中 0.02 / 未命中 1 / 输出 4 元每百万 token，工作日 9-12、14-18 点 ×2）折算，与账本偏差通常 <10%；`day_recon` 表里记着每天的账本值/折算值/校准系数，`factor` 就是「折算要乘多少才等于账本」。
- **高峰倍率以官方实测为准**：抓到官方小时桶后，拿官方的 token 分别按**谷价 / 峰价**各折一次，谁更接近官方金额就用谁（`official_peak` 表），并把 `messages.peak / cost_est` 校正过来——**实际算钱以这个校验结果为准**，没官方数据的小时保持原判定。查看：`python src\holiday_check.py`。
- **节假日表（`config/holidays.txt`）只作对照**：记录法定放假与调休上班日，用来核对"实测倍率跟官方节假日安排对不对得上"，**不参与实际开销计算**。文件里带有效期，超范围会提示更新。
- **价目表（`config/prices.txt`）**：按 **(模型, 生效时刻)** 查档 —— `号 | 模型 | 生效起止 | 峰谷 | 谷价 | 峰价 | 备注`，命中哪档就按哪档算。**查不到 = 未知价目**：金额记 `-1`（界面显示横杠 + 蓝色问号），**钱不计、token 照常统计**，KPI 会报"未知价目 N 次"。以后 DS 改价或换模型，改这个文件就行（用 `python src\prices.py` 看它读成了什么）。
- **旁路调用**（压缩摘要等，不写 `assistant/message`）进 `side_calls` 表，单独列为「未归因」，不硬摊到某个会话头上。
- **原生搜索（deepseek-official）单独记账、但默认不区分**：它每次都是完整的模型调用，token 只写在
  `~/.dsh/search-usage.jsonl` 里（官方小时桶是含它的）。工具把它按 `ts` 归到**所属轮次**，
  **和普通调用并进同一棵树、同一套 KPI**（所以"按轮次统计开销"的口径不变，不用多切一个维度）；
  KPI 里只多一行「其中原生搜索 N 次 · X 元」方便对照。**不要重复统计**：free-search 各引擎的
  调用是普通工具结果，token 早已记在会话日志的 `assistant/message` 里。
  账本里 `src:"estimate"` 的行是「N 次调用的聚合」估算（历史补账），按 N 摊到每次调用上。

## 输出

- 库：`token_stats.db`（`messages` / `sessions` / `search_calls` / `official_hour` / `official_peak` / `day_model` / `events` / `snapshots` / `day_recon` / `side_calls` / `sync_log` / `meta`）
- 图与 CSV：`temp/`
- 自测明细：`<工作区>/temp/token_stats_tests.txt`

## 依赖与边界

- anaconda python 3.11 + `matplotlib` / `zstandard`（本机已装，**不需要 pip 装任何东西**）。
- 本机数据起点是 **2026-09-17**（用户从这天开始用 DSH），没有更早的。
- 除会话日志外，其它源拿不到逐次调用的 `input/output/cache` 拆分。
- 界面（HTML 透视表）尚未做，目前是命令行；`src/hier.py` 的分组逻辑就是将来界面要调的那层。

细节、踩坑与验证方法见 `DESIGN.md`。
