"""本地记忆库模块

用于记录和读取用户习惯，构造个性化上下文文案。
底层委托给 AffectionManager 的 habits 段。
"""

from typing import Any

from .affection_manager import AffectionManager


class MemoryManager:
    """本地记忆库管理器。

    封装对用户习惯的读写操作，并提供个性化上下文构造能力。
    """

    def __init__(self, affection_manager: AffectionManager) -> None:
        """初始化记忆管理器。

        Args:
            affection_manager: 好感度管理器实例，底层存储由其 habits 段承载。
        """
        self._affection: AffectionManager = affection_manager

    def get_habit(self, key: str) -> Any:
        """获取指定习惯。

        Args:
            key: 习惯键名，如 'wednesday_overtime'。

        Returns:
            习惯值，不存在返回 None。
        """
        return self._affection.habits().get(key)

    def set_habit(self, key: str, value: Any) -> None:
        """设置指定习惯。

        Args:
            key: 习惯键名。
            value: 习惯值。
        """
        self._affection.set_habit(key, value)

    def all_habits(self) -> dict[str, Any]:
        """获取所有习惯。

        Returns:
            习惯字典。
        """
        return self._affection.habits()

    def build_personal_context(self) -> str:
        """构造个性化上下文文案。

        遍历所有习惯条目，生成一段可供 LLM prompt 使用的
        自然语言描述，如 '用户经常周三加班，喜欢吃辣'。

        Returns:
            个性化上下文文本。无习惯时返回空字符串。
        """
        habits = self.all_habits()
        if not habits:
            return "暂无用户习惯记录。"

        parts: list[str] = []
        for key, value in habits.items():
            # 将 key 中的下划线替换为中文描述
            key_display = key.replace("_", " ")
            parts.append(f"用户习惯：{key_display} → {value}")

        return "。".join(parts)
