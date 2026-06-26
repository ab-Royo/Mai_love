"""好感度管理模块

负责好感度档位、发言计数、想念标记、早晚安标记的读写与持久化。
数据存储在 affection_memory.json 中。

使用节流保存机制减少磁盘 I/O：
- 属性变更时设置 _dirty 标记并尝试节流保存（距上次写入 >1s 才写）
- 关键节点（reset_daily、flush、插件卸载）立即写入
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class AffectionManager:
    """好感度数据管理器。

    管理好感度档位、用户习惯引用、每日发言计数、想念标记、
    早晚安标记、最后用户发言时间、最后发言时间等。

    通过 _dirty 标记 + 节流保存减少 I/O 频率。
    """

    # 两次自动保存的最小间隔（秒）
    _SAVE_THROTTLE_SECONDS: float = 1.0

    def __init__(self, data_dir: str) -> None:
        """初始化好感度管理器。

        Args:
            data_dir: 数据目录路径。
        """
        self._file: Path = Path(data_dir) / "affection_memory.json"
        self._data: dict[str, Any] = {}
        self._dirty: bool = False
        self._last_save_time: float = 0.0

        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._data = self._default_data()
        else:
            self._data = self._default_data()
            self._do_save()

    # ── 公共查询方法 ──────────────────────────────────────────────────

    def level(self) -> int:
        """获取当前好感度档位。

        Returns:
            好感度档位：0（初识）、1（熟悉）、2（热恋）。
        """
        return int(self._data.get("affection_level", 0))

    def habits(self) -> dict[str, Any]:
        """获取用户习惯字典。

        Returns:
            用户习惯键值对。
        """
        return self._data.get("habits", {})

    def today_speak_count(self) -> float:
        """获取当日已发言次数。

        Returns:
            当日发言计数。
        """
        return float(self._data.get("today_speak_count", 0.0))

    def miss_sent_today(self) -> bool:
        """检查今日是否已发送想念。

        Returns:
            是否已发送过想念。
        """
        return bool(self._data.get("miss_sent_today", False))

    def morning_sent_today(self) -> bool:
        """检查今日是否已发送早安。

        Returns:
            是否已发送过早安。
        """
        return bool(self._data.get("morning_sent_today", False))

    def night_sent_today(self) -> bool:
        """检查今日是否已发送晚安。

        Returns:
            是否已发送过晚安。
        """
        return bool(self._data.get("night_sent_today", False))

    def last_user_msg_time(self) -> Optional[datetime]:
        """获取最后用户发言时间。

        Returns:
            datetime 对象，或 None（无记录）。
        """
        val: str = self._data.get("last_user_msg_time", "")
        if not val:
            return None
        try:
            return datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def last_speak_time(self) -> Optional[datetime]:
        """获取最后发言时间（用于冷却期判断）。

        Returns:
            datetime 对象，或 None。
        """
        val: str = self._data.get("last_speak_time", "")
        if not val:
            return None
        try:
            return datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def today_date(self) -> str:
        """获取当前记录的日期。

        Returns:
            日期字符串（YYYY-MM-DD）。
        """
        return str(self._data.get("today_date", ""))

    # ── 修改方法（节流保存）───────────────────────────────────────────

    def update_level(self, level: int) -> None:
        """更新好感度档位。

        Args:
            level: 新的档位值（0/1/2）。
        """
        self._data["affection_level"] = max(0, min(2, int(level)))
        self._maybe_save()

    def set_habit(self, key: str, value: Any) -> None:
        """设置用户习惯。

        Args:
            key: 习惯键名。
            value: 习惯值。
        """
        if "habits" not in self._data:
            self._data["habits"] = {}
        self._data["habits"][key] = value
        self._maybe_save()

    def increment_speak(self, amount: float = 1.0) -> None:
        """发言计数 +amount 并节流保存。

        Args:
            amount: 增量（默认 1.0，加权计数时可传 0.5）。
        """
        self._data["today_speak_count"] = self.today_speak_count() + amount
        self._data["last_speak_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._maybe_save()

    def set_miss_sent(self) -> None:
        """标记今日已发送想念并节流保存。"""
        self._data["miss_sent_today"] = True
        self._maybe_save()

    def set_morning_sent(self) -> None:
        """标记今日已发送早安并节流保存。"""
        self._data["morning_sent_today"] = True
        self._maybe_save()

    def set_night_sent(self) -> None:
        """标记今日已发送晚安并节流保存。"""
        self._data["night_sent_today"] = True
        self._maybe_save()

    def update_last_user_msg_time(self, dt: datetime) -> None:
        """更新最后用户发言时间。

        Args:
            dt: 发言时间。
        """
        self._data["last_user_msg_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        self._maybe_save()

    def reset_daily(self, date: str) -> None:
        """新的一天：重置发言计数、想念标记、早晚安标记。

        关键节点，强制立即写入磁盘（不节流）。

        Args:
            date: 新日期字符串（YYYY-MM-DD）。
        """
        self._data["today_date"] = date
        self._data["today_speak_count"] = 0.0
        self._data["miss_sent_today"] = False
        self._data["morning_sent_today"] = False
        self._data["night_sent_today"] = False
        self._data["last_speak_time"] = ""
        self.flush()

    # ── 持久化控制 ────────────────────────────────────────────────────

    def save(self) -> None:
        """显式持久化（节流：距上次写入不足 _SAVE_THROTTLE_SECONDS 秒则跳过）。

        一般由外部在适当时机调用；高频修改场景下由 _maybe_save 自动触发。
        """
        self._maybe_save()

    def flush(self) -> None:
        """强制立即持久化到磁盘，忽略节流限制。

        用于以下关键节点：
        - 每日重置 (reset_daily)
        - 插件卸载 (on_unload)
        - 配置热更新 (on_config_update)
        """
        self._do_save()

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _maybe_save(self) -> None:
        """标记脏数据并尝试节流保存。

        若距离上次写入超过 _SAVE_THROTTLE_SECONDS 秒，
        则立即执行写入；否则仅设置 _dirty 标记。
        """
        self._dirty = True
        now = time.time()
        if now - self._last_save_time >= self._SAVE_THROTTLE_SECONDS:
            self._do_save()

    def _do_save(self) -> None:
        """实际执行 JSON 持久化写入。"""
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            self._dirty = False
            self._last_save_time = time.time()
        except IOError:
            # I/O 错误由上层通过 logger 报告，此处静默
            pass

    @staticmethod
    def _default_data() -> dict[str, Any]:
        """返回默认初始数据。

        Returns:
            默认数据字典。
        """
        return {
            "affection_level": 0,
            "habits": {},
            "today_speak_count": 0.0,
            "today_date": "",
            "miss_sent_today": False,
            "last_user_msg_time": "",
            "morning_sent_today": False,
            "night_sent_today": False,
            "last_speak_time": "",
        }
