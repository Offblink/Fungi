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

- type：`chat`（对话）、`task`（goal/reply_format/context 委派）、`result`（task 回执）、`ask`（同意/提问请求）、`answer`（对 ask 的回答，reply_to=ask_id）、`err`（另有 `transfer` §10、`mail` §14）。
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

- 工具：`send_peer(text)`（只发 chat；task 由本机 Agent 的 `delegate` 发，见 §6.2）、`send_file(host, path, name, reason)`（§10）、`amail(host, subject, body)`（§14）、`read_file/write_file/edit_file/glob_files/grep_files`（路径守卫版，hub 侧 op 名是 `ls|read|write|edit|glob|grep`，见 §4）、`confirm(host, action, path, reason)`、`inquire(...)`，另挂 `todo`（§16）。
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
- **chat 回复兜底（2026-09-10 废除，见 §20）**：chat 回合若 LLM 未调用 send_peer 且最终文本非空，回合结束钩子自动补发
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
  GUI（今 `fungi/gui/` 包，2026-09-10 由 gui.py 拆包）全程 PyQt5——托盘（`fungi/tray.py`）与 CLI 房间模式（`__main__.py`）、
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
  **2026-09-10 修订**：ruff 已进 CI（`python -m ruff check`，只 check 不 format），
  版本从 pyproject 的 dev extra 读出（`ruff==0.13.0`）；playwright 已装、浏览器用例真跑
  （不再 skip）。

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
| `mail/<host>.jsonl`（仅 server，§14/§15） | 留言邮箱（append-only，每箱 500 封滚旧） | hub |
| `todos.json`（§16） | 主人的日历待办（GUI 日历与信使共写） | 用户与信使 |

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
- **好友视图的实时思考自动展开**（2026-09-11 用户报告：「只显示一个 Thinking，点进去才展开；
  本机会话是流式时自动展开、思考结束收起」）：本机会话的实时渲染器 `app.js::renderTurnLive` 一直在
  `<details>` 上写 `det.open = !closed`，而好友视图的实时磁带走共享的 `common.js::renderLiveEvents`，
  它建 `<details>` 时**根本没设 `open`**——于是永远收起（`56a8e5a` 引入好友视图旁观时就没这一步）。
  现在共享渲染器同样按 `reasoning_end` 记 `closed`：正在写的那个 `open`，写完了收起，一轮里多个思考块
  各按自己的结束事件算（与 `renderTurnLive` 的语义逐条一致）。转录里的旧思考两边都仍是收起——只动实时。
  回归：`test_webui_friend.py::test_friend_live_thinking_opens_while_it_streams`（去掉修复即超时失败）。
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

## 20. 增补（2026-09-10 深夜）：信使的双通道（汇报 ≠ 发给对面）+ 二维码依赖自愈

### 现场（对方机器上的 `comm-OwO.json`，22 行）
主人一句「你明天早上有空吗？」，两边信使**互相确认了 4 轮 8 行**，正文一轮比一轮长
（49→329→525 / 199→704→553 字），最后靠模型自己 `<<SILENT>>` 才停下（那条 3245 字 reasoning
全是在说服自己"别回了"）。根因不是渲染，而是**回合收尾文本的身份混了**：
`clone/comm.py::_chat_end` 的兜底把"没有调用 send_peer 的收尾文本"直接投给对面，
而模型把它写成了**给自家主人的汇报**（「**你**反问『啥远程控制？』」「另外有一点我想**单独跟你讲明白**」），
于是私密汇报出网、对面逐条作答，两边互相放大。同一份转录里还暴露出：`public/` 里一份无意翻到的
文档被当成"上次的活"，信使据此用 `inquire` 推给主人一道虚构选择题。

### 新契约（用户指令，2026-09-10）
- **`send_peer` 是唯一上网通道**：回合收尾文本＝给自家主人的汇报，只留在本机。实现上
  `Clone.run_turn` 的 chat 分支只把文本记进历史，`on_chat_end` 钩子与 `CommTools.peer_sends`
  一并删除（clean cutover，不留死插口）。
- 汇报要说清"我发了什么、对面回了什么、有什么要主人定"；不想汇报就裸 `<<SILENT>>`
  （存储侧 `_drop_silent` 仍会剥掉它，只留下 reasoning）。
- prompt 另加两条约束：**别翻与本轮无关的文件**（不许把无意翻到的文档当成任务依据；
  查不出来的就照实说）；**`inquire` 不得用来确认自己编出来的计划**。
- 好友视图把这类行标成 **信使汇报**：`renderTranscript` 在 `opts.report` 下给 assistant 行加
  `.report`，`style.css`/`m.css` 用 `::before{content:"信使汇报"}` 渲染（不进 `textContent`，
  行文本语义不变；会话视图不带这个类）。

### 二维码依赖（用户报告「缺少依赖 segno」）
`MobilePage.refresh()` 原先在 `ImportError` 分支**提前 return**：连"手机端地址"都不填，
页面等于废掉。现在**先算地址**（手机手输也能进，光标停在开头而不是滚到 token 尾巴）、提示可换行，
缺依赖时给**一键安装**（`python -m pip install segno`，独立控制台 + 1s 轮询，装完自动重绘；
`sys.frozen` 的打包版不给按钮、提示更新）。`release.yml` 加 `--collect-all segno`：
发布包不再可能缺这个纯 Python 小依赖。

## 21. 增补（2026-09-11）：GUI 的"记忆"不再被测试改坏；发起房间页也会记

现场：「为啥我每次点进加入房间就是新昵称和 tok_new？我记得有持久化的。」——记忆确实存在
（`HKCU\Software\Offblink\FungiGUI` 的 `last_token` / `last_nick` / `last_ip`），但被两类东西毁掉：

- **测试写进了用户的真 QSettings**：`tests/test_gui.py` 的 join 用例把自己的夹具值落盘（`tok-new`、
  `新昵称`、`pc-alpha`、`192.168.1.20` —— 注册表里逮到的就是这些），跑完再 `remove()` 那三个 key
  「别漏进用户设置」。**每跑一次全量测试，用户的 token/昵称/IP 就被删一次**。
  修法：`tests/conftest.py::_hermetic_gui_settings`（session 级）把两个页面指到 test-only 的 app 名
  （`FungiGUI-test`），跑完 clear；手工 remove 全部删掉（同 config.json 事故，同一类结构性防线）。
  注意：`QSettings.setDefaultFormat(IniFormat)` 在 Windows 上**不影响** `QSettings(org, app)`（实测仍走注册表），
  所以走"换 app 名"而不是"换格式"。
- **发起房间页根本没有持久化**：token 每次构造都 `secrets.token_urlsafe(12)` 现生成、`_leave()` 又换一个，
  昵称/主机名也不记。现在三者都从 QSettings 还原（`last_token` / `last_nick` / `last_host_name`），
  开房、热更新 token、实时改名时写回；**离开房间不再换 token**（下一个房间继续用，好友不必重输）。

顺带修了两条被午夜打翻的时间标签用例（正午锚点 + 星期几由时间戳推导，别写死）。

## 22. 增补（2026-09-11）：信使的第三件正事——替两边把「约」定下来

用户指令：**需要约会（广义）时，信使要帮助人类双方确定时间、地点和事件。** 广义＝见面、通话、
吃饭、拜访，任何需要定下来的计划。`COMM_SYSTEM_PROMPT`（`fungi/clone/comm.py`）新增一条：
钉死三件让计划成真的东西——**事件、时间、地点**；本机主人不知道的向对面信使要，给具体选项
而不是来回「你什么时候方便」；没有任何一方点头的细节不算数；落定的计划带钟点写进主人的日历
（`todo` 工具），只把真正该由主人回答的问题交给主人（沿用 §20 的 `inquire` 约束）。

与既有约束的关系：这条**不是**给信使新开一个聊天理由——`send_peer` 仍只在回复有必要时调用、
「别为确认而回复」照旧；它管的是「计划该被推着走完」，不是「多说两句」。GUI 帮助页（`fungi/gui/help.py`
的「信使」一条）同步补上这半句；README 场景①正是这类对话，故未改动。

## 23. 增补（2026-09-11）：好友视图的顺序——信使的转录被自己写坏的那次

现场：「好友视图的顺序依旧会乱，刷新也不行，整体呈现对话沉底、工具和思考上浮」。
渲染没错，**`merge_comm_history` 写坏的是文件本身**。

- 克隆的 history **不是转录的逐行镜像**：它只有对话（对面的行 + 信使的汇报文本），而转录里还有
  这一轮 Agent 自己写下的行——reasoning、tool_calls、tool 结果（`Agent.run` 就地往 `messages`
  里追加）。旧实现**按下标逐对比较** `carried_c[si] == stored_c[si]`：走到第一个「只有 store 有」
  的工具行就再也对不上，于是判定成「克隆把历史全忘了」，走 `keep + carried` 兜底——
  **把整段对话连同新的 `ts` 重新追加到末尾**。结果是 store 里已有的工具/思考行原地不动留在上面，
  全部对话（对面的消息、信使的汇报）沉到下面；每次 chat 回合都重演一遍，刷新也只是重画同一个文件。
  证据：用户机器上那份 `data/comm-sessions/comm-pc.json` 正是这个形状——前 18 行全是 rich 行（4 个
  回合的 reasoning/tool），后 12 行是**整段 lean 对话**（对面消息 + 汇报）且 `ts` 全等于最后一次
  merge 的时刻。
- **新语义**：store 是基底，只增不改。逐行在 store 里**向后找**（`while stored_c[j] != comp: j += 1`），
  跨过只有 store 才有的行；每个 store 行最多被认领一次；只有**最后一个被认领的 carried 行之后**
  的行才算新行，追加到末尾并盖 `ts`。store 丢了的行（history 被裁剪）不再被重新追加。
- 回归：`tests/test_room.py::test_comm_history_merge_keeps_the_tool_rows_above_the_dialogue_they_turn_belonged_to`
  （旧实现下第一个元素就是 `('assistant', None)`——工具行被抬到对话之前；新实现顺序不变）。
  既有三条 merge 用例（丢标记、跨回合回复、克隆遗忘）语义不变，全绿。
- **已写坏的文件不会自愈**：那份转录的顺序是坏 merge 烙进文件的，新代码只保证此后不再写坏。
  要修复旧转录需按其真实时间重排（需 pc 那台的 `data/comm/OwO__pc.jsonl` 镜像给出对面消息的真实 ts）。

## 24. 增补（2026-09-11）：信使能改自己写错的日历；汇报卡片上的反馈框

### 日历：`update`（用户可以删/改，别人不许乱动）
用户裁决：「之前我强调不要乱删日程，指的是不要无缘无故删除。但如果他自己写错了，还是可以修改的。」
- `todo` 工具新增 **`update`**（`date` + `old` → `text`）：**就地替换**一天里的一条，保持位置；
  `old` 不在那天就报 `no such item`（绝不凭空造条目），新文本与既有条目重名时只留一条。
- `todos.RULES` 随三处 prompt 注入，写明：**改自己的错是正当理由**（钟点/地点/措辞写错），
  用 `update` 而不是 `remove` + 重加（后者对用户就是一次静默取消，那是 2026-09-10 的教训）；
  **用户自己写下的条目不许动**，`remove` 仍需有理由相信用户想让它消失。
- 回归：`tests/test_todos.py::test_update_fixes_an_entry_in_place`。

### 反馈框：每条汇报卡片下面，主人 ↔ 自家信使
用户要求：「为了纠正信使偶尔会犯的小错误，每条信使汇报卡片上都提供一个反馈输入框。
提交的内容与对面没有关系，只是主人与信使之间的对话。」
- **每条 `.msg.assistant.report`（好友视图的我方汇报行）下带一个 `.report-feedback`**：
  一行输入 + 发送，`common.js::attachReportFeedback` 构建（桌面 app.js 与手机 m.js 共用，
  由 `opts.feedbackHost` 触发，两个客户端都传 `friendView`）。
- **只在本地唤醒自家信使**：`POST /comm-note {host, text}` → `RoomBase.comm_note_human` →
  `Clone.note()` 把一个 `from_owner` 的 chat 信封**直接塞进自己的 worker 队列**，
  从不过 `transport`：对面既收不到信封，hub 的 comm 镜像里也没有行（回归直接断言
  `commlog.read() == []` 且对面 agent 不醒）。渲染成 `[主人的反馈] …` 一行，走 chat 回合
  的全部好处：进 history、进转录（刷新后还在）、收尾文本仍是给主人的汇报。
- **与对面的关系**：`COMM_SYSTEM_PROMPT` 写明这是主人私下对你说的话——**不许转达对面、
  不许在对面的对话里提起**，只当纠错依据。
- **框里允许为空**：空提交**不发请求**（前端直接早退，不空转一次 LLM 回合），也不报错。
  服务端对空文本返回 `error: empty feedback`（防御性，正常 UI 到不了）。
- **成功与否看响应体**：`FC.postJSON` 返回的是 `Response`（既有调用都是"发了不管"），
  这里必须 `await res.json()` 再看 `ok`——HTTP 200 里带 `{"error": …}` 不能读成成功
  （实测踩到过：信使还没就绪时界面谎报"已发给信使"）。
- 回归：`tests/test_friend_send.py::test_courier_feedback_wakes_our_courier_and_never_the_peer`、
  `test_an_empty_note_never_wakes_the_courier`；
  `tests/test_webui_friend.py::test_every_report_row_offers_feedback_for_our_courier_only`
  （真浏览器：空框不发、填了才发、转录里出现主人的行）。

## 25. 增补（2026-09-11）：信使不再被提问卡住；来信的铃声与闪动；发文件的进度条

### 25.1 信使的 `inquire` 不再阻塞（否则它会替对面闭嘴 30 分钟）

用户提问：「目前的 inquire 是阻塞的吗？如果是的话，一旦用户没有及时回复 inquire，那信使是不是
就卡死不动，对面来信也不动了？这可不行。」

- **事实确认**：是阻塞的。`CommTools.inquire` → `_blocking_ask` → `PendingAsks.wait(timeout_s=1800)`，
  而一个 clone **只有一条 worker 线程**（`Clone._work` 队列 + `_work_loop`）：问题没人答，工人的这一轮
  就停在 `Event.wait` 上，最长 30 分钟。期间轮询线程照旧收信，但**对面每一条 chat 都排在队列里不动**
  （它还会连带卡住 `send_peer`，因为网络通道也长在同一个 worker 上）。
- **新语义**：信使的提问只发信封、立刻返回（工具结果 `ASKED (not blocking)…`），卡片照旧等人。
  主人答复后 `RoomBase._send_answer` **把 `questions` 一起带回**（同意类裁决没有 `questions`，只有
  `question`），`Clone.dispatch` 发现**没人在 wait**（`pending.resolve()` 返回 False）时走
  `Clone._answer_turn`：把答复变成**一轮新的 chat**，渲染成 `[主人的答复] 问: … 答: …`。
  问题必须跟着答复走——那一轮的 tool 调用不在 clone 的 history 里（history 只存对话文本），
  只给一句「周六有空」信使不知道在答什么。
- **边界**：`confirm` / `send_file` / `receive_transfer` 的等待**保持阻塞**。它们的返回值就是裁决本身，
  工具要在这一轮里拿到答案才能继续；`_answer_turn` 只认 `questions`，所以迟到的同意裁决**不会**
  被误当成一轮对话（回归在 `test_friend_send.py::test_a_consent_verdict_stays_a_bare_value`）。
- **为什么不是复用 `background`**：`TriLayer.build_clone_agent` 的 docstring 已经写明
  「a clone has no re-activation channel … its background reports had nowhere to land」，comm clone 走
  `subagents=False`（连 spawn/background 都没有）；而 background 的复活链路是 **WebUI 会话级**的
  （`server.py::_PENDING_SPAWNS` + 浏览器 `/spawn-pending` → `/resume`），信使既没有会话 id 也没有浏览器。
  这一次补的正是 clone 侧的复活通道——**答复信封自己就是唤醒**，与上一轮的「反馈框」（`Clone.note`）
  同一条路：不过 transport、直接进 worker 队列。
- prompt 与 schema 同步（`COMM_SYSTEM_PROMPT` 的 inquire 一条 + `_SCHEMA_INQUIRE`）：
  「不阻塞、答复晚些作为 `[主人的答复]` 到达、别等、别再问第二遍」。
- 回归：`tests/test_comm_clone.py::test_inquire_never_blocks_the_courier_and_the_answer_arrives_as_a_turn`
  （一轮提问未答 → 这一轮照常收尾 → 期间对面的 chat 被照常服务并回话 → 答复到达后再起一轮，
  且带问题原文）、`test_an_answer_without_questions_is_not_a_turn`。

### 25.2 来信提醒：托盘图标闪动 + 铃声（可关）

用户要求：「对于来信，当用户不在好友视图时（和显示未读一个判断条件），需要响铃和托盘图标闪动。」

- **条件与未读徽标同源**：房间自己的邮箱轮询把 `unread` 记在 `RoomBase.last_unread`
  （`_mail_watch_loop` / `_mail_poll_once`），GUI 每秒读它——不额外打一份到 hub。
  WebUI 里「点开好友视图即整线标已读」，那正是停铃的开关（与 `initMailUnread` 的 `byPeer` 同一个字段）。
- **宽限期 10 秒**（`fungi/gui/app.py::RING_GRACE_S`）：好友视图要 ~8 秒才把这一条标成已读
  （5s `/comm-log` 轮询 + 3s 邮箱轮询），立刻响铃就会为「你正看着的那条」响。闪动**不等**宽限
  ——它就是未读提示本身，误报的代价只是一枚图标。
- **铃声资产**：`assets/ringtones/*.wav`（7 个：叮咚/风铃/蜂鸣/警示/通知/钢琴/合成器），
  由 `scripts/make_ringtones.py` 按用户 Get It 应用的合成配方生成（44.1kHz 单声道，仓库里不带 numpy）。
  播放是 QtMultimedia 的 `QSoundEffect` 循环；缺多媒体插件退 `winsound`；都没有就静音——
  没有声卡不能拖垮 GUI。exe 打包加了 `--add-data "assets;assets"`。
- **设置页「来信提醒」**：铃声开关（默认开，写 `config.ring`）+ 铃声选择下拉（换一个即保存并试听一次）。
  **关掉不显示铃声选择**（用户明确要求），未读的图标闪动照旧。
- **托盘**：未读时图标在两版之间闪（`tray.make_icon(badge=True)` 的红点版），菜单只在响铃时多出
  「停止铃声」——停的是这一条的铃，闪动留着；未读清零后重新武装，下一条照响。
- 回归：`tests/test_gui.py` 的四条（宽限期、关铃仍闪、停止铃声只停这一条、开关收起下拉）
  + `test_tray_icon_flashes_and_offers_to_stop_the_ring`。

### 25.3 发文件的进度条（模态，完成自动关闭；手机两步）

用户要求：「对于文件传输，我希望添加进度条。进度条本身是模态框（风格一致），进度完成后自动关闭。
手机端的上传文件需要多一步——首先上传到服务器端，再从服务器上传到目标主机。对于不同的进行步骤，
进度条应进行说明。」

- **字节只在服务器上流动**（页面看不到它们），所以进度只能由服务器数：**浏览器自己铸一个 job id**
  → `POST /comm-send {host, file, job}` → `GET /transfer-progress?id=` 轮询 →
  `fungi/xfer.py::TransferJobs`（`running` / `done` / `error`，跑完 300 秒后清）。
  不带 `job` 的调用（测试、命令行、旧页面）**行为与返回体完全不变**（`{ok, kind, name}`）。
- **进度钩子**：`upload_transfer(path, name, to_host, progress=)` 从 room 穿到
  `HubClient`（256 KiB 一块地数）与 `LocalTransport`（`read` 闭包，同一根进度条）。
- **手机多一跳，就多一条进度**：① 浏览器 → 电脑（XHR 的 `upload.onprogress`，只有客户端能测）
  ② 电脑 → 对方（服务器的 job）。手机聊天框的文件上传也走同一个模态（单步）。
  桌面发文件是单步——文件本来就在这台电脑上。
- **模态**（`web/common.js::initTransfer`，桌面与手机共用；`#xfer-overlay` + `.xf-step`）：
  每一跳一行「标签 + 进度条 + 字节/百分比说明」，完成时全行 100% 并标绿，等 900ms 自动关闭；
  失败则把原因写在那一行并**留在屏幕上**（关掉它不会取消传输：按钮只是收起卡片）。
  job id 存在 `#xfer-overlay.dataset.job` 上（控制台与浏览器测试的唯一把手）。
- 回归：`tests/test_webui_transfer.py`（真浏览器 4 条：条与说明的渲染、桌面上传全流程并落在 hub、
  手机两跳并把文件落进 inbox、失败留在屏幕上）+ `tests/test_friend_send.py` 的 job 状态两条。
