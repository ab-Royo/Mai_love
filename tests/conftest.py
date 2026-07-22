import os
import sys
from types import ModuleType

from pydantic import BaseModel, Field

# 确保插件包可被导入：
# 项目根目录是 g:/Dev/Mai_love，包名为 Mai_love
# Python import Mai_love.plugin 需要 g:/Dev 在 sys.path 中
_project_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_parent not in sys.path:
    sys.path.insert(0, _project_parent)

sdk = ModuleType("maibot_sdk")
sdk.PluginConfigBase = BaseModel
sdk.Field = Field

# mock decorators — 均透传被装饰函数
def _identity_dec(*args: object, **kwargs: object) -> object:
    """通用装饰器 mock：有参或无参调用均透传原函数。"""
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]

    def dec(fn: object) -> object:
        return fn
    return dec

sdk.HookHandler = _identity_dec
sdk.Tool = _identity_dec
sdk.Command = _identity_dec
sdk.API = _identity_dec

# MaiBotPlugin 基类 mock
class _MockPluginBase:
    pass

sdk.MaiBotPlugin = _MockPluginBase

# maibot_sdk.types mock
types_mod = ModuleType("maibot_sdk.types")

class _MockErrorPolicy:
    SKIP = "SKIP"

types_mod.ErrorPolicy = _MockErrorPolicy

class _MockHookMode:
    OBSERVE = "OBSERVE"

types_mod.HookMode = _MockHookMode

class _MockHookOrder:
    LATE = "LATE"

types_mod.HookOrder = _MockHookOrder

sys.modules.setdefault("maibot_sdk", sdk)
sys.modules.setdefault("maibot_sdk.types", types_mod)
