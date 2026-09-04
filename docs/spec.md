# Spec: Fungi

> 定位：以 YESIR 为基座的 LAN 多主机 Orchestrator 协作网络。server 发起房间，client 直连 server，client 间流量由 server relay。存储统一在 server 主机。核心洞见：(1) 通讯 Orchestrator 之间自主交流仅限 `public/`，其他目录需征求同意；(2) 用户仅与本机 Orchestrator 交流，跨主机事务交由通讯 Orchestrator 处理。

> 2026-09-03 评审定案：无 Redis（见 brainstorm 修订记录）；托盘栈 PyQt6；consent 裁决者为目录属主 host 的用户。

## 1. 术语与实体

| 术语 | 定义 |
|---|---|
| host | 一台运行 Fungi 进程的主机，用户起名，房间内唯一 |
| server | 发起 LAN 的 host，承载 hub（HTTP relay + 存储） |
| client | 直连 server 的 host |
| clone | host 进程内的一个 Orchestrator 分身 |
| 本机 clone（local） | 专职与用户交互的 clone，每 host 恰一个 |
| 通讯 clone（comm） | 专职与一台远端 host 的对位通讯 clone 交互的 clone，每个远端 host 一个 |
| 对位（counterpart） | host A 上对接 host B 的通讯 clone 与 host B 上对接 host A 的通讯 clone 互为对位 |
| `public/` | server 存储上的公共目录，通讯 clone 自由读写 |
| `homes/<host>/` | server 存储上各 host 的属地目录，非属主访问需属主用户 consent |

## 2. 拓扑与生命周期

- server 启动：生成/读取房间 token，起 hub HTTP 服务 + 本机 clone；通讯 clone 按名册动态增删。
- client 加入：HTTP join（name + token）→ server 建名册项并回发 host 列表；此后心跳保活。
- clone 生成规则：每 host 维护「对端 host → 通讯 clone」映射；名册变化（新 host join / 心跳超时剔除）时增删通讯 clone，两端同步。
- 退出：client leave 或心跳超时被剔除；server 关停即房间解散。

## 3. 消息协议

JSON envelope，HTTP 承载：

```json
{"v": 1, "id": "uuid", "src": "alpha:comm-beta", "dst": "beta:comm-alpha",
 "type": "chat", "ts": 1730000000, "reply_to": null, "body": {}}
```

- type：`chat`（对话）、`task`（goal/reply_format/context 委派）、`result`（task 回执）、`ask`（同意/提问请求）、`answer`（对 ask 的回答，reply_to=ask_id）、`err`。
- 可靠性：server 为每 clone 维护内存 inbox，收端长轮询拉取后 ack；投递按消息 id 去重，语义 at-least-once。
- server 重启丢未拉取消息与未决 ask，v1 容忍，文档明示。

## 4. Server（hub）职责

端点（Face 风格，token 鉴权）：

- `POST /api/join` `{name, token}` → `{host_id, peers, fs_base}`；`POST /api/leave`
- `POST /api/heartbeat` → 顺带返回待办通知（pull 模型，与 Face 一致）
- `POST /api/send` envelope → 投递（本地直投或 relay 转发，同一函数）
- `GET /api/poll?after=<cursor>` → 长轮询 inbox
- 存储代理：`/api/fs/ls|read|write|edit|glob|grep`、`/api/sessions...`（YESIR session 语义），全部经路径守卫

hub 内存态：名册、各 clone inbox、pending-ask 注册表（ask_id → 投递状态，供 heartbeat 重放未决通知与去重）。跨 clone 文件写锁用 hub 内存锁（LAN 规模无需分布式锁）。

存储布局（server `data/`）：`sessions/`（YESIR 兼容 JSON）、`public/`、`homes/<host>/`。

> **会话归属修订（2026-09-04）**：会话必须按 host 隔离——server 角色存 hub store
> `data/sessions/`；client 角色存**本机** `sessions/`（YESIR 默认目录），不再经
> `/api/save` 落到对面操作的磁盘上。此前共享单目录导致任一方的 WebUI 会话列表
> 列出对方全部对话（真机回归发现，用户判定为严重隐私问题）。hub 的
> `/api/sessions` 仅供 server 角色自身使用；另加 `POST /api/transfer/upload`
> （raw 字节流式上传，token 查询串鉴权，413=超 max_file_mb），让用户面 clone
> 能发送**本机真实文件**（store 之外的路径）。

## 5. 同意流（消息面承载，无 Redis）

ask 是普通消息，不需要独立协调设施：

```
请求方 clone 调 ask_consent / ask_user
  → 发 ask envelope（to=目标 host:local）
  → PendingAsk 注册表登记，threading.Event 阻塞（复用 YESIR tools/ask.py 机制）
  → relay 投到目标 host 的本机 clone → 弹系统通知
  → 用户打开 WebUI → 卡片（允许 / 禁止 / 自定义输入）
  → 本机 clone 回 answer envelope（reply_to=ask_id，value=yes|no|自定义文本）
  → 请求方唤醒，返回 "USER: <value>" / "DENIED"
```

- 超时默认 600s（通知场景用户可能不在电脑前，比 YESIR 的 300s 长，可配）；超时返回 `"ERROR: 用户未回答"`。
- 断线补偿：本机 clone 心跳时从 hub pending-ask 注册表重放未决通知。
- 裁决者：ask 涉及 `homes/<owner>/` 时 to=属主 host 的 local clone；本机属主操作 to=本机 local clone（同进程直连，不走网络）。

## 6. Clone 规格

### 6.1 通讯 clone

- 工具：`send_peer(text|task)`、`read_file/write_file/edit/glob/grep`（路径守卫版）、`ask_consent(host, action, path, reason)`、`ask_user(...)`、spawn。
- 路径守卫：`public/` 自由；`homes/<owner>/` 非属主需 consent（ask_consent 发往属主 host 的 local clone）；`homes/<own>/` 与自身会话目录需自身用户 consent；`sessions/` 拒绝。
- 自主交流：对位通讯 clone 之间 chat/task 自由往来，无需用户参与；涉及 `public/` 之外的文件操作才触发 consent。

### 6.2 本机 clone

- 工具：YESIR 原生全套（shell/web/ask_user…）+ `delegate(host, goal, reply_format)` + `peers()`。
- 用户仅与本机 clone 对话（核心洞见 2）；delegate 内部把 task envelope 发给对应通讯 clone 并阻塞等 result。

### 6.3 ask 汇聚

所有 ask（含通讯 clone 的 ask_user / ask_consent）统一为 ask envelope 落到目标 host 的本机 clone → 系统通知 → WebUI 卡片。本机 clone 自己的 ask_user 是同一机制的同进程特例（直连 PendingAsk，不过网络）。

## 7. WebUI 与托盘

- WebUI 默认关闭：进程启动即最小化到托盘（PyQt6，洞见同款：运行时画图标、菜单、单实例）；托盘菜单「打开 WebUI / 打开数据目录 / 退出」，双击托盘打开 WebUI。
- 有未决 ask 时弹系统通知（PyQt6 showMessage，标题含来源 host 与摘要）；用户点通知或托盘 → 打开 WebUI。
- ask 卡片渲染于聊天流：允许 / 禁止 / 自定义输入框，对应 answer value `yes` / `no` / 自定义文本。
- 会话存储在 server；WebUI 经本机 clone 代理读写（对用户透明）。

## 8. 安全

- 房间 token：join 与所有 API 必带，错误 token 403。
- 路径守卫在 server 端强制（不只靠 clone 自觉）：resolve 后前缀校验，拒绝 `..` 与绝对路径逃逸。
- v1 不做传输加密（LAN 内网假设，明文 HTTP），文档明示。

## 9. 工程约束

- Python ≥ 3.13；标准库 + PyQt6（托盘/通知）。
- pytest；真实 LLM 只做冒烟（ZAI_API_KEY 配方）。
- ruff 全套（lint + format），`scripts/check.ps1` 门禁与 YESIR 相同。
- 一任务一 commit（英文祈使句）；push 需用户发话。

## 10. 增补（2026-09-03 定稿）：好友列表、会话旁观、文件传输

- **消息类型**：新增 `transfer`（文件传输元数据：id/name/size/reason/from）。
- **通讯会话落盘**：hub 投递成功后镜像 chat/task/result/transfer envelope 到
  `data/comm/<hostA>__<hostB>.jsonl`（按 host 名排序，双向同文件，单写者 = relay）。
  `Clone.history` 仍只作 LLM 上下文。
- **chat 回复兜底**：chat 回合若 LLM 未调用 send_peer 且最终文本非空，回合结束钩子自动补发
  （防止 LLM 忘调工具导致回复静默丢失，2026-09-03 真机实测发现）；显式调用过则不重复。
- **好友列表**：`GET /api/peers`（hub）→ 本机 clone 代理 `/peers` → WebUI 侧栏在线成员；
  点击进入只读会话视图（`GET /comm-log?host=` 渲染双方通讯 clone 对话流），无输入框
  （核心洞见 2 不破）。
- **文件传输（C2，落对端本地盘）**：字节面 store-and-forward——`POST /api/transfer`
  服务端从 store 复制暂存（上限 `max_file_mb`，config.json，默认 200），envelope 只传元数据；
  控制面复用 consent——接收方 comm clone 向属主本机 clone 发 ask（同意模式由滑块控制，见下），
  同意后经 `GET /api/transfer` 下载落盘 `<inbox_dir>/<来源host>/<文件名>`（config.json
  `inbox_dir`，默认 `<repo>/inbox`，重名加序号，basename 消毒）。落盘路径经 result envelope
  回执发送方。transfers 注册表在内存，server 重启丢失未拉取的暂存文件（v1 容忍，与 §3 一致）。
- **同意模式滑块（2026-09-04 修订）**：一次性「始终允许」废除——持久放行改为每个好友的
  可见可逆开关（WebUI 好友会话顶部滑块，左=允许，右=询问），存于 `~/.fungi/consent_rules.json`
  的 `modes`（host → allow|ask，默认 ask）；旧版 `always_allow` 地址列表自动迁移为 host 模式。
  判定键为 ask body 的 `from`（逻辑请求方）——传输回执的 envelope src 是接收方自己的
  comm clone，按 src 键控会错挂到自家 host。ask_user（通用提问）永不自动放行。
- **display-name 层（2026-09-04）**：wire 身份仍是 ASCII 安全的 host 名（envelope 地址、
  URL、文件名——主机名强校验的理由不变），昵称只走展示层。`Member.display` 随 join 携带、
  re-join 刷新（UI 改名无需重启）；`/api/peers` 与 join/heartbeat 的 `roster` 字段返回
  `[{name, display}]`；WebUI 侧栏/好友会话标题/旁观消息来源/通知标题显示昵称，无昵称回退
  显示 name。入口 `--display`，config.json `display` 可存；昵称做清洗（去控制字符、归一
  空白、截断 64 字符）但不受 ASCII 限制，中文/emoji 均可，且永不进入任何 wire 地址。

## 11. 增补（2026-09-04）：skill 系统

- **存储**：每 host 本地 `data/skills/<name>.md`（frontmatter `name`/`description` + markdown 正文；
  name 即文件名，kebab-case ≤64 字符，正文上限 32k）。每 host 一份，不随房间同步（v1）。
- **注入（每次初始化读列表）**：每个 agent 构建点（TriLayer `build_orchestrator` /
  `build_clone_agent` / `_run_task` 子代理）重新读盘，把「名称+描述」清单追加到 system
  prompt——本回合保存的 skill 下一回合即对全体 clone 可见。WebUI 已存 session 的 system
  消息保留原有内容，仅尾部托管 skills 段（去旧附新，见 `agent.run`）。
- **工具与元技能**：`skills` 工具（list/read/save）；`writing-skills` 元技能在首次访问时自动
  播种到目录，写明格式与质量标准（description 写触发条件、步骤给精确命令/路径、记录坑与验证法）。
- **安全**：save 仅限用户面 agent（本机 clone、WebUI 编排者）；通讯 clone 及其 spawn 只读——
  自主跨 host 代理不得在本 host 持久化 prompt 影响（与 §5/§8 的 consent 思路一致）。
