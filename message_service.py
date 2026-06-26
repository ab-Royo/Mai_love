"""消息发送模块

封装消息发送逻辑，负责：
1. 追加情绪锚点后缀
2. 调用 SDK 发送文本消息
3. 更新发言计数

stream_id 解析统一由 plugin.py:_resolve_stream_id() 负责，
本模块不再自行查找。
"""

import random
from typing import Any

from .affection_manager import AffectionManager
from .config import MaiLoverPluginSettings
from .constants import AFFECTION_SUFFIXES, BRACKET_THEATERS


class MessageService:
    """消息发送服务。

    封装消息发送流程，包括情绪后缀追加、实际发送和计数更新。
    stream_id 由调用方预先解析后传入。
    """

    def __init__(
        self,
        ctx: Any,
        config: MaiLoverPluginSettings,
        affection_manager: AffectionManager,
    ) -> None:
        """初始化消息服务。

        Args:
            ctx: MaiBot PluginContext 实例。
            config: 插件强类型配置模型。
            affection_manager: 好感度管理器。
        """
        self._ctx: Any = ctx
        self._config: MaiLoverPluginSettings = config
        self._affection: AffectionManager = affection_manager

    async def send_to_target(self, text: str, stream_id: str, is_proactive: bool = True) -> bool:
        """发送消息给白名单用户。

        流程：
        1. 追加情绪锚点后缀
        2. 调用 ctx.send.text 发送
        3. 若为主动发言，更新发言计数与冷却时间

        Args:
            text: 消息正文（不含后缀）。
            stream_id: 聊天流 ID（由调用方通过 plugin._resolve_stream_id() 获取）。
            is_proactive: 是否为主动发言。True=主动发言（计数+更新冷却），
                False=被动回复（不计数不影响冷却）。

        Returns:
            True 表示发送成功，False 表示失败。
        """
        if not stream_id:
            self._ctx.logger.error("stream_id 为空，无法发送消息")
            return False

        # 追加情绪锚点后缀
        final_text = self.append_affection_suffix(text)

        try:
            result = await self._ctx.send.text(text=final_text, stream_id=stream_id)
            if result:
                if is_proactive:
                    self._affection.increment_speak()
                    self._ctx.logger.info(
                        f"主动消息发送成功: {final_text[:50]}..."
                    )
                else:
                    self._ctx.logger.info(
                        f"被动回复发送成功: {final_text[:50]}..."
                    )
                return True
            self._ctx.logger.error("消息发送失败: 发送返回 False")
            return False
        except Exception as e:
            self._ctx.logger.error(f"消息发送异常: {e}")
            return False

    def append_affection_suffix(self, text: str) -> str:
        """根据好感度档位追加情绪锚点后缀。

        规则：
        - 档位 0: 随机追加 "~", "哦", "呢", "哈"
        - 档位 1: 随机追加 "~❤️", "啦！", "嘿嘿~", "嗯呐~"
        - 档位 2: 随机追加 "~抱抱", "亲亲~", "想你啦~", "mua~"
          + 10% 概率额外追加括号小剧场

        Args:
            text: 原始消息文本。

        Returns:
            追加后缀后的完整消息。
        """
        level = self._affection.level()
        suffixes = AFFECTION_SUFFIXES.get(level, AFFECTION_SUFFIXES[0])
        suffix = self._random_suffix(suffixes)

        result = text + suffix

        # 档位 2: 10% 概率追加括号小剧场
        if level == 2 and random.random() < 0.1:
            theater = self._random_suffix(BRACKET_THEATERS)
            result = result + theater

        return result

    @staticmethod
    def _random_suffix(suffixes: list[str]) -> str:
        """从后缀列表中随机选取一个。

        Args:
            suffixes: 后缀列表。

        Returns:
            随机选中的后缀。列表为空返回空字符串。
        """
        if not suffixes:
            return ""
        return random.choice(suffixes)
