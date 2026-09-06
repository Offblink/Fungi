# 移动端 WebUI 设计（2026-09-06）

目标：手机扫码进入专用移动网页端；电脑端继续走「打开 WebUI」。扫码/点按钮天然分流。

## 决策（已与用户确认）

| 分叉 | 决策 |
|---|---|
| 二维码入口 | GUI 新页面「手机端」，位置：加入房间之下、模型配置之上。不放在桌面 WebUI 里（用户不想先进 WebUI） |
| 局域网安全 | token 门禁：非 loopback 请求必须带 `?t=<token>`，否则 403。本机访问不受影响 |
| 功能范围 | 全功能：聊天 + 会话抽屉 + ask/consent 卡片 + 好友视图 + agent tray + 文件上传，分切片交付 |
| 实现方式 | 独立 `web/m.html` + `web/m.css` + `web/m.js`，复用现有后端 API；不改造桌面 app.js |

## URL 与 token

- 服务端首次启动时生成 token 并持久化到 `~/.fungi/webui_token`（重启不变，手机书签长期有效）。
- WebUI server 绑定从 `127.0.0.1` 改为 `0.0.0.0`（hub 先例）。
- 二维码内容：`http://<LAN-IP>:<port>/m?t=<token>`。LAN IP 复用 `__main__.py` 的 `_lan_ip()` 思路（UDP connect 探测，失败回落 127.0.0.1）。
- `m.js` 首次从 URL query 取 `t` 存 localStorage，之后所有 fetch/`/events` 请求自动附带 `?t=`；收到 403 → 清 token 显示「请重新扫码」遮罩。
- 新端点 `GET /lan` → `{ip, port, token, url}`（本机 loopback 才返回 token；非 loopback 请求不回显 token）。

### 鉴权规则

- 请求源 IP 是 loopback（127.0.0.1/::1）→ 放行（桌面 WebUI、GUI 自检零改动）。
- 非 loopback → 校验 `t` 参数（GET query / POST 由前端一律拼 query）；不匹配 403。
- 静态文件同样受门禁：`m.html` 本身就要 token 才能拿到（token 在 URL 里，无额外交互）。

## 服务端改动（fungi/server.py）

1. `make_webui_server` 绑 `0.0.0.0`；token 懒生成 + 持久化。
2. `do_GET`/`do_POST` 入口统一过 `_authorized(self)` 门禁。
3. 静态路由加 `/m`、`/m.css`、`/m.js`（沿用 `_send_static`）。
4. `GET /lan` 端点。
5. 现有 API 全部不动（/chat /events /sessions /session /asks /peers /comm-log /consent-mode /answer /stop /retry /upload /model /config-status）。

## GUI：手机端页（fungi/gui.py）

- `MobilePage(QWidget)`，`addSubInterface` 顺序插在 join_page 与 cfg_page 之间（图标 `FluentIcon.QRCODE` 若可用，否则 CAMERA/SCAN 兜底）。
- 进入页面（或点「生成二维码」）时若 WebUI 未启动则懒启动（`room.open_webui(open_browser=False)`），随后：
  - segno（纯 Python、零依赖）生成 PNG bytes → QPixmap → 居中显示（白底留 quiet zone，深色前景）；
  - 下方显示可复制的 URL 文本 + 「刷新 IP」按钮（换网络后重测 LAN IP）；
  - 未发起/未加入房间时显示提示文案「先发起或加入房间」。
- 依赖：`segno` 加进 pyproject（GUI 运行时依赖；exe 打包随之带入）。
- 页面提示一行防火墙说明：连不上时检查 Windows 防火墙入站规则（Public 配置文件常拦 Python）。

## 移动端页面设计（m.html/m.css/m.js）

### 布局与手势（用户规格）

- 首页 = 当前会话聊天页。**右划（手指从左边缘向右）→ 会话列表抽屉**：抽屉宽度 = 视口 2/3，停在右侧露出 1/3 主页面；**左滑收回**。抽屉上方为主页面半透明暗罩，点击暗罩也收回。
- 跟手拖拽 + GSAP 松手惯性回弹（速度阈值 + 位移阈值判定开/合）；CSS `will-change: transform`，仅 transform/opacity 动画。
- gasp-design 细节：抽屉列表项 stagger 入场、发送按钮按压微缩、流式尾部光标呼吸、新会话按钮弹性、ask 卡片入场翻动。

### 功能切片

1. **MVP**：消息流式渲染（复用桌面的事件契约：text/reasoning/tool/done/status/error）、会话抽屉（新建/切换/删除/重命名暂缓——重命名先不做，桌面独有）、停止（⏹ 替换发送键）、`/events` 断线重连、token 处理。
2. **ask 卡片**：`pollPendingAsks` 同款 3s 轮询 + 卡片重挂契约（Map 注册 + 重绘后重挂，见 skill://fungi-webui-card-contract）——移动端同样必须遵守，否则卡片被流式重绘吞掉。
3. **好友视图**：抽屉里好友分组 + `/comm-log` 只读会话 + consent 滑条（allow/ask）。
4. **agent tray + 上传**：子代理气泡列表 + 点开模态；`/upload` 走 `<input type=file>`。
5. 明确不做：config 弹窗（API key 在 GUI 配）、文件浏览按钮复用上传、Alt+R 重试改成长按消息重试（后续再议，首版不做）。

### 视觉

- 沿用桌面主题变量（浅色默认/深色切换，localStorage 同键 `fungi-theme`），viewport 基础字号 16px 防 iOS 缩放；触控目标 ≥ 44px；`100dvh` 布局 + `env(safe-area-inset-*)`。
- 输入区固定底部；键盘弹出用 `visualViewport` 监听补偿（iOS Safari 必踩）。

## 测试与验证

- server：`tests/test_room.py` 风格新增用例——token 门禁（伪造非 loopback：单元级测 `_authorized`）、`/lan`、`/m` 静态路由；现有 localhost 测试必须保持全绿。
- GUI：offscreen 冒烟（MobilePage 可实例化、按钮存在）。
- 端到端：browser-act 开 `http://127.0.0.1:<port>/m?t=...` 断言抽屉手势、流式渲染、卡片跨重绘存活；真机手机扫码验证（防火墙放行后）。
- 一切片一 commit。

## 风险

- Windows 防火墙拦入站 → GUI 页提示 + 文档记录；真机联调时实测。
- iOS Safari 长按选择/橡皮筋滚动与手势冲突 → `touch-action` 与 `user-select` 按区域控制。
- `_TURN_TAPES` 内存上限未实测（既有已知项），移动端长时间挂后台重连会加重它——不在本切片解决，记录观察。

## 实施结果（2026-09-06，HEAD f6cf578+）

全部切片已交付：25b7a37（server 门禁+lan+m 路由）、5ea6c57（GUI MobilePage+segno）、2eeeac7（磁带 bug 修复）、f6cf578（移动端前端）、后续好友/agent tray commit。

- **好友视图**：抽屉内「好友」分组（/peers 5s 轮询）；点开 = 只读 comm-log + 顶栏「←」返回 + 权限分段控件（允许/询问，POST /consent-mode）。
- **agent tray**：agent_spawn/status/event → 输入区上方横向气泡（状态色点）；点开底部模态看 goal/status/最近 30 条事件。
- **文件上传取消**：桌面的「选择文件」调 /pickfile（服务端 tkinter 对话框选**电脑**文件），手机触发会在电脑上弹窗，语义不成立；跨主机传文件走 hub transfer（agent 工具），不经过 WebUI。移动端不设文件入口。
- **验证中抓到并修复的既有 bug**（2eeeac7）：磁带 60s grace-pop 按会话 id 无世代 pop——上一回合 done 后 60s 内同会话开新回合，新回合运行中磁带被弹掉，刷新重连拿到裸 done 静默丢失直播视图（桌面同样中招）。修复：pop 按磁带对象身份校验；grace 提为 `_TAPE_GRACE_S` 供测试。
- **前端坑**：scroll-bottom 按钮必须放在 `#messages` 外层（全量重绘 innerHTML='' 会销毁它，桌面靠 `if (!b) return` 掩盖成功能缺失而非崩溃；移动端曾因此 TypeError 吞掉 reattach）。
- **验证**：280 tests 全绿（+7）；门控 harness + browser-act 实测 token 门禁（LAN 无 token 403 / 带 token 200 / loopback 免检）、抽屉合成触摸手势开合、流式回合、done 落点、中途刷新重连接续、agent 气泡+模态。真机手机扫码待用户实测（Windows 防火墙可能需放行 Python 入站）。

## 真机首扫反馈修复（2026-09-06 晚）

手机扫码后弹「链接已失效」遮罩。根因**不是 token 不一致**（GUI 二维码与 `~/.fungi/webui_token` 同源，房间 Token 与 WebUI t= 是两回事，GUI 已加说明文字），而是：`m.html` 引用的 `/m.css`、`/m.js`、`/vendor/*` 是写死路径无法带 token，手机（非 loopback）全部 403 → CSS 丢失使遮罩失去 `display:none` 直接露出、JS 丢失页面死掉。修复：静态壳资源（页面/css/js/vendor）豁免门禁——壳里没有数据，无 token 打开 `/m` 时 m.js 正常运行并显示有样式的重扫码遮罩；数据端点保持全门禁；`/vendor/..` 穿越仍 403。同时：marked 从 jsdelivr CDN vendor 化到 `/vendor/marked.min.js`（国内手机网络 CDN 不可达会让 m.js 首行 ReferenceError 全页死掉，桌面 index.html 一并改本地）；遮罩改为默认可见、有 token 才隐藏（JS 挂掉也显示有意义提示）；连接被手机 reset 的 10054 噪音 traceback 由 `WebUIServer.handle_error` 吞掉。
