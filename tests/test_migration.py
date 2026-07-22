"""测试数据迁移逻辑。"""

import json
import os
import tempfile

import pytest

# 设置 SDK mock
import sys
from types import ModuleType
from pydantic import BaseModel, Field

sdk = ModuleType("maibot_sdk")
sdk.PluginConfigBase = BaseModel
sdk.Field = Field
sys.modules.setdefault("maibot_sdk", sdk)


class TestMigration:
    """旧数据迁移测试。"""

    def _make_plugin_mock(self, base_data_dir: str):
        """构造一个带有 logger mock 的最小插件实例用于测试迁移。"""
        from unittest.mock import Mock
        from Mai_love.plugin import MaiLoverPlugin

        plugin = object.__new__(MaiLoverPlugin)
        plugin.ctx = Mock()
        plugin.ctx.logger = Mock()
        plugin._users = Mock()
        plugin.config = Mock()
        return plugin

    def test_migrate_noop_when_no_old_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin = self._make_plugin_mock(tmpdir)
            plugin._migrate_single_user_data(tmpdir, [123456789])
            # 不应有任何日志迁移记录
            info_calls = [
                c[0][0] for c in plugin.ctx.logger.info.call_args_list
                if c[0]
            ]
            migration_msgs = [m for m in info_calls if "迁移" in str(m)]
            assert len(migration_msgs) == 0

    def test_migrate_old_data_to_qq_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建模拟旧数据
            old_aff = os.path.join(tmpdir, "affection_memory.json")
            old_sch = os.path.join(tmpdir, "schedule_cache.json")
            with open(old_aff, "w") as f:
                json.dump({"affection_level": 1, "habits": {"likes": "coffee"}}, f)
            with open(old_sch, "w") as f:
                json.dump({"date": "2025-01-01", "nodes": []}, f)

            plugin = self._make_plugin_mock(tmpdir)
            plugin._migrate_single_user_data(tmpdir, [123456789])

            # 验证目标目录
            new_dir = os.path.join(tmpdir, "123456789")
            assert os.path.exists(os.path.join(new_dir, "affection_memory.json"))
            assert os.path.exists(os.path.join(new_dir, "schedule_cache.json"))

            # 验证内容正确
            with open(os.path.join(new_dir, "affection_memory.json"), "r") as f:
                data = json.load(f)
                assert data["affection_level"] == 1
                assert data["habits"]["likes"] == "coffee"

    def test_migrate_skips_if_already_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建旧数据
            old_aff = os.path.join(tmpdir, "affection_memory.json")
            with open(old_aff, "w") as f:
                json.dump({"affection_level": 0}, f)

            # 目标目录已有数据
            new_dir = os.path.join(tmpdir, "123456789")
            os.makedirs(new_dir)
            existing_aff = os.path.join(new_dir, "affection_memory.json")
            with open(existing_aff, "w") as f:
                json.dump({"affection_level": 2}, f)

            plugin = self._make_plugin_mock(tmpdir)
            plugin._migrate_single_user_data(tmpdir, [123456789])

            # 目标文件应保持原样（level=2），不被旧数据覆盖
            with open(existing_aff, "r") as f:
                data = json.load(f)
                assert data["affection_level"] == 2

    def test_migrate_multiple_qqs_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            old_aff = os.path.join(tmpdir, "affection_memory.json")
            with open(old_aff, "w") as f:
                json.dump({"affection_level": 0}, f)

            plugin = self._make_plugin_mock(tmpdir)
            plugin._migrate_single_user_data(tmpdir, [111, 222, 333])

            # 数据应迁移到第一个 QQ
            assert os.path.exists(os.path.join(tmpdir, "111", "affection_memory.json"))
            # 其他 QQ 不应有数据
            assert not os.path.exists(os.path.join(tmpdir, "222", "affection_memory.json"))
            assert not os.path.exists(os.path.join(tmpdir, "333", "affection_memory.json"))

            # 应有 warning
            assert any(
                "其他用户将从零开始" in str(c[0][0])
                for c in plugin.ctx.logger.warning.call_args_list
                if c[0]
            )
