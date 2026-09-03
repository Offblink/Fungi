# Design: 好友列表、通讯会话旁观、文件传输

> 2026-09-03 评审定案：chat 落盘 server store；好友会话只读旁观；文件传输 C2（落对端本地盘），传输面参考 Around/ts 项目的 store-and-forward 模式（HTTP 上传字节 + 元数据走消息面 + 按需下载）。

## Problem

1. 用户在 WebUI 看不到房间里有谁、看不到通讯 clone 之间在聊什么（`Clone.history` 纯内存，只喂 LLM）。
2. 通讯 clone 之间没有显式文件传输：文件字节只能落在 server store，永远到不了对端主机本地磁盘。

## 现状与约束

- roster 已有：hub 内存名册，client heartbeat 返回 peers。
- WebUI 只经本机 clone 代理访问数据；本机 clone 经 HubClient 访问 hub。
- consent 机制成熟（ask envelope → 属主本机 clone → 通知 → 卡片），可复用。
- envelope 面不做大载体：文件字节走 hub HTTP，消息面只传元数据。

## 一、通讯 chat 落盘（server relay 镜像）

**单写者原则：relay 投递 chat envelope 时镜像落盘。** relay 是所有 clone 间消息的必经点，无需 clone 自己上报，两端天然对称。

- 落盘位置：`data/comm/<src>__<dst>.jsonl`，一行一条 `{ts, src, dst, body}`，append-only。
- 只记 `chat` 类型；`task`/`result` 记为 `{kind:"task"}` 行（旁观时显示"委派了任务"，细节不落）。
- 进程重启不丢（server 磁盘）；`Clone.history` 维持现状（喂 LLM 用），仅不再是对话的唯一载体。

## 二、好友列表与会话视图（WebUI）

- hub 新端点 `GET /api/peers`（token 鉴权）→ 名册快照。
- 本机 clone 代理两个端点给 WebUI：
  - `GET /peers` → hub 名册（剔除本机）。
  - `GET /comm-log?host=<name>` → 读 `data/comm/<own-own>__<host>__...` 对应两个方向文件合并按 ts 排序。
- WebUI 侧栏"好友"区：在线成员列表（名字 + 在线态，随 heartbeat 刷新）。
- 点击好友 → 会话视图，**只读旁观**：渲染该对位通讯 clone 的 chat 流（含 task 卡片），不提供输入框——用户只与本机 clone 对话（核心洞见 2 不破）。

## 三、文件传输（C2，store-and-forward 落对端本地盘）

### 数据面

- hub 新端点（token 鉴权）：
  - `POST /api/transfer`（raw body，`X-Fungi-File-Name` 头带原名）→ 存 `data/transfers/<id>_<sanitized-name>`，返回 `{id, size}`。流式写盘，无大小膨胀。
  - `GET /api/transfer/<id>` → 流式回传字节，仅 transfer 记录的对端 host 可拉。
- 大小上限 `max_file_mb`（config.json，默认 200MB），超限 413。

### 控制面（复用 consent）

```
A 的通讯 clone 调 send_file("beta", "public/report.pdf", reason)
  → 1. 路径守卫检查（同 fs 工具：public/ 自由；homes/ 越界先 ask_consent）
  → 2. POST /api/transfer 上传字节 → {id, size}
  → 3. 发 transfer envelope（元数据：id/name/size/src/reason）到 beta:comm-alpha
  → 4. 对端 comm clone 收到 → ask_consent 到 beta 的本机 clone
       "alpha 想给你传文件 report.pdf (2.3MB)：收 / 拒"（支持始终允许 alpha）
  → 5. 同意 → beta host 进程 GET /api/transfer/<id> 下载
       落盘 <repo>/inbox/<alpha>/<原名>（重名加序号；basename 消毒防路径逃逸）
  → 6. result envelope 回 A："已保存到 beta inbox" / "被拒绝"
```

- 接收 consent 由**接收方用户**裁决——"远端内容写我本地盘"是必须过人的安全面，走现有 ask 卡片与 `~/.fungi/consent_rules.json` 始终允许。
- 落盘后弹系统通知（复用 notify）。
- WebUI 好友会话流中渲染 transfer 记录（文件名/大小/状态：已收/待裁决/已拒）。

## 实现清单

1. `hub/app.py`：`/api/peers`、`/api/transfer` POST/GET、relay 镜像落盘钩子。
2. `clone/tools_comm.py`：`send_file` 工具（schema + 路径守卫 + 上传 + envelope）。
3. `clone/base.py`：comm clone 处理 transfer envelope → consent → 下载落盘 → result。
4. `hub/relay.py`：chat/task 镜像写 `data/comm/*.jsonl`。
5. `clone/local.py` + `server.py`：WebUI 代理 `/peers`、`/comm-log`。
6. `web/app.js`：好友侧栏、会话只读视图、transfer 卡片。
7. `config.py`：`max_file_mb`、`inbox_dir`（默认 `<repo>/inbox`）。
8. 测试：transfer 全生命周期（consent 允许/拒绝/始终允许/超限/路径消毒）、comm-log 落盘与读取、roster 代理。

## Open Questions

- inbox 默认 `<repo>/inbox/` 是否可接受（还是放用户下载目录）？——默认仓库内，config 可改。
- transfers 存储清理策略：v1 不自动清理，手动删 `data/transfers/`；观察后再定。
