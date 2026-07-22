import sys
from types import ModuleType

from pydantic import BaseModel, Field


sdk = ModuleType("maibot_sdk")
sdk.PluginConfigBase = BaseModel
sdk.Field = Field
sys.modules.setdefault("maibot_sdk", sdk)
