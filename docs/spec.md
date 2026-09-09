# Spec: Fungi

> 定位：以 YESIR 为基座的 LAN 多主机 Orchestrator 协作网络。server 发起房间，client 直连 server，client 间流量由 server relay。存储统一在 server 主机。核心洞见：(1) 通讯 Orchestrator 之间自主交流仅限 `public/`，其他目录需征求同意；(2) 用户仅与本机 Orchestrator 交流，跨主机事务交由通讯 Orchestrator 处理。

> 2026-09-03 评审定案：无 Redis（见 docs/architecture.md 的 Brainstorm 修订记录）；托盘栈 PyQt6；consent 裁决者为目录属主 host 的用户。

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
请求方 clone 调 confirm / inquire
  → 发 ask envelope（to=目标 host:local）
  → PendingAsk 注册表登记，threading.Event 阻塞（复用 YESIR tools/ask.py 机制）
  → relay 投到目标 host 的本机 clone → WebUI 卡片（asks 横幅，打开即见）
  → 用户打开 WebUI → 卡片（允许 / 禁止 / 自定义输入）
  → 本机 clone 回 answer envelope（reply_to=ask_id，value=yes|no|自定义文本）
  → 请求方唤醒，返回 "USER: <value>" / "DENIED"
```

- 超时默认 600s（用户可能不在电脑前，比 YESIR 的 300s 长，可配）；超时返回 `"ERROR: 用户未回答"`。
- 断线补偿：本机 clone 心跳时从 hub pending-ask 注册表重放未决卡片。
- 裁决者：ask 涉及 `homes/<owner>/` 时 to=属主 host 的 local clone；本机属主操作 to=本机 local clone（同进程直连，不走网络）。

## 6. Clone 规格

### 6.1 通讯 clone

- 工具：`send_peer(text|task)`、`read_file/write_file/edit/glob/grep`（路径守卫版）、`confirm(host, action, path, reason)`、`inquire(...)`、spawn、background（后台直跑命令，报告异步回传）。
- 路径守卫：`public/` 自由；`homes/<owner>/` 非属主需 consent（confirm 发往属主 host 的 local clone）；`homes/<own>/` 与自身会话目录需自身用户 consent；`sessions/` 拒绝。
- 自主交流：对位通讯 clone 之间 chat/task 自由往来，无需用户参与；涉及 `public/` 之外的文件操作才触发 consent。

### 6.2 本机 clone

- 工具：YESIR 原生全套（shell/web/inquire…）+ `delegate(host, goal, reply_format)` + `peers()`。
- 用户仅与本机 clone 对话（核心洞见 2）；delegate 内部把 task envelope 发给对应通讯 clone 并阻塞等 result。

### 6.3 ask 汇聚

所有 ask（含通讯 clone 的 inquire / confirm）统一为 ask envelope 落到目标 host 的本机 clone → WebUI 卡片。本机 clone 自己的 inquire 是同一机制的同进程特例（直连 PendingAsk，不过网络）。

## 7. WebUI 与托盘

- WebUI 默认关闭：进程启动即最小化到托盘（PyQt5 + qfluentwidgets，2026-09-05 起统一；
  运行时画图标、fluent 菜单、单实例）；托盘菜单「打开 WebUI / 打开数据目录 / 退出」，双击托盘打开 WebUI。
- 有未决 ask 时在 WebUI 顶部横幅展示卡片（asks banner）；用户点托盘 → 打开 WebUI。
- ask 卡片渲染于聊天流：允许 / 禁止 / 自定义输入框，对应 answer value `yes` / `no` / 自定义文本。
- 会话存储在 server；WebUI 经本机 clone 代理读写（对用户透明）。

## 8. 安全

- 房间 token：join 与所有 API 必带，错误 token 403。
- 路径守卫在 server 端强制（不只靠 clone 自觉）：resolve 后前缀校验，拒绝 `..` 与绝对路径逃逸。
- v1 不做传输加密（LAN 内网假设，明文 HTTP），文档明示。

## 9. 工程约束

- Python ≥ 3.13；标准库 + PyQt5 + PyQt-Fluent-Widgets（GUI 与托盘同一套绑定与组件）。
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
  comm clone，按 src 键控会错挂到自家 host。inquire（通用提问）永不自动放行。
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

## 12. 增补（2026-09-05）：托盘栈统一、房主 Token 自定义与热更新、CI/CD

- **托盘栈修订（推翻 2026-09-03 的 PyQt6 定案）**：全局 qfluentwidgets 是 PyQt5 build，
  GUI（gui.py）全程 PyQt5——托盘（`fungi/tray.py`）与 CLI 房间模式（`__main__.py`）、
  selftest 一并迁到 PyQt5，**全仓库单一 Qt 绑定**。托盘菜单用 qfluentwidgets
  `SystemTrayMenu`（与 GUI 同一套 fluent 组件；右键弹出，零新增依赖）。
  CLI 托盘行为不变（左键/双击开 WebUI）；GUI 托盘点击/双击回主界面、右键弹 fluent 菜单；
  另有**单实例 IPC** 兜底：GUI 监听 QLocalServer `FungiGuiIPC`，二次启动经 QLocalSocket
  发 `show`，运行实例 `show_and_raise` 后第二实例退出（洞见 v2 同款）。
- **房主 Token 自定义**：GUI「发起房间」页 Token 行是可编辑输入项（位于发起按钮上方，
  预填自动生成值）。字符集 `[A-Za-z0-9_-]`、1-64 位——token 进 URL 与加入命令，空格/中文非法。
  发起前修改：开房即用该值；留空则自动生成。
- **Token 运行中热更新**：房间运行中改完 Token 按回车（或移开焦点，`editingFinished`）
  即改写 `hub.token`；所有 API 鉴权逐请求读它，无需重启房间。语义：旧 token 立即失效
  （403）——**已加入的好友需用新 Token 重新加入**；UI 以 InfoBar 提示。CLI `--token`
  语义不变（仅开房时定值）。
- **CI/CD**：`.github/workflows/ci.yml`（push main / PR）与 `release.yml`（tag `v*`）均以
  pytest 为门禁（windows-latest + py3.13，Qt 测试在 runner 上 importorskip 跳过）；
  release 追加 `git archive` 源码 zip + GitHub Release（generate_release_notes）。
  ruff 仍是本地门禁（`scripts/check.ps1`），暂不进 CI——版本漂移待统一后钉版。

## 13. 增补（2026-09-06）：文件空间全景、会话目录统一、视频理解与移动端打磨

### 文件空间全景

fs 守卫仍是白名单三分区（`public/` 自由、`homes/<host>/` 属主、其余一律拒绝），
但 `data/` 的实际布局早已不止三个目录——**每台主机一份自己的 `data/`**，server
主机的那份额外承载共享空间：

| 目录（每主机 `<repo>/data/`） | 内容 | 写者 |
|---|---|---|
| `sessions/` | 本机 WebUI 会话（每会话一个 JSON，逐回合落盘） | 各自主机——server 角色=hub store 后端；client/单机=本地落盘，**绝不代理进 hub store**（2026-09-04 真机教训：共享一个 sessions/ 会让每台主机的 WebUI 列出所有主机的聊天） |
| `comm-sessions/` | 好友视图：通讯 clone 的会话式转录（与 sessions 同构） | room 进程 |
| `comm/`（仅 server） | clone 间信封流量镜像，`发送方__接收方.jsonl` | hub，每次投递一条 |
| `public/`、`homes/<host>/` | clone 文件空间（守卫白名单内） | clone |
| `transfers/` | send_file 暂存（store-and-forward，取走即删） | hub |
| `skills/<name>/` | 每主机技能沉淀（SKILL.md + 脚本） | 仅用户面 agent；通讯 clone 只读 |

仓库根另有 `inbox/`（send_file 收件，`<来源主机>/` 子目录）与 `config.json`（模型、
展示昵称）；用户级配置在 `~/.fungi/`（`webui_token`=WebUI 门禁、
`consent_rules.json`=好友同意模式开关）。以上全部不入库。

**会话目录统一**：单机模式原本写仓库根 `sessions/`，与房间模式的 `data/sessions/`
双轨——同一台机器的聊天史裂成两处。现 `session.py SESSIONS_DIR` 统一指向
`data/sessions/`，根目录旧会话已迁移，目录删除。

### 视频理解（`video` 工具）

- 零配置发现旁置的 VidSense checkout（config 显式 > `<安装根>/Skill/VidSense` 兄弟目录
  > 桌面备用路径）；子进程跑 VidSense **原生本地管线**（`--no-api`：ffmpeg/ffprobe 抽取、
  faster-whisper 转写、CLIP 镜头切分），Fungi 读取事件卡 JSON 后自己用 ffmpeg 按时间戳
  重抽关键帧——理解交给 Fungi 自己的视觉模型，无第二 API key，VidSense 仓库零改动。
- 可靠性细节：子进程 env 注入 `HF_ENDPOINT=hf-mirror.com`（防 GFW 下 transformers
  HEAD 校验卡死）；路径含 CJK 时先复制成 ASCII 临时副本再喂管线（本机 ffmpeg 解码
  中文路径输入会失败）；相对路径按 Fungi cwd 解析；错误路径透传子进程 stderr。

### 移动端打磨（真机 X5 反馈闭环）

- **右划抽屉任意位置可起**：48px 边缘 wedge 从未在真机触发且与系统边缘手势冲突，废除；
  touchstart 无卫语句（X5 吞手势后 drag 卡 truthy 的教训）；touchmove 垂直锁隔离滚动。
- **横向滚动优先**：touchstart 命中横向可滚动元素（长工具输出 `<pre>`、agent tray）时不启动
  抽屉拖动，pan-x 交还给内容本身（touch-action 沿祖先链取交集，`#chat-wrap/#messages`
  需 `pan-x pan-y`）。
- **空输入即重试**：发送键图标随输入框状态切换（↻ 重试 / ↑ 发送 / ■ 停止）；重试走桌面
  Alt+R 同一契约（POST /retry，server 剥离失败回合的合成尾）。
- **状态栏**：tool 事件显示 `⚙ <工具名>…`，tool_result 复位 Thinking——长工具不再挂着
  陈旧的 "Writing..."。
- **停止丢卡片修复**：流未收到 done 就终结（硬中断/断网）时，`recoverAfterDrop` 先按磁盘
  reconcile 再视情重连接续，live 卡片不再在下次渲染时凭空消失（桌面 app.js 同步）。


## 12. 增补（2026-09-09）：amail 文字邮件 + 信使开关

- **amail**：comm clone 工具（`host/subject/body`），发 `type="mail"` envelope；hub.send 对 mail
  直接落 `data/mail/<host>.jsonl`（server 权威、append-only、每箱 500 封滚旧），**不进 relay**——
  收件两端零 agent 参与，离线容忍。WebUI（桌面+手机）「邮件」入口轮询 `GET /mail`（WebUI runtime
  端点，runtime 解析本机主机名）出未读红点，模态框阅读，`POST /mail/read` 标记已读。
- **信使（courier）**：config `courier: bool = True`，GUI 设置页开关，每个信封重读（免重启）。
  - 开（默认）：现状——本机 comm clone 醒来转述对面留言、跑 receive_transfer 推卡。
  - 关：`Clone.on_direct` 钩子（chat/transfer 在入队前询问）→ room `_courier_direct`：
    chat 直接落会话视图 transcript（署名 `[<addr>]`）；transfer 由 room 合成同 id ask 进卡片
    管线，用户答 yes 后 room 直接 `download_transfer` 落盘并回 `{ok,saved}`——收方 agent 全程不醒。
- **前端 common.js**：fetch 封装（`initHttp` 支持 prefix/onUnauthorized）+ 工具卡片/consent 卡/
  确认弹窗/邮件 UI 抽到 `window.FungiCommon`，app.js/m.js 只留壳；server.py 静态白名单加 `/common.js`。
  注意 `[hidden]` 属性会被 CSS `display:flex` 覆盖——mail 列表/详情面板必须显式 `[hidden]{display:none}`。
- **好友视图可写（人类直发）**：`POST /comm-send {host, text|file}` → `RoomBase.comm_send_human`——
  envelope（chat/transfer，`body.from_human=true` + `sender_name`）从本机 comm clone 地址直投
  `peer:comm-<host>`，**不经本机信使**；收端语义由对面的信使开关决定：开 → `Clone.render_input`
  署「来自 ○○ 的用户」走正常转述轮；关 → `_courier_direct` 直落对面 transcript/consent 卡
  （transfer 卡问句署「来自 ○○ 的用户」）。发送侧 transcript 以 `sender:"human", mine:true`
  追加自己的消息；两侧 transcript 写入均持 per-sid 锁（`RoomBase._comm_lock`，覆盖 turn end、
  courier-off 直写、卡片 verdict 三个线程）。人类发的文件发送侧免确认，接收侧确认管线不变（卡片注明落盘位置）。投递成功后 hub 暂存副本经
  `DELETE /api/transfer`（`Transfers.discard_for`，仅收件方可删）清除，`data/transfers/` 只承担中转暂存。
