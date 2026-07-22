"""测试多用户插件层面的路由和状态管理。"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

# 设置 SDK mock（确保在导入插件前生效）
import sys
from types import ModuleType

from pydantic import BaseModel, Field

sdk = ModuleType("maibot_sdk")
sdk.PluginConfigBase = BaseModel
sdk.Field = Field

# mock 装饰器
sdk.HookHandler = lambda *args, **kw: lambda fn: fn
sdk.Tool = lambda *args, **kw: lambda fn: fn
sdk.Command = lambda *args, **kw: lambda fn: fn
sdk.API = lambda *args, **kw: lambda fn: fn
sdk.MaiBotPlugin = type("MaiBotPlugin", (object,), {"__init__": lambda self: None})
sys.modules.setdefault("maibot_sdk", sdk)
sys.modules.setdefault("maibot_sdk.types", ModuleType("maibot_sdk.types"))
sys.modules["maibot_sdk.types"].ErrorPolicy = Mock()
sys.modules["maibot_sdk.types"].HookMode = Mock()
sys.modules["maibot_sdk.types"].HookOrder = Mock()

from Mai_love.plugin import UserContext, UserRegistry


class TestUserRegistryRouting:
    """测试 UserRegistry 的路由功能（用于 Hook/Tool/Command）。"""

    def test_resolve_by_stream_id_finds_correct_user(self) -> None:
        reg = UserRegistry()
        alice = UserContext(qq="111", data_dir="/tmp/111", stream_id="sid_alice")
        bob = UserContext(qq="222", data_dir="/tmp/222", stream_id="sid_bob")
        reg.register(alice)
        reg.register(bob)

        assert reg.get_by_stream_id("sid_alice") is alice
        assert reg.get_by_stream_id("sid_bob") is bob
        assert reg.get_by_stream_id("sid_unknown") is None

    def test_resolve_by_qq_finds_correct_user(self) -> None:
        reg = UserRegistry()
        alice = UserContext(qq="111", data_dir="/tmp/111")
        bob = UserContext(qq="222", data_dir="/tmp/222")
        reg.register(alice)
        reg.register(bob)

        assert reg.get_by_qq("111") is alice
        assert reg.get_by_qq("222") is bob
        assert reg.get_by_qq("333") is None

    def test_unregister_then_lookup_fails(self) -> None:
        reg = UserRegistry()
        alice = UserContext(qq="111", data_dir="/tmp/111", stream_id="sid")
        reg.register(alice)
        reg.unregister("111")

        assert reg.get_by_qq("111") is None
        assert reg.get_by_stream_id("sid") is None

    def test_update_stream_id_mid_session(self) -> None:
        """模拟 stream_id 延迟解析的场景。"""
        reg = UserRegistry()
        alice = UserContext(qq="111", data_dir="/tmp/111", stream_id="")
        reg.register(alice)

        # 初始无 stream_id 映射
        assert reg.get_by_stream_id("") is None

        # 解析到真正的 stream_id
        reg.update_stream_id("111", "resolved_sid")
        assert reg.get_by_stream_id("resolved_sid") is alice
        assert alice.stream_id == "resolved_sid"


class TestUserContextCreation:
    """测试 UserContext 的创建和数据目录结构。"""

    def test_user_context_fields_are_initialized(self) -> None:
        ctx = UserContext(
            qq="12345",
            data_dir="/data/12345",
            stream_id="sid_test",
        )
        assert ctx.qq == "12345"
        assert ctx.data_dir == "/data/12345"
        assert ctx.stream_id == "sid_test"
        assert ctx.affection_mgr is None
        assert ctx.scheduler is None

    def test_user_context_defaults(self) -> None:
        ctx = UserContext(qq="999", data_dir="/data/999")
        assert ctx.stream_id == ""
        assert ctx.schedule_gen is None
        assert ctx.stream_retry_task is None


class TestPluginMessageResolution:
    """测试 _resolve_user_from_message 的消息路由逻辑。"""

    def _make_plugin_with_users(self, users: list[UserContext]):
        """构造带有注册用户的 plugin mock。"""
        from unittest.mock import MagicMock
        from Mai_love.plugin import MaiLoverPlugin

        plugin = object.__new__(MaiLoverPlugin)
        plugin._users = UserRegistry()
        plugin.ctx = MagicMock()
        plugin.ctx.logger = MagicMock()
        plugin.config = MagicMock()
        for u in users:
            plugin._users.register(u)
        return plugin

    def test_resolves_target_private_message(self) -> None:
        plugin = self._make_plugin_with_users([
            UserContext(qq="11111", data_dir="/tmp/11111"),
        ])
        message = {
            "message_info": {
                "user_info": {"user_id": 11111},
                "group_info": None,
            }
        }
        ctx = plugin._resolve_user_from_message(message)
        assert ctx is not None
        assert ctx.qq == "11111"

    def test_ignores_group_message(self) -> None:
        plugin = self._make_plugin_with_users([
            UserContext(qq="11111", data_dir="/tmp/11111"),
        ])
        message = {
            "message_info": {
                "user_info": {"user_id": 11111},
                "group_info": {"group_id": 999},
            }
        }
        ctx = plugin._resolve_user_from_message(message)
        assert ctx is None

    def test_ignores_notify_message(self) -> None:
        plugin = self._make_plugin_with_users([
            UserContext(qq="11111", data_dir="/tmp/11111"),
        ])
        message = {
            "is_notify": True,
            "message_info": {
                "user_info": {"user_id": 11111},
            }
        }
        ctx = plugin._resolve_user_from_message(message)
        assert ctx is None

    def test_ignores_unknown_user(self) -> None:
        plugin = self._make_plugin_with_users([
            UserContext(qq="11111", data_dir="/tmp/11111"),
        ])
        message = {
            "message_info": {
                "user_info": {"user_id": 99999},
            }
        }
        ctx = plugin._resolve_user_from_message(message)
        assert ctx is None

    def test_handles_malformed_message(self) -> None:
        plugin = self._make_plugin_with_users([
            UserContext(qq="11111", data_dir="/tmp/11111"),
        ])
        assert plugin._resolve_user_from_message(None) is None
        assert plugin._resolve_user_from_message("not a dict") is None
        assert plugin._resolve_user_from_message({}) is None

    def test_resolves_user_from_kwargs_stream_id(self) -> None:
        plugin = self._make_plugin_with_users([
            UserContext(qq="11111", data_dir="/tmp/11111", stream_id="sid_abc"),
        ])
        ctx = plugin._resolve_user_from_kwargs({"stream_id": "sid_abc"})
        assert ctx is not None
        assert ctx.qq == "11111"

    def test_resolve_from_kwargs_unknown_stream_id(self) -> None:
        plugin = self._make_plugin_with_users([
            UserContext(qq="11111", data_dir="/tmp/11111", stream_id="sid_abc"),
        ])
        ctx = plugin._resolve_user_from_kwargs({"stream_id": "unknown"})
        assert ctx is None

    def test_api_resolve_with_explicit_user_id(self) -> None:
        plugin = self._make_plugin_with_users([
            UserContext(qq="11111", data_dir="/tmp/11111"),
            UserContext(qq="22222", data_dir="/tmp/22222"),
        ])
        ctx = plugin._resolve_user_from_api("22222")
        assert ctx is not None
        assert ctx.qq == "22222"

    def test_api_resolve_falls_back_to_first(self) -> None:
        plugin = self._make_plugin_with_users([
            UserContext(qq="11111", data_dir="/tmp/11111"),
            UserContext(qq="22222", data_dir="/tmp/22222"),
        ])
        # 未指定 user_id → 回退到第一个
        ctx = plugin._resolve_user_from_api("")
        assert ctx is not None
        assert ctx.qq == "11111"

    def test_api_resolve_nonexistent_user_id_falls_back(self) -> None:
        plugin = self._make_plugin_with_users([
            UserContext(qq="11111", data_dir="/tmp/11111"),
        ])
        ctx = plugin._resolve_user_from_api("99999")
        # 找不到时回退到第一个
        assert ctx is not None
        assert ctx.qq == "11111"
