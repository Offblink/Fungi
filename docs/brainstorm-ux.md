# Brainstorm: WebUI UX 美化（结合 Gasp-Design 组件库）

> 2026-09-05。素材：`useful/基于LLM/Skill/Gasp-Design`（37 个自包含 GSAP 3.12 + Three.js 0.174 动效组件）。
> 目标对象：`web/index.html` + `web/style.css`（26KB）+ `web/app.js`（46KB）。

## Problem

WebUI 功能完整（会话流、工具卡片、consent 卡、好友旁观、agent 气泡、双主题），但视觉层是
"能用"级别：入场动画只有一条 CSS `msgIn` 关键帧，状态变化（consent 切换、文件落地、任务
委派）没有反馈动效，主题切换是整页 0.4s 淡淡的 crossfade，空状态/加载态是纯文字。用户要求
结合 Gasp-Design 彻头彻尾美化 UX。

## Context

- **现有动效资产**：`msgIn` 入场、agent 气泡 `breath` 呼吸、按钮 scale 微动、主题 crossfade。
  其余全是静态。
- **技术约束**：无构建工具（纯 HTML/JS/CSS）；`marked` 已走 jsdelivr CDN——但 WebUI 常在
  **无外网的 LAN joiner 机器**上打开，GSAP 必须落 `web/vendor/` 本地（gsap.min.js ~70KB +
  Flip ~10KB，比 Three.js 600KB 便宜两个量级）。
- **主题**：CSS 变量驱动（浅粉 `#ec4899` / 暗青 `#2dd4bf`），`html.theme-anim` 交叉渐变机制
  已有，必须保留变量体系不动。
- **性能**：聊天流是高频重绘区（流式 markdown），动画必须在 `prefers-reduced-motion` 下
  可关、不得给每条消息常驻 rAF。
- **红线**：UX 违背直觉零容忍——动效不得拖慢操作（过渡 ≤400ms，阻塞交互的动画禁止）；
  263 个 Python 测试不测 JS，但 DOM 结构大改会碰 `app.js` 渲染函数，需手动回归。

## Options Considered

### Option A: 微动效点缀（现有皮肤 + GSAP 交互层）

- **How**：DOM/CSS 不动，新增 `web/motion.js`（单文件，gsap 本地 vendored），只在关键点
  挂 GSAP：消息入场编排、send 按钮液态反馈、consent 卡翻转、agent 气泡进度环、数字计数器。
- **Pros**：一天内完成；每个效果独立可回退；零 DOM 结构风险。
- **Cons**：天花板低——信息层级、排版、空状态、质感原样保留，称不上"彻头彻尾"。
- **Risk**：几乎为零。最坏情况：动效显得贴皮（CSS keyframes 与 GSAP 双轨并存，动效语言不统一）。

### Option B: 设计语言重做 + 统一 GSAP 动效系统（推荐）

- **How**：三层重做，一 commit 一层可单独回退：
  1. **Token 层**（style.css 重构为变量体系）：完整 type scale（12/13/14.5/17/22/28）、
     spacing 4px 栅格、radius/shadow/easing token（`--ease-out-expo` 等与 GSAP 同曲线）、
     表面质感（surface 上再叠极淡 accent 渐变 + 内描边）。DOM 不动，纯 CSS。
  2. **动效层**（motion.js，GSAP 统一引擎，替换散落的 CSS keyframes）：
     - 消息编排：用户消息右入、助手消息左入 + 流式期间 caret 光晕；工具卡片展开用弹性曲线。
     - **consent 卡 = Gasp `flip-drag-reorder` 的 Flip 技法**：允许/询问切换时卡片 3D 翻转
       落章（"已放行" 印章缩放盖下）；asks banner 新卡从横幅弹性垂落。
     - 文件落地：`floating-orbs` 孢子粒子从卡片飘向好友头像（真菌身份梗）。
     - 会话列表：过滤/置顶重排用 **Flip** 布局过渡；活动项左侧 light-trail 指示条。
     - 主题切换：`day-night-cycle` 的日月轨迹 + 背景色相插值，替代现 0.4s crossfade
       （GSAP 驱动 `--bg` 等 4 个核心变量插值，其余变量仍走 CSS transition）。
     - 发送按钮 `liquid-button`；思考指示器 `elastic-wave`（替代三点跳动）；
       token/会话计数 `number-counter`。
     - agent 气泡：idle 时缓慢轨道漂移，运行中叠 `scroll-progress-ring` 式进度环。
  3. **状态层**：空状态（无会话/无好友）配 `floating-orbs` 微景 + 一句引导；加载/重连态
       顶部细光带（`light-trail`）。
- **Pros**：动效语言统一（单一 GSAP ticker、统一 easing token）；真菌身份贯穿（孢子/菌丝
  光带）；三层各自可回退；不引 Three.js，体积可控。
- **Cons**：动量最大——style.css 全量重构 + app.js 渲染函数加钩子约 6-8 处；需要两轮
  真机/浏览器回归。
- **Risk**：流式重绘与 GSAP tween 打架（消息 append 频繁）→ 对策：只对"新节点首帧"做
  enter 动画，流式更新走纯 CSS；`reduced-motion` 一刀切跳过。

### Option C: 沉浸 3D 层（Three.js 点云 + 着色器）

- **How**：背景 `pointcloud` 孢子场、主题切换 `shader-distortion`、agent modal `disassembly-3d`。
- **Pros**：观感冲击最强，演示效果拉满。
- **Cons**：Three.js ~600KB 本地化；日常聊天工具里是性能税与注意力税；低配 joiner 机器
  风扇起飞；对"违背直觉零容忍"红线最危险。
- **Risk**：高。作为日常生产力 UI 是负资产。
- **判定**：不作为基础方案。Option B 完成后若想要一个"哇"点，可单加主题切换的
  `pixelation-transition`（2 行内联 shader 成本）——单列 open question。

## Recommended: Option B

理由：A 达不到"彻头彻尾"的要求；C 违背 LAN 生产力工具的性能直觉；B 用 GSAP 一个引擎
吃到 Gasp-Design 的编排精华（Flip/light-trail/orbs/day-night），体积 +80KB，三层分离
每层可独立回退（git revert 单 commit 即可，符合"不满意随时回退"）。

### Implementation outline

1. `web/vendor/gsap.min.js` + `web/vendor/Flip.min.js` 本地化（jsdelivr 拉取一次落盘）。
2. commit 1：`style.css` token 重构（纯 CSS，零 JS 改动）→ 浏览器全页面回归。
3. commit 2：`motion.js` 消息编排 + 输入区 + 主题 day-night → 手动回归流式聊天。
4. commit 3：consent 卡 Flip 落章 + asks banner 垂落 + 孢子落地粒子。
5. commit 4：会话列表 Flip + light-trail + 空状态微景 + agent 气泡进度环。
6. 每步 `prefers-reduced-motion` 全量旁路；`app.js` 钩子以 `window.fungiMotion?.xxx` 可选
   调用（motion.js 缺失时 UI 完全正常——天然回退开关）。

## Open Questions

1. 主题切换是否要加 `pixelation-transition` 彩蛋（Option C 的唯一残留）？默认不加。
2. 磁吸光标 / 鼠标聚光灯这类桌面级炫技默认不做（生产力工具注意力成本），同意吗？
3. 字体是否引入本地化中文字体文件（体现 type scale），还是继续系统字体栈只调字号层级？
   默认后者（零体积成本）。
