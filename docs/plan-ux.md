# Implementation Plan: WebUI UX 美化（方案 B：设计语言重做 + 统一 GSAP 动效系统）

> Design: `docs/brainstorm-ux.md`（2026-09-05 用户批准 Option B）。
> 已拍板：不加 pixelation 彩蛋；不做磁吸光标；字体走系统栈。

## Component Map

```
NEW:
- web/vendor/gsap.min.js          （3.12.x core，本地化，jsdelivr 拉取一次落盘）
- web/vendor/Flip.min.js          （Flip 插件，会话/好友列表重排）
- web/motion.js                   （动效模块，唯一 GSAP 入口；可整文件删除）
- docs/plan-ux.md                 （本文件）

MODIFIED:
- web/index.html                  （<head> 加 vendor+motion 两个 <script>，其余不动）
- web/style.css                   （commit 1 重构 token；commit 2-4 配合微调）
- web/app.js                      （8 处钩子，全部 window.fungiMotion?.x?.() 可选调用）

DELETED: (none)
```

## Interface Contract（动效模块唯一接口）

```js
window.fungiMotion = {
  reduced: Boolean,          // prefers-reduced-motion 命中时 true，其余全短路
  msgIn(el, kind),           // kind: 'user'|'assistant'|'tool'|'error'|'ask'
  askCardIn(el),             // asks banner / 内联 pending 卡垂落
  askResolved(card, ok),     // 3D 翻转落章（ok=允许→印章"已放行"）
  spores(fromEl),            // 孢子粒子（文件落地）
  listFlip(container, fn),   // Flip 包装：fn() 内做 DOM 变更
  themeTo(t),                // day-night 插值核心变量；app.js 仍负责 data-theme
  ring(el, pct),             // agent 气泡进度环
  waveOn(el)/waveOff(el),    // 思考波
  counter(el, to)            // 数字递增
};
```

app.js 侧**只允许** `window.fungiMotion?.msgIn?.(...)` 形式调用——motion.js 缺失时 UI
完全静态可用（天然回退开关 + 每层 revert 不留死引用）。

## Tasks

### Task 0: vendor 本地化
**Files:** `web/vendor/gsap.min.js`、`web/vendor/Flip.min.js`
**Acceptance:** 文件存在且浏览器加载 `gsap.version` 正常；直连失败走 7897 代理拉取。
**Depends on:** none

### Task 1: Token 层重构（commit 1，纯 CSS 零 JS）
**Files:** `web/style.css`
**What:** `:root`/dark 补齐 type scale（12/13/14.5/17/22/28）、spacing 4px 栅格、
`--ease-out-expo` 等 easing token（与 GSAP 曲线一致）、shadow/surface 质感（淡 accent
渐变 + 内描边）；全选择器换算到 token。**现有变量名一个不删**（只增不改语义）。
**Acceptance:** 浏览器实测浅/深主题全部 surface（会话/好友/聊天流/三张 modal/横幅）；
截图给用户过目 → **检查点：停等确认**。
**Depends on:** none

### Task 2: 动效引擎 + 输入区 + 主题（commit 2）
**Files:** `web/motion.js`（新）、`web/index.html`、`web/app.js`（`addDiv:13`、
`renderTurnLive:582`、`applyTheme:1030`、send 按钮 wiring 处）、`web/style.css`（微调）
**What:** motion.js 骨架 + reduced-motion 守卫；`msgIn` 编排（用户右入/助手左入，流式
节点只动首帧）；liquid 发送钮；`elastic-wave` 思考指示器替换现三点/状态文字；主题
day-night（GSAP 插值 `--bg/--surface/--text/--accent` 四核心变量，其余走现有
`theme-anim` CSS transition 兜底）。
**Acceptance:** 流式聊天无卡顿（DevTools Performance 无长任务峰值）；删 motion.js 刷新
后 UI 正常；主题切换往返 5 次无残留中间态。
**Depends on:** Task 0, 1

### Task 3: consent/ask 卡与孢子（commit 3）
**Files:** `web/motion.js`、`web/app.js`（`buildActiveAskCard:686`、`answerPendingAsk:823`、
`buildPendingAskCard:773`、好友视图 transfer 落地处）
**What:** asks banner 新卡垂落；应答翻转落章；transfer 落盘成功 → `spores()` 从卡片飘向
好友行。
**Acceptance:** `FUNGI_SELFTEST=1` 全链路 + 浏览器实测 allow/ask/no 三路径动画完整、
连点不炸（动画期间按钮仍可点，无队列堆积）。
**Depends on:** Task 2

### Task 4: 列表 + 托盘 + 空状态（commit 4）
**Files:** `web/motion.js`、`web/app.js`（`renderSessionList:220`、`renderFriendList:927`、
`agentBubble:299`、空状态节点）、`web/style.css`
**What:** 会话/好友列表 Flip 重排 + light-trail 活动指示条；空状态 floating-orbs 微景；
agent 气泡轨道漂移 + 进度环。
**Acceptance:** 过滤输入实时重排无跳动；好友进出房间列表平滑；reduced-motion 下全部静止。
**Depends on:** Task 2

## Execution Strategy

- 顺序执行 0→1→2→3→4（同文件串行，无并行空间）。
- **检查点：Task 1 完成后停**，截图给用户确认 token 层观感（最激进的一步，此时回退成本最低）；
  2→4 连续执行，每 commit 后浏览器截图随交付汇报。
- 每 commit 前跑 `python -m pytest -q`（Python 侧应零影响，跑全量兜底）+ 手动浏览器回归。

## Global Constraints

1. 动效只动 `transform/opacity`（Flip 一次性读布局除外）；时长 ≤380ms；ambient 循环必须
   可暂停且 reduced-motion 全关。
2. 不阻塞输入：动画期间按钮可点、无动画队列堆积（GSAP `overwrite:'auto'`）。
3. CSS 变量名向后兼容：只增不删；`html.theme-anim` 机制保留为 fallback。
4. 不引构建工具、不引 Three.js、不改任何 Python 文件。
5. 一任务一 commit；浏览器验证按 fungi-webui-verify 配方（in-process harness + hub start，
   注意 proxy/JS 陷阱）。
