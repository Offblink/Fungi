# Fungi

LAN 多主机 Orchestrator 协作网络，构建于 [YESIR](https://github.com/Offblink/YESIR) TriLayer agent harness 之上。

一台机器一个进程，进程内跑多个 LLM Orchestrator 分身（clone）。不同主机的 Orchestrator 之间可以自主交流、协作完成任务；涉及对方地盘的文件操作时，由对方主机的人类用户通过系统通知 + WebUI 卡片裁决。**用户只与本机 Orchestrator 对话，跨主机事务由它转交通讯 clone 处理。**

## 核心设计

- **星型拓扑，server relay**：一台主机 `--server` 起房（承载 HTTP hub + 存储），其余 `--join` 直连；clone 间流量全部经 server 投递（at-least-once，消息 id 去重）。
- **两类 clone**：
  - 本机 clone（local）：每 host 恰一个，专职与用户交互，持原生全套工具 + `delegate(host, goal, reply_format)` 跨主机委派。
  - 通讯 clone（comm）：每远端 host 一个，与对位通讯 clone 自由 chat/task 往来，**不自动回信**——LLM 想回才显式调 `send_peer`；工具面只有守卫版文件工具 + `send_peer` / `confirm` / `inquire`，原生 bash/read/write 不下放。
- **同意流即消息**：无 Redis 等协调设施。ask 是普通 envelope，投到属主 host 的本机 clone → PyQt6 系统通知 → WebUI 卡片（允许 / 禁止 / 自定义输入 / 始终允许）→ answer envelope 唤醒阻塞中的请求方。断线重连后心跳重放未决通知。
- **文件空间**（存于 server `data/`）：

  | 目录 | 规则 |
  |---|---|
  | `public/` | 所有通讯 clone 自由读写 |
  | `homes/<host>/` | 属主 clone 写需自身用户同意；非属主读写都要属主用户同意 |
  | `sessions/` | 拒绝 clone 访问，仅经本机 clone 代理给用户 |

  路径守卫在 **server 端强制**（前缀校验，拒绝 `..` 与绝对路径逃逸），不依赖 clone 自觉。

```
          ┌─────────────── server (hub) ───────────────┐
          │  roster / relay / pending-asks / data store │
          └──────┬──────────────────┬──────────────────┘
                 │ HTTP             │
        ┌────────┴───────┐  ┌───────┴────────┐
        │ host A (local) │  │ host B (local) │   用户 ↔ 本机 clone
        │  └ comm-B      │  │  └ comm-A      │   comm-B ↔ comm-A 自主交流
        └────────────────┘  └────────────────┘   越界文件操作 → consent
```

## 快速开始

要求 Python ≥ 3.13。运行时唯一第三方依赖是 PyQt6（托盘/通知）；LLM 与 HTTP 均走标准库。开发另需 ruff + pytest。

```powershell
# 依赖
pip install PyQt6
pip install ruff pytest  # 仅开发

# 模型配置（config.json，同目录；不入库）
# { "api_key": "...", "endpoint": "https://api.z.ai/api/paas/v4/chat/completions", "model": "glm-5.3-flash" }

# 主机 A：起房（图形启动器 python start.py，或托盘模式直接：
python -m fungi --server [--name alpha] [--token T] [--port P] [--data DIR]

# 主机 B / C：加入（join 命令与真实 LAN IP 由 server 启动时打印）
python -m fungi --join http://<server-ip>:<port> --token <token> [--name beta]
```

server 启动后最小化到系统托盘；有未决同意请求时弹系统通知，点通知或托盘打开 WebUI（会话、聊天、ask 卡片均在其中）。

单机模式（无房间）仍可用：`python -m fungi --web`（WebUI）或 `python -m fungi "查询"`（命令行单发）。

## 验证

```powershell
# 全量门禁：ruff --fix → format → 复检 → pytest（186 passed）
powershell -File scripts/check.ps1

# 三进程冒烟（1 server + 2 client，FakeLLM，~18s；--real 走真实 LLM ~90s，--keep 留数据调试）
python scripts/smoke_fungi.py

# 自测钩子：托盘 + 通知 + 卡片应答 + 阻塞解除全链路，~7s 出 "FUNGI SELFTEST OK"
FUNGI_SELFTEST=1 python -m fungi --server --token x --data %TEMP%\fungi-selftest
```

## v1 已知边界

- LAN 内明文 HTTP，不做传输加密；房间 token 做鉴权。
- server 重启丢失未拉取消息与未决 ask（内存 inbox，v1 容忍）。
- 通讯 clone 的聊天历史在内存（上限 40 条），进程重启丢失；用户↔本机 clone 会话已持久化。
- ask 超时默认 600s，目前写死在代码，尚未暴露到 config.json。

## 文档

设计定稿见 [`docs/`](docs/)：[spec.md](docs/spec.md)（规格与术语）、[design.md](docs/design.md)（架构决策）、[brainstorm.md](docs/brainstorm.md)、[plan.md](docs/plan.md)。
