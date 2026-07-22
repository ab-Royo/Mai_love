from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from Mai_love.scheduler import Scheduler


def make_scheduler(cooldown_minutes: int = 30) -> Scheduler:
    scheduler = object.__new__(Scheduler)
    scheduler._config = SimpleNamespace(
        schedule=SimpleNamespace(user_cooldown_minutes=cooldown_minutes)
    )
    scheduler._affection = Mock()
    return scheduler


def test_cooldown_uses_latest_user_message() -> None:
    scheduler = make_scheduler()
    now = datetime.now()
    scheduler._affection.last_speak_time.return_value = None
    scheduler._affection.last_user_msg_time.return_value = now - timedelta(minutes=5)

    assert scheduler._is_in_cooldown(now)

    scheduler._affection.last_user_msg_time.return_value = now - timedelta(minutes=31)
    assert not scheduler._is_in_cooldown(now)


def test_cooldown_uses_latest_bot_message() -> None:
    scheduler = make_scheduler()
    now = datetime.now()
    scheduler._affection.last_speak_time.return_value = now - timedelta(minutes=5)
    scheduler._affection.last_user_msg_time.return_value = now - timedelta(minutes=60)

    assert scheduler._is_in_cooldown(now)


def test_time_window_accepts_single_digit_hour() -> None:
    assert not Scheduler._is_in_time_window("9:00", "10:00", "08:30")
    assert Scheduler._is_in_time_window("9:00", "10:00", "09:30")


def test_time_window_supports_cross_midnight() -> None:
    assert Scheduler._is_in_time_window("22:00", "02:00", "01:00")
    assert not Scheduler._is_in_time_window("22:00", "02:00", "12:00")
