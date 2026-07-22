# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

麦麦恋人（MaiLover）是 MaiBot 的私聊专用虚拟恋人插件，基于 **MaiBot Plugin SDK ≥ 2.5.0**。核心思路：日程定骨架 + 概率定节奏 + 情绪定温度。

## 关键约束

- **所有与主程序通信必须走 SDK**（`self.ctx.*`），绝不直接访问 MaiBot 内部模块/数据库。
- **主动发言统一走 planner 触发**（v2.0.0 架构）：调用 `ctx.maisaka.proactive.trigger(intent, reason)` 而不是插件自己调 LLM + `ctx.send.text`。planner 自主决定是否发言、说什么。
- **插件 ID**：`maibot-community.mai-love`。在 `_manifest.json` 和 API 调用中保持一致。
- **数据目录**：插件根目录下的 `data/` 子目录。运行时数据（`affection_memory.json`、`schedule_cache.json`）写入此处，不被 git 追踪。模板文件 `mai_template.json` 在插件根目录（不在 data/ 下）。

## 命令

```bash
# 运行所有测试
pytest tests/ -v

# 运行单个测试文件
pytest tests/test_scheduler_regressions.py -v

# 运行单个测试函数
pytest tests/test_scheduler_regressions.py::test_cooldown_uses_latest_user_message -v
```

没有 build/lint/typecheck 步骤 — 这是 MaiBot 插件，由 MaiBot 运行时加载。

## 架构

### 模块依赖图

```
plugin.py (入口: MaiLoverPlugin)
├── config.py          — Pydantic WebUI 配置模型 (MaiLoverPluginSettings)
├── scheduler.py       — 巡检调度引擎 (两个 asyncio 循环)
├── schedule_generator.py — 每日日程生成 + 当前活动查询
├── affection_manager.py  — 好感度档位、发言计数、标记持久化 (节流 I/O)
├── llm_service.py     — LLM 调用封装 (ctx.llm.generate)
├── message_service.py — 消息发送 + 情绪后缀
├── holiday_service.py — 节假日 API (timor.tech) + 本地降级
├── memory_manager.py  — 用户习惯存储 (委托 AffectionManager.habits)
└── constants.py       — Prompt 模板 + 情绪后缀池
```

### plugin.py 是唯一的 SDK 接触面

`MaiLoverPlugin(MaiBotPlugin)` 是唯一直接与 SDK 交互的类，组装所有子模块并管理生命周期。它注册：

- **HookHandler**（3 个）：`chat.receive.after_process`（记录私聊时间）、`maisaka.planner.before_request`（注入麦麦当前活动到 extra_prompt）、`maisaka.replyer.after_response`（补计发言数）
- **Tool**（6 个）：供 planner 自主调用的工具函数
- **API**（3 个）：供其他插件调用的公开接口
- **Command**（6 个）：用户斜杠命令

其他模块只接受具体依赖注入（ctx / config / 子模块实例），不 import SDK。

### v2.0.0 触发流程

```
scheduler._tick() → _trigger_planner(intent, reason)
  → ctx.maisaka.proactive.trigger(stream_id, intent, reason)
  → planner 自主决策是否发言
  → Hook: plugin.on_planner_before_request 注入"麦麦正在做XX"
  → planner 生成回复
  → Hook: plugin.on_replyer_after_response 补计 0.5 发言数
```

发言计数采用 0.5 + 0.5 模式：`_trigger_planner` 先计 0.5，`on_replyer_after_response` 检测 90 秒内的 trigger 后补计 0.5。只有 trigger 入队成功才累计。

### Scheduler 两级循环

1. **`_daily_generation_loop`**：在 `_stop_event` 上等待到每天 `generate_hour` 时刻 → 调用 `schedule_gen.generate_daily_schedule()` → 日切重置
2. **`_patrol_loop`**（需要 stream_id）：按 `check_interval_minutes` 间隔调用 `_tick()`

`_tick()` 的优先级顺序：静默检查 → S 级（早安/晚安，强制触发免冷却）→ A 级（想念，6 条件全满足）→ B 级（日程节点匹配 / 日常巡检，需过概率+冷却+上限）。

### 配置热重载

`on_config_update(scope, config_data, version)` — `self.config` 已由 SDK 自动更新。`scope="bot"` 时特殊处理：刷新人设缓存（`ctx.config.get("personality.personality")`）。其他 scope 下停止旧调度器并用新配置重启。

### stream_id 解析

`plugin.py:_resolve_stream_id()` 使用两条 SDK 路径：
1. `ctx.chat.get_stream_by_user_id(user_id)` — 首选
2. `ctx.chat.get_private_streams()` — 遍历降级

失败时启动后台重试协程（退避 5/10/20/30/60 秒），日程生成循环不依赖 stream_id，始终启动。

### 配置模型层级

`MaiLoverPluginSettings` 聚合 6 个 `PluginConfigBase` 子模型：`PluginConfig` → `WhitelistConfig` → `ScheduleConfig` → `ProbabilityConfig` → `TimeWindowsConfig` → `AffectionConfig`。每个子模型有 `__ui_label__` 和 `__ui_order__` 控制 WebUI 渲染。

### 好感度数据流

`AffectionManager` 使用 JSON 文件持久化，带节流写入（距上次 <1s 跳过）。`reset_daily()`、`flush()`、`on_unload()` 走强制立即写入。发言计数使用 float（支持 0.5 增量）。

## 测试

- 框架：`pytest`
- `conftest.py`：mock `maibot_sdk` 模块（因为 SDK 在 MaiBot 外部不可用）
- 测试使用 `object.__new__(Scheduler)` 绕过 `__init__`，手动注入 mock 依赖
- 时间窗口方法均为 `@staticmethod`，可直接单元测试无需实例化

## 依赖

- **Python ≥ 3.10**
- **MaiBot Plugin SDK ≥ 2.5.0**（不装在开发环境，由 MaiBot 运行时提供）
- **httpx** — 节假日 API 调用（唯一外部依赖）
- **pydantic** — 配置模型（随 SDK 安装）

`requirements.txt` 仅列出 `httpx>=0.24.0`。SDK 不需要在 requirements 中声明。
