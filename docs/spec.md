# Spec: Fungi

> 定位：以 YESIR 为基座的 LAN 多主机 Agent 协作网络。server 发起房间，client 直连 server，client 间流量由 server relay。存储统一在 server 主机。核心洞见：(1) 通讯 Agent 之间自主交流仅限 `public/`，其他目录需征求同意；(2) 用户仅与本机 Agent 交流，跨主机事务交由通讯 Agent 处理。

> 2026-09-03 评审定案：无 Redis（见 docs/architecture.md 的 Brainstorm 修订记录）；托盘栈 PyQt6；consent 裁决者为目录属主 host 的用户。

## 1. 术语与实体

| 术语 | 定义 |
|---|---|
| host | 一台运行 Fungi 进程的主机，用户起名，房间内唯一 |
| server | 发起 LAN 的 host，承载 hub（HTTP relay + 存储） |
| client | 直连 server 的 host |
| Agent | host 进程内的一个 Agent 分身 |
| 本机 Agent（local） | 专职与用户交互的 Agent，每 host 恰一个 |
| 通讯 Agent（comm） | 专职与一台远端 host 的对位通讯 Agent 交互的 Agent，每个远端 host 一个 |
| 对位（counterpart） | host A 上对接 host B 的通讯 Agent 与 host B 上对接 host A 的通讯 Agent 互为对位 |
| `public/` | server 存储上的公共目录，通讯 Agent 自由读写 |
| `homes/<host>/` | server 存储上各 host 的属地目录，非属主访问需属主用户 consent |

## 2. 拓扑与生命周期

- server 启动：生成/读取房间 token，起 hub HTTP 服务 + 本机 Agent；通讯 Agent 按名册动态增删。
- client 加入：HTTP join（name + token）→ server 建名册项并回发 host 列表；此后心跳保活。
- Agent 生成规则：每 host 维护「对端 host → 通讯 Agent」映射；名册变化（新 host join / 心跳超时剔除）时增删通讯 Agent，两端同步。
- 退出：client leave 或心跳超时被剔除；server 关停即房间解散。

## 3. 消息协议

JSON envelope，HTTP 承载：

```json
{"v": 1, "id": "uuid", "src": "alpha:comm-beta", "dst": "beta:comm-alpha",
 "type": "chat", "ts": 1730000000, "reply_to": null, "body": {}}
```

- type：`chat`（对话）、`task`（goal/reply_format/context 委派）、`result`（task 回执）、`ask`（同意/提问请求）、`answer`（对 ask 的回答，reply_to=ask_id）、`err`。
- 可靠性：server 为每 Agent 维护内存 inbox，收端长轮询拉取后 ack；投递按消息 id 去重，语义 at-least-once。

## 4. Server（hub）职责

端点（Face 风格，token 鉴权）：

- `POST /api/join` `{name, token}` → `{host_id, peers, fs_base}`；`POST /api/leave`
- `POST /api/heartbeat` → 顺带返回待办通知（pull 模型，与 Face 一致）
- `POST /api/send` envelope → 投递（本地直投或 relay 转发，同一函数）
- `GET /api/poll?after=<cursor>` → 长轮询 inbox
- 存储代理：`/api/fs/ls|read|write|edit|glob|grep`、`/api/sessions...`（YESIR session 语义），全部经路径守卫

hub 内存态：名册、各 Agent inbox、pending-ask 注册表（ask_id → 投递状态，供 heartbeat 重放未决通知与去重）。跨 Agent 文件写锁用 hub 内存锁（LAN 规模无需分布式锁）。

存储布局（server `data/`）：`sessions/`（YESIR 兼容 JSON）、`public/`、`homes/<host>/`。

> **会话归属修订（2026-09-04）**：会话必须按 host 隔离——server 角色存 hub store
> `data/sessions/`；client 角色存**本机** `sessions/`（YESIR 默认目录），不再经
> `/api/save` 落到对面操作的磁盘上。此前共享单目录导致任一方的 WebUI 会话列表
> 列出对方全部对话（真机回归发现，用户判定为严重隐私问题）。hub 的
> `/api/sessions` 仅供 server 角色自身使用；另加 `POST /api/transfer/upload`
> （raw 字节流式上传，token 查询串鉴权，413=超 max_file_mb），让用户面 Agent
> 能发送**本机真实文件**（store 之外的路径）。

## 5. 同意流（消息面承载，无 Redis）

ask 是普通消息，不需要独立协调设施：

```
请求方 Agent 调 confirm / inquire
  → 发 ask envelope（to=目标 host:local）
  → PendingAsk 注册表登记，threading.Event 阻塞（复用 YESIR tools/ask.py 机制）
  → relay 投到目标 host 的本机 Agent → WebUI 卡片（asks 横幅，打开即见）
  → 用户打开 WebUI → 卡片（允许 / 禁止 / 自定义输入）
  → 本机 Agent 回 answer envelope（reply_to=ask_id，value=yes|no|自定义文本）
  → 请求方唤醒，返回 "USER: <value>" / "DENIED"
```

- 超时默认 600s（用户可能不在电脑前，比 YESIR 的 300s 长，可配）；超时返回 `"ERROR: 用户未回答"`。
- 断线补偿：本机 Agent 心跳时从 hub pending-ask 注册表重放未决卡片。
- 裁决者：ask 涉及 `homes/<owner>/` 时 to=属主 host 的 本机 Agent；本机属主操作 to=本机 本机 Agent（同进程直连，不走网络）。

## 6. Agent 规格

### 6.1 通讯 Agent

- 工具：`send_peer(text|task)`、`read_file/write_file/edit/glob/grep`（路径守卫版）、`confirm(host, action, path, reason)`、`inquire(...)`。
- 2026-09-10 移除 `spawn` / `background`：通讯 Agent 由对端驱动、身边没有用户监督，而 clone 没有 spawn 的再激活通道（子代理只能同步多跑一跳，`background` 的报告也无处落地）——两者都只放大这个不受控 Agent 的爆炸半径（`trilayer.build_clone_agent(subagents=False)`）。
- 路径守卫：`public/` 自由；`homes/<owner>/` 非属主需 consent（confirm 发往属主 host 的 本机 Agent）；`homes/<own>/` 与自身会话目录需自身用户 consent；`sessions/` 拒绝。
- 自主交流：对位通讯 Agent 之间 chat/task 自由往来，无需用户参与；涉及 `public/` 之外的文件操作才触发 consent。

### 6.2 本机 Agent

- 工具：YESIR 原生全套（shell/web/inquire…）+ `delegate(host, goal, reply_format)` + `peers()`。
- 用户仅与本机 Agent 对话（核心洞见 2）；delegate 内部把 task envelope 发给对应通讯 Agent 并阻塞等 result。

### 6.3 ask 汇聚

所有 ask（含通讯 Agent 的 inquire / confirm）统一为 ask envelope 落到目标 host 的本机 Agent → WebUI 卡片。本机 Agent 自己的 inquire 是同一机制的同进程特例（直连 PendingAsk，不过网络）。

## 7. WebUI 与托盘

- WebUI 默认关闭：进程启动即最小化到托盘（PyQt5 + qfluentwidgets，2026-09-05 起统一；
  运行时画图标、fluent 菜单、单实例）；托盘菜单「打开 WebUI / 打开数据目录 / 退出」，双击托盘打开 WebUI。
- 有未决 ask 时在 WebUI 顶部横幅展示卡片（asks banner）；用户点托盘 → 打开 WebUI。
- ask 卡片渲染于聊天流：允许 / 禁止 / 自定义输入框，对应 answer value `yes` / `no` / 自定义文本。
- 会话存储在 server；WebUI 经本机 Agent 代理读写（对用户透明）。

## 8. 安全

- 房间 token：join 与所有 API 必带，错误 token 403。
- 路径守卫在 server 端强制（不只靠 Agent 自觉）：resolve 后前缀校验，拒绝 `..` 与绝对路径逃逸。
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
- **好友列表**：`GET /api/peers`（hub）→ 本机 Agent 代理 `/peers` → WebUI 侧栏在线成员；
  点击进入只读会话视图（`GET /comm-log?host=` 渲染双方通讯 Agent 对话流），无输入框
  （核心洞见 2 不破）。
- **文件传输（C2，落对端本地盘）**：字节面 store-and-forward——`POST /api/transfer`
  服务端从 store 复制暂存（上限 `max_file_mb`，config.json，默认 200），envelope 只传元数据；
  控制面复用 consent——接收方 通讯 Agent 向属主本机 Agent 发 ask（同意模式由滑块控制，见下），
  同意后经 `GET /api/transfer` 下载落盘 `<inbox_dir>/<来源host>/<文件名>`（config.json
  `inbox_dir`，默认 `<repo>/inbox`，重名加序号，basename 消毒）。落盘路径经 result envelope
  回执发送方。transfers 注册表在内存，server 重启丢失未拉取的暂存文件（v1 容忍，与 §3 一致）。
- **同意模式滑块（2026-09-04 修订）**：一次性「始终允许」废除——持久放行改为每个好友的
  可见可逆开关（WebUI 好友会话顶部滑块，左=允许，右=询问），存于 `~/.fungi/consent_rules.json`
  的 `modes`（host → allow|ask，默认 ask）；旧版 `always_allow` 地址列表自动迁移为 host 模式。
  判定键为 ask body 的 `from`（逻辑请求方）——传输回执的 envelope src 是接收方自己的
  通讯 Agent，按 src 键控会错挂到自家 host。inquire（通用提问）永不自动放行。
- **display-name 层（2026-09-04）**：wire 身份仍是 ASCII 安全的 host 名（envelope 地址、
  URL、文件名——主机名强校验的理由不变），昵称只走展示层。`Member.display` 随 join 携带、
  re-join 刷新（UI 改名无需重启）；`/api/peers` 与 join/heartbeat 的 `roster` 字段返回
  `[{name, display}]`；WebUI 侧栏/好友会话标题/旁观消息来源/通知标题显示昵称，无昵称回退
  显示 name。入口 `--display`，config.json `display` 可存；昵称做清洗（去控制字符、归一
  空白、截断 64 字符）但不受 ASCII 限制，中文/emoji 均可，且永不进入任何 wire 地址。

## 11. 增补（2026-09-04）：skill 系统

- **存储**：每 host 本地 `data/skills/<name>/SKILL.md`（目录式：SKILL.md 载 frontmatter `description`
  与 markdown 正文，旁置脚本等伴随文件经 `skills` 工具的 `path` 读取；旧版扁平 `<name>.md` 仍可读，
  同名目录优先）。name 即目录名，kebab-case ≤64 字符，正文上限 32k。每 host 一份，不随房间同步（v1）。
- **注入（每次初始化读列表）**：每个 agent 构建点（TriLayer `build_orchestrator` /
  `build_clone_agent` / `_run_task` 子代理）重新读盘，把「名称+描述」清单追加到 system
  prompt——本回合保存的 skill 下一回合即对全体 Agent 可见。WebUI 已存 session 的 system
  消息保留原有内容，仅尾部托管 skills 段（去旧附新，见 `agent.run`）。
- **工具与元技能**：`skills` 工具（list/read/save）；`writing-skills` 元技能在首次访问时自动
  播种到目录，写明格式与质量标准（description 写触发条件、步骤给精确命令/路径、记录坑与验证法）。
- **安全**：save 仅限用户面 agent（本机 Agent、WebUI 编排者）；通讯 Agent 及其 spawn 只读——
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
| `comm-sessions/` | 好友视图：通讯 Agent 的会话式转录（与 sessions 同构） | room 进程 |
| `comm/`（仅 server） | Agent 间信封流量镜像，`发送方__接收方.jsonl` | hub，每次投递一条 |
| `public/`、`homes/<host>/` | Agent 文件空间（守卫白名单内） | Agent |
| `transfers/` | send_file 暂存（store-and-forward，取走即删） | hub |
| `skills/<name>/` | 每主机技能沉淀（SKILL.md + 脚本） | 仅用户面 agent；通讯 Agent 只读 |

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


## 14. 增补（2026-09-09）：文字留言 + 信使开关

- **amail**：通讯 Agent 工具（`host/subject/body`），发 `type="mail"` envelope；hub.send 对 mail
  直接落 `data/mail/<host>.jsonl`（server 权威、append-only、每箱 500 封滚旧），**不进 relay**——
  收件两端零 agent 参与，离线容忍。WebUI（桌面+手机）「邮件」入口轮询 `GET /mail`（WebUI runtime
  端点，runtime 解析本机主机名）出未读红点，模态框阅读，`POST /mail/read` 标记已读。
- **信使（courier）**：config `courier: bool = True`，GUI 设置页开关，每个信封重读（免重启）。
  - 开（默认）：现状——本机 通讯 Agent 醒来转述对面留言、跑 receive_transfer 推卡。
  - 关：`Clone.on_direct` 钩子（chat/transfer 在入队前询问）→ room `_courier_direct`：
    chat 直接落会话视图 transcript（署名 `[<addr>]`）；transfer 由 room 合成同 id ask 进卡片
    管线，用户答 yes 后 room 直接 `download_transfer` 落盘并回 `{ok,saved}`——收方 agent 全程不醒。
- **前端 common.js**：fetch 封装（`initHttp` 支持 prefix/onUnauthorized）+ 工具卡片/consent 卡/
  确认弹窗/邮件 UI 抽到 `window.FungiCommon`，app.js/m.js 只留壳；server.py 静态白名单加 `/common.js`。
  注意 `[hidden]` 属性会被 CSS `display:flex` 覆盖——mail 列表/详情面板必须显式 `[hidden]{display:none}`。
- **好友视图可写（人类直发）**：`POST /comm-send {host, text|file}` → `RoomBase.comm_send_human`——
  envelope（chat/transfer，`body.from_human=true` + `sender_name`）从本机 通讯 Agent 地址直投
  `peer:comm-<host>`，**不经本机信使**；收端语义由对面的信使开关决定：开 → `Clone.render_input`
  署「来自 ○○ 的用户」走正常转述轮；关 → `_courier_direct` 直落对面 transcript/consent 卡
  （transfer 卡问句署「来自 ○○ 的用户」）。发送侧 transcript 以 `sender:"human", mine:true`
  追加自己的消息；两侧 transcript 写入均持 per-sid 锁（`RoomBase._comm_lock`，覆盖 turn end、
  courier-off 直写、卡片 verdict 三个线程）。人类发的文件发送侧免确认，接收侧确认管线不变（卡片注明落盘位置）。投递成功后 hub 暂存副本经
  `DELETE /api/transfer`（`Transfers.discard_for`，仅收件方可删）清除，`data/transfers/` 只承担中转暂存。

## 15. 增补（2026-09-10）：文字消息统一入留言邮箱 + 好友视图排序修复

- **统一存储**：好友视图人类直发文字不再写 comm transcript（SessionStore），而是发
  `type="mail"` envelope（`from: <host>:human`）——hub `deliver_pair` 同时落收发两端邮箱：
  收件方未读、发件方已读（`mine: true`），记录新增 `peer`（对话对面主机）。通讯 Agent 的
  留言工具同路径受益。协议 `parse_addr` 补 `mail` 角色——**修复潜在 bug：真实 hub 链路上
  `amail` 信封此前会被 `parse_addr("host:mail")` 拒绝**（旧测试全走 FakeTransport 未暴露）。
- **信使语义收敛**：文字/邮件投递与信使开关解耦（永远直达、零 agent）；信使只管 Agent 对话
  转述与 consent 卡片。`_courier_direct` 的 from_human chat 分支随之删除。
- **好友视图渲染序修复**：此前 transcript → events 两段拼接，事件行永远压在最新消息下面
  （人类消息"不在最下面"的根因）。现在 `/comm-log` 载荷增加 `mails`（按 `peer` 过滤、ts 排序），
  桌面/手机统一渲染 transcript → events → mails，人类消息恒在最底。
- **未读入口收敛（2026-09-10 二次修订）**：✉ 统一收件箱入口废弃；未读数改为好友列表逐人徽标
  （common.js `initMailUnread` 轮询 /mail 按 peer 计数），点开好友视图即整线标读
  （前端逐封 POST /mail/read）。人类发送者标签与代码块对比度提高。

## 16. 增补（2026-09-10）：信使的记忆、日历与 todo 工具

- **信使概念收窄**：信使只指「代收代复留言」的消息信使。文件传输从不经过 LLM
  （transfer 信封 → consent 卡片 → inbox/<src>/，与 courier 开关无关），不再是信使的一部分。
- **长期记忆**：`config.courier_memory`（GUI 信使页编辑，即时写盘）。通讯 Agent 每轮
  chat turn 重建 system prompt 时重读注入——`Clone.system_prompt` 支持 callable，
  `resolved_prompt()` 每轮求值（与 courier 开关同款 live 语义）。
- **四周日历待办**：`fungi/todos.py`，存储 `data/todos.json`（date → [items]，gitignore）。
  注入窗口 = overdue 30 天尾 + 今天 + 未来 21 天，按日排列随 prompt 注入。
- **todo 工具**：action add/list/remove，同一份存储。挂载三处：L1 Orchestrator
  （trilayer，parallel_tools 含 todo）、**本机 Agent 的 clone 工具集**——房间模式 WebUI
  回合走 `RoomRuntime.build_agent`（由 clone.tools 装配），不走 build_orchestrator，
  漏挂本地 clone 会导致 WebUI 侧看不到该工具（真机教训）——以及信使 comm clone。
  信使与用户 thus 共写一份日历。
- **日历条目属于用户（2026-09-10 真机教训）**：信使把主人 9/11 的约「remove 后 add 改写」
  了一遍——对主人就是一次静默取消，而当时的本意只是「这条之外再加一条」。现在 `add`
  只追加（同条不重复），`remove` 必须给出确切条目文本（整日清空只留在 GUI 日历）；
  `todos.RULES` 随上述三处 prompt 注入，写明**新信息另加一条**而非改写旧条，落定的约要
  主动记（含钟点）。
- **GUI 信使页**：长期记忆编辑 + 4 周圆形日历（点日期弹窗录入，一行一条）；
  今天 accent 实心、有待办淡橙底色。信使开关仍在设置页。

## 17. 增补（2026-09-10）：输入框回车即更新

用户定调：凡是「要点按钮才提交」的输入框，键盘上按回车就该等效提交（按钮保留，不是替换）。
- **单行 `LineEdit` → `returnPressed`**（不用 `editingFinished`：移开焦点也提交会变成
  「一切换焦点就开房／加入」）。发起房间页 主机名/昵称 → `_start`；加入房间页四个框
  （IP/Token/昵称/主机名）→ `_join`；设置页三个框 → `_save`（与按钮同语义：留空的框不覆盖
  已存配置，保存后清空三格）。`JoinPage._join` 入口新增 `not join_btn.isEnabled()` 早退——
  禁用态就是「扫描进行中」，否则第二次回车会再开一个发现线程并二次 emit `join_done`。
- **多行 `TextEdit` → `Ctrl+Enter`**：回车必须留给换行。信使页长期记忆 →
  `_save_courier_memory`；日历录入弹窗 → `accept`。`QShortcut("Ctrl+Return")` 挂在输入框上、
  `WidgetWithChildrenShortcut` 上下文——只在该框聚焦时生效，不做窗口级劫持（HostPage 的
  Ctrl+C 是窗口级旧例，新加的一律不跟）。保存按钮 tooltip 注明该键位。
- **已有同款先例**：发起房间页 Token 的 `editingFinished`（§12）——单行框回车即热更新。
- **开房/加入之后，那几格仍能「回车即更新」（2026-09-10 用户二次点名）**：`HostPage._apply_identity`
  （主机名/昵称；Token 早有热更）与 `JoinPage._enter`（加入页四格）在房间运行中提交**能改的那部分**：
  昵称 → `RoomBase.set_display()`（服务端直接 `hub.join` 刷 roster；客户端 `client.display` + re-join，
  `roster.join` 本就刷新 display，所以对面 5s 轮询 `/peers` 立刻看到新名字），Token → `RoomClient.set_token()`
  （赋值 + 心跳校验，失败还原）——房主换了 Token 后加入方靠它续上，否则请求全线 403。
  **wire 名与房主 IP 拒绝并还原字段**（地址、roster key、`data/` 文件名、对面 comm clone 都以它们为准，
  换它们等于换房间/换身份），InfoBar 说明原因，不静默失败。
  坑：`join_btn` 加入成功后**一直保持禁用**（职责已交给「离开房间」），所以实时提交的守卫只能看
  `room is None`，不能看按钮状态——否则整条实时路径静默失效（真机探针当场抓到）。

## 18. 增补（2026-09-10）：好友视图一张时间轴（把乱序根治掉）

现场两句：**「好友对话跑完一轮就没了」**（已由 §15 的 `#messages` 归属修复，见 `docs/webui-ux.md`
前端坑）与**「thinking 和工具调用的卡片没有按时间顺序渲染」**。后者是结构问题，不是渲染 bug：

- **根因一（串接）**：`renderFriendChat` 把三份各自有序的列表**接在一起**（transcript 列表 →
  hub 事件按 ts → mail 按 ts），而 ask 卡要靠「按存储顺序 shift」猜位置，猜不中就 append 到末尾。
  实测（真转录 17 条消息 / 3 条 ask）：两条 ask 被甩到最底部，而它们其实比画面里所有消息都早。
- **根因二（数据没有排序信息）**：`asks` 记录只有 `{id, questions, answers, status}`——既不知道
  是哪次工具调用触发的，也没有时间戳；转录消息同样没有时间戳。没有可排序的量。
- **改法**：`make_ask_tool` 改 `with_call_id=True`，记录带 `call_id` + `ts`；卡片 ask
  （`_record_card_verdict`）带 `ts`；comm 转录的消息落盘时由 `merge_comm_history` 给**新到**的行打
  `ts`（已有行保留原 ts）。前端 `renderTranscript`/`renderFriendChat` 合成**一张时间轴**：ask 按
  `call_id` 精确锚在它的工具调用处（旧记录退化为「问题原文与调用参数逐字相等」的精确匹配，再退化
  到存储顺序）；带 `ts` 的事件/邮件/卡片 ask 用 `insertByTs` 插到第一个更晚的兄弟节点前；没有 `ts`
  的行保持到达顺序（旧转录整块在前）。
- **旧数据**：2026-09-10 之前的 ask 记录没有 `ts`/`call_id`，无法回溯定位——它们的工具调用已不在
  转录里，属于比画面更早的回合，因此**前置**到最前（不再甩末尾）；旧转录消息没有 `ts`，与事件/邮件
  的合并从新数据开始生效。
- **顺带修数据丢失**：chat 分支原先 `msgs = list(messages)` 直接覆盖；clone 被 roster 回收后重建时
  历史为空，下一轮就把整段转录覆盖掉（磁盘一起丢）。现由 `merge_comm_history` 做并集：clone 仍带着
  的行沿用存储副本（保 ts），被遗忘的行前置携带，新增行追加；任务分支本就是追加。回归测试：
  `test_chat_turn_after_a_clone_rebuild_keeps_the_earlier_transcript`、
  `test_chat_turn_with_cumulative_history_does_not_duplicate`、`test_ask_record_carries_its_tool_call_id`。

## 19. 增补（2026-09-10 深夜）：消息时间标签、好友视图分侧、设置页预填、转录合并的身份判据

同一夜的四件现场事（用户报告为准）+ 一件事故善后：

- **每条消息悬停显示发送时间**（用户要求）：`common.js::whenLabel(ts)` 把 ts 说成人话——今天/昨天/前天，
  七天内用星期几，再远用 `YYYY-MM-DD`，时间一律 24 小时制。`markTs()` 顺手写进 `data-when`；
  `style.css`/`m.css` 用 `::after{content:attr(data-when)}`。**位置**：绝对定位在卡片**下方**、贴**发送方**
  那一侧（我方靠右、对面靠左），默认 `display:none`，`:hover` 才 `block`——不参与布局，行高与滚动高度
  都不变（第一版把时间写在气泡里，一悬停就把行撑高、整页跟着跳，用户当场退回）。伪元素不进
  `textContent`，行内容探测不受影响。会话转录原先没有逐行时间：
  `public_messages()` 在持久化边界给**首次落盘**的行打 `ts`（就地写，回合开始的那次 save 定住 user 行、
  结束那次 save 定住本轮新行——否则会把老行重打成回合结束时刻）。`ts` 是存储字段，不是协议字段：
  `llm._wire_messages()` 在出网前剥掉，避免凭空给 provider 造一个字段。
- **好友视图分侧**：我方（信使）的**思考 `<details>`、工具卡、错误行**原先是 `align-self:flex-start`，
  而我方正文靠右——同一轮的东西被拆到两边（用户：「因为这是我方的」）。共享渲染器把 `opts.side.agent`
  一并加到 reasoning/tool/error 行（含 live tape 的 reasoning/tool/result）。桌面此前只给我方正文加了
  `friend-mine`；手机端干脆**没有侧**，对面的行还落进 `.msg.user` 的强调色气泡里（看着像"我说的"）。
  现两端同一套 `side` 类：对面在左（素底），我方在右。移动端会话视图不受影响。
- **设置页显示当前配置**（用户要求）：三个框不再是空的——接口地址/模型直接预填（可编辑），API Key
  只把掩码 `sk-12…a5f4` 放进**占位符**（真 key 依旧不上屏，空框 = 保持不变的老语义不变）；保存后
  `_load_fields()` 让框回到当前值（不是清空），进页(`showEvent`)也刷新一次，命令行/WebUI 改过模型能立刻看到。
- **转录合并按"身份"对齐，不按整字典**：`_comparable()` 原先把除 `ts` 外的整个字典拿来比。clone 的
  history 副本与存储副本并不逐字相同（`run_turn` 补写的 assistant 行不带 agent 那份 `reasoning`），
  于是一模一样的行被当新行 → 对齐错位 → 走了"clone 忘了这些行"的携带分支 → **信使的上一条回复在
  最新对话里出现两次，还被顶到它自己的提问前面**。现在只比 (role, content, tool_call_id, 工具调用名)，
  匹配上的行沿用存储副本（连带 reasoning 与 ts）。复现：对面一次发两条 + 信使两条都回 → 浏览器里
  DOM 出现两行相同 assistant；修后 0 重复。回归：`test_comm_history_merge_keeps_a_reply_with_its_question`。
- **事故善后（拆包写坏用户配置）**：`fungi/gui/` 拆包后页面改读 `fungi.config`，而 GUI 测试还打在
  `fungi.gui.load_config/save_config` 门面上，patch 失效 → 真 `save_config` 被调用，把测试串写进了用户
  的长期记忆（`courier_memory`）与 key。已从 `data/comm-sessions/comm-pc.json` 的 system prompt 与用户
  自己留存的配置副本取回旧值并写回。**结构性防复发**：`tests/conftest.py::_never_write_the_user_config`
  把 `config.CONFIG_PATH` 指向 tmp 副本（放在子目录里，避免被把 tmp_path 当数据目录的用例 glob 到），
  从此任何测试都不可能碰到仓库里的 `config.json`。
