# Plan: Fungi

门禁：每阶段收尾 `scripts/check.ps1`（ruff --fix → format → 复检 → pytest）全绿后 commit（英文祈使句，本地，push 需用户发话）。

## Phase 0: Scaffold

- 建 `Harness/Fungi` 仓库：pyproject（ruff 配置照抄 YESIR）、.gitignore（config.json / data/ / sessions/）、`fungi/__init__.py`。
- 从 YESIR 移植 `agent.py llm.py trilayer.py session.py events.py config.py tools/`，原样跑通既有 pytest。
- 验收：`scripts/check.ps1` 绿；`python -m fungi "只回复两个字母: OK"` 单机降级模式可用。

## Phase 1: protocol + hub

- protocol.py envelope；hub/app.py join/heartbeat/send/poll/fs/session 端点；roster 心跳剔除；relay 投递函数；store 路径守卫。
- 验收：单测（协议校验、守卫、relay 单元）；localhost 两进程 join 后互发 chat 的集成测试。

## Phase 2: bus（Redis 协调面）

- bus/coord.py：ask 状态机（stream/hash/pub-sub）、presence、锁；fakeredis 单测。
- 验收：单测绿；WSL redis-server 冒烟脚本跑通一个 ask 全生命周期。

## Phase 3: clones

- clone/base.py inbox 循环；comm.py（send_peer/ask_consent/守卫文件工具）；local.py（WebUI 桥 + delegate/peers + 本地 ask）。
- 验收：FakeLLM 契约测试——comm clone 收 task → 回 result；consent 卡片 answer 唤醒阻塞工具。

## Phase 4: 本机集成（托盘/WebUI/通知）

- tray.py + notify.py；WebUI 默认关、托盘唤起；web/app.js consent 卡片。
- 验收：进程启动仅托盘驻留；模拟 ask → 系统通知弹出 → WebUI 打开 → 卡片回答 → clone 解除阻塞（DONGJIAN_SELFTEST 式自测钩子）。

## Phase 5: E2E

- localhost 三进程冒烟脚本（scripts/smoke_fungi.py）：server + 2 clients，FakeLLM，跑通自主交流 + consent + delegate。
- 真实 LLM 冒烟（ZAI_API_KEY 配方）；真机 LAN 手动测试清单（README 编辑需用户明示）。
- 验收：冒烟脚本绿；手动清单留待用户执行。
