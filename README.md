# glm-auto-reset-card

> **GLM Coding Plan 额度守护**：5 小时（可选：每周）额度剩余不足时，自动使用最早过期的重置卡恢复额度。
>
> **⚠️ 请先读 [DISCLAIMER.md](DISCLAIMER.md)**：非官方工具，调用非公开接口，消耗重置卡**不可撤销**，风险自负。

## 背景

GLM Coding Plan 的 5 小时额度用完后要等下一个自然窗口才恢复；端内发放的"额度重置卡"可以立即回满，但必须手动点。如果你正在跑长任务、定时任务，额度恰好在深夜或跑批中途耗尽，干等很浪费。

本工具把这个动作自动化：**每轮检查剩余额度，低于阈值（默认剩余 ≤ 5%）就自动消耗一张对应类型的重置卡**（多张时选**最早过期**的，与官方"先用快过期的"规则一致，避免浪费）。

> 截至 2026-10，智谱尚未开放重置卡相关公开 API（参见 zai-org/feedback #854 的社区呼吁），本项目与 [glm-reset-card](https://github.com/crazykun/glm-reset-card)、[GLMeter](https://github.com/crazykun/GLMeter) 一样，复用的是网页端同源接口。

## 特点

- **单文件、零依赖**：纯 Python 标准库（3.8+），Windows / Linux / macOS 通吃。
- **默认 dry-run**：只报告"将消耗哪张卡"，不动真格。真实消耗需 `--execute` **加** 环境变量 `GLM_AUTO_RESET_CONFIRM=YES` 双重确认。
- **防连环烧卡**：进程锁防止多实例并发；成功用卡后有冷却期；"网络超时、结果未知"的用卡请求绝不自动重试，而是记录下来并暂停自动流程，等你用 `resolve` 确认（该卡到底消耗没有）后才恢复。
- **自带自测**：`selftest` 子命令在本地 mock 上跑通全部 16 个用例，不访问任何真实接口。

## 安装

```bash
git clone https://github.com/ZhenyuZhang123/glm-auto-reset-card.git
# 没有其他依赖，Python 3.8+ 即可
```

设置 API Key（控制台获取：国内 <https://open.bigmodel.cn>，国际站 <https://z.ai>）：

```bash
# Linux / macOS
export GLM_API_KEY="你的key"

# Windows PowerShell（持久化到用户环境变量）
setx GLM_API_KEY "你的key"
```

国际站用户另需 `export GLM_BASE_URL="https://api.z.ai"`。

## 使用

### 1. 先核对（必做，只读）

```bash
python glm_auto_reset_card.py check
```

输出类似：

```
额度窗口（假设 percentage=已用百分比，剩余=100-已用；请与端内显示核对）：
  five_hour 已用  97.0%  剩余   3.0%  下次自然重置 2026-10-20 02:37:25   <-- 低于阈值将触发
  week      已用  40.0%  剩余  60.0%  下次自然重置 2026-10-25 01:37:25
  重置卡[five_hour] recordId=100 可用  过期 2026-10-20 02:37:25
```

**把这里的百分比与官方端内显示逐项核对。** 接口返回的 `percentage` 按"已用百分比"理解（与 GLMeter / glm-reset-card 一致）。若与端内显示对不上，说明语义假设不成立，**请勿启用 execute**。

### 2. 试跑一轮（默认 dry-run，不消耗卡）

```bash
python glm_auto_reset_card.py run
# dry-run 下若触发条件满足，会打印将消耗哪张卡，并以退出码 2 结束
# （方便你在定时任务里对退出码做告警，但不真烧卡）
```

### 3. 启用真实消耗（双重确认）

```bash
export GLM_AUTO_RESET_CONFIRM=YES        # 没有这个变量，--execute 会被拒绝
python glm_auto_reset_card.py run --execute
```

### 4. 定时运行（真正实现"自动用"）

**Linux / macOS（cron）**，每 5 分钟守护一轮：

```cron
GLM_API_KEY=你的key
GLM_AUTO_RESET_CONFIRM=YES
*/5 * * * * /usr/bin/python3 /opt/glm-auto-reset-card/glm_auto_reset_card.py run --execute >> $HOME/.glm-auto-reset-card/cron.log 2>&1
```

**Windows（任务计划程序）**：先用 `setx` 设好两个环境变量，然后（管理员 PowerShell）：

```powershell
schtasks /Create /TN "glm-auto-reset-card" /SC MINUTE /MO 5 `
  /TR "python C:\tools\glm-auto-reset-card\glm_auto_reset_card.py run --execute" /F
```

**systemd timer**：见 [examples/](examples/)。

### 5. 异常恢复

某轮用卡请求超时、结果未知时，工具会**拒绝继续自动用卡**（防止同一张卡被重复消耗），此时：

```bash
python glm_auto_reset_card.py resolve   # 查卡列表确认那张卡到底消耗没有，并恢复自动流程
```

## 参数

| 参数 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `--api-key` | `GLM_API_KEY` | 必填 | 智谱 API Key（建议只用环境变量，避免出现在进程列表里） |
| `--base-url` | `GLM_BASE_URL` | `https://open.bigmodel.cn` | 国际站设为 `https://api.z.ai` |
| `--threshold` | — | `5` | 剩余百分比触发线（默认剩余 ≤ 5% 时用卡） |
| `--pool` | — | `five_hour` | 守护哪个池：`five_hour` / `week` / `both` |
| `--execute` | `GLM_AUTO_RESET_CONFIRM=YES` | 关 | 允许真实消耗（两个条件缺一不可） |
| `--cooldown-minutes` | — | `10` | 成功用卡后的冷却期 |
| `--state-dir` | — | `~/.glm-auto-reset-card` | 锁与用卡历史存放目录 |

子命令：`check`（只读核对）、`run`（守护一轮）、`resolve`（确认未知结果）、`selftest`（本地 mock 自测）。

退出码：`0` 正常；`1` 配置/API 错误；`2` dry-run 下本应烧卡（可用于告警）；`4` 存在结果未知的用卡请求（需 `resolve`）；`5` 已有实例在运行。

## 安全设计

- **双重确认**：`--execute` + `GLM_AUTO_RESET_CONFIRM=YES`，缺一不烧。
- **单实例锁**：状态目录下的锁文件（15 分钟视为残留）。
- **冷却期**：刚用完卡额度必然回满，若立刻又"低于阈值"多半是接口/语义异常，宁可不烧。
- **幂等请求**：每次消耗带唯一 `requestId`；结果未知时持久化记录、停止自动化，绝不盲目重试。
- **recordId 校验**：非纯数字的 recordId 直接跳过。

## 自测

```bash
python glm_auto_reset_card.py selftest
# 16/16 通过。全部基于本地 mock 服务器，不访问真实接口、不消耗任何卡。
```

覆盖：dry-run 不发请求、烧最早过期卡、冷却期不连烧、额度充足不触发、无卡不触发、网络结果未知进入 ambiguous 且不重试、resolve 恢复、缺确认环境变量时拒绝。

## 致谢

- [crazykun/glm-reset-card](https://github.com/crazykun/glm-reset-card) 与 [GLMeter](https://github.com/crazykun/GLMeter) —— 接口路径与字段语义的来源
- [Lemon-57/rs-quota-manager](https://github.com/Lemon-57/rs-quota-manager) —— "结果未知即停机待确认"的防重复消耗思路

## License

[MIT](LICENSE)（本仓库不构成任何形式的官方服务承诺，详见 [DISCLAIMER.md](DISCLAIMER.md)）
