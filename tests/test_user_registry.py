"""测试 UserRegistry 的注册、查找、更新、注销功能。"""

import pytest
from Mai_love.plugin import UserContext, UserRegistry


def make_ctx(qq: str = "123", stream_id: str = "") -> UserContext:
    """构造最小 UserContext 用于测试。"""
    return UserContext(qq=qq, data_dir=f"/tmp/{qq}", stream_id=stream_id)


class TestUserRegistry:
    """UserRegistry 单元测试。"""

    def test_register_and_lookup_by_qq(self) -> None:
        reg = UserRegistry()
        ctx = make_ctx("111")
        reg.register(ctx)
        assert reg.get_by_qq("111") is ctx
        assert reg.get_by_qq("999") is None

    def test_register_and_lookup_by_stream_id(self) -> None:
        reg = UserRegistry()
        ctx = make_ctx("111", stream_id="sid_abc")
        reg.register(ctx)
        assert reg.get_by_stream_id("sid_abc") is ctx
        assert reg.get_by_stream_id("sid_xyz") is None

    def test_lookup_nonexistent_returns_none(self) -> None:
        reg = UserRegistry()
        assert reg.get_by_qq("123") is None
        assert reg.get_by_stream_id("") is None
        assert reg.get_by_stream_id("nonexistent") is None

    def test_update_stream_id_replaces_old_mapping(self) -> None:
        reg = UserRegistry()
        ctx = make_ctx("111", stream_id="old_sid")
        reg.register(ctx)

        reg.update_stream_id("111", "new_sid")

        # 旧映射应失效
        assert reg.get_by_stream_id("old_sid") is None
        # 新映射应生效
        assert reg.get_by_stream_id("new_sid") is ctx
        # QQ 查找仍然生效
        assert reg.get_by_qq("111") is ctx
        # ctx.stream_id 已更新
        assert ctx.stream_id == "new_sid"

    def test_update_stream_id_nonexistent_qq_noop(self) -> None:
        reg = UserRegistry()
        # 不应抛异常
        reg.update_stream_id("999", "some_sid")
        assert reg.get_by_qq("999") is None

    def test_unregister_removes_both_mappings(self) -> None:
        reg = UserRegistry()
        ctx = make_ctx("111", stream_id="sid")
        reg.register(ctx)
        reg.unregister("111")

        assert reg.get_by_qq("111") is None
        assert reg.get_by_stream_id("sid") is None

    def test_get_all_returns_all_contexts(self) -> None:
        reg = UserRegistry()
        a = make_ctx("111")
        b = make_ctx("222")
        c = make_ctx("333")
        reg.register(a)
        reg.register(b)
        reg.register(c)

        all_ctx = reg.get_all()
        assert len(all_ctx) == 3
        assert a in all_ctx
        assert b in all_ctx
        assert c in all_ctx

    def test_get_first_returns_first_registered(self) -> None:
        reg = UserRegistry()
        assert reg.get_first() is None

        a = make_ctx("111")
        reg.register(a)
        assert reg.get_first() is a

    def test_register_without_stream_id(self) -> None:
        reg = UserRegistry()
        ctx = make_ctx("111", stream_id="")
        reg.register(ctx)
        # QQ 查找应生效
        assert reg.get_by_qq("111") is ctx
        # stream_id 不应建立映射
        assert reg.get_by_stream_id("") is None
