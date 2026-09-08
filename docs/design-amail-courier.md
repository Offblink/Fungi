# Design: amail（文字邮件）+ 信使开关（v2，已定稿待确认）

## Problem

跨主机通讯目前只有两条路：`send_peer`（闲聊，收方 agent 起一轮 LLM 转述）和 `send_file`（文件，收方 agent 起一轮只为推 consent 卡片）。用户想要：

1. **amail**：文字邮件。独立 WebUI 页展示（桌面+手机），未读红点，点开模态框读。
2. **信使开关**（作用于 amail / send_file / send_peer）：开=本地 comm clone 收到对面消息后转述/推卡（现状）；关=消息直达用户界面，零 agent 消耗。

## 已定决策（用户拍板）

- 邮件入口：**WebUI（桌面 app.js + 手机 m.js），不加 GUI 页**。
- 邮件界面：列表卡片式，点击开**模态框**阅读（复用现有 modal 体系）。
- 信使：**默认开**；关闭为用户主动省 token 的选择。
- 署名：信使关时对面留言进聊天流，前缀用**主机名**（如「来自 ○○ 主机」）。
- consent 卡片：信使关时 hub 直接合成同种 ask 进同一 pending-asks store，**前端零改动、样式一致**。

## 方案（Option A：hub 邮箱存储 + REST API）

邮件是 server 权威数据：`data/mail/<host>/`（per-host 邮箱，append-only jsonl），离线容忍——对方不在线邮件躺着，上线红点亮。

### 后端
1. `fungi/hub/mail.py`：Mailbox 类（`deliver/list/mark_read`；每箱上限 500 封，超出滚掉最旧）。
2. `hub/app.py` 三个端点（鉴权同 fs API）：`POST /mail`（投递）、`GET /mail/<host>`（列表+未读计数）、`POST /mail/<host>/read`（标记已读）。
3. `amail` 工具进 comm clone 工具集（tools_comm.py）：schema `host/subject/body`，返回 SENT。发信在聊天流显示工具卡片（通讯行为非隐私，用户应可见）。
4. 信使开关：config `courier: bool = True`；关时 room.py envelope 分发对 `chat`/`transfer` 直通：
   - `chat` → 直接渲染进聊天流，前缀「来自 <主机名>」；
   - `transfer` → hub 合成 consent ask 直推 pending-asks（不进收方 clone）。
   - 开关每轮重读（复用 diary 免重启模式），GUI 配置页 SwitchButton。

### 前端（两套 JS 都要动）
5. 顺手抽 `common.js`：fetch 封装 + 邮件列表卡/模态框渲染共享，app.js/m.js 只留壳（否则邮件 UI 维护两遍）。
6. server.py 静态白名单加 `/common.js` 路由（白名单式，必须同步加）。
7. 邮件页：导航入口 + 列表卡（发件人/主题/时间/未读红点）+ 点击模态框阅读 + 「标记已读」；红点计数轮询 `GET /mail/<host>`。

### 文档
8. README、gui.py HELP_SECTIONS、docs/spec.md 同步（README+帮助页是钦定两大必更文档）。

## 风险
- app.js/m.js 平行实现是历史坑（pendingAskCards 契约）：common.js 抽取只覆盖新增邮件 UI，**不**整体重构旧渲染——降低风险。
- CI 无前端覆盖：手动回归（桌面+手机宽度、无头 HTTP 静态服务验证）。
- 邮箱清理策略（500 封滚旧）先固定值，不做配置。

## 验证
- pytest：mail.py 单元（投递/列表/已读/滚旧）+ courier 关时 envelope 直通路由。
- 前端：无头起 server，浏览器开 `/` 与 `/m`，发测试邮件→红点→模态框阅读→标记已读。
