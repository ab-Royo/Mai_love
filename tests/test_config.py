"""测试 config.py 中新增的多用户配置功能。"""

import json
import os
import tempfile
from types import SimpleNamespace

import pytest

# 导入前设置 maibot_sdk mock（必须在 conftest 之前）
import sys
from types import ModuleType
from pydantic import BaseModel, Field

sdk = ModuleType("maibot_sdk")
sdk.PluginConfigBase = BaseModel
sdk.Field = Field

# 确保 mock 已设置（conftest 在测试收集时已运行）
# 此处再补一次以防单跑此文件
sys.modules.setdefault("maibot_sdk", sdk)

from Mai_love.config import (
    MaiLoverPluginSettings,
    WhitelistConfig,
    load_user_config_overrides,
    merge_config_with_overrides,
)


class TestWhitelistConfig:
    """WhitelistConfig 多用户功能测试。"""

    def test_get_effective_qqs_from_target_qqs(self) -> None:
        cfg = WhitelistConfig(target_qqs=[111, 222, 333])
        assert cfg.get_effective_qqs() == [111, 222, 333]

    def test_get_effective_qqs_from_legacy_target_qq(self) -> None:
        cfg = WhitelistConfig(target_qqs=[], target_qq=123456789)
        # 旧默认值 123456789 应被过滤
        assert cfg.get_effective_qqs() == []

        cfg2 = WhitelistConfig(target_qqs=[], target_qq=555666777)
        assert cfg2.get_effective_qqs() == [555666777]

    def test_get_effective_qqs_merges_and_deduplicates(self) -> None:
        cfg = WhitelistConfig(target_qqs=[111, 222], target_qq=111)
        assert cfg.get_effective_qqs() == [111, 222]

    def test_get_effective_qqs_filters_default_values(self) -> None:
        cfg = WhitelistConfig(target_qqs=[], target_qq=0)
        assert cfg.get_effective_qqs() == []

        cfg2 = WhitelistConfig(target_qqs=[0, 123456789, 111])
        assert cfg2.get_effective_qqs() == [111]

    def test_target_qqs_normalizes_strings_to_ints(self) -> None:
        cfg = WhitelistConfig(target_qqs=["111", "222", 333])
        assert cfg.target_qqs == [111, 222, 333]

    def test_target_qqs_empty_on_invalid_input(self) -> None:
        cfg = WhitelistConfig(target_qqs="not-a-list")
        assert cfg.target_qqs == []

    def test_target_qqs_none_returns_empty(self) -> None:
        cfg = WhitelistConfig(target_qqs=None)  # type: ignore[arg-type]
        assert cfg.target_qqs == []


class TestConfigMerge:
    """配置合并功能测试。"""

    def test_merge_no_overrides_returns_copy(self) -> None:
        global_cfg = MaiLoverPluginSettings()
        merged = merge_config_with_overrides(global_cfg, {})
        # 值应相同
        assert merged.schedule.daily_max_speak == global_cfg.schedule.daily_max_speak
        # 但是不同实例
        assert merged is not global_cfg

    def test_merge_overrides_schedule_field(self) -> None:
        global_cfg = MaiLoverPluginSettings()
        overrides = {"schedule": {"daily_max_speak": 99}}
        merged = merge_config_with_overrides(global_cfg, overrides)
        assert merged.schedule.daily_max_speak == 99
        # 其他字段保持全局默认
        assert merged.schedule.check_interval_minutes == global_cfg.schedule.check_interval_minutes

    def test_merge_overrides_affection_level(self) -> None:
        global_cfg = MaiLoverPluginSettings()
        overrides = {"affection": {"current_level": 2}}
        merged = merge_config_with_overrides(global_cfg, overrides)
        assert merged.affection.current_level == 2

    def test_merge_overrides_probability(self) -> None:
        global_cfg = MaiLoverPluginSettings()
        overrides = {"probability": {"default_speak_rate": 0.99}}
        merged = merge_config_with_overrides(global_cfg, overrides)
        assert merged.probability.default_speak_rate == 0.99

    def test_merge_overrides_time_windows(self) -> None:
        global_cfg = MaiLoverPluginSettings()
        overrides = {"time_windows": {"morning_start": "07:30"}}
        merged = merge_config_with_overrides(global_cfg, overrides)
        assert merged.time_windows.morning_start == "07:30"
        # 未覆盖字段保持全局值
        assert merged.time_windows.morning_end == global_cfg.time_windows.morning_end

    def test_merge_multiple_sections(self) -> None:
        global_cfg = MaiLoverPluginSettings()
        overrides = {
            "schedule": {"daily_max_speak": 10, "user_cooldown_minutes": 15},
            "probability": {"default_speak_rate": 0.8},
        }
        merged = merge_config_with_overrides(global_cfg, overrides)
        assert merged.schedule.daily_max_speak == 10
        assert merged.schedule.user_cooldown_minutes == 15
        assert merged.probability.default_speak_rate == 0.8


class TestLoadOverrides:
    """配置覆盖文件加载测试。"""

    def test_load_nonexistent_file_returns_empty(self) -> None:
        result = load_user_config_overrides("/nonexistent/path/12345")
        assert result == {}

    def test_load_valid_override_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            override_file = os.path.join(tmpdir, "config_override.json")
            data = {
                "schedule": {"daily_max_speak": 7},
                "affection": {"current_level": 1},
            }
            with open(override_file, "w", encoding="utf-8") as f:
                json.dump(data, f)

            result = load_user_config_overrides(tmpdir)
            assert result == data

    def test_load_corrupted_json_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            override_file = os.path.join(tmpdir, "config_override.json")
            with open(override_file, "w", encoding="utf-8") as f:
                f.write("this is not json {{{")

            result = load_user_config_overrides(tmpdir)
            assert result == {}

    def test_load_strips_none_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            override_file = os.path.join(tmpdir, "config_override.json")
            data = {
                "schedule": {"daily_max_speak": 7, "check_interval_minutes": None},
            }
            with open(override_file, "w", encoding="utf-8") as f:
                json.dump(data, f)

            result = load_user_config_overrides(tmpdir)
            assert result == {"schedule": {"daily_max_speak": 7}}
