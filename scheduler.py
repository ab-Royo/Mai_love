"""巡检调度引擎 - 核心模块

v2.0.0: 主动发言从「插件自行调 LLM + send.text」重构为「统一触发 planner」。
- S级：早安/晚安检查 → _trigger_morning / _trigger_night
- A级：想念机制（6 条件全部满足）→ _trigger_missing
- B级：日程节点匹配 / 日常巡检 → _trigger_activity / _trigger_daily

所有触发统一走 _trigger_planner → ctx.maisaka.proactive.trigger，
planner 自主决策是否发言、说什么。
"""

import asyncio
import random
from datetime import datetime, timedelta
from typing import Any, Optional

from .affection_manager import AffectionManager
from .config import MaiLoverPluginSettings
from .schedule_generator import ScheduleGenerator


class Scheduler:
    """巡检调度引擎。

    管理两个后台循环：
    - _daily_generation_loop: 每日凌晨生成日程
    - _patrol_loop: 按配置间隔执行巡检

    v2.0.0: 依赖精简为 4 个（ctx + config + affection + schedule_gen），
    不再直接调 LLM 和 send.text，统一通过 _trigger_planner 触发 planner。
    """

    def __init__(
        self,
        ctx: Any,
        config: MaiLoverPluginSettings,
        affection_manager: AffectionManager,
        schedule_generator: ScheduleGenerator,
    ) -> None:
        """初始化调度器。

        Args:
            ctx: MaiBot PluginContext 实例。
            config: 插件强类型配置模型。
            affection_manager: 好感度管理器。
            schedule_generator: 日程生成器。
        """
        self._ctx: Any = ctx
        self._config: MaiLoverPluginSettings = config
        self._affection: AffectionManager = affection_manager
        self._schedule_gen: ScheduleGenerator = schedule_generator
        self._stop_event: asyncio.Event = asyncio.Event()
        self._target_qq: str = ""
        self._stream_id: str = ""
        self._personality: str = ""
        self._last_trigger_time: Optional[datetime] = None
        self._generation_in_progress: bool = False
        self._daily_loop_task: Optional[asyncio.Task[Any]] = None
        self._patrol_loop_task: Optional[asyncio.Task[Any]] = None

    def set_target(self, target_qq: str, stream_id: str) -> None:
        """设置白名单目标用户。

        Args:
            target_qq: 目标 QQ 号。
            stream_id: 聊天流 ID。
        """
        self._target_qq = target_qq
        self._stream_id = stream_id

    def set_personality(self, personality: str) -> None:
        """设置麦麦人设性格文本。

        由 plugin 在 on_load 和 on_config_update 时传入，
        用于日程生成时融入人设性格。

        Args:
            personality: 人设性格文本。
        """
        self._personality = personality

    def get_last_trigger_time(self) -> Optional[datetime]:
        """返回上次成功 proactive trigger 的时间。"""
        return self._last_trigger_time

    def clear_last_trigger_time(self) -> None:
        """清除上次触发时间（replyer Hook 补计后调用）。"""
        self._last_trigger_time = None

    async def start(self) -> None:
        """启动调度引擎。

        日程生成循环不依赖 stream_id，始终启动。
        巡检循环需要 stream_id 才能触发 proactive trigger，无 stream_id 时跳过。

        防重复生成机制（三层防护）：
        1. 内存级 _generation_in_progress 标记 — 阻止同进程并发生成
        2. 日期标记文件 .schedule_generated — 跨重启快速判断
        3. schedule_cache.json 回退检查 — 兼容旧数据
        """
        self._stop_event.clear()  # 重置停止标志，支持 stop() 后重新 start()
        self._ctx.logger.info(f"Scheduler 启动，目标用户: {self._target_qq or '(未设置)'}")

        # 取消旧的后台任务（防止 stop/start 快速切换时残留重复任务）
        self._cancel_loop_tasks()

        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        # 三层检查：内存标记 → 日期标记文件 → 缓存 JSON
        if self._generation_in_progress:
            self._ctx.logger.info("日程生成正在进行中（由另一个 start 调用触发），跳过重复生成")
        elif self._schedule_gen.is_generated_today(today_str):
            self._ctx.logger.debug(f"{today_str} 日程已生成（标记文件或缓存命中），跳过生成")
        else:
            self._ctx.logger.info("今日无日程缓存，立即生成")
            self._generation_in_progress = True
            try:
                await self._schedule_gen.generate_daily_schedule(
                    today_str, self._personality
                )
            except Exception as e:
                self._ctx.logger.error(f"立即生成日程失败: {e}")
            finally:
                self._generation_in_progress = False

        # 检查是否需要重置每日计数（复用 now）
        if self._affection.today_date() != today_str:
            self._affection.reset_daily(today_str)

        # 日程生成循环始终启动（不依赖 stream_id）
        self._daily_loop_task = asyncio.create_task(self._daily_generation_loop())

        # 巡检循环需要 stream_id
        if self._stream_id:
            self._patrol_loop_task = asyncio.create_task(self._patrol_loop())
            self._ctx.logger.info("巡检循环已启动")
        else:
            self._ctx.logger.warning("无 stream_id，巡检循环未启动（日程生成不受影响）")

    async def start_patrol(self) -> None:
        """单独启动巡检循环（用于 stream_id 延迟获取后补启）。"""
        if not self._stream_id:
            self._ctx.logger.warning("无 stream_id，无法启动巡检循环")
            return
        # 取消旧的巡检任务（如有）
        if self._patrol_loop_task is not None and not self._patrol_loop_task.done():
            self._patrol_loop_task.cancel()
        self._patrol_loop_task = asyncio.create_task(self._patrol_loop())
        self._ctx.logger.info("巡检循环已启动（延迟补启）")

    async def _daily_generation_loop(self) -> None:
        """每日凌晨唤醒，生成当日日程。

        在 _stop_event 上等待到下一个 generate_hour 时刻，
        然后生成日程、重置每日计数，循环往复。
        """
        generate_hour = self._config.schedule.generate_hour

        while not self._stop_event.is_set():
            now = datetime.now()
            next_run = now.replace(
                hour=generate_hour, minute=0, second=0, microsecond=0
            )
            if now >= next_run:
                # 已过今天凌晨，等到明天凌晨
                next_run = next_run + timedelta(days=1)

            wait_seconds = (next_run - now).total_seconds()
            self._ctx.logger.debug(
                f"下次日程生成时间: {next_run}，等待 {wait_seconds:.0f} 秒"
            )

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=max(wait_seconds, 1),
                )
                # 如果 wait 被 set 触发（非超时），说明是 stop 信号
                if self._stop_event.is_set():
                    break
            except asyncio.TimeoutError:
                # 超时 = 到达生成时间
                pass

            if self._stop_event.is_set():
                break

            # 执行日程生成
            date_str = datetime.now().strftime("%Y-%m-%d")
            self._ctx.logger.info(f"开始生成 {date_str} 的日程")
            try:
                await self._schedule_gen.generate_daily_schedule(
                    date_str, self._personality
                )
            except Exception as e:
                self._ctx.logger.error(f"日程生成失败: {e}")

            # 日切由 _tick 中 today_date 检测统一负责，此处不再 reset
            # 避免 generate_hour 落在白天时覆盖已发送的早安/晚安状态
            if self._affection.today_date() != date_str:
                self._affection.reset_daily(date_str)

    async def _patrol_loop(self) -> None:
        """巡检循环。

        按 check_interval_minutes 间隔执行 _tick()。
        """
        check_interval = self._config.schedule.check_interval_minutes
        self._ctx.logger.info(f"巡检循环启动，间隔 {check_interval} 分钟")

        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as e:
                self._ctx.logger.error(f"巡检 _tick 异常: {e}")

            # 等待下一次巡检
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=check_interval * 60,
                )
                # 如果 wait 被 set 触发（非超时），说明是 stop 信号
                if self._stop_event.is_set():
                    break
            except asyncio.TimeoutError:
                # 超时 = 下一轮巡检
                continue

    async def _tick(self) -> None:
        """单次巡检，严格按优先级执行。

        优先级顺序：
        1. S级：早安/晚安检查（强制性，跳过概率和冷却检查）
        2. A级：想念机制（6 条件）
        3. B级：日程节点匹配 / 日常巡检（概率 + 冷却 + 上限）

        所有触发统一走 _trigger_planner → ctx.maisaka.proactive.trigger，
        planner 自主决策是否发言、说什么。所有时间相关操作复用同一个 now。
        """
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        current_date = now.strftime("%Y-%m-%d")

        # 确保当日数据已重置
        if self._affection.today_date() != current_date:
            self._affection.reset_daily(current_date)

        # 静默时段：完全不触发任何主动行为
        silence_start = self._config.time_windows.silence_start
        silence_end = self._config.time_windows.silence_end
        if self._is_in_time_window(silence_start, silence_end, current_time):
            return  # 静默时段，跳过本次巡检

        daily_max_speak = self._config.schedule.daily_max_speak

        # 为晚安预留 1 个配额：晚安未发时，非晚安触发只能用 daily_max_speak-1
        if self._affection.night_sent_today():
            non_night_budget = daily_max_speak  # 晚安已发，释放预留
        else:
            non_night_budget = daily_max_speak - 1  # 为晚安预留
        morning_start = self._config.time_windows.morning_start
        morning_end = self._config.time_windows.morning_end
        night_start = self._config.time_windows.night_start
        night_end = self._config.time_windows.night_end

        # ==============================
        # S级：早安检查
        # ==============================
        if self._is_in_time_window(morning_start, morning_end, current_time):
            if not self._affection.morning_sent_today():
                if self._affection.today_speak_count() < non_night_budget:
                    await self._trigger_morning()

        # ==============================
        # S级：晚安检查
        # ==============================
        if self._is_in_time_window(night_start, night_end, current_time):
            if not self._affection.night_sent_today():
                if self._affection.today_speak_count() < daily_max_speak:
                    await self._trigger_night()

        # ==============================
        # A级：想念机制（6 条件）
        # ==============================
        miss_trigger_hours = self._config.time_windows.miss_trigger_hours
        miss_speak_rate = self._config.probability.miss_speak_rate

        if not self._affection.miss_sent_today():
            last_msg = self._affection.last_user_msg_time()
            # 用户从未发过消息 → 不触发想念（避免首次启动就喊想你了）
            hours_since_last = (
                (now - last_msg).total_seconds() / 3600 if last_msg else 0
            )
            if hours_since_last > miss_trigger_hours:
                if not self._has_future_schedule(2, now):
                    if self._affection.today_speak_count() < non_night_budget:
                        if not self._is_in_cooldown(now):
                            if random.random() < miss_speak_rate:
                                await self._trigger_missing()

        # ==============================
        # B级：日程节点匹配 / 日常巡检
        # ==============================
        activity_trigger_rate = self._config.probability.activity_trigger_rate
        default_speak_rate = self._config.probability.default_speak_rate

        schedule = self._schedule_gen.load_cached_schedule(current_date)
        node_matched = False
        for node in schedule:
            node_time = str(node.get("time", ""))
            if not node_time:
                continue
            if self._time_match(node_time, current_time):
                node_matched = True
                # 节点匹配 → activity_trigger_rate 概率触发活动分享
                if random.random() < activity_trigger_rate:
                    if not self._is_in_cooldown(now):
                        if self._affection.today_speak_count() < non_night_budget:
                            await self._trigger_activity(node)
                break  # 只匹配一个节点

        # 没有节点匹配 → default_speak_rate 概率触发日常巡检
        if not node_matched:
            if random.random() < default_speak_rate:
                if not self._is_in_cooldown(now):
                    if self._affection.today_speak_count() < non_night_budget:
                        await self._trigger_daily()

    async def _trigger_planner(self, intent: str, reason: str) -> bool:
        """统一触发 planner 主动处理。

        Args:
            intent: 触发意图（"morning"/"night"/"missing"/"daily"/"activity"）
            reason: 传给 planner 的提示文本

        Returns:
            True 表示触发成功（trigger 入队成功），False 表示失败或未触发
        """
        if not self._stream_id:
            return False
        if not self._config.schedule.proactive_trigger_enabled:
            return False
        try:
            await self._ctx.maisaka.proactive.trigger(
                stream_id=self._stream_id,
                intent=intent,
                reason=reason,
            )
            self._affection.increment_speak(0.5)  # 先计 0.5，replyer 回复后再补 0.5
            self._last_trigger_time = datetime.now()
            return True
        except Exception as e:
            self._ctx.logger.error(f"proactive_trigger 失败: {e}")
            return False

    async def _trigger_morning(self) -> None:
        """触发早安 planner。"""
        self._ctx.logger.info("S级触发: 早安")
        success = await self._trigger_planner("morning", "早上好，可以说早安")
        if success:
            self._affection.set_morning_sent()

    async def _trigger_night(self) -> None:
        """触发晚安 planner。"""
        self._ctx.logger.info("S级触发: 晚安")
        success = await self._trigger_planner("night", "晚上好，可以说晚安")
        if success:
            self._affection.set_night_sent()

    async def _trigger_missing(self) -> None:
        """触发想念 planner。"""
        self._ctx.logger.info("A级触发: 想念机制")
        success = await self._trigger_planner("missing", "用户很久没理你了")
        if success:
            self._affection.set_miss_sent()

    async def _trigger_activity(self, node: dict[str, Any]) -> None:
        """触发日程节点活动 planner。

        Args:
            node: 日程节点（含 activity 字段）。
        """
        activity = str(node.get("activity", ""))
        self._ctx.logger.info(f"B级触发: 日程节点 - {activity}")
        await self._trigger_planner(
            "activity", f"麦麦现在在{activity}，可以分享"
        )

    async def _trigger_daily(self) -> None:
        """触发日常巡检 planner。"""
        self._ctx.logger.info("B级触发: 日常巡检")
        await self._trigger_planner("daily", "日常巡检")

    def _is_in_cooldown(self, now: datetime) -> bool:
        """检查是否在冷却期内。

        Args:
            now: 当前时间（复用 _tick 的 now）。

        Returns:
            True 表示在冷却期内。
        """
        cooldown_minutes = self._config.schedule.user_cooldown_minutes
        recent_times = [
            value
            for value in (
                self._affection.last_speak_time(),
                self._affection.last_user_msg_time(),
            )
            if value is not None
        ]
        if not recent_times:
            return False
        elapsed = (now - max(recent_times)).total_seconds() / 60
        return elapsed < cooldown_minutes

    @staticmethod
    def _is_in_time_window(
        window_start: str, window_end: str, current: str
    ) -> bool:
        """检查当前时间是否在指定时间窗口内。

        处理跨日窗口的情况（如 22:00 ~ 02:00）。

        Args:
            window_start: 窗口开始时间（HH:MM）。
            window_end: 窗口结束时间（HH:MM）。
            current: 当前时间（HH:MM）。

        Returns:
            True 表示在窗口内。
        """
        try:
            start_minutes = Scheduler._time_to_minutes(window_start)
            end_minutes = Scheduler._time_to_minutes(window_end)
            current_minutes = Scheduler._time_to_minutes(current)
        except ValueError:
            return False

        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes <= end_minutes
        return current_minutes >= start_minutes or current_minutes <= end_minutes

    @staticmethod
    def _time_to_minutes(value: str) -> int:
        """将 H:MM/HH:MM 转为当日分钟数，避免字符串比较误判。"""
        parsed = datetime.strptime(value, "%H:%M")
        return parsed.hour * 60 + parsed.minute

    @staticmethod
    def _time_match(node_time: str, current: str) -> bool:
        """检查节点时间是否匹配当前时间（±1分钟容差）。

        Args:
            node_time: 节点时间（HH:MM）。
            current: 当前时间（HH:MM）。

        Returns:
            True 表示匹配。
        """
        try:
            node_dt = datetime.strptime(node_time, "%H:%M")
            curr_dt = datetime.strptime(current, "%H:%M")
            diff_seconds = abs((curr_dt - node_dt).total_seconds())
            return diff_seconds <= 60  # ±1 分钟
        except ValueError:
            return False

    def _has_future_schedule(self, hours: int, now: datetime) -> bool:
        """检查未来指定小时内是否有日程节点。

        Args:
            hours: 未来时间窗口（小时数）。
            now: 当前时间（复用 _tick 的 now）。

        Returns:
            True 表示有日程节点。
        """
        current_date = now.strftime("%Y-%m-%d")
        schedule = self._schedule_gen.load_cached_schedule(current_date)

        if not schedule:
            return False

        future_limit = now + timedelta(hours=hours)

        for node in schedule:
            node_time = str(node.get("time", ""))
            if not node_time:
                continue
            try:
                node_dt = datetime.strptime(
                    f"{current_date} {node_time}", "%Y-%m-%d %H:%M"
                )
                if now < node_dt <= future_limit:
                    return True
            except ValueError:
                continue

        return False

    def stop(self) -> None:
        """停止所有协程。"""
        self._ctx.logger.info("Scheduler 收到停止信号")
        self._stop_event.set()
        self._cancel_loop_tasks()

    def _cancel_loop_tasks(self) -> None:
        """取消并清理旧的后台循环任务。

        在 start() 时调用，防止 stop/start 快速切换导致残留的
        旧 _daily_generation_loop / _patrol_loop 任务继续运行。
        """
        for task_attr, label in (
            ("_daily_loop_task", "日程生成循环"),
            ("_patrol_loop_task", "巡检循环"),
        ):
            task: Optional[asyncio.Task[Any]] = getattr(self, task_attr, None)
            if task is not None and not task.done():
                task.cancel()
                self._ctx.logger.debug(f"已取消旧的{label}任务")
            setattr(self, task_attr, None)
