from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import field_validator

from maibot_sdk import Field, PluginConfigBase

GATEWAY_NAME = "mai_api_gateway"
DEFAULT_PLATFORM = "mai_api"
DEFAULT_ACCOUNT_ID = "local-maibot"
DEFAULT_SCOPE = "default"
SUPPORTED_CONFIG_VERSION = "0.2.0"


def _schema_i18n(
    *,
    label_en: str,
    label_ja: str,
    hint_en: Optional[str] = None,
    hint_ja: Optional[str] = None,
    placeholder_en: Optional[str] = None,
    placeholder_ja: Optional[str] = None,
) -> Dict[str, Dict[str, str]]:
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


class HttpApiPluginSection(PluginConfigBase):
    """插件开关配置。"""

    __ui_label__: ClassVar[str] = "插件设置"
    __ui_icon__: ClassVar[str] = "package"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=True,
        description="是否启用 Mai API 适配器。",
        json_schema_extra={
            "label": "启用适配器",
            "i18n": _schema_i18n(
                label_en="Enable adapter",
                label_ja="アダプターを有効化",
                hint_en="When disabled, the plugin only registers the message gateway and will not start the HTTP server.",
                hint_ja="無効にすると、プラグインはメッセージゲートウェイの登録のみを行い、HTTP サーバーを起動しません。",
            ),
            "order": 0,
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="当前配置结构版本。",
        json_schema_extra={
            "disabled": True,
            "hidden": True,
            "i18n": _schema_i18n(label_en="Config version", label_ja="設定バージョン"),
            "label": "配置版本",
            "order": 99,
        },
    )
    enable_debug_log: bool = Field(
        default=False,
        description="启用后记录所有入站/出站消息原文到调试日志。",
        json_schema_extra={
            "label": "调试日志",
            "i18n": _schema_i18n(
                label_en="Debug log",
                label_ja="デバッグログ",
                hint_en="Log all inbound/outbound message content for troubleshooting.",
                hint_ja="トラブルシューティング用に全送受信メッセージの内容を記録します。",
            ),
            "order": 1,
        },
    )


class HttpApiServerSection(PluginConfigBase):
    """HTTP 服务器配置。"""

    __ui_label__: ClassVar[str] = "HTTP 服务"
    __ui_icon__: ClassVar[str] = "globe"
    __ui_order__: ClassVar[int] = 1

    enabled: bool = Field(
        default=True,
        description="是否启动 HTTP 服务器。",
        json_schema_extra={
            "label": "启用 HTTP 服务",
            "hint": "关闭后不会监听端口，但消息网关仍保持注册。",
            "i18n": _schema_i18n(
                label_en="Enable HTTP server",
                label_ja="HTTP サーバーを有効化",
                hint_en="When disabled, the HTTP server won't listen, but the gateway registration remains.",
                hint_ja="無効にしてもゲートウェイ登録は残りますが、ポートのリッスンは停止します。",
            ),
            "order": 0,
        },
    )
    host: str = Field(
        default="127.0.0.1",
        description="HTTP 服务器绑定地址。留空或 127.0.0.1 为仅本机访问。",
        json_schema_extra={
            "label": "绑定地址",
            "i18n": _schema_i18n(
                label_en="Bind address",
                label_ja="バインドアドレス",
                hint_en="Use 127.0.0.1 for local-only access.",
                hint_ja="ローカル限定アクセスには 127.0.0.1 を使用してください。",
                placeholder_en="127.0.0.1",
                placeholder_ja="127.0.0.1",
            ),
            "order": 1,
            "placeholder": "127.0.0.1",
        },
    )
    port: int = Field(
        default=8110,
        description="Mai API 服务器监听端口。",
        json_schema_extra={
            "label": "监听端口",
            "i18n": _schema_i18n(
                label_en="Port",
                label_ja="ポート",
                hint_en="Port number for the Mai API server.",
                hint_ja="Mai API サーバーのポート番号です。",
            ),
            "order": 2,
        },
    )
    token: str = Field(
        default="maibot-local-api",
        description="API 访问令牌。Bearer Token 或 X-MaiBot-Token。",
        json_schema_extra={
            "label": "访问令牌",
            "i18n": _schema_i18n(
                label_en="Access token",
                label_ja="アクセストークン",
                hint_en="Used for Bearer Authorization or X-MaiBot-Token header.",
                hint_ja="Bearer 認証または X-MaiBot-Token ヘッダーに使用します。",
                placeholder_en="maibot-local-api",
                placeholder_ja="maibot-local-api",
            ),
            "input_type": "password",
            "order": 3,
            "placeholder": "maibot-local-api",
        },
    )
    sync_timeout_sec: float = Field(
        default=45.0,
        description="同步聊天接口默认超时时间，单位为秒。",
        json_schema_extra={
            "label": "同步聊天超时",
            "hint": "/v1/chat 等待麦麦回复的最长时间。",
            "i18n": _schema_i18n(
                label_en="Sync chat timeout (sec)",
                label_ja="同期チャットタイムアウト（秒）",
                hint_en="Maximum wait time for MaiBot to reply in /v1/chat.",
                hint_ja="/v1/chat で麦麦の返信を待つ最大時間です。",
            ),
            "order": 4,
            "step": 1,
        },
    )
    max_history_per_session: int = Field(
        default=100,
        description="每个会话缓存的最大出站消息条数。",
        json_schema_extra={
            "label": "会话消息上限",
            "hint": "超出时自动丢弃旧消息。",
            "i18n": _schema_i18n(
                label_en="Max history per session",
                label_ja="セッションごとの最大履歴数",
                hint_en="Oldest messages are dropped when this limit is exceeded.",
                hint_ja="上限を超えたら古いメッセージから自動的に削除されます。",
            ),
            "order": 5,
        },
    )
    stream_chunk_size: int = Field(
        default=3,
        description="SSE 流式响应每次发送的字符数。",
        json_schema_extra={
            "label": "流式块大小",
            "hint": "每次 SSE 事件推送的字符数，越小越平滑但事件越多。",
            "i18n": _schema_i18n(
                label_en="Stream chunk size",
                label_ja="ストリームチャンクサイズ",
                hint_en="Number of characters per SSE event. Smaller = smoother but more events.",
                hint_ja="SSE イベントごとの文字数。小さいほど滑らかですがイベント数が増えます。",
            ),
            "order": 6,
            "step": 1,
        },
    )
    stream_interval_ms: int = Field(
        default=30,
        description="SSE 流式响应发送间隔，单位为毫秒。",
        json_schema_extra={
            "label": "流式间隔(ms)",
            "hint": "两次 SSE 事件之间的等待时间。",
            "i18n": _schema_i18n(
                label_en="Stream interval (ms)",
                label_ja="ストリーム間隔（ミリ秒）",
                hint_en="Delay between SSE events in milliseconds.",
                hint_ja="SSE イベント間の待機時間（ミリ秒）。",
            ),
            "order": 7,
            "step": 5,
        },
    )


_DEFAULT_LIST_TYPE = "whitelist"


class HttpApiFilterSection(PluginConfigBase):
    """用户过滤配置。"""

    __ui_label__: ClassVar[str] = "访问过滤"
    __ui_icon__: ClassVar[str] = "shield"
    __ui_order__: ClassVar[int] = 2

    enable_filter: bool = Field(
        default=False,
        description="是否启用用户白名单/黑名单过滤。关闭时仅 ban_user_id 生效。",
        json_schema_extra={
            "label": "启用过滤",
            "i18n": _schema_i18n(
                label_en="Enable filter",
                label_ja="フィルターを有効化",
                hint_en="When disabled, only ban_user_id is enforced.",
                hint_ja="無効時は ban_user_id のみが適用されます。",
            ),
            "order": 0,
        },
    )
    user_list_type: Literal["whitelist", "blacklist"] = Field(
        default=_DEFAULT_LIST_TYPE,
        description="用户列表类型：whitelist 仅允许列表中的用户，blacklist 拒绝列表中的用户。",
        json_schema_extra={
            "label": "列表类型",
            "i18n": _schema_i18n(
                label_en="List type",
                label_ja="リストタイプ",
                hint_en="Whitelist = only listed users allowed; Blacklist = listed users blocked.",
                hint_ja="ホワイトリスト＝リスト内のユーザーのみ許可；ブラックリスト＝リスト内のユーザーを拒否。",
            ),
            "order": 1,
        },
    )
    user_list: List[str] = Field(
        default_factory=list,
        description="用户 ID 列表，配合 user_list_type 使用。",
        json_schema_extra={
            "label": "用户列表",
            "i18n": _schema_i18n(
                label_en="User list",
                label_ja="ユーザーリスト",
                hint_en="User IDs for whitelist/blacklist filtering.",
                hint_ja="ホワイトリスト/ブラックリストに使うユーザー ID。",
            ),
            "order": 2,
        },
    )
    ban_user_id: List[str] = Field(
        default_factory=list,
        description="全局封禁用户列表，无论过滤开关状态始终生效。",
        json_schema_extra={
            "label": "封禁用户",
            "i18n": _schema_i18n(
                label_en="Banned users",
                label_ja="禁止ユーザー",
                hint_en="Always blocked regardless of filter toggle.",
                hint_ja="フィルターの有効/無効にかかわらず常に拒否されます。",
            ),
            "order": 3,
        },
    )
    show_dropped_messages: bool = Field(
        default=False,
        description="是否在日志中记录被过滤拒绝的请求。",
        json_schema_extra={
            "label": "记录被拒请求",
            "i18n": _schema_i18n(
                label_en="Log dropped requests",
                label_ja="拒否リクエストをログに記録",
                hint_en="Log requests rejected by the filter for debugging.",
                hint_ja="フィルターで拒否されたリクエストをデバッグ用に記録します。",
            ),
            "order": 4,
        },
    )

    @field_validator("user_list_type", mode="before")
    @classmethod
    def _normalize_list_type(cls, value: Any) -> str:
        normalized = str(value or _DEFAULT_LIST_TYPE).strip().lower()
        if normalized not in {"whitelist", "blacklist"}:
            return _DEFAULT_LIST_TYPE
        return normalized

    @field_validator("user_list", "ban_user_id", mode="before")
    @classmethod
    def _normalize_id_lists(cls, value: Any) -> List[str]:
        if value is None:
            return []
        raw = value if isinstance(value, list) else [value]
        result: List[str] = []
        seen: set[str] = set()
        for item in raw:
            normalized = str(item or "").strip()
            if not normalized or normalized in seen:
                continue
            result.append(normalized)
            seen.add(normalized)
        return result


class HttpApiAdapterSettings(PluginConfigBase):
    """Mai API 适配器完整配置。"""

    plugin: HttpApiPluginSection = Field(default_factory=HttpApiPluginSection)
    server: HttpApiServerSection = Field(default_factory=HttpApiServerSection)
    filter: HttpApiFilterSection = Field(default_factory=HttpApiFilterSection)

    def should_listen(self) -> bool:
        return self.plugin.enabled and self.server.enabled
