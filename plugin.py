"""麦麦恋人（MaiLover）插件入口

MaiBot 私聊专用虚拟恋人插件（NapCat 适配器）。
核心逻辑：日程定骨架 + 概率定节奏 + 情绪定温度。

v2.0.0 架构变更：
- 主动发言统一触发 planner（ctx.maisaka.proactive.trigger），
  不再自行调 LLM + send.text
- 新增 planner.before_request Hook，注入麦麦当前活动状态
- 新增 mai_lover_current_activity Tool，供 planner 查询麦麦在干嘛
- 人设缓存：on_load 时读取 ctx.config.get("personality.personality")，
  on_config_update(scope="bot") 时刷新
- 删除关键词拦截（hook_handler.py），消息正常放行给 planner

v2.1.0: 多用户支持
- 支持配置多个目标 QQ（target_qqs 列表）
- 每用户独立数据目录 data/<qq>/
- 每用户独立调度器、好感度、日程
- 每用户可选 config_override.json 覆盖全局配置
- Hook/Tool/Command/API 均按 stream_id 路由到正确用户

导出 create_plugin() 函数返回 MaiLoverPlugin 实例。
"""

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Iterable, Optional

from maibot_sdk import API, Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .affection_manager import AffectionManager
from .config import (
    MaiLoverPluginSettings,
    load_user_config_overrides,
    merge_config_with_overrides,
)
from .holiday_service import HolidayService
from .llm_service import LLMService
from .memory_manager import MemoryManager
from .message_service import MessageService
from .schedule_generator import ScheduleGenerator
from .scheduler import Scheduler


# ── Per-User Data Structures ────────────────────────────────────────────


@dataclass
class UserContext:
    """单个目标用户的所有运行时状态。

    每个 QQ 用户拥有独立的子模块实例和数据目录，
    调度器、好感度、日程互不干扰。
    """

    qq: str
    data_dir: str
    stream_id: str = ""
    affection_mgr: Optional[AffectionManager] = None
    memory_mgr: Optional[MemoryManager] = None
    message_svc: Optional[MessageService] = None
    schedule_gen: Optional[ScheduleGenerator] = None
    scheduler: Optional[Scheduler] = None
    effective_config: Any = None  # MaiLoverPluginSettings (merged)
    stream_retry_task: Optional[asyncio.Task[Any]] = None


class UserRegistry:
    """目标用户注册表。

    维护 QQ ↔ UserContext 的双向映射（按 QQ 和按 stream_id），
    供 Hook/Tool/Command 快速路由到正确用户。
    """

    def __init__(self) -> None:
        self._by_qq: dict[str, UserContext] = {}
        self._by_stream_id: dict[str, UserContext] = {}

    def register(self, ctx: UserContext) -> None:
        """注册用户上下文。"""
        self._by_qq[ctx.qq] = ctx
        if ctx.stream_id:
            self._by_stream_id[ctx.stream_id] = ctx

    def unregister(self, qq: str) -> None:
        """注销用户上下文（移除双向映射）。"""
        ctx = self._by_qq.pop(qq, None)
        if ctx and ctx.stream_id:
            self._by_stream_id.pop(ctx.stream_id, None)

    def get_by_qq(self, qq: str) -> Optional[UserContext]:
        """按 QQ 号查找用户上下文。"""
        return self._by_qq.get(qq)

    def get_by_stream_id(self, stream_id: str) -> Optional[UserContext]:
        """按 stream_id 反向查找用户上下文。"""
        if not stream_id:
            return None
        return self._by_stream_id.get(stream_id)

    def get_all(self) -> list[UserContext]:
        """获取所有已注册用户上下文。"""
        return list(self._by_qq.values())

    def update_stream_id(self, qq: str, new_stream_id: str) -> None:
        """更新用户的 stream_id 映射（先移除旧映射再注册新映射）。"""
        ctx = self._by_qq.get(qq)
        if ctx is None:
            return
        old_sid = ctx.stream_id
        if old_sid:
            self._by_stream_id.pop(old_sid, None)
        ctx.stream_id = new_stream_id
        if new_stream_id:
            self._by_stream_id[new_stream_id] = ctx

    def get_first(self) -> Optional[UserContext]:
        """获取第一个用户上下文（API 回退用）。"""
        for ctx in self._by_qq.values():
            return ctx
        return None


# ── Plugin Entry ────────────────────────────────────────────────────────


class MaiLoverPlugin(MaiBotPlugin):
    """麦麦恋人插件主类。

    组装所有模块，管理插件生命周期：
    - on_load: 初始化所有子模块，缓存人设，为每个用户启动调度器
    - on_unload: 停止所有调度器，刷新好感度数据
    - on_config_update: 热重载配置，scope="bot" 时刷新人设
    - on_planner_before_request: 注入麦麦当前活动到 planner extra_prompt
    - Tools: mai_lover_status / mai_lover_schedule / mai_lover_send_message /
             mai_lover_affection / mai_lover_config / mai_lover_current_activity
    - Commands: /mai_status / /mai_schedule / /mai_affection / /mai_help /
                /mai_config / /mai_test
    """

    config_model = MaiLoverPluginSettings

    # 订阅主程序 bot 配置热重载（含 personality.personality）
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("bot",)

    def __init__(self) -> None:
        super().__init__()
        self._users: UserRegistry = UserRegistry()
        self._llm_svc: Optional[LLMService] = None
        self._holiday_svc: Optional[HolidayService] = None
        self._cached_personality: str = ""

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """初始化插件：为每个目标 QQ 创建独立的用户上下文和调度器。"""
        base_data_dir = self._get_data_dir()
        self.ctx.logger.info(f"MaiLover 数据目录: {base_data_dir}")
        os.makedirs(base_data_dir, exist_ok=True)

        if not self.config.plugin.enabled:
            self.ctx.logger.info("MaiLover 插件已禁用（plugin.enabled=false），跳过初始化")
            return

        # 创建共享服务（无状态，所有用户共用）
        self._llm_svc = LLMService(self.ctx, self.config)
        self._holiday_svc = HolidayService(self.config)

        # 读取主程序人格配置并缓存
        await self._refresh_personality()

        # 获取有效 QQ 列表
        effective_qqs = self.config.whitelist.get_effective_qqs()
        if not effective_qqs:
            self.ctx.logger.warning("未配置任何目标 QQ，插件不会主动触发。请在配置中填写 target_qqs。")
            return

        self.ctx.logger.info(f"目标用户: {effective_qqs}")

        # 迁移旧单用户数据（如有）
        self._migrate_single_user_data(base_data_dir, effective_qqs)

        # 为每个 QQ 创建独立上下文
        for qq_int in effective_qqs:
            qq = str(qq_int)
            await self._init_user(qq)

        self.ctx.logger.info(
            f"MaiLover 插件加载完成，共 {len(self._users.get_all())} 个用户"
        )

    async def on_unload(self) -> None:
        """卸载插件：停止所有调度器，刷新好感度持久化。"""
        self.ctx.logger.info("MaiLover 插件正在卸载...")
        for user_ctx in self._users.get_all():
            if user_ctx.stream_retry_task is not None:
                user_ctx.stream_retry_task.cancel()
                user_ctx.stream_retry_task = None
            if user_ctx.scheduler is not None:
                user_ctx.scheduler.stop()
            if user_ctx.affection_mgr is not None:
                user_ctx.affection_mgr.flush()
        self.ctx.logger.info("MaiLover 插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        """热重载。

        self.config 已由 MaiBot 自动更新为最新值。
        scope="bot" 时额外刷新人设缓存（主程序人格配置变更）。
        其他 scope 时对比新旧 QQ 列表，增删用户并重启调度器。
        """
        self.ctx.logger.info(f"配置热更新: scope={scope}, version={version}")

        if scope == "bot":
            await self._refresh_personality()
            for user_ctx in self._users.get_all():
                if user_ctx.scheduler is not None:
                    user_ctx.scheduler.set_personality(self._cached_personality)
            self.ctx.logger.info("主程序人设已同步，无需重启 MaiLover 调度器")
            return None

        if not self.config.plugin.enabled:
            for user_ctx in self._users.get_all():
                if user_ctx.scheduler is not None:
                    user_ctx.scheduler.stop()
            return None

        # 更新共享服务配置引用
        if self._llm_svc is not None:
            self._llm_svc._config = self.config
        if self._holiday_svc is not None:
            self._holiday_svc._config = self.config

        # 对比新旧 QQ 列表
        new_qqs = {str(q) for q in self.config.whitelist.get_effective_qqs()}
        old_qqs = {ctx.qq for ctx in self._users.get_all()}

        removed = old_qqs - new_qqs
        added = new_qqs - old_qqs
        kept = old_qqs & new_qqs

        for qq in removed:
            self.ctx.logger.info(f"用户 {qq} 已从配置中移除，正在清理...")
            self._teardown_user(qq)

        for qq in kept:
            await self._reload_user(qq)

        for qq in added:
            self.ctx.logger.info(f"新增用户 {qq}，正在初始化...")
            await self._init_user(qq)

        self.ctx.logger.info("配置热更新完成")
        return None

    # ── HookHandler ────────────────────────────────────────────────────

    @HookHandler(
        "chat.receive.after_process",
        name="mai_lover_target_private_message_observer",
        description="记录白名单用户的私聊时间，供主动聊天冷却和想念机制使用",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_target_private_message(
        self, message: dict[str, Any] | None = None, **kwargs: Any
    ) -> None:
        """记录目标用户的入站私聊；群聊和其他用户不影响恋人调度。"""
        del kwargs
        user_ctx = self._resolve_user_from_message(message)
        if user_ctx is None or user_ctx.affection_mgr is None:
            return

        user_ctx.affection_mgr.update_last_user_msg_time(datetime.now())
        self.ctx.logger.debug(f"已更新用户 {user_ctx.qq} 最后私聊时间")

    @HookHandler("maisaka.planner.before_request")
    async def on_planner_before_request(self, **kwargs: Any) -> dict[str, Any]:
        """注入麦麦当前活动状态到 planner 的 extra_prompt。

        按 stream_id 查找对应用户，注入该用户的当前活动。
        非目标用户的 planner 请求则跳过注入。

        每次 planner 请求时触发（含用户正常回复和 proactive trigger），
        让麦麦在任何时候都知道自己在做什么。

        追加方式（非覆盖）：在已有 extra_prompt 后拼接状态后缀。
        """
        user_ctx = self._resolve_user_from_kwargs(kwargs)
        if user_ctx is None or user_ctx.schedule_gen is None:
            return {"action": "continue", "modified_kwargs": kwargs}

        # 防护：kwargs 过大时跳过注入（避免触发主程序帧大小限制）
        try:
            import json as _json
            kwargs_size = len(_json.dumps(kwargs, default=str, ensure_ascii=False))
            if kwargs_size > 1_000_000:  # 1MB
                self.ctx.logger.warning(
                    f"planner kwargs 过大 ({kwargs_size} bytes)，跳过活动注入"
                )
                return {"action": "continue", "modified_kwargs": kwargs}
        except Exception:
            pass

        now = datetime.now()
        current_time = now.strftime("%H:%M")
        activity = user_ctx.schedule_gen.get_current_activity(now)
        suffix = f"\n【麦麦当前状态】现在 {current_time}，麦麦正在{activity}。"

        # 追加非覆盖
        kwargs["extra_prompt"] = (kwargs.get("extra_prompt") or "") + suffix
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler("maisaka.replyer.after_response", mode=HookMode.OBSERVE)
    async def on_replyer_after_response(self, **kwargs: Any) -> None:
        """planner 实际生成回复时，补计 0.5 触发数。

        与 _trigger_planner 的 0.5 配合：回复成功总计 1.0，未回复总计 0.5。
        仅在最近 90 秒内有 proactive trigger 时生效，避免误匹配用户消息的回复。
        按 stream_id 路由到正确用户。
        """
        user_ctx = self._resolve_user_from_kwargs(kwargs)
        if user_ctx is None or user_ctx.scheduler is None or user_ctx.affection_mgr is None:
            return

        last_trigger = user_ctx.scheduler.get_last_trigger_time()
        if last_trigger is None:
            return  # 没有待确认的 proactive trigger

        now = datetime.now()
        # 90 秒窗口内的回复视为对 proactive trigger 的响应
        if (now - last_trigger).total_seconds() < 90:
            user_ctx.affection_mgr.increment_speak(0.5)  # 补计 0.5
            user_ctx.scheduler.clear_last_trigger_time()
            self.ctx.logger.debug(f"replyer 回复 detected (user={user_ctx.qq})，补计 0.5 触发数")

    # ── Tools (LLM 可主动调用) ─────────────────────────────────────────

    @Tool(
        name="mai_lover_current_activity",
        description="查询麦麦现在正在做什么。当用户问'在干嘛''在做什么'或想了解麦麦当前状态时调用。",
    )
    async def tool_mai_lover_current_activity(self, **kwargs: Any) -> str:
        """LLM Tool: 查询麦麦当前活动。"""
        user_ctx = self._resolve_user_from_kwargs(kwargs)
        if user_ctx is None or user_ctx.schedule_gen is None:
            return "麦麦还未连接到目标用户。"
        activity = user_ctx.schedule_gen.get_current_activity(datetime.now())
        return f"麦麦现在正在{activity}。"

    @Tool(
        name="mai_lover_status",
        description="查看麦麦恋人的当前状态：好感度档位、今日发言次数、日程摘要",
    )
    async def tool_mai_lover_status(self, **kwargs: Any) -> str:
        """LLM Tool: 查看麦麦状态。"""
        user_ctx = self._resolve_user_from_kwargs(kwargs)
        return self._build_status_report(user_ctx)

    @Tool(
        name="mai_lover_schedule",
        description="查看麦麦的今日完整日程安排",
    )
    async def tool_mai_lover_schedule(self, **kwargs: Any) -> str:
        """LLM Tool: 查看今日日程。"""
        user_ctx = self._resolve_user_from_kwargs(kwargs)
        return self._build_schedule_report(user_ctx)

    @Tool(
        name="mai_lover_send_message",
        description="以麦麦恋人的口吻向用户主动发送一条恋人消息",
        parameters={
            "message": {
                "type": "string",
                "description": "要发送的消息文本（可选，留空则自动生成）",
                "required": False,
            },
        },
    )
    async def tool_mai_lover_send_message(
        self, message: str = "", **kwargs: Any
    ) -> str:
        """LLM Tool: 主动发送恋人消息。"""
        user_ctx = self._resolve_user_from_kwargs(kwargs)
        if user_ctx is None:
            return "麦麦还没有连接到目标用户，请稍后再试。"
        stream_id = user_ctx.stream_id
        if not stream_id:
            return "麦麦还没有连接到目标用户，请稍后再试。"
        if user_ctx.message_svc is None or self._llm_svc is None:
            return "消息或 LLM 服务未初始化，无法发送。"

        if not message:
            message = await self._llm_svc.generate_or_fallback(
                prompt="请用温柔恋人的口吻说一句问候或分享一件小事（20-40字）。",
                fallback="想你了呢~在忙什么呀？",
                system_prompt="你是一个温柔体贴的虚拟恋人「麦麦」。",
                temperature=0.8,
            )

        final_text = user_ctx.message_svc.append_affection_suffix(message)
        try:
            result = await self.ctx.send.text(text=final_text, stream_id=stream_id)
            if result:
                if user_ctx.affection_mgr:
                    user_ctx.affection_mgr.increment_speak()
                return f"消息已发送: {final_text[:60]}..."
            return f"消息发送失败: 发送返回 False"
        except Exception as e:
            self.ctx.logger.error(f"[tool_mai_lover_send_message] 发送异常: {e}")
            return f"消息发送异常: {e}"

    @Tool(
        name="mai_lover_affection",
        description="调整麦麦恋人的好感度档位。0=熟悉（温柔拘谨），1=亲密（活泼热情），2=热恋（撒娇抱抱）",
        parameters={
            "level": {
                "type": "integer",
                "description": "好感度档位，只能填 0、1 或 2",
                "required": True,
            },
        },
    )
    async def tool_mai_lover_affection(self, level: int = 0, **kwargs: Any) -> str:
        """LLM Tool: 调整好感度。"""
        user_ctx = self._resolve_user_from_kwargs(kwargs)
        if user_ctx is None or user_ctx.affection_mgr is None:
            return "好感度管理器未初始化。"
        if level not in (0, 1, 2):
            return f"档位只能填 0（熟悉）、1（亲密）或 2（热恋），收到的是 {level}。"
        user_ctx.affection_mgr.update_level(level)
        descs = {0: "熟悉（温柔拘谨）", 1: "亲密（活泼热情）", 2: "热恋（撒娇抱抱）"}
        self.ctx.logger.info(f"好感度已通过 Tool 调整为 {level} (user={user_ctx.qq})")
        return f"好感度已更新: {level} - {descs[level]}"

    @Tool(
        name="mai_lover_config",
        description="查看麦麦恋人当前插件配置：巡检间隔、时间窗口、概率、今日发言上限、好感度",
    )
    async def tool_mai_lover_config(self, **kwargs: Any) -> str:
        """LLM Tool: 查看配置。"""
        user_ctx = self._resolve_user_from_kwargs(kwargs)
        if user_ctx is not None and user_ctx.effective_config is not None:
            cfg = user_ctx.effective_config
        else:
            cfg = self.config
        s = cfg.schedule
        t = cfg.time_windows
        p = cfg.probability
        a = cfg.affection
        return (
            f"⚙️ 麦麦配置: "
            f"巡检间隔 {s.check_interval_minutes}min | "
            f"每日上限 {s.daily_max_speak} 条 | "
            f"冷却 {s.user_cooldown_minutes}min | "
            f"触发开关 {'开' if s.proactive_trigger_enabled else '关'} | "
            f"早安 {t.morning_start}~{t.morning_end} | "
            f"晚安 {t.night_start}~{t.night_end} | "
            f"想念触发 >{t.miss_trigger_hours}h | "
            f"日常概率 {p.default_speak_rate} | "
            f"想念概率 {p.miss_speak_rate} | "
            f"日程节点概率 {p.activity_trigger_rate} | "
            f"好感度 {a.current_level} | "
            f"模型 {cfg.plugin.llm_model}"
        )

    # ── API (供其他插件调用) ────────────────────────────────────────────

    @API(
        name="get_current_activity",
        description="获取麦麦当前正在做什么。返回活动描述字符串。可指定 user_id。",
        version="1",
        public=True,
    )
    async def api_get_current_activity(self, **kwargs: Any) -> str:
        """API: 获取麦麦当前活动。"""
        user_ctx = self._resolve_user_from_api(kwargs.get("user_id", ""))
        if user_ctx is None or user_ctx.schedule_gen is None:
            return ""
        return user_ctx.schedule_gen.get_current_activity(datetime.now())

    @API(
        name="get_schedule",
        description="获取麦麦今日完整日程。返回节点列表 [{time, activity}]。可指定 user_id。",
        version="1",
        public=True,
    )
    async def api_get_schedule(self, **kwargs: Any) -> list[dict[str, Any]]:
        """API: 获取今日日程。"""
        user_ctx = self._resolve_user_from_api(kwargs.get("user_id", ""))
        if user_ctx is None or user_ctx.schedule_gen is None:
            return []
        today_str = datetime.now().strftime("%Y-%m-%d")
        return user_ctx.schedule_gen.load_cached_schedule(today_str)

    @API(
        name="get_affection_level",
        description="获取当前好感度档位。返回 0/1/2。可指定 user_id。",
        version="1",
        public=True,
    )
    async def api_get_affection_level(self, **kwargs: Any) -> int:
        """API: 获取好感度档位。"""
        user_ctx = self._resolve_user_from_api(kwargs.get("user_id", ""))
        if user_ctx is None or user_ctx.affection_mgr is None:
            return 0
        return user_ctx.affection_mgr.level()

    # ── Commands (用户手动交互) ────────────────────────────────────────

    @Command(name="/mai_status", pattern=r"^/mai_status\b", description="查看麦麦恋人状态（好感度、发言计数、日程摘要）")
    async def cmd_mai_status(self, **kwargs: Any) -> tuple[bool, str, int]:
        """查看麦麦恋人状态。主动发送报告给用户并拦截消息。"""
        stream_id = str(kwargs.get("stream_id", ""))
        user_ctx = self._resolve_user_from_kwargs(kwargs)
        report = self._build_status_report(user_ctx)
        try:
            await self.ctx.send.text(text=report, stream_id=stream_id)
            return True, "状态已发送", 2
        except Exception as e:
            self.ctx.logger.error(f"cmd_mai_status 发送失败: {e}")
            return False, f"发送失败: {e}", 2

    @Command(name="/mai_schedule", pattern=r"^/mai_schedule\b", description="查看麦麦今日完整日程")
    async def cmd_mai_schedule(self, **kwargs: Any) -> tuple[bool, str, int]:
        """查看麦麦今日完整日程。主动发送报告给用户并拦截消息。"""
        stream_id = str(kwargs.get("stream_id", ""))
        user_ctx = self._resolve_user_from_kwargs(kwargs)
        report = self._build_schedule_report(user_ctx)
        try:
            await self.ctx.send.text(text=report, stream_id=stream_id)
            return True, "日程已发送", 2
        except Exception as e:
            self.ctx.logger.error(f"cmd_mai_schedule 发送失败: {e}")
            return False, f"发送失败: {e}", 2

    @Command(name="/mai_affection", pattern=r"^/mai_affection\b", description="调整好感度档位。用法: /mai_affection <0|1|2>")
    async def cmd_mai_affection(self, **kwargs: Any) -> tuple[bool, str, int]:
        """调整好感度档位。解析参数、发送确认/用法消息给用户并拦截消息。"""
        stream_id = str(kwargs.get("stream_id", ""))
        user_ctx = self._resolve_user_from_kwargs(kwargs)
        raw_message = str(kwargs.get("text", "")).strip()
        parts = raw_message.split()
        if len(parts) < 2:
            current = user_ctx.affection_mgr.level() if (user_ctx and user_ctx.affection_mgr) else "?"
            usage = (
                f"用法: /mai_affection <0|1|2>\n"
                f"0 = 熟悉（温柔拘谨）\n"
                f"1 = 亲密（活泼热情）\n"
                f"2 = 热恋（撒娇抱抱）\n"
                f"当前档位: {current}"
            )
            try:
                await self.ctx.send.text(text=usage, stream_id=stream_id)
            except Exception as e:
                self.ctx.logger.error(f"cmd_mai_affection 用法发送失败: {e}")
            return False, "参数错误", 2
        level_str = parts[1]
        try:
            level = int(level_str)
        except (ValueError, TypeError):
            msg = f"「{level_str}」不是有效数字，请使用 0、1 或 2。"
            try:
                await self.ctx.send.text(text=msg, stream_id=stream_id)
            except Exception as e:
                self.ctx.logger.error(f"cmd_mai_affection 错误提示发送失败: {e}")
            return False, "参数错误", 2
        if level not in (0, 1, 2):
            msg = f"好感度档位只能是 0（熟悉）、1（亲密）或 2（热恋），收到的是 {level}。"
            try:
                await self.ctx.send.text(text=msg, stream_id=stream_id)
            except Exception as e:
                self.ctx.logger.error(f"cmd_mai_affection 错误提示发送失败: {e}")
            return False, "参数错误", 2
        if user_ctx is None or user_ctx.affection_mgr is None:
            msg = "好感度管理器未初始化，无法调整。"
            try:
                await self.ctx.send.text(text=msg, stream_id=stream_id)
            except Exception as e:
                self.ctx.logger.error(f"cmd_mai_affection 错误提示发送失败: {e}")
            return False, "管理器未初始化", 2
        user_ctx.affection_mgr.update_level(level)
        descs = {0: "熟悉（温柔拘谨）", 1: "亲密（活泼热情）", 2: "热恋（撒娇抱抱）"}
        self.ctx.logger.info(f"好感度已通过命令调整为 {level} (user={user_ctx.qq})")
        confirm = f"好感度已更新: {level} - {descs.get(level, '未知')}"
        try:
            await self.ctx.send.text(text=confirm, stream_id=stream_id)
            return True, "好感度已调整", 2
        except Exception as e:
            self.ctx.logger.error(f"cmd_mai_affection 确认发送失败: {e}")
            return False, f"发送失败: {e}", 2

    @Command(name="/mai_help", pattern=r"^/mai_help\b", description="查看麦麦恋人所有可用命令")
    async def cmd_mai_help(self, **kwargs: Any) -> tuple[bool, str, int]:
        """查看所有可用命令。主动发送帮助文本给用户并拦截消息。"""
        stream_id = str(kwargs.get("stream_id", ""))
        help_text = (
            "🐱 麦麦恋人 可用命令:\n"
            "/mai_status    — 查看麦麦状态（好感度/今日发言/日程摘要）\n"
            "/mai_schedule  — 查看今日完整日程\n"
            "/mai_affection — 调整好感度档位: /mai_affection <0|1|2>\n"
            "/mai_config    — 查看当前插件配置摘要\n"
            "/mai_test      — 发送一条测试消息（验证发送通道）"
        )
        try:
            await self.ctx.send.text(text=help_text, stream_id=stream_id)
            return True, "帮助已发送", 2
        except Exception as e:
            self.ctx.logger.error(f"cmd_mai_help 发送失败: {e}")
            return False, f"发送失败: {e}", 2

    @Command(name="/mai_config", pattern=r"^/mai_config\b", description="查看麦麦恋人当前配置摘要")
    async def cmd_mai_config(self, **kwargs: Any) -> tuple[bool, str, int]:
        """查看当前配置摘要。主动发送配置信息给用户并拦截消息。"""
        stream_id = str(kwargs.get("stream_id", ""))
        user_ctx = self._resolve_user_from_kwargs(kwargs)
        if user_ctx is not None and user_ctx.effective_config is not None:
            cfg = user_ctx.effective_config
        else:
            cfg = self.config
        s = cfg.schedule
        t = cfg.time_windows
        p = cfg.probability
        a = cfg.affection
        summary = (
            f"⚙️ 麦麦配置摘要\n"
            f"调度: 巡检间隔 {s.check_interval_minutes}min | "
            f"每日上限 {s.daily_max_speak} 条 | "
            f"冷却 {s.user_cooldown_minutes}min | "
            f"触发开关 {'开启' if s.proactive_trigger_enabled else '关闭'}\n"
            f"时间窗口: 早安 {t.morning_start}~{t.morning_end} | "
            f"晚安 {t.night_start}~{t.night_end} | "
            f"想念触发 >{t.miss_trigger_hours}h\n"
            f"概率: 日常巡检 {p.default_speak_rate} | "
            f"想念 {p.miss_speak_rate} | "
            f"日程节点 {p.activity_trigger_rate}\n"
            f"好感度: {a.current_level} | "
            f"模型: {cfg.plugin.llm_model}"
        )
        try:
            await self.ctx.send.text(text=summary, stream_id=stream_id)
            return True, "配置已发送", 2
        except Exception as e:
            self.ctx.logger.error(f"cmd_mai_config 发送失败: {e}")
            return False, f"发送失败: {e}", 2

    @Command(name="/mai_test", pattern=r"^/mai_test\b", description="发送一条测试消息以验证发送通道")
    async def cmd_mai_test(self, **kwargs: Any) -> tuple[bool, str, int]:
        """发送测试消息验证发送通道。优先使用 Command 传入的 stream_id。"""
        stream_id = str(kwargs.get("stream_id", ""))
        if not stream_id:
            # 回退到第一个用户的 stream_id
            first = self._users.get_first()
            if first is not None:
                stream_id = first.stream_id
        if not stream_id:
            return False, "暂无 stream_id", 2
        try:
            result = await self.ctx.send.text(
                text="麦麦测试消息~ 发送通道正常 ✅",
                stream_id=stream_id,
            )
            if result:
                return True, "测试消息发送成功", 2
            return False, "发送返回 False", 2
        except Exception as e:
            return False, f"发送异常: {e}", 2

    # ── Status / Schedule Reports ──────────────────────────────────────

    def _build_status_report(self, user_ctx: Optional[UserContext] = None) -> str:
        """构造麦麦状态报告文本。

        Args:
            user_ctx: 目标用户上下文。为 None 时显示提示信息。
        """
        if user_ctx is None or user_ctx.affection_mgr is None:
            return "麦麦恋人插件尚未初始化完成，或未找到对应用户。"

        level_names: dict[int, str] = {0: "熟悉", 1: "亲密", 2: "热恋"}
        level = user_ctx.affection_mgr.level()
        speak_count = user_ctx.affection_mgr.today_speak_count()
        cfg = (
            user_ctx.effective_config
            if user_ctx.effective_config is not None
            else self.config
        )
        daily_max = cfg.schedule.daily_max_speak
        morning_ok = user_ctx.affection_mgr.morning_sent_today()
        night_ok = user_ctx.affection_mgr.night_sent_today()
        miss_ok = user_ctx.affection_mgr.miss_sent_today()
        today_str = datetime.now().strftime("%Y-%m-%d")

        lines: list[str] = [
            f"❤️ 麦麦恋人状态 ({today_str})",
            f"目标用户: {user_ctx.qq}",
            f"好感度档位: {level} - {level_names.get(level, '未知')}",
            f"今日触发: {speak_count}/{daily_max}",
            f"早安: {'✅已触发' if morning_ok else '❌未触发'}  |  "
            f"晚安: {'✅已触发' if night_ok else '❌未触发'}  |  "
            f"想念: {'✅已触发' if miss_ok else '❌未触发'}",
        ]

        # 日程摘要
        if user_ctx.schedule_gen:
            schedule = user_ctx.schedule_gen.load_cached_schedule(today_str)
            if schedule:
                lines.append(f"今日日程 ({len(schedule)} 个节点):")
                for node in schedule[:5]:
                    t = node.get("time", "??:??")
                    activity = node.get("activity", "未知")
                    lines.append(f"  [{t}] 🐱 {activity}")
                if len(schedule) > 5:
                    lines.append(f"  ... 还有 {len(schedule) - 5} 个节点")
            else:
                lines.append("今日暂无日程缓存。")

        return "\n".join(lines)

    def _build_schedule_report(self, user_ctx: Optional[UserContext] = None) -> str:
        """构造今日完整日程报告文本。

        Args:
            user_ctx: 目标用户上下文。为 None 时显示提示信息。
        """
        today_str = datetime.now().strftime("%Y-%m-%d")

        if user_ctx is None or user_ctx.schedule_gen is None:
            return "日程生成器未初始化，或未找到对应用户。"

        schedule = user_ctx.schedule_gen.load_cached_schedule(today_str)
        cfg = (
            user_ctx.effective_config
            if user_ctx.effective_config is not None
            else self.config
        )
        if not schedule:
            return (
                f"📅 今日 ({today_str}) 暂无日程缓存。\n"
                f"可能尚未生成，请等待下次凌晨 {cfg.schedule.generate_hour}:00 自动生成。"
            )

        lines: list[str] = [
            f"📅 麦麦今日日程 ({today_str})",
            f"目标用户: {user_ctx.qq}",
            "",
        ]
        for node in schedule:
            t = node.get("time", "??:??")
            activity = node.get("activity", "未知")
            lines.append(f"  [{t}] 🐱 {activity}")

        return "\n".join(lines)

    # ── Internal Helpers ───────────────────────────────────────────────

    async def _refresh_personality(self) -> None:
        """从主程序配置刷新人设缓存并同步给所有调度器。

        读取 ctx.config.get("personality.personality") 并缓存到
        self._cached_personality，同时同步给所有 scheduler（若已创建）。
        """
        try:
            self._cached_personality = await self.ctx.config.get(
                "personality.personality", ""
            )
        except Exception as e:
            self.ctx.logger.warning(f"读取人设配置失败: {e}")
            self._cached_personality = ""
        for user_ctx in self._users.get_all():
            if user_ctx.scheduler is not None:
                user_ctx.scheduler.set_personality(self._cached_personality)
        self.ctx.logger.debug(
            f"人设缓存已刷新: {self._cached_personality[:50]}..."
        )

    def _get_data_dir(self) -> str:
        """获取插件数据目录路径。

        数据写入插件目录下的 data/ 子目录，避免插件更新（git pull）时
        覆盖用户运行时数据（affection_memory.json、schedule_cache.json）。

        Returns:
            数据目录绝对路径。
        """
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

    # ── User Context Management ────────────────────────────────────────

    async def _init_user(self, qq: str) -> UserContext:
        """为一个 QQ 创建完整的用户上下文并启动调度器。

        创建独立的子模块实例、加载配置覆盖、解析 stream_id、启动调度器。
        """
        base_data_dir = self._get_data_dir()
        user_data_dir = os.path.join(base_data_dir, qq)
        os.makedirs(user_data_dir, exist_ok=True)

        # 加载用户独立配置覆盖并合并
        overrides = load_user_config_overrides(user_data_dir)
        effective_config = merge_config_with_overrides(self.config, overrides)
        if overrides:
            self.ctx.logger.info(f"用户 {qq} 已加载配置覆盖: {list(overrides.keys())}")

        # 创建该用户的子模块
        affection_mgr = AffectionManager(user_data_dir)
        affection_mgr.update_level(effective_config.affection.current_level)
        memory_mgr = MemoryManager(affection_mgr)
        message_svc = MessageService(self.ctx, effective_config, affection_mgr)
        schedule_gen = ScheduleGenerator(
            user_data_dir, effective_config, self._llm_svc, self._holiday_svc
        )

        scheduler = Scheduler(
            self.ctx, effective_config, affection_mgr, schedule_gen,
        )
        scheduler.set_personality(self._cached_personality)

        user_ctx = UserContext(
            qq=qq,
            data_dir=user_data_dir,
            affection_mgr=affection_mgr,
            memory_mgr=memory_mgr,
            message_svc=message_svc,
            schedule_gen=schedule_gen,
            scheduler=scheduler,
            effective_config=effective_config,
        )
        self._users.register(user_ctx)

        # 启动该用户的调度器
        await self._start_scheduler_for_user(user_ctx)
        return user_ctx

    def _teardown_user(self, qq: str) -> None:
        """停止一个用户的调度器、取消重试任务、刷新数据、注销。"""
        user_ctx = self._users.get_by_qq(qq)
        if user_ctx is None:
            return

        if user_ctx.stream_retry_task is not None:
            user_ctx.stream_retry_task.cancel()
            user_ctx.stream_retry_task = None

        if user_ctx.scheduler is not None:
            user_ctx.scheduler.stop()

        if user_ctx.affection_mgr is not None:
            user_ctx.affection_mgr.flush()

        self._users.unregister(qq)
        self.ctx.logger.info(f"用户 {qq} 已清理")

    async def _reload_user(self, qq: str) -> None:
        """重载一个用户的配置并重启调度器（热更新时使用）。"""
        user_ctx = self._users.get_by_qq(qq)
        if user_ctx is None:
            return

        base_data_dir = self._get_data_dir()
        user_data_dir = os.path.join(base_data_dir, qq)

        # 重新加载配置覆盖并合并
        overrides = load_user_config_overrides(user_data_dir)
        effective_config = merge_config_with_overrides(self.config, overrides)

        # 更新用户上下文中的配置引用
        user_ctx.effective_config = effective_config
        if user_ctx.scheduler is not None:
            user_ctx.scheduler._config = effective_config
        if user_ctx.schedule_gen is not None:
            user_ctx.schedule_gen._config = effective_config
        if user_ctx.message_svc is not None:
            user_ctx.message_svc._config = effective_config

        # 更新好感度档位
        if user_ctx.affection_mgr is not None:
            user_ctx.affection_mgr.update_level(effective_config.affection.current_level)
            user_ctx.affection_mgr.flush()

        # 重启调度器
        if user_ctx.scheduler is not None:
            user_ctx.scheduler.stop()
        await self._start_scheduler_for_user(user_ctx)

    async def _start_scheduler_for_user(self, user_ctx: UserContext) -> None:
        """解析 stream_id 并为单个用户启动调度器。

        stream_id 解析失败时启动后台重试协程。
        日程生成循环不依赖 stream_id，始终启动。
        """
        if user_ctx.scheduler is None:
            self.ctx.logger.error(f"用户 {user_ctx.qq} 调度器未初始化，无法启动")
            return

        if user_ctx.stream_retry_task is not None:
            user_ctx.stream_retry_task.cancel()
            user_ctx.stream_retry_task = None

        # 先启动 scheduler（日程生成循环不依赖 stream_id）
        user_ctx.scheduler.set_target(user_ctx.qq, "")  # stream_id 暂空
        await user_ctx.scheduler.start()

        # 解析 stream_id
        stream_id = await self._resolve_stream_id(user_ctx.qq)
        if stream_id:
            self._users.update_stream_id(user_ctx.qq, stream_id)
            user_ctx.scheduler.set_target(user_ctx.qq, stream_id)
            await user_ctx.scheduler.start_patrol()
            self.ctx.logger.info(
                f"用户 {user_ctx.qq} 就绪, stream_id: {stream_id}"
            )
        else:
            self.ctx.logger.warning(
                f"用户 {user_ctx.qq} 无法获取 stream_id，"
                f"日程生成已启动但巡检暂不可用。将在后台重试解析..."
            )
            user_ctx.stream_retry_task = asyncio.create_task(
                self._retry_stream_id(user_ctx)
            )

    async def _resolve_stream_id(self, target_qq: str) -> str:
        """获取目标 QQ 的私聊 stream_id（纯 SDK 路径）。

        使用 SDK 提供的 chat 代理，不直接访问 MaiBot 内部数据库。
        所有层级均做 try/except 降级保护。

        Args:
            target_qq: 目标 QQ 号。

        Returns:
            stream_id 字符串，获取失败返回空字符串。
        """
        # 方法1: get_stream_by_user_id（自动匹配已注册的适配器）
        try:
            stream_info = await self.ctx.chat.get_stream_by_user_id(
                user_id=target_qq
            )
            if isinstance(stream_info, dict) and stream_info.get("stream_id"):
                sid = str(stream_info["stream_id"])
                self.ctx.logger.debug(f"从 get_stream_by_user_id 获取 stream_id: {sid}")
                return sid
        except Exception as e:
            self.ctx.logger.warning(f"get_stream_by_user_id 失败: {e}")

        # 方法2: 遍历 get_private_streams
        try:
            streams = await self.ctx.chat.get_private_streams()
            if isinstance(streams, list):
                for s in streams:
                    if isinstance(s, dict) and str(s.get("user_id", "")) == str(target_qq):
                        sid = str(s.get("stream_id", ""))
                        self.ctx.logger.debug(f"从 get_private_streams(list) 获取 stream_id: {sid}")
                        return sid
            elif isinstance(streams, dict):
                for key, s in streams.items():
                    if isinstance(s, dict) and str(s.get("user_id", "")) == str(target_qq):
                        sid = str(s.get("stream_id", key))
                        self.ctx.logger.debug(f"从 get_private_streams(dict) 获取 stream_id: {sid}")
                        return sid
        except Exception as e:
            self.ctx.logger.error(f"get_private_streams 失败: {e}")

        return ""

    async def _retry_stream_id(self, user_ctx: UserContext) -> None:
        """后台重试解析单个用户的 stream_id。

        启动阶段使用短退避重试，避免每次重启后固定失效 5 分钟。
        """
        retry_delays = (5, 10, 20, 30, 60)
        attempt = 0
        while True:
            try:
                delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                await asyncio.sleep(delay)
                stream_id = await self._resolve_stream_id(user_ctx.qq)
                if stream_id:
                    self._users.update_stream_id(user_ctx.qq, stream_id)
                    if user_ctx.scheduler is not None:
                        user_ctx.scheduler.set_target(user_ctx.qq, stream_id)
                        await user_ctx.scheduler.start_patrol()
                    self.ctx.logger.info(
                        f"用户 {user_ctx.qq} stream_id 重试成功: {stream_id}，巡检循环已就绪"
                    )
                    user_ctx.stream_retry_task = None
                    return
                else:
                    attempt += 1
                    self.ctx.logger.debug(
                        f"用户 {user_ctx.qq} stream_id 重试失败，"
                        f"{retry_delays[min(attempt, len(retry_delays) - 1)]} 秒后再次重试"
                    )
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.ctx.logger.error(f"用户 {user_ctx.qq} stream_id 重试异常: {e}")

    # ── User Resolution Helpers ────────────────────────────────────────

    def _resolve_user_from_message(self, message: Any) -> Optional[UserContext]:
        """从入站消息提取 user_id 并查找用户上下文。

        Args:
            message: 入站消息字典。

        Returns:
            匹配的 UserContext，或 None（非目标用户/群聊/通知）。
        """
        if not isinstance(message, dict) or message.get("is_notify"):
            return None
        message_info = message.get("message_info")
        if not isinstance(message_info, dict) or message_info.get("group_info"):
            return None
        user_info = message_info.get("user_info")
        if not isinstance(user_info, dict):
            return None
        user_id = str(user_info.get("user_id", "")).strip()
        if not user_id:
            return None
        return self._users.get_by_qq(user_id)

    def _resolve_user_from_kwargs(self, kwargs: dict[str, Any]) -> Optional[UserContext]:
        """从 hook/tool/command 的 kwargs 中提取 stream_id 并查找用户上下文。

        支持多种可能的 key 名称以兼容不同 SDK 版本。

        Args:
            kwargs: 回调参数字典。

        Returns:
            匹配的 UserContext，或 None。
        """
        stream_id = ""
        for key in ("stream_id", "streamId", "chat_stream_id"):
            val = kwargs.get(key, "")
            if val:
                stream_id = str(val).strip()
                break
        if stream_id:
            return self._users.get_by_stream_id(stream_id)
        return None

    def _resolve_user_from_api(self, user_id: Any) -> Optional[UserContext]:
        """从 API 调用参数解析目标用户。

        优先使用传入的 user_id 查找，未指定则回退到第一个用户。

        Args:
            user_id: API 调用方传入的 user_id 参数。

        Returns:
            匹配的 UserContext，或 None。
        """
        if user_id:
            uid = str(user_id).strip()
            if uid:
                ctx = self._users.get_by_qq(uid)
                if ctx is not None:
                    return ctx
        return self._users.get_first()

    # ── Migration ──────────────────────────────────────────────────────

    def _migrate_single_user_data(
        self, base_data_dir: str, effective_qqs: list[int]
    ) -> None:
        """将旧版单用户数据（data/ 根目录）迁移到 data/<qq>/ 子目录。

        仅在旧数据文件存在且目标目录不存在时迁移。
        """
        old_affection = os.path.join(base_data_dir, "affection_memory.json")
        old_schedule = os.path.join(base_data_dir, "schedule_cache.json")

        if not os.path.exists(old_affection) and not os.path.exists(old_schedule):
            return  # 无旧数据，无需迁移

        if not effective_qqs:
            return

        # 使用第一个 QQ 作为迁移目标
        qq = str(effective_qqs[0])
        new_dir = os.path.join(base_data_dir, qq)

        # 目标目录已有数据 → 跳过迁移
        if os.path.exists(os.path.join(new_dir, "affection_memory.json")):
            self.ctx.logger.info(f"用户 {qq} 已有独立数据，跳过迁移")
            return

        os.makedirs(new_dir, exist_ok=True)

        migrated = 0
        if os.path.exists(old_affection):
            shutil.copy2(old_affection, os.path.join(new_dir, "affection_memory.json"))
            migrated += 1
            self.ctx.logger.info(f"已迁移 affection_memory.json → {new_dir}/")

        if os.path.exists(old_schedule):
            shutil.copy2(old_schedule, os.path.join(new_dir, "schedule_cache.json"))
            migrated += 1
            self.ctx.logger.info(f"已迁移 schedule_cache.json → {new_dir}/")

        if migrated > 0 and len(effective_qqs) > 1:
            self.ctx.logger.warning(
                f"多个目标 QQ ({effective_qqs}) 但仅有一份旧数据。"
                f"已将数据迁移到 {qq}/，其他用户将从零开始。"
            )

        self.ctx.logger.info(f"旧数据迁移完成 ({migrated} 个文件)")


def create_plugin() -> MaiBotPlugin:
    """MaiBot 插件工厂函数。"""
    return MaiLoverPlugin()
