"""LLM 服务模块

封装 LLM 调用，提供降级策略：
1. 调用 SDK 的 llm.generate
2. 失败 → 返回空字符串（由调用方降级）
3. 提供便捷的 generate_or_fallback、generate_schedule 方法

v2.0.0: 移除 generate_speak 方法（主动发言统一走 planner 触发）。
"""

import json
from typing import Any

from .config import MaiLoverPluginSettings
from .constants import SCHEDULE_GENERATION_PROMPT


class LLMService:
    """LLM 调用封装服务。

    封装 SDK 的 LLM 调用接口，提供日程生成和通用文本生成的
    专用方法，均带降级处理。
    """

    def __init__(self, ctx: Any, config: MaiLoverPluginSettings) -> None:
        """初始化 LLM 服务。

        Args:
            ctx: MaiBot PluginContext 实例。
            config: 插件强类型配置模型。
        """
        self._ctx: Any = ctx
        self._config: MaiLoverPluginSettings = config

    @property
    def _model(self) -> str:
        """获取配置的 LLM 模型名称。

        Returns:
            模型名称字符串（如 'planner'），空字符串表示使用全局默认。
        """
        return self._config.plugin.llm_model

    async def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.7,
        max_tokens: int = 512,
    ) -> str:
        """调用 LLM 生成文本，失败返回空字符串。

        Host 端 _cap_llm_generate 不处理 system_prompt 参数，
        需要通过消息列表格式传递 system prompt。

        Args:
            prompt: 用户提示词。
            system_prompt: 系统提示词。
            temperature: 采样温度。
            max_tokens: 最大生成 token 数。

        Returns:
            生成的文本，失败返回空字符串。
        """
        try:
            if system_prompt:
                prompt_arg: str | list[dict[str, Any]] = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ]
            else:
                prompt_arg = prompt
            result: dict[str, Any] = await self._ctx.llm.generate(
                prompt=prompt_arg,
                model=self._model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if result.get("success") and result.get("response"):
                return str(result["response"]).strip()
            self._ctx.logger.warning(
                f"LLM 生成失败: {result.get('error', '未知错误')}"
            )
            return ""
        except Exception as e:
            self._ctx.logger.error(f"LLM 调用异常: {e}")
            return ""

    async def generate_or_fallback(
        self,
        prompt: str,
        fallback: str,
        system_prompt: str = "",
        temperature: float = 0.7,
    ) -> str:
        """LLM 调用，失败时返回预设降级文案。

        Args:
            prompt: 用户提示词。
            fallback: 降级文案。
            system_prompt: 系统提示词。
            temperature: 采样温度。

        Returns:
            生成的文本或降级文案。
        """
        result = await self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
        )
        if result:
            return result
        self._ctx.logger.info("LLM 返回空，使用降级文案")
        return fallback

    async def generate_schedule(
        self,
        date: str,
        weekday: str,
        holiday_info: str,
        mai_template: str,
        personality: str,
    ) -> list[dict[str, Any]]:
        """生成日程 JSON 数组。

        使用 SCHEDULE_GENERATION_PROMPT 模板构造提示词，
        调用 LLM 生成 JSON，解析失败返回空列表。

        Args:
            date: 日期字符串（YYYY-MM-DD）。
            weekday: 中文星期名称（如"星期五"）。
            holiday_info: 节假日/工作日描述。
            mai_template: 麦麦作息模板格式化文本。
            personality: 麦麦人设性格文本。

        Returns:
            日程节点列表 [{time, activity}, ...]，失败返回 []。
        """
        prompt = SCHEDULE_GENERATION_PROMPT.format(
            date=date,
            weekday=weekday,
            holiday_info=holiday_info,
            mai_template=mai_template,
            personality=personality,
        )
        response = await self.generate(
            prompt=prompt,
            system_prompt="你是一个 JSON 生成助手，只返回合法的 JSON 数组。",
            temperature=0.5,
            max_tokens=2048,
        )
        if not response:
            return []
        return self._parse_schedule_response(response)

    def _parse_schedule_response(self, response: str) -> list[dict[str, Any]]:
        """解析 LLM 返回的 JSON 数组。

        尝试多种策略提取 JSON：
        1. 直接解析整段文本
        2. 提取 ```json...``` 代码块
        3. 提取最外层 [ ... ] 内容
        4. 提取最外层 { ... } 并用 [] 包裹

        Args:
            response: LLM 返回的原始文本。

        Returns:
            解析后的节点列表，失败返回 []。
        """
        candidates: list[str] = [response]

        # 尝试提取 ```json 代码块
        if "```json" in response:
            start = response.find("```json") + 7
            end = response.find("```", start)
            if end > start:
                candidates.append(response[start:end].strip())
        if "```" in response:
            start = response.find("```") + 3
            end = response.find("```", start)
            if end > start:
                candidates.append(response[start:end].strip())

        # 尝试提取最外层 [ ... ]
        bracket_start = response.find("[")
        bracket_end = response.rfind("]")
        if bracket_start >= 0 and bracket_end > bracket_start:
            candidates.append(response[bracket_start:bracket_end + 1])

        for text in candidates:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
                if isinstance(parsed, dict):
                    # LLM 可能返回单个对象
                    return [parsed]
            except (json.JSONDecodeError, ValueError):
                continue

        self._ctx.logger.warning(f"无法解析 LLM 日程响应: {response[:200]}")
        return []
