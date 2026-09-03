# Brainstorm: Fungi

## Problem

把 YESIR 的单机 Orchestrator 扩展成 LAN 多主机协作网络：一台主机发起（server），其余以 client 直连 server，client 之间的流量由 server relay。

每台主机跑一个 Fungi 进程，内含多个 Orchestrator 分身：每个通讯分身专职对接一台远端主机，另有一个本机分身专职与用户交互。

存储统一放 server 主机；通讯分身既能与对端通讯分身自主交流，又能向用户征求意见，两者用 Redis 协调。

## Context

- YESIR（`Harness/YESIR`）提供全套单机基座：Agent 主循环、TriLayer 编排、SSE LLM 客户端、工具面、session 存储、WebUI、ask_user 阻塞问答。纯 stdlib，ruff+pytest 门禁。
- Face（`Online/Face`）提供 LAN 房间参考：HTTP 控制通道（join/心跳/名册，pull 模型）+ UDP relay 按名册转发 + UDP 广播发现 + token 加入；但其 relay 面向音视频 UDP 分片，没有文本消息可靠性语义。
- 洞见 v2（`洞见/v2`）提供 PyQt6 托盘参考：运行时画图标、菜单、showMessage 通知、单实例 IPC。
- redis-py 是官方客户端；Redis 官方不支持 Windows，需 WSL / Memurai / Docker 承载；pytest 可用 fakeredis。

## Options

### Option A: 双平面 —— HTTP relay 消息面 + Redis 协调面（推荐）

消息面沿用 Face 的星型：clone 间消息以 JSON envelope 走 HTTP POST 到 server；收件人是 server 本机 clone 就直接投递，否则转发给目标 client（relay）。收端长轮询拉取，按消息 id 去重。

协调面：server 主机跑 Redis；同意征求（ask/answer）、presence、跨 clone 互斥走 Redis（stream + hash + pub/sub）。

Pros:
- 忠实满足两项既定需求（relay 功能 + Redis 协调），职责边界清晰。
- 消息 payload 不过 Redis，大文本/文件内容友好。
- transport 与 coordination 解耦，pytest 可分别 fake（fake roster / fakeredis）。

Cons:
- 要维护两套基础设施；server 重启时内存 inbox 丢未拉取消息（v1 明确容忍）。
- client 断线重连期间的消息缓存策略需要设计。

Risk: relay 转发与本地直投两条路径行为漂移 → 收敛到同一个投递函数消除。

### Option B: Redis 单总线（无自研 relay）

全部 clone 消息走 server 上 Redis Streams（每 host 一条 inbox stream），relay 语义由 Redis 天然承担，无需自研。

Pros: 单一基础设施；Streams 天然持久可 ack；实现代码最少。
Cons: 与「server 具备 relay 功能」的既定需求相悖；消息全部压在 Redis 进程上，payload 与运维耦合；所有安全只押 Redis 一个 auth。
Risk: 以后想换通道（长连接/gRPC）时 Redis 里的数据语义迁不走。

### Option C: P2P 全互联（否决）

各 clone 直接 TCP 互联，无中心。违反「client 只与 server 直连」的需求；N 台主机 O(N²) 连接、防火墙穿透难。仅记录否决原因。

## 分身（clone）模型

一台主机一个 Fungi 进程，内含 N+1 个 clone（N = 远端主机数）：N 个通讯 clone + 1 个本机 clone。

clone 是角色化的 YESIR L1 Orchestrator：

- 通讯 clone：工具面 = 消息工具（send_peer）+ 路径守卫版文件工具 + ask_consent / ask_user；专职对接一台远端主机的对位通讯 clone。
- 本机 clone：工具面 = YESIR 原生工具 + ask_user + delegate（把跨主机任务委派给对应通讯 clone）。

TriLayer 的 spawn（L2/L3）保留，白名单继承所在 clone 的文件限制。

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
