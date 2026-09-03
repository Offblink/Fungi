# Spec: Fungi

> 定位：以 YESIR 为基座的 LAN 多主机 Orchestrator 协作网络。server 发起房间，client 直连 server，client 间流量由 server relay。存储统一在 server 主机。核心洞见：(1) 通讯 Orchestrator 之间自主交流仅限 `public/`，其他目录需征求同意；(2) 用户仅与本机 Orchestrator 交流，跨主机事务交由通讯 Orchestrator 处理。

## 1. 术语与实体

| 术语 | 定义 |
|---|---|
| host | 一台运行 Fungi 进程的主机，用户起名，房间内唯一 |
| server | 发起 LAN 的 host，承载 hub（HTTP relay + 存储 + Redis） |
| client | 直连 server 的 host |
| clone | host 进程内的一个 Orchestrator 分身 |
| 本机 clone（local） | 专职与用户交互的 clone，每 host 恰一个 |
| 通讯 clone（comm） | 专职与一台远端 host 的对位通讯 clone 交互的 clone，每个远端 host 一个 |
| 对位（counterpart） | host A 上对接 host B 的通讯 clone 与 host B 上对接 host A 的通讯 clone 互为对位 |
| `public/` | server 存储上的公共目录，通讯 clone 自由读写 |
| `homes/<host>/` | server 存储上各 host 的属地目录，非属主访问需 consent |

clone 编址 `host:role[-peer]`，如 `alpha:local`、`alpha:comm-beta`。

## 2. 拓扑与生命周期

- server 启动：生成/读取房间 token，起 hub HTTP 服务 + Redis 协调面 + 本机 clone；通讯 clone 按名册动态增删。
- client 加入：HTTP join（name + token）→ server 建名册项并回发 host 列表；此后心跳保活。
- clone 生成规则：每 host 维护「对端 host → 通讯 clone」映射；名册变化（新 host join / 心跳超时剔除）时增删通讯 clone，两端同步。
- 退出：client leave 或心跳超时被剔除；server 关停即房间解散。

## 3. 消息协议

JSON envelope，HTTP 承载：

```json
{"v": 1, "id": "uuid", "from": "alpha:comm-beta", "to": "beta:comm-alpha",
 "type": "chat", "ts": 1730000000, "reply_to": null, "body": {}}
```

- type：`chat`（对话）、`task`（goal/reply_format/context 委派）、`result`（task 回执）、`err`。
- 可靠性：server 为每 clone 维护内存 inbox，收端长轮询拉取后 ack；投递按消息 id 去重，语义 at-least-once。
- server 重启丢未拉取消息，v1 容忍，文档明示。

## 4. Server（hub）职责

端点（Face 风格，token 鉴权）：

- `POST /api/join` `{name, token}` → `{host_id, peers, fs_base}`；`POST /api/leave`
- `POST /api/heartbeat` → 顺带返回待办通知（pull 模型，与 Face 一致）
- `POST /api/send` envelope → 投递（本地直投或 relay 转发，同一函数）
- `GET /api/poll?after=<cursor>` → 长轮询 inbox
- 存储代理：`/api/fs/ls|read|write|edit|glob|grep`、`/api/sessions...`（YESIR session 语义），全部经路径守卫

存储布局（server `data/`）：`sessions/`（YESIR 兼容 JSON）、`public/`、`homes/<host>/`。Redis 仅存协调状态，不存文件。

## 5. Redis 协调面

| 结构 | 键 | 用途 |
|---|---|---|
| Hash | `fungi:hosts` | host_id → {addr, last_seen, clones} |
| Stream | `fungi:askq:<host>` | 发往该 host 用户的同意/提问请求 |
| Hash | `fungi:ask:<id>` | 单个 ask 的状态机：pending → answered/denied/timeout |
| Pub/Sub | `fungi:ask:<host>` | ask 实时唤醒（避免轮询） |
| String | `fungi:lock:<name>` | 跨 clone 互斥（文件写锁，SET NX PX） |

同意流状态机：

```
comm clone 调 ask_consent / ask_user
  → XADD fungi:askq:<target_host> {ask_id, from, action, path, reason}
  → PUBLISH fungi:ask:<target_host>
  → target host 进程弹系统通知 → 用户打开 WebUI → 卡片（允许 / 禁止 / 自定义输入）
  → POST /answer {ask_id, value} → HSET fungi:ask:<id> status=answered value=... → PUBLISH 唤醒
  → 请求方 clone 阻塞解除，拿到 "USER: <value>" / "DENIED"
```

- 超时默认 600s（通知场景用户可能不在电脑前，比 YESIR 的 300s 长，可配）；超时写 status=timeout。
- 断线补偿：本机 clone 重启时 XREAD 扫描 `fungi:askq:<host>` 未决项重建通知。

## 6. Clone 规格

### 6.1 通讯 clone

- 工具：`send_peer(text|task)`、`read_file/write_file/edit/glob/grep`（路径守卫版）、`ask_consent(host, action, path, reason)`、`ask_user(...)`、spawn。
- 路径守卫：`public/` 自由；`homes/<owner>/` 非属主需 consent（ask_consent 发往属主 host）；`homes/<own>/` 与自身会话目录需自身用户 consent；`sessions/` 拒绝。
- 自主交流：对位通讯 clone 之间 chat/task 自由往来，无需用户参与；涉及 `public/` 之外的文件操作才触发 consent。

### 6.2 本机 clone

- 工具：YESIR 原生全套（shell/web/ask_user…）+ `delegate(host, goal, reply_format)` + `peers()`。
- 用户仅与本机 clone 对话（核心洞见 2）；delegate 内部把 task envelope 发给对应通讯 clone 并阻塞等 result。

### 6.3 ask 汇聚

任一 clone 的提问/consent 都经 Redis 协调面落到目标 host 的本机 clone → 系统通知 → WebUI 卡片。本机 clone 自己的 ask_user 走 YESIR 原生 PendingAsk 机制（同进程直连，不过 Redis）。

## 7. WebUI 与托盘

- WebUI 默认关闭：进程启动即最小化到托盘；托盘菜单「打开 WebUI / 打开数据目录 / 退出」，双击托盘打开 WebUI。
- 有未决 ask 时弹系统通知（标题含来源 host 与摘要）；用户点通知或托盘 → 打开 WebUI。
- ask 卡片渲染于聊天流：允许 / 禁止 / 自定义输入框，对应 answer value `yes` / `no` / 自定义文本。
- 会话存储在 server；WebUI 经本机 clone 代理读写（对用户透明）。

## 8. 安全

- 房间 token：join 与所有 API 必带，错误 token 403。
- 路径守卫在 server 端强制（不只靠 clone 自觉）：resolve 后前缀校验，拒绝 `..` 与绝对路径逃逸。
- v1 不做传输加密（LAN 内网假设，明文 HTTP），文档明示。

## 9. 工程约束

- Python ≥ 3.13；标准库 + `redis`（redis-py）+ 托盘栈（待定，见 brainstorm）。
- pytest + fakeredis；真实 Redis 与真实 LLM 只做冒烟。
- ruff 全套（lint + format），`scripts/check.ps1` 门禁与 YESIR 相同。
- 一任务一 commit（英文祈使句）；push 需用户发话。
