# Fungi

LAN 多主机 Orchestrator 协作网络，构建于 [YESIR](https://github.com/Offblink/YESIR) TriLayer agent harness 之上——是 Psi → YESIR → Fungi 三代 harness 里的第三代。

一台机器一个进程，进程内跑多个 LLM Orchestrator 分身（clone）。不同主机的 Orchestrator 之间可以自主交流、协作完成任务；涉及对方地盘的文件操作时，由对方主机的人类用户通过 WebUI 卡片裁决。**用户只与本机 Orchestrator 对话，跨主机事务由它转交通讯 clone 处理。**

## 血缘：从 MnemeNet 到 Fungi

六个先行项目（五个定骨相，一个定动相），各有分工：

| | 它是什么 | 给 Fungi 留下了什么 |
|---|---|---|
| [MnemeNet](https://github.com/Offblink/MnemeNet) | 连接全体 AI Agent 的记忆网：个体记忆 + 技能沉淀 + 群体记忆（薪火相传） | **根理念——agent 不从零开始。** Fungi 的会话持久化、每主机技能沉淀（`SKILL.md` 结构与 MnemeNet 的 skills/ 同构）、断线重连重放未决卡片，都是这个理念在多主机网络里的工程化 |
| [Psi](https://github.com/Offblink/Psi) | PowerShell 单文件 harness（~900 行） | **哲学**——harness 的核心可以小到一个下午读完；零依赖传统；WebUI 视觉语言（配色、版式、模态框）的源头 |
| [YESIR](https://github.com/Offblink/YESIR) | AIOS 构思的第一块实体：TriLayer + Inquire + MCP | **骨架**——`agent.py / llm.py / trilayer.py / session.py / events.py / tools/` 直接移植；spawn 派发、MCP 客户端、分层模型、`Alt+R` 重试原样保留 |
| [Face](https://github.com/Offblink/Face) | 纯局域网 PySide6 视频聊天 | **产品形态**——房主创建 / 加入 / 心跳名册 / 自动发现 / 托盘驻留的房间体验；端口约定（8899 起向上扫描）；GUI 内置帮助页的样式 |
| [Gasp-Design](https://github.com/Offblink/Gasp-Design) | GSAP 3.12 + Three.js 的 37 个自包含动效组件 | **动效语言**——WebUI 动效层 `web/motion.js` 的编排母本：会话/好友列表 Flip 重排（`flip-drag-reorder` 技法）、consent 卡 3D 翻转落章、主题切换（`day-night-cycle`）、light-trail 指示条、floating-orbs 孢子落地（真菌身份梗）、number-counter。GSAP core + Flip 已 vendor 到 `web/vendor/`（LAN joiner 常无外网），`motion.js` 是唯一 GSAP 入口，删整文件即回退纯 CSS |

串起来读：**MnemeNet 给了为什么（agent 要延续、要沉淀），Psi 给了多小才够（一晚上读完），YESIR 给了骨架（TriLayer 编排），Face 给了房间长什么样，Around 提醒了别做什么（过度设计），Gasp-Design 给了这一切怎么动。** Fungi 把这些放进一张 LAN 网络：让每台主机上的 Orchestrator 拥有记忆、技能和彼此。

几条具体的继承：

- **WebUI 视觉语言一脉相承**：Psi 的 `agent.ps1` 内嵌前端奠定，YESIR 原样复用，Fungi 继续沿用并扩展（好友视图、暗色主题、ask 卡片）。
- **Fungi 回答了 YESIR 没回答的问题**：一个进程里的 Orchestrator 再强，也只在一台机器上。Fungi 把 Orchestrator 撒到 LAN 的每台主机上，让它们彼此成为工具。
- **Inquire 正名**：YESIR 的主动发问机制叫 Inquire，工具却叫 `ask_user`；Fungi 把工具名改回了 **`inquire`**，并新增 **`confirm`**（consent 卡：跨主机文件操作的允许/禁止裁决）——两者统一为 ask envelope 走同一条投递链。


## Fungi 新增了什么

- **星型拓扑，server relay**：一台主机 `--server` 起房（承载 HTTP hub + 存储），其余 `--join` 直连；clone 间流量全部经 server 投递（at-least-once，消息 id 去重）。
- **两类 clone**：
  - 本机 clone（local）：每 host 恰一个，专职与用户交互，持原生全套工具 + `delegate(host, goal, reply_format)` 跨主机委派。
  - 通讯 clone（comm）：每远端 host 一个，与对位通讯 clone 自由 chat/task 往来，**不自动回信**——LLM 想回才显式调 `send_peer`；工具面只有守卫版文件工具 + `send_peer` / `confirm` / `inquire`，原生 bash/read/write 不下放。
- **同意流即消息**：无 Redis 等协调设施。ask 是普通 envelope，投到属主 host 的本机 clone → WebUI 卡片（允许 / 禁止 / 自定义输入 / 始终允许）→ answer envelope 唤醒阻塞中的请求方。断线重连后心跳重放未决卡片。
- **文件空间**（存于 server `data/`）：

  | 目录 | 规则 |
  |---|---|
  | `public/` | 所有通讯 clone 自由读写 |
  | `homes/<host>/` | 属主 clone 写需自身用户同意；非属主读写都要属主用户同意 |
  | `sessions/` | 拒绝 clone 访问，仅经本机 clone 代理给用户 |

  路径守卫在 **server 端强制**（前缀校验，拒绝 `..` 与绝对路径逃逸），不依赖 clone 自觉。
- **send_file 传输**：字节流在 hub 暂存（store-and-forward），接收方用户 consent 后落到对方 `inbox/<来源主机>/`。
- **skill 系统**：每台主机的 clone 可沉淀可复用流程——`data/skills/<name>/SKILL.md` + 可选配套脚本，列表注入 system prompt，通讯 clone 只读。
- **GUI 启动器**：PyQt5 + qfluentwidgets 程序（`python start.py`）——发起/加入房间、打开 WebUI、模型配置、使用帮助，关窗转托盘后台房间不停。单实例：再次启动会唤起已运行的主界面。Token 支持自定义（字母/数字/-/_，1-64 位），发起前改即开房生效；运行中改完按回车（或移开焦点）即时热更新，已加入的好友需用新 Token 重新加入。

```
          ┌─────────────── server (hub) ───────────────┐
          │  roster / relay / pending-asks / data store │
          └──────┬──────────────────┬──────────────────┘
                 │ HTTP             │
        ┌────────┴───────┐  ┌───────┴────────┐
        │ host A (local) │  │ host B (local) │   用户 ↔ 本机 clone
        │  └ comm-B      │  │  └ comm-A      │   comm-B ↔ comm-A 自主交流
        └────────────────┘  └────────────────┘   越界文件操作 → confirm
```

## 数据目录口径

两套存储根按角色划分，**不是重复**，请勿合并：

| 目录 | 角色 | 内容 |
|---|---|---|
| `data/` | hub 共享存储（server 角色） | `public/`、`homes/`、`transfers/`、房间会话 `sessions/`、好友视图 `comm-sessions/` |
| 仓库根 | 本机 UI 数据（单机/加入方） | WebUI 会话 `sessions/`、收件箱 `inbox/`、`comm-sessions/` |

房间会话进 `data/` 是 server 角色的会话后端；本机会话放仓库根，因为 2026-09-04 真机
验证发现共享一个 `sessions/` 会让每台主机的 WebUI 列出所有主机的聊天（见
`ClientSessions` docstring）。两侧均被 `.gitignore` 覆盖，不入库。

## 快速开始

要求 Python ≥ 3.13。运行时第三方依赖仅 PyQt5 + PyQt-Fluent-Widgets（GUI 与托盘，同一套 fluent 风格）；LLM 与 HTTP 均走标准库。开发另需 ruff + pytest。

```powershell
# 依赖
pip install PyQt5 PyQt-Fluent-Widgets
pip install ruff pytest  # 仅开发

# 模型配置（config.json，同目录；不入库）
# { "api_key": "...", "endpoint": "https://api.z.ai/api/paas/v4/chat/completions", "model": "glm-5.3-flash" }

# 图形启动器（推荐）：发起 / 加入房间、WebUI、模型配置、帮助都在里面
python start.py

# 或纯托盘模式：
# 主机 A：起房
python -m fungi --server [--name alpha] [--token T] [--port P] [--data DIR]

# 主机 B / C：加入（join 命令与真实 LAN IP 由 server 启动时打印）
python -m fungi --join http://<server-ip>:<port> --token <token> [--name beta]
```

server 启动后最小化到系统托盘；未决同意请求以 WebUI 卡片呈现（顶部横幅 + 聊天流），点托盘打开 WebUI。

单机模式（无房间）仍可用：`python -m fungi --web`（WebUI）或 `python -m fungi "查询"`（命令行单发）。

## 验证

```powershell
# 全量门禁：ruff --fix → format → 复检 → pytest（266 passed）
powershell -File scripts/check.ps1

# 三进程冒烟（1 server + 2 client，FakeLLM，~18s；--real 走真实 LLM ~90s，--keep 留数据调试）
python scripts/smoke_fungi.py

# 自测钩子：托盘 + 卡片应答 + 阻塞解除全链路，~7s 出 "FUNGI SELFTEST OK"
FUNGI_SELFTEST=1 python -m fungi --server --token x --data %TEMP%\fungi-selftest
```

## CI / CD

GitHub Actions（windows-latest + Python 3.13）：

- **CI（`.github/workflows/ci.yml`）**：push main / PR 时跑 `pytest` 硬门禁（Qt 相关测试在无 Qt 的 runner 上优雅跳过）。ruff 只在本地跑（`scripts/check.ps1`），暂不进 CI。
- **Release（`.github/workflows/release.yml`）**：打 tag 触发（`git tag v0.1.0 && git push origin v0.1.0`）——先过同一 pytest 门禁，再 `git archive` 打源码 zip 并创建 GitHub Release（自动生成 release notes）。zip 只含 tracked 文件，`config.json`（真实 key）不入档。

## v1 已知边界

- LAN 内明文 HTTP，不做传输加密；房间 token 做鉴权。
- ask 超时默认 600s，目前写死在代码，尚未暴露到 config.json。

## 文档

设计定稿见 [`docs/`](docs/)：[spec.md](docs/spec.md)（规格与术语）、[design.md](docs/design.md)（架构决策）、[brainstorm.md](docs/brainstorm.md)、[plan.md](docs/plan.md)。
