# 给 AI 助手的接手说明（README-FOR-AI）

> 这份是**写给接手方（DSH 里的 AI 助手）看的**，比 README 更直接：拿到这个目录后照做即可。
> 人看 `README.md`，维护代码看 `doc/MAINTAIN.md`。

## 这是什么

`token统计/`：把本机 DSH 的会话日志 + 官方小时桶做**离线对账**，生成一份自带全部数据的
单文件看板（`dashboard.html`，双击就看，不需要起服务、不需要装插件）。纯 Python + SQLite。

## 三步跑起来

```powershell
$env:PYTHONIOENCODING="utf-8"
cd <这个目录>
python src\refresh.py            # 第一次：扫描会话日志 → 生成 dashboard.html（约 10~60 秒）
```

然后打开 `dashboard.html`。之后日常双击 **`刷新看板.bat`** 即可（增量，几秒）。

## 配置（都可以不做）

| 想干嘛 | 怎么做 |
|---|---|
| 让**官方对账**有数（推荐） | 把 platform.deepseek.com 的 userToken 写进 `config/usertoken.txt`（一行，见 `config/usertoken.txt.example`）。**没有也能用**：只出会话日志侧的数据，对账列显示 `-` |
| 官方对账认哪把 key | **自动**（默认取库里花钱最多的那把）；要指定就用 `python src\ds_usage.py --key-name <名字>` 或环境变量 `DSH_API_KEY_NAME` |
| 抓多少天官方数据 | `python src\ds_usage.py --days 30`（refresh 里默认 14 天） |
| 价目表不准 | 改 `config/prices.txt`（格式见文件头注释），`python src\prices.py` 可查看解析结果 |
| 全量重算（数字看着不对 / 改过统计逻辑） | 双击 **`全量刷新.bat`**，或 `python src\refresh.py --full`（会重扫所有日志 + 强制重抓官方数据） |
| 想要独立窗口而不是浏览器标签页 | 双击 **`看板窗口.bat`**（用 Edge/Chrome 的 `--app` 模式打开，没有就退回默认浏览器） |

## 改代码前

1. 读 `doc/MAINTAIN.md`（口径、去重规则、各种坑都在里面）；
2. 改完必跑自测：`python src\tests\run_tests.py`（退出码 0 = PASS；**Windows 上请在 pwsh/PowerShell 里跑**，
   git-bash 里起 node 会报 WinError 5）；
3. 改了去重/归属逻辑要再跑一次 `python src\scan_sessions.py --full --recon`。

## 绝对不要提交（`.gitignore` 已覆盖）

- `temp/`（里面是 `token_stats.db`：你所有会话的时间、token、花费）
- `dashboard.html`（数据全部内联在里面，4MB+）
- `config/usertoken.txt`（平台凭据）、`config/search-fix.txt`（个人补记）

## 已知边界

- 主要面向 Windows（`.bat` 入口）；其它平台直接 `python src/refresh.py` 也能跑；
- 官方小时桶要 token；没有就是"只统计会话日志"模式；
- 被**硬删**过的对话/轮次补不回来（日志没了），已删会话的历史行不会被全量刷新删掉；
- 空转/重试按"官方 requests − 我们数得出来的调用数"补次数和金额（见 MAINTAIN §2.6b）。
