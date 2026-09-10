# 架构与立项（Fungi）

> 2026-09-06 合并自原 `docs/brainstorm.md`、`docs/design.md`、`docs/plan.md`（内容原样保留，按 立项脑暴 → 设计定稿 → 阶段计划 顺序）。现状规格以 [`spec.md`](spec.md) 为准；本文档是历史决策记录。

# Brainstorm: Fungi

## Problem

把 YESIR 的单机 Agent 扩展成 LAN 多主机协作网络：一台主机发起（server），其余以 client 直连 server，client 之间的流量由 server relay。

每台主机跑一个 Fungi 进程，内含多个 Agent 分身：每个通讯分身专职对接一台远端主机，另有一个本机分身专职与用户交互。

存储统一放 server 主机；通讯分身既能与对端通讯分身自主交流，又能向用户征求意见，两者用 Redis 协调。

## Context

- YESIR（`Harness/YESIR`）提供全套单机基座：Agent 主循环、TriLayer 编排、SSE LLM 客户端、工具面、session 存储、WebUI、ask_user 阻塞问答。纯 stdlib，ruff+pytest 门禁。
- Face（`Online/Face`）提供 LAN 房间参考：HTTP 控制通道（join/心跳/名册，pull 模型）+ UDP relay 按名册转发 + UDP 广播发现 + token 加入；但其 relay 面向音视频 UDP 分片，没有文本消息可靠性语义。
- 洞见 v2（`洞见/v2`）提供 PyQt6 托盘参考：运行时画图标、菜单、showMessage 通知、单实例 IPC。
- redis-py 是官方客户端；Redis 官方不支持 Windows，需 WSL / Memurai / Docker 承载；pytest 可用 fakeredis。

## Options

### Option A: 双平面 —— HTTP relay 消息面 + Redis 协调面（推荐）

消息面沿用 Face 的星型：Agent 间消息以 JSON envelope 走 HTTP POST 到 server；收件人是 server 本机 Agent 就直接投递，否则转发给目标 client（relay）。收端长轮询拉取，按消息 id 去重。

协调面：server 主机跑 Redis；同意征求（ask/answer）、presence、跨 Agent 互斥走 Redis（stream + hash + pub/sub）。

Pros:
- 忠实满足两项既定需求（relay 功能 + Redis 协调），职责边界清晰。
- 消息 payload 不过 Redis，大文本/文件内容友好。
- transport 与 coordination 解耦，pytest 可分别 fake（fake roster / fakeredis）。

Cons:
- 要维护两套基础设施；server 重启时内存 inbox 丢未拉取消息（v1 明确容忍）。
- client 断线重连期间的消息缓存策略需要设计。

Risk: relay 转发与本地直投两条路径行为漂移 → 收敛到同一个投递函数消除。

### Option B: Redis 单总线（无自研 relay）

全部 Agent 消息走 server 上 Redis Streams（每 host 一条 inbox stream），relay 语义由 Redis 天然承担，无需自研。

Pros: 单一基础设施；Streams 天然持久可 ack；实现代码最少。
Cons: 与「server 具备 relay 功能」的既定需求相悖；消息全部压在 Redis 进程上，payload 与运维耦合；所有安全只押 Redis 一个 auth。
Risk: 以后想换通道（长连接/gRPC）时 Redis 里的数据语义迁不走。

### Option C: P2P 全互联（否决）

各 Agent 直接 TCP 互联，无中心。违反「client 只与 server 直连」的需求；N 台主机 O(N²) 连接、防火墙穿透难。仅记录否决原因。

## 分身（Agent）模型

一台主机一个 Fungi 进程，内含 N+1 个 Agent（N = 远端主机数）：N 个通讯 Agent + 1 个本机 Agent。

Agent 是角色化的 YESIR L1 Agent：

- 通讯 Agent：工具面 = 消息工具（send_peer）+ 路径守卫版文件工具 + ask_consent / ask_user；专职对接一台远端主机的对位通讯 Agent。
- 本机 Agent：工具面 = YESIR 原生工具 + ask_user + delegate（把跨主机任务委派给对应通讯 Agent）。

TriLayer 的 spawn（L2/L3）保留，白名单继承所在 Agent 的文件限制。

## 托盘与通知选型

- A: pystray + Pillow 托盘 + PowerShell WinRT Toast 通知（本机 PS 5.1 已验证可用）。轻，主线程只跑托盘循环。
- B: PyQt6 全家桶（洞见同款），托盘+通知一体。重，且 Qt 事件循环需与 HTTP server 多线程共存。
- C: 纯 stdlib ctypes 托盘。工作量大，否决。

## Redis on Windows

候选：WSL 内 redis-server（本机已有 WSL 免密 root）/ Memurai（Windows 原生服务）/ Docker Desktop。测试用 fakeredis 脱网跑。

## Self-Review

- 每个选项回应同一个问题（多主机协调架构），差异是架构性的：消息面落在哪。
- Option C 被需求直接否决，如实记录；A 与 B 的 cons 均为真实代价。
- 推荐 A 的依据是需求忠实度与可测试性，不是偏好。
- 真正的开放问题：Redis 承载方式、托盘依赖栈、consent 征求对象——留给 human review。

## 修订记录

- 2026-09-03 用户评审：去掉 Redis——LAN 无高并发需求，ask 本质是两跳消息，由消息面（relay）+ 进程内 PendingAsk（threading.Event）承载即可，见 spec §5；保留 relay 消息面（Option A 的消息面部分，协调面取消）。
- 托盘栈定 PyQt6（洞见同款，托盘+通知一体）。
- consent 裁决者：目录属主 host 的用户（homes/<owner>/ 非属主访问 → 属主用户裁决；本机属主操作 → 本机用户）。


---

# Design: Fungi

## 决策总览

| 决策点 | 结论 | 理由 |
|---|---|---|
| 消息面 | HTTP 星型 + server relay | 忠实需求；与 Face 模式同构，测试容易 |
| 协调面 | 无独立协调设施；ask 复用消息面 + 进程内 PendingAsk | 2026-09-03 评审去 Redis：LAN 无高并发，ask 是两跳消息，threading.Event 足够 |
| YESIR 代码 | fork-copy 进 `fungi/` 后改造 | YESIR 不发包，单仓库复制是最小依赖路径 |
| 进程模型 | 每 host 一个进程，多线程多 Agent | 共享 hub 连接与托盘，最简 |
| Agent 本体 | 角色化 L1 Agent（复用 Agent） | L2/L3 spawn 机制照常工作 |
| 传输 | JSON over HTTP（非 UDP） | Agent 消息需要可靠有序，Face 的 UDP 仅适合媒体分片 |
| 托盘 | PyQt5 + qfluentwidgets `SystemTrayMenu` | 2026-09-05 修订（原 PyQt6 定案废除）：全局 qfluentwidgets 是 PyQt5 build，GUI 与托盘统一到单一 Qt 绑定、同一套 fluent 组件，零新增依赖 |

## 包结构

```
pyproject.toml              # 元数据 + ruff 配置
fungi/
  __init__.py  __main__.py  # 入口：python -m fungi --server | --join <url> --token <t>
  config.py                 # role/name/token/server/ports/models（config.json > env）
  protocol.py               # envelope 校验/序列化
  hub/
    app.py                  # ThreadingHTTPServer + 房间路由 + pending-ask 注册表
    roster.py               # 名册 + 心跳剔除（Face Roster 同构）
    relay.py                # 投递函数：本地直投 / client 转发收敛于此
    store.py                # data/ 存储 API + 路径守卫 + 内存文件锁
  clone/
    base.py                 # inbox 循环 + Agent 装配 + PendingAsk 适配
    comm.py                 # 通讯 Agent
    local.py                # 本机 Agent：WebUI 桥 + 本地 ask + 通知触发
    tools_comm.py           # send_peer / ask_consent / 守卫版文件工具
    delegate.py             # 本机 Agent 的 delegate / peers 工具
  agent.py llm.py trilayer.py session.py events.py   # 移植自 YESIR（Sink 适配）
  tools/                    # 移植自 YESIR + 路径守卫包装
  tray.py                   # PyQt5 托盘：运行时画图标 + fluent 菜单 + showMessage 通知
  web/                      # YESIR web 移植 + consent 卡片
scripts/check.ps1
tests/
```

## 线程模型

- 主线程：Qt 事件循环（托盘 + 通知）。
- hub 线程：ThreadingHTTPServer（仅 server 角色）。
- 每 Agent 一条 inbox 循环线程：收信 → 起 Agent 回合（per Agent 串行，复用 YESIR `_session_lock` 思路）。
- 本机 Agent 的 WebUI HTTP 线程：YESIR server 模式，`/chat /answer /stop` 语义保留。

Qt 与后台线程交互经信号桥（洞见 `_Bridge` 同构，queued 连接）。关闭顺序：托盘退出 → 停 inbox → 停 hub → flush → join 全部线程。

## 关键数据流

1. 对位自主交流：`alpha:comm-beta` ↔ `beta:comm-alpha` 经 server relay 互发 chat/task；`public/` 内文件直接读写，用户无感知。
2. consent：ask envelope → relay → 属主 host 的 本机 Agent → Qt 通知 → WebUI 卡片 → answer envelope → 请求方 PendingAsk 唤醒（spec §5 状态机）。
3. 用户跨主机请求：用户 → 本机 Agent → delegate → 通讯 Agent（task envelope）→ 对位执行（可能再经 relay 协作）→ result → 本机 Agent 转述。
4. 文件写入守卫：Agent 工具 → HTTP → server store.resolve_path() 前缀校验 → `public/` 直写；`homes/` 非属主须附 consent_id（hub 查 pending-ask 注册表放行）→ 写入。

## 移植与改造清单（相对 YESIR）

- `server.py` 拆为 hub/app.py（房间 API）+ 本机 WebUI server（保留原语义）。
- `session.py` 存储改为经 server API（本机 Agent 代理）；保留本地落盘开关作为单机降级模式。
- `tools/files.py` 等包一层路径守卫（同一实现，策略注入）。
- `tools/ask.py` 的 PendingAsk 抽成可复用组件：唤醒源既支持本进程 `/answer`（本机 Agent），也支持 answer envelope（通讯 Agent）。
- web/app.js 增 consent 卡片与通知唤起逻辑；改完 `node --check` 验证。

## 测试策略

- 单测：protocol 校验、路径守卫、relay 投递（fake roster）、pending-ask 注册表。
- 契约测试：FakeLLM 驱动 通讯 Agent 回合（send_peer / ask_consent 工具调用序列）；ask→answer 全生命周期（进程内模拟两端）。
- E2E 冒烟：单机起 1 server + 2 client 进程（127.0.0.1 模拟三主机）+ FakeLLM，跑通自主交流与 consent 全链路；真实 LLM 冒烟沿用 ZAI_API_KEY 配方。


---

# Plan: Fungi

门禁：每阶段收尾 `scripts/check.ps1`（ruff --fix → format → 复检 → pytest）全绿后 commit（英文祈使句，本地，push 需用户发话）。

## Phase 0: Scaffold

- 建 `Harness/Fungi` 仓库：pyproject（ruff 配置照抄 YESIR）、.gitignore（config.json / data/ / sessions/）、`fungi/__init__.py`。
- 从 YESIR 移植 `agent.py llm.py trilayer.py session.py events.py config.py tools/`，原样跑通既有 pytest。
- 验收：`scripts/check.ps1` 绿；`python -m fungi "只回复两个字母: OK"` 单机降级模式可用。

## Phase 1: protocol + hub

- protocol.py envelope；hub/app.py join/heartbeat/send/poll/fs/session 端点；roster 心跳剔除；relay 投递函数；store 路径守卫与内存锁；pending-ask 注册表。
- 验收：单测（协议校验、守卫、relay 单元）；localhost 两进程 join 后互发 chat 的集成测试。

## Phase 2: ask 机制（PendingAsk 适配消息面）

- PendingAsk 抽成可复用组件：本进程 `/answer` 与 answer envelope 双唤醒源；ask/answer envelope 全生命周期（注册 → 阻塞 → 唤醒/超时）。
- 验收：单测覆盖超时/拒绝/自定义回答三分支；进程内模拟两端的 ask→answer 集成测试。

## Phase 3: Agents

- clone/base.py inbox 循环；comm.py（send_peer/ask_consent/守卫文件工具）；local.py（WebUI 桥 + delegate/peers + 本地 ask）。
- 验收：FakeLLM 契约测试——通讯 Agent 收 task → 回 result；consent 卡片 answer 唤醒阻塞工具。

## Phase 4: 本机集成（托盘/WebUI/通知）

- tray.py（PyQt5 + qfluentwidgets `SystemTrayMenu`，2026-09-05 托盘栈修订；通知即 `QSystemTrayIcon.showMessage`，未另建 notify.py）；WebUI 默认关、托盘唤起；web/app.js consent 卡片。
- 验收：进程启动仅托盘驻留；模拟 ask → 系统通知弹出 → WebUI 打开 → 卡片回答 → Agent 解除阻塞（DONGJIAN_SELFTEST 式自测钩子）。

## Phase 5: E2E

- localhost 三进程冒烟（单机 127.0.0.1 模拟三主机）：server + 2 clients，FakeLLM，跑通自主交流 + consent + delegate；曾以 scripts/smoke_fungi.py 落地，2026-09-07 已删除，链路由 pytest 契约测试覆盖。
- 真实 LLM 冒烟（ZAI_API_KEY 配方）；真机 LAN 手动测试清单（README 编辑需用户明示）。
- 验收：链路由 pytest 契约测试覆盖（scripts/smoke_fungi.py 已删除，仓库中已无该脚本）；真实 LLM 冒烟与手动清单留待用户执行。
