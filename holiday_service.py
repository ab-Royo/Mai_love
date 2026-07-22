"""节假日服务模块

负责获取指定日期的节假日/工作日信息：
1. 优先调用 timor.tech 节假日 API（异步 httpx）
2. 失败则本地根据星期几判断
"""

import json
from datetime import datetime
from typing import Optional

import httpx

from .config import MaiLoverPluginSettings
from .constants import CHINESE_WEEKDAYS, HOLIDAY_FALLBACK_WEEKDAYS


class HolidayService:
    """节假日信息服务。

    通过 httpx 异步调用 API 获取节假日信息，带本地降级策略。
    按日期缓存查询结果，当天首次查询后由日程生成和 Planner 共用，
    避免每次 Planner 请求都调用外部 API。
    """

    # API 地址模板
    API_URL_TEMPLATE: str = "https://timor.tech/api/holiday/info/{date}"

    # 节假日类型映射
    TYPE_MAP: dict[int, str] = {
        0: "工作日",
        1: "休息日",
        2: "节假日",
    }

    def __init__(self, config: MaiLoverPluginSettings) -> None:
        """初始化节假日服务。

        Args:
            config: 插件强类型配置模型。
        """
        self._config: MaiLoverPluginSettings = config
        self._cache: dict[str, str] = {}

    @staticmethod
    def get_weekday_name(date: str) -> str:
        """根据日期返回中文星期名称。

        Args:
            date: 日期字符串（YYYY-MM-DD）。

        Returns:
            中文星期名称（如"星期五"），解析失败返回空字符串。
        """
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            return CHINESE_WEEKDAYS.get(dt.weekday(), "")
        except ValueError:
            return ""

    async def get_holiday_info(self, date: str) -> str:
        """获取指定日期的节假日/工作日信息（带日期缓存）。

        当天首次查询时调用外部 API，后续直接从内存缓存返回。
        日程生成（凌晨）触发首次查询后，Planner 后续请求零开销。

        Args:
            date: 日期字符串（YYYY-MM-DD）。

        Returns:
            中文描述，如 "工作日" / "周末休息日" / "春节假期"。
        """
        if date in self._cache:
            return self._cache[date]

        result = await self._call_api(date)
        if result is not None:
            holiday_type: int = result.get("type", -1)
            if holiday_type in self.TYPE_MAP:
                name: str = result.get("name", "")
                if holiday_type == 2 and name:
                    info = f"{name}假期"
                else:
                    info = self.TYPE_MAP[holiday_type]
                self._cache[date] = info
                return info

        # API 失败，本地判断
        info = self._local_judge(date)
        self._cache[date] = info
        return info
        """获取指定日期的节假日/工作日信息。

        流程：
        1. 异步调用 timor.tech API
        2. 成功 → 根据 type 返回描述
        3. 失败 → 本地根据星期几判断

        Args:
            date: 日期字符串（YYYY-MM-DD）。

        Returns:
            中文描述，如 "工作日" / "周末休息日" / "春节假期"。
        """
        result = await self._call_api(date)
        if result is not None:
            holiday_type: int = result.get("type", -1)
            if holiday_type in self.TYPE_MAP:
                name: str = result.get("name", "")
                if holiday_type == 2 and name:
                    return f"{name}假期"
                return self.TYPE_MAP[holiday_type]

        # API 失败，本地判断
        return self._local_judge(date)

    async def _call_api(self, date: str) -> Optional[dict]:
        """异步调用 timor.tech 节假日 API（httpx）。

        Args:
            date: 日期字符串（YYYY-MM-DD）。

        Returns:
            API 响应中的 holiday 字段（dict），失败返回 None。
        """
        url = self.API_URL_TEMPLATE.format(date=date)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    url, headers={"User-Agent": "MaiLover/1.0"}
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
                if isinstance(data, dict) and data.get("code") == 0:
                    holiday = data.get("holiday")
                    if isinstance(holiday, dict):
                        return holiday
                return None
        except Exception:
            return None

    @staticmethod
    def _local_judge(date: str) -> str:
        """本地根据星期几判断工作日/休息日。

        Args:
            date: 日期字符串（YYYY-MM-DD）。

        Returns:
            "工作日" 或 "周末休息日"。
        """
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            if dt.weekday() in HOLIDAY_FALLBACK_WEEKDAYS:
                return "工作日"
            return "周末休息日"
        except ValueError:
            return "工作日"
