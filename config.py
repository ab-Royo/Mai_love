"""麦麦恋人（MaiLover）WebUI 配置模型。

提供符合 MaiBot PluginConfigBase 规范的配置定义，
支持 WebUI 表单渲染与多语言说明。

v2.0.0: 新增多用户支持 — target_qqs 列表、PerUserConfigOverride、配置合并工具。
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any, ClassVar, Dict, List, Literal, Optional

from maibot_sdk import Field, PluginConfigBase
from pydantic import BaseModel, field_validator


def _schema_i18n(
    *,
    label_en: str,
    label_ja: str,
    hint_en: Optional[str] = None,
    hint_ja: Optional[str] = None,
    placeholder_en: Optional[str] = None,
    placeholder_ja: Optional[str] = None,
) -> Dict[str, Dict[str, str]]:
    """构造 WebUI 配置项多语言说明。"""

    i18n: Dict[str, Dict[str, str]] = {
        "en_US": {"label": label_en},
        "ja_JP": {"label": label_ja},
    }
    if hint_en is not None:
        i18n["en_US"]["hint"] = hint_en
    if hint_ja is not None:
        i18n["ja_JP"]["hint"] = hint_ja
    if placeholder_en is not None:
        i18n["en_US"]["placeholder"] = placeholder_en
    if placeholder_ja is not None:
        i18n["ja_JP"]["placeholder"] = placeholder_ja
    return i18n


# ---------------------------------------------------------------------------
# 插件总开关
# ---------------------------------------------------------------------------

CONFIG_SCHEMA_VERSION = "2.0.0"


class PluginConfig(PluginConfigBase):
    """控制插件是否启用。关闭后插件完全静默，不主动找你说话。"""

    __ui_label__: ClassVar[str] = "插件设置"
    __ui_order__: ClassVar[int] = 0

    config_version: str = Field(
        default=CONFIG_SCHEMA_VERSION,
        description="配置 schema 版本，请勿手动修改。",
        json_schema_extra={
            "disabled": True,
            "hidden": True,
            "label": "配置版本",
            "i18n": _schema_i18n(label_en="Config version", label_ja="設定バージョン"),
            "order": 99,
        },
    )
    llm_model: Literal["reply", "planner", "utils"] = Field(
        default="planner",
        description="生成日程和回复使用的模型。reply=回复模型，planner=规划模型，utils=工具模型。",
        json_schema_extra={
            "hint": "和 MaiBot 里配的模型名对应。planner 通用性好，reply 回复更自然。",
            "i18n": _schema_i18n(
                label_en="LLM Model",
                label_ja="LLMモデル",
                hint_en="Matches model names configured in MaiBot. planner is versatile, reply is more natural.",
                hint_ja="MaiBotで設定したモデル名に対応。plannerは汎用的、replyはより自然な返信。",
            ),
            "label": "LLM 模型",
            "order": 1,
        },
    )
    enabled: bool = Field(
        default=True,
        description="插件总开关。开启后麦麦才会主动找你说话；关闭则完全静默，不检查日程、不主动发消息。热更新即时生效。",
        json_schema_extra={
            "hint": "总闸：打开麦麦才会主动找你说话。关闭后一切主动行为（早安晚安、日程、想念）全部停止。热更新即时生效。",
            "i18n": _schema_i18n(
                label_en="Enable Plugin",
                label_ja="プラグインを有効化",
                hint_en="Master switch. When off, MaiMai is completely silent — no proactive messages.",
                hint_ja="マスタースイッチ。オフにすると麦麦は完全に沈黙し、能動的メッセージも送信しません。",
            ),
            "label": "启用插件",
            "order": 0,
        },
    )


# ---------------------------------------------------------------------------
# 白名单配置
# ---------------------------------------------------------------------------


class WhitelistConfig(PluginConfigBase):
    """绑定目标 QQ 号（可多个）。只有这些号码的私聊消息会触发麦麦的所有主动逻辑。"""

    __ui_label__: ClassVar[str] = "白名单设置"
    __ui_order__: ClassVar[int] = 1

    target_qqs: List[int] = Field(
        default_factory=list,
        description="目标 QQ 号列表。支持多个用户，每个用户独立存储数据和配置。修改后需要重启插件。",
        json_schema_extra={
            "hint": "可以填多个 QQ 号。每个号的私聊都会激活麦麦。数据互不干扰。改完需要重启插件或 MaiBot。",
            "i18n": _schema_i18n(
                label_en="Target QQ Numbers (List)",
                label_ja="対象QQ番号（リスト）",
                hint_en="One or more QQ numbers. Each user gets independent data and config. Restart required after change.",
                hint_ja="複数のQQ番号を指定可能。各ユーザーは独立したデータと設定を持ちます。変更後は再起動が必要です。",
                placeholder_en="[123456789, 987654321]",
                placeholder_ja="[123456789, 987654321]",
            ),
            "label": "目标 QQ 号列表",
            "order": 0,
            "placeholder": "[123456789]",
        },
    )
    target_qq: int = Field(
        default=0,
        description="[已弃用] 单用户目标 QQ。请改用 target_qqs 列表。若同时设置两者，会自动合并去重。",
        json_schema_extra={
            "hint": "已弃用，请迁移到「目标 QQ 号列表」。若同时设置，会自动合并去重。",
            "i18n": _schema_i18n(
                label_en="Target QQ (Deprecated)",
                label_ja="対象QQ（非推奨）",
                hint_en="Deprecated. Please use the QQ number list instead. Values from both fields are merged.",
                hint_ja="非推奨です。QQ番号リストを使用してください。両方の値は自動的にマージされます。",
                placeholder_en="123456789",
                placeholder_ja="123456789",
            ),
            "label": "目标 QQ 号（已弃用）",
            "order": 1,
            "placeholder": "123456789",
        },
    )

    # 过滤器：排除这些默认占位值
    _INVALID_DEFAULTS: ClassVar[set] = {0, 123456789}

    def get_effective_qqs(self) -> list[int]:
        """合并 target_qqs 和 target_qq，自动去重和过滤默认值。

        Returns:
            有效 QQ 号列表（已去重）。
        """
        qqs: list[int] = list(self.target_qqs)
        if self.target_qq and self.target_qq not in self._INVALID_DEFAULTS:
            if self.target_qq not in qqs:
                qqs.append(self.target_qq)
        return [q for q in qqs if q not in self._INVALID_DEFAULTS]

    @field_validator("target_qq", mode="before")
    @classmethod
    def _normalize_target_qq(cls, value: Any) -> int:
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return 0
        if isinstance(value, (int, float)):
            return int(value)
        return 0

    @field_validator("target_qqs", mode="before")
    @classmethod
    def _normalize_target_qqs(cls, value: Any) -> list[int]:
        """规范化 target_qqs：支持字符串列表、单个字符串等输入。"""
        if value is None:
            return []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    value = parsed
            except (json.JSONDecodeError, ValueError):
                return []
        if not isinstance(value, list):
            return []
        result: list[int] = []
        for item in value:
            if isinstance(item, str):
                try:
                    result.append(int(item.strip()))
                except ValueError:
                    pass
            elif isinstance(item, (int, float)):
                result.append(int(item))
        return result


# ---------------------------------------------------------------------------
# 日程与巡检配置
# ---------------------------------------------------------------------------


class ScheduleConfig(PluginConfigBase):
    """控制麦麦什么时候检查、一天最多说几句话、说话冷却多久。"""

    __ui_label__: ClassVar[str] = "调度设置"
    __ui_order__: ClassVar[int] = 2

    generate_hour: int = Field(
        default=3,
        description="每天几点自动生成麦麦今天的活动安排。0~23 之间的整数。",
        json_schema_extra={
            "hint": "默认 3 点（凌晨生成，不影响白天）。改成 0~23 之间的整数。",
            "i18n": _schema_i18n(
                label_en="Schedule Generation Hour",
                label_ja="スケジュール生成時刻",
                hint_en="Which hour (0-23) to generate the daily schedule. Default 3 AM, during off-peak time.",
                hint_ja="毎日のスケジュールを生成する時間（0-23）。デフォルトは午前3時、オフピーク時間です。",
            ),
            "label": "日程生成时间（时）",
            "order": 0,
        },
    )
    check_interval_minutes: int = Field(
        default=10,
        description="麦麦每隔多少分钟看一眼现在该不该找你说话。建议 5~10 分钟。",
        json_schema_extra={
            "hint": "值越小麦麦反应越快。默认 10 分钟即可。",
            "i18n": _schema_i18n(
                label_en="Check Interval (min)",
                label_ja="チェック間隔（分）",
                hint_en="How often MaiMai checks if it's time to speak. Lower = faster response. Default 5.",
                hint_ja="麦麦が話すタイミングをチェックする頻度です。低いほど反応が速くなります。",
            ),
            "label": "巡检间隔（分钟）",
            "order": 1,
        },
    )
    daily_max_speak: int = Field(
        default=5,
        description="麦麦一天最多主动找你几次。到了上限就不找了，连早安晚安也不发。0=完全禁言。",
        json_schema_extra={
            "hint": "含早安晚安。想话多调大（8~12），想安静调小（2~3）。0=完全静音。",
            "i18n": _schema_i18n(
                label_en="Daily Max Messages",
                label_ja="1日の最大メッセージ数",
                hint_en="Hard cap on proactive messages per day, including morning/night. Increase for more chatty, decrease for quieter. 0 = total silence.",
                hint_ja="1日の能動的メッセージの上限（おはよう/おやすみを含む）。おしゃべりにしたい場合は増やし、静かにしたい場合は減らします。0 = 完全無音。",
            ),
            "label": "每日发言上限",
            "order": 2,
        },
    )
    user_cooldown_minutes: int = Field(
        default=30,
        description="你刚发完消息后，麦麦多久之内不会主动找你。比如你刚说了句话，如果设为 30 分钟，这 30 分钟内麦麦不会突然蹦出来打扰你。早安晚安不受此限制。",
        json_schema_extra={
            "hint": "冷却期（分钟）。你刚发完消息后麦麦会闭嘴这多久。避免「刚说完就又来」的骚扰感。早安晚安无视冷却。",
            "i18n": _schema_i18n(
                label_en="User Cooldown (min)",
                label_ja="ユーザークールダウン（分）",
                hint_en="After you send a message, MaiMai stays quiet for this many minutes to avoid feeling intrusive. Morning/night greetings ignore cooldown.",
                hint_ja="あなたがメッセージを送った後、麦麦がこの分数だけ静かにします。押し付けがましさを避けるためです。おはよう/おやすみはクールダウンを無視します。",
            ),
            "label": "用户冷却时间（分钟）",
            "order": 3,
        },
    )
    proactive_trigger_enabled: bool = Field(
        default=True,
        description="麦麦会不会主动找你说话。关掉后麦麦就乖乖等你先说话，不会主动来烦你。日程表照常生成。",
        json_schema_extra={
            "hint": "关闭后早安晚安、想念、日常全部停，但日程照常生成。适合想安静一阵。",
            "i18n": _schema_i18n(
                label_en="Proactive Trigger",
                label_ja="プロアクティブトリガー",
                hint_en="When off, schedule still generates but MaiMai won't proactively message you.",
                hint_ja="オフ時、スケジュールは生成されますが麦麦は能動的にメッセージを送信しません。",
            ),
            "label": "主动触发开关",
            "order": 4,
        },
    )

    @field_validator("generate_hour", mode="before")
    @classmethod
    def _normalize_hour(cls, value: Any) -> int:
        return _normalize_int_in_range(value, 3, 0, 23)

    @field_validator("check_interval_minutes", mode="before")
    @classmethod
    def _normalize_interval(cls, value: Any) -> int:
        return _normalize_int_in_range(value, 5, 0, 60)

    @field_validator("daily_max_speak", mode="before")
    @classmethod
    def _normalize_max_speak(cls, value: Any) -> int:
        return _normalize_int_in_range(value, 5, 0, 100)

    @field_validator("user_cooldown_minutes", mode="before")
    @classmethod
    def _normalize_cooldown(cls, value: Any) -> int:
        return _normalize_int_in_range(value, 5, 0, 60)


# ---------------------------------------------------------------------------
# 概率配置
# ---------------------------------------------------------------------------


class ProbabilityConfig(PluginConfigBase):
    """控制麦麦主动找你说话的概率。三个独立概率分别控制日常、想念和日程节点。"""

    __ui_label__: ClassVar[str] = "概率设置"
    __ui_order__: ClassVar[int] = 3

    default_speak_rate: float = Field(
        default=0.6,
        description="麦麦日常主动找你说话的概率。0.6=60% 概率会来搭话。调高更黏人，调低更高冷。0=只在她有事（早晚安/想念/到点活动）时才说话。",
        json_schema_extra={
            "hint": "0.0~1.0，越高越话多。",
            "i18n": _schema_i18n(
                label_en="Default Speak Rate",
                label_ja="デフォルト発話率",
                hint_en="Probability MaiMai chats with you. Higher = more talkative. 0 = only morning/night/missing-you.",
                hint_ja="麦麦が日常的に話す確率です。高いほどおしゃべりになります。0 = おはよう/おやすみ/「会いたい」のみ。",
            ),
            "label": "常规发言概率",
            "order": 0,
        },
    )
    miss_speak_rate: float = Field(
        default=0.5,
        description="你很久没理她时，麦麦跑来说想你的概率。默认 50%，基本一定会说。每天最多 1 次。",
        json_schema_extra={
            "hint": "0.0~1.0，默认 50%。",
            "i18n": _schema_i18n(
                label_en="Missing-You Rate",
                label_ja="「会いたい」発話率",
                hint_en="Probability MaiMai says she misses you when you've been silent. 50% default, once per day.",
                hint_ja="あなたが長く沈黙しているとき麦麦が「会いたい」と言う確率です。デフォルト 50%、1 日 1 回。",
            ),
            "label": "想念触发概率",
            "order": 1,
        },
    )
    activity_trigger_rate: float = Field(
        default=0.6,
        description="日程节点到点时，麦麦分享她在干嘛的概率。比如 14:00 安排了晒太阳，到点了按这个概率决定要不要告诉你。",
        json_schema_extra={
            "hint": "0.0~1.0，默认 60% 比较自然。",
            "i18n": _schema_i18n(
                label_en="Activity Trigger Rate",
                label_ja="アクティビティトリガー率",
                hint_en="Probability MaiMai shares her activity at schedule nodes. 60% feels natural.",
                hint_ja="スケジュールノードで麦麦が活動を共有する確率です。60%が自然です。",
            ),
            "label": "日程节点触发概率",
            "order": 2,
        },
    )

    @field_validator("default_speak_rate", "miss_speak_rate", "activity_trigger_rate", mode="before")
    @classmethod
    def _normalize_rate(cls, value: Any) -> float:
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except (ValueError, TypeError):
                return 0.5
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
        return 0.5


# ---------------------------------------------------------------------------
# 时间窗口配置
# ---------------------------------------------------------------------------


class TimeWindowsConfig(PluginConfigBase):
    """设定早安晚安的时间范围，以及多久不说话算「想你了」。"""

    __ui_label__: ClassVar[str] = "时间窗口"
    __ui_order__: ClassVar[int] = 4

    morning_start: str = Field(
        default="06:00",
        description="早安时间窗开始。在这个时间段内麦麦会主动跟你说早安。格式 HH:MM。",
        json_schema_extra={
            "hint": "格式 HH:MM，如 06:00 表示早上 6 点开始。",
            "i18n": _schema_i18n(
                label_en="Morning Start",
                label_ja="おはよう開始",
                hint_en="Start of morning window (HH:MM).",
                hint_ja="おはようの時間枠開始（HH:MM）。",
                placeholder_en="06:00",
                placeholder_ja="06:00",
            ),
            "label": "早安开始时间",
            "order": 0,
            "placeholder": "06:00",
        },
    )
    morning_end: str = Field(
        default="09:00",
        description="早安时间窗结束。",
        json_schema_extra={
            "hint": "格式 HH:MM。",
            "i18n": _schema_i18n(
                label_en="Morning End",
                label_ja="おはよう終了",
                hint_en="End of morning window (HH:MM).",
                hint_ja="おはようの時間枠終了（HH:MM）。",
                placeholder_en="09:00",
                placeholder_ja="09:00",
            ),
            "label": "早安结束时间",
            "order": 1,
            "placeholder": "09:00",
        },
    )
    night_start: str = Field(
        default="22:00",
        description="晚安时间窗开始。在这个时间段内麦麦会主动跟你说晚安。格式 HH:MM。",
        json_schema_extra={
            "hint": "格式 HH:MM，如 22:00。",
            "i18n": _schema_i18n(
                label_en="Night Start",
                label_ja="おやすみ開始",
                hint_en="Start of night window (HH:MM).",
                hint_ja="おやすみの時間枠開始（HH:MM）。",
                placeholder_en="22:00",
                placeholder_ja="22:00",
            ),
            "label": "晚安开始时间",
            "order": 2,
            "placeholder": "22:00",
        },
    )
    night_end: str = Field(
        default="23:59",
        description="晚安时间窗结束。",
        json_schema_extra={
            "hint": "格式 HH:MM。",
            "i18n": _schema_i18n(
                label_en="Night End",
                label_ja="おやすみ終了",
                hint_en="End of night window (HH:MM).",
                hint_ja="おやすみの時間枠終了（HH:MM）。",
                placeholder_en="23:59",
                placeholder_ja="23:59",
            ),
            "label": "晚安结束时间",
            "order": 3,
            "placeholder": "23:59",
        },
    )
    miss_trigger_hours: int = Field(
        default=6,
        description="你多久不理麦麦，她就会想你。默认 6 小时——超过 6 小时没说话，她就有概率跑来说想你。",
        json_schema_extra={
            "hint": "前提是接下来没有安排其他活动、且今天还没说过想你。",
            "i18n": _schema_i18n(
                label_en="Miss Trigger (hours)",
                label_ja="「会いたい」トリガー（時間）",
                hint_en="Only triggers if no other activity is scheduled soon and she hasn't said it today.",
                hint_ja="近くに他の活動がなく、今日まだ「会いたい」と言っていない場合のみトリガーされます。",
            ),
            "label": "想念触发时长（小时）",
            "order": 4,
        },
    )
    silence_start: str = Field(
        default="00:00",
        description="静默时段开始。在这个时间之后麦麦不主动找你说话，让你好好休息。格式 HH:MM。",
        json_schema_extra={
            "hint": "比如设 00:00，表示零点后麦麦闭嘴。配合 silence_end 一起用。格式 HH:MM。",
            "i18n": _schema_i18n(
                label_en="Silence Start",
                label_ja="サイレンス開始",
                hint_en="Start of quiet hours. MaiMai won't proactively message after this time. Format HH:MM.",
                hint_ja="サイレンス時間の開始。この時間以降、麦麦は能動的にメッセージを送りません。形式 HH:MM。",
                placeholder_en="00:00",
                placeholder_ja="00:00",
            ),
            "label": "静默开始时间",
            "order": 5,
            "placeholder": "00:00",
        },
    )
    silence_end: str = Field(
        default="08:00",
        description="静默时段结束。过了这个时间麦麦恢复正常，可以主动找你。格式 HH:MM。",
        json_schema_extra={
            "hint": "比如设 08:00，表示早上 8 点后麦麦恢复话痨。格式 HH:MM。",
            "i18n": _schema_i18n(
                label_en="Silence End",
                label_ja="サイレンス終了",
                hint_en="End of quiet hours. MaiMai resumes proactive messaging after this time. Format HH:MM.",
                hint_ja="サイレンス時間の終了。この時間以降、麦麦は能動的メッセージを再開します。形式 HH:MM。",
                placeholder_en="08:00",
                placeholder_ja="08:00",
            ),
            "label": "静默结束时间",
            "order": 6,
            "placeholder": "08:00",
        },
    )


# ---------------------------------------------------------------------------
# 好感度配置
# ---------------------------------------------------------------------------


class AffectionConfig(PluginConfigBase):
    """好感度档位：0=温柔有分寸，1=活泼热情，2=撒娇卖萌。"""

    __ui_label__: ClassVar[str] = "好感度"
    __ui_order__: ClassVar[int] = 5

    current_level: Literal[0, 1, 2] = Field(
        default=0,
        description="决定麦麦说话的语气。0=温柔有分寸，1=活泼热情，2=撒娇卖萌。",
        json_schema_extra={
            "hint": "0=温柔 | 1=活泼 | 2=撒娇。",
            "i18n": _schema_i18n(
                label_en="Affection Level",
                label_ja="好感度レベル",
                hint_en="0=gentle | 1=lively | 2=cute. Controls her tone.",
                hint_ja="0=優しい | 1=活発 | 2=甘えん坊。話し方のトーンを制御します。",
            ),
            "label": "好感度档位",
            "order": 0,
        },
    )

    @field_validator("current_level", mode="before")
    @classmethod
    def _normalize_level(cls, value: Any) -> int:
        return _normalize_int_in_range(value, 0, 0, 2)


# ---------------------------------------------------------------------------
# 顶层配置聚合
# ---------------------------------------------------------------------------


class MaiLoverPluginSettings(PluginConfigBase):
    """麦麦恋人插件完整配置。包含开关、白名单、调度、概率、时间窗、好感度六大模块。"""

    plugin: PluginConfig = Field(default_factory=PluginConfig)
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    probability: ProbabilityConfig = Field(default_factory=ProbabilityConfig)
    time_windows: TimeWindowsConfig = Field(default_factory=TimeWindowsConfig)
    affection: AffectionConfig = Field(default_factory=AffectionConfig)


# ---------------------------------------------------------------------------
# 用户独立配置覆盖模型（非 WebUI，仅用于文件校验）
# ---------------------------------------------------------------------------


class _PartialScheduleConfig(BaseModel):
    """ScheduleConfig 的可选部分覆盖。"""

    generate_hour: Optional[int] = None
    check_interval_minutes: Optional[int] = None
    daily_max_speak: Optional[int] = None
    user_cooldown_minutes: Optional[int] = None
    proactive_trigger_enabled: Optional[bool] = None


class _PartialProbabilityConfig(BaseModel):
    """ProbabilityConfig 的可选部分覆盖。"""

    default_speak_rate: Optional[float] = None
    miss_speak_rate: Optional[float] = None
    activity_trigger_rate: Optional[float] = None


class _PartialTimeWindowsConfig(BaseModel):
    """TimeWindowsConfig 的可选部分覆盖。"""

    morning_start: Optional[str] = None
    morning_end: Optional[str] = None
    night_start: Optional[str] = None
    night_end: Optional[str] = None
    miss_trigger_hours: Optional[int] = None
    silence_start: Optional[str] = None
    silence_end: Optional[str] = None


class _PartialAffectionConfig(BaseModel):
    """AffectionConfig 的可选部分覆盖。"""

    current_level: Optional[int] = None


class _PartialPluginConfig(BaseModel):
    """PluginConfig 的可选部分覆盖。"""

    llm_model: Optional[str] = None
    enabled: Optional[bool] = None


class PerUserConfigOverride(BaseModel):
    """用户独立配置覆盖模型。

    所有字段均为可选，仅为校验 `config_override.json` 的结构。
    未指定的字段将沿用全局配置。
    """

    plugin: Optional[_PartialPluginConfig] = None
    schedule: Optional[_PartialScheduleConfig] = None
    probability: Optional[_PartialProbabilityConfig] = None
    time_windows: Optional[_PartialTimeWindowsConfig] = None
    affection: Optional[_PartialAffectionConfig] = None


def load_user_config_overrides(user_data_dir: str) -> dict[str, Any]:
    """从用户数据目录加载配置覆盖文件。

    Args:
        user_data_dir: 用户数据目录（如 data/123456789/）。

    Returns:
        配置覆盖字典。文件不存在或损坏时返回空字典。
    """
    override_file = os.path.join(user_data_dir, "config_override.json")
    if not os.path.isfile(override_file):
        return {}
    try:
        with open(override_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

    if not isinstance(raw, dict):
        return {}

    # 校验结构（忽略未知/无效字段）
    try:
        PerUserConfigOverride(**raw)
    except Exception:
        # 校验失败 → 静默降级为全局配置
        return {}

    # 过滤掉 None 值，确保合并时不覆盖全局配置中的实际值
    return _strip_none_values(raw)


def merge_config_with_overrides(
    global_config: MaiLoverPluginSettings,
    overrides: dict[str, Any],
) -> MaiLoverPluginSettings:
    """深合并全局配置与用户覆盖，返回新的配置实例。

    合并规则：用户覆盖中非 None 的值优先，其余沿用全局。
    不对传入的 global_config 做修改（返回全新实例）。

    Args:
        global_config: 全局插件配置。
        overrides: load_user_config_overrides() 返回的覆盖字典。

    Returns:
        合并后的新配置实例。
    """
    merged_dict = deepcopy(global_config.model_dump())
    _deep_merge(merged_dict, overrides)
    return MaiLoverPluginSettings(**merged_dict)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """原地深合并 override 到 base 中。"""
    for key, value in override.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(base[key], value)
        elif value is not None:
            base[key] = value


def _strip_none_values(d: dict[str, Any]) -> dict[str, Any]:
    """递归移除字典中值为 None 的键。"""
    result: dict[str, Any] = {}
    for key, value in d.items():
        if isinstance(value, dict):
            stripped = _strip_none_values(value)
            if stripped:
                result[key] = stripped
        elif value is not None:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# 通用校验辅助函数
# ---------------------------------------------------------------------------


def _normalize_int_in_range(value: Any, default: int, low: int, high: int) -> int:
    """规范化整数并限制在 [low, high] 范围内。"""
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except (ValueError, TypeError):
            return default
    if isinstance(value, float):
        value = int(value)
    if isinstance(value, int):
        return max(low, min(high, value))
    return default
