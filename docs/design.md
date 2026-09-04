# Design: Fungi

## 决策总览

| 决策点 | 结论 | 理由 |
|---|---|---|
| 消息面 | HTTP 星型 + server relay | 忠实需求；与 Face 模式同构，测试容易 |
| 协调面 | 无独立协调设施；ask 复用消息面 + 进程内 PendingAsk | 2026-09-03 评审去 Redis：LAN 无高并发，ask 是两跳消息，threading.Event 足够 |
| YESIR 代码 | fork-copy 进 `fungi/` 后改造 | YESIR 不发包，单仓库复制是最小依赖路径 |
| 进程模型 | 每 host 一个进程，多线程多 clone | 共享 hub 连接与托盘，最简 |
| clone 本体 | 角色化 L1 Orchestrator（复用 Agent） | L2/L3 spawn 机制照常工作 |
| 传输 | JSON over HTTP（非 UDP） | Orchestrator 消息需要可靠有序，Face 的 UDP 仅适合媒体分片 |
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
    comm.py                 # 通讯 clone
    local.py                # 本机 clone：WebUI 桥 + 本地 ask + 通知触发
    tools_comm.py           # send_peer / ask_consent / 守卫版文件工具
    delegate.py             # 本机 clone 的 delegate / peers 工具
  agent.py llm.py trilayer.py session.py events.py   # 移植自 YESIR（Sink 适配）
  tools/                    # 移植自 YESIR + 路径守卫包装
  tray.py                   # PyQt5 托盘：运行时画图标 + fluent 菜单 + showMessage 通知
  web/                      # YESIR web 移植 + consent 卡片
scripts/check.ps1  scripts/smoke_fungi.py
tests/
```

## 线程模型

- 主线程：Qt 事件循环（托盘 + 通知）。
- hub 线程：ThreadingHTTPServer（仅 server 角色）。
- 每 clone 一条 inbox 循环线程：收信 → 起 Agent 回合（per clone 串行，复用 YESIR `_session_lock` 思路）。
- 本机 clone 的 WebUI HTTP 线程：YESIR server 模式，`/chat /answer /stop` 语义保留。

Qt 与后台线程交互经信号桥（洞见 `_Bridge` 同构，queued 连接）。关闭顺序：托盘退出 → 停 inbox → 停 hub → flush → join 全部线程。

## 关键数据流

1. 对位自主交流：`alpha:comm-beta` ↔ `beta:comm-alpha` 经 server relay 互发 chat/task；`public/` 内文件直接读写，用户无感知。
2. consent：ask envelope → relay → 属主 host 的 local clone → Qt 通知 → WebUI 卡片 → answer envelope → 请求方 PendingAsk 唤醒（spec §5 状态机）。
3. 用户跨主机请求：用户 → 本机 clone → delegate → 通讯 clone（task envelope）→ 对位执行（可能再经 relay 协作）→ result → 本机 clone 转述。
4. 文件写入守卫：clone 工具 → HTTP → server store.resolve_path() 前缀校验 → `public/` 直写；`homes/` 非属主须附 consent_id（hub 查 pending-ask 注册表放行）→ 写入。

## 移植与改造清单（相对 YESIR）

- `server.py` 拆为 hub/app.py（房间 API）+ 本机 WebUI server（保留原语义）。
- `session.py` 存储改为经 server API（本机 clone 代理）；保留本地落盘开关作为单机降级模式。
- `tools/files.py` 等包一层路径守卫（同一实现，策略注入）。
- `tools/ask.py` 的 PendingAsk 抽成可复用组件：唤醒源既支持本进程 `/answer`（本机 clone），也支持 answer envelope（通讯 clone）。
- web/app.js 增 consent 卡片与通知唤起逻辑；改完 `node --check` 验证。

## 测试策略

- 单测：protocol 校验、路径守卫、relay 投递（fake roster）、pending-ask 注册表。
- 契约测试：FakeLLM 驱动 comm clone 回合（send_peer / ask_consent 工具调用序列）；ask→answer 全生命周期（进程内模拟两端）。
- E2E 冒烟：单机起 1 server + 2 client 进程（127.0.0.1 模拟三主机）+ FakeLLM，跑通自主交流与 consent 全链路；真实 LLM 冒烟沿用 ZAI_API_KEY 配方。
