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
