from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int = 0
    api_hash: str = ""
    phone: str = ""
    session_path: Path = Path("data/telegram_account")
    allow_private: bool = False


@dataclass(frozen=True)
class TelegramWebConfig:
    url: str
    profile_path: Path
    login_screenshot_path: Path
    error_screenshot_path: Path
    headless: bool = True
    poll_seconds: float = 5.0
    allowed_chat_ids: tuple[str, ...] = ()
    allowed_chat_titles: tuple[str, ...] = ()
    mention_keywords: tuple[str, ...] = ()
    self_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    thinking_enabled: bool = False
    temperature: float = 1.15
    max_tokens: int = 220
    timeout_seconds: float = 45.0


@dataclass(frozen=True)
class TriggerConfig:
    default_mode: str = "mention"
    free_reply_probability: float = 0.16
    free_decision_enabled: bool = True
    min_message_chars: int = 2
    cooldown_seconds: int = 45
    direct_cooldown_seconds: int = 4
    fast_cooldown_seconds: int = 2
    # 0 表示不限制；仍受每个群的冷却时间控制。
    max_replies_per_hour: int = 0
    # 单个群按北京时间自然日计算；0 表示不限制。
    max_replies_per_chat_per_day: int = 0
    # 单群每日软目标范围；仅用于调节活跃群里的参与概率，不会强制凑数。
    target_replies_per_chat_per_day_min: int = 0
    target_replies_per_chat_per_day_max: int = 0


@dataclass(frozen=True)
class ProactiveConfig:
    enabled: bool = False
    active_only: bool = True
    probability: float = 0.25
    min_interval_seconds: int = 600
    max_replies_per_hour: int = 4
    open_prompt_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextConfig:
    message_count: int = 18
    max_message_chars: int = 500


@dataclass(frozen=True)
class BehaviorConfig:
    typing_enabled: bool = True
    typing_min_seconds: float = 0.8
    typing_max_seconds: float = 4.5
    direct_typing_min_seconds: float = 0.3
    direct_typing_max_seconds: float = 1.4
    active_typing_min_seconds: float = 0.15
    active_typing_max_seconds: float = 0.8
    recent_typing_min_seconds: float = 0.1
    recent_typing_max_seconds: float = 0.5
    active_window_seconds: int = 45
    active_message_threshold: int = 4
    recent_exchange_seconds: int = 180
    reply_to_trigger: bool = True
    max_reply_chars: int = 260


@dataclass(frozen=True)
class PersonaConfig:
    name: str = "自然群友"
    identity: str = "你是账号主人的群聊回复助手。"
    tone: tuple[str, ...] = ()
    habits: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()
    examples: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class AppConfig:
    root: Path
    telegram: TelegramConfig
    telegram_web: TelegramWebConfig
    deepseek: DeepSeekConfig
    trigger: TriggerConfig
    proactive: ProactiveConfig
    context: ContextConfig
    behavior: BehaviorConfig
    persona: PersonaConfig
    database_path: Path = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "database_path", self.root / "data" / "state.db")


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"配置段 {key!r} 必须是对象")
    return value


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"缺少环境变量 {name}，请复制 .env.example 为 .env 后填写")
    return value


def _mode(value: Any) -> str:
    mode = str(value or "mention").strip().lower()
    if mode not in {"mention", "free", "off"}:
        raise ConfigError("trigger.default_mode 只能是 mention、free 或 off")
    return mode


def load_config(root: Path | None = None, require_deepseek: bool = True) -> AppConfig:
    root = (root or Path.cwd()).resolve()
    load_dotenv(root / ".env")
    config_value = os.getenv("APP_CONFIG", "config/config.yaml")
    config_path = Path(config_value)
    if not config_path.is_absolute():
        config_path = root / config_path
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在：{config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("配置文件根节点必须是对象")

    telegram = _section(raw, "telegram")
    telegram_web = _section(raw, "telegram_web")
    deepseek = _section(raw, "deepseek")
    trigger = _section(raw, "trigger")
    proactive = _section(raw, "proactive")
    context = _section(raw, "context")
    behavior = _section(raw, "behavior")
    persona = _section(raw, "persona")

    api_id_value = os.getenv("TELEGRAM_API_ID", "").strip()
    try:
        api_id = int(api_id_value) if api_id_value else 0
    except ValueError as exc:
        raise ConfigError("TELEGRAM_API_ID 必须是数字") from exc

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if require_deepseek and not api_key:
        raise ConfigError("缺少环境变量 DEEPSEEK_API_KEY")

    session_value = str(telegram.get("session_path", "data/telegram_account"))
    session_path = Path(session_value)
    if not session_path.is_absolute():
        session_path = root / session_path

    probability = float(trigger.get("free_reply_probability", 0.16))
    if not 0 <= probability <= 1:
        raise ConfigError("free_reply_probability 必须在 0 到 1 之间")
    proactive_probability = float(proactive.get("probability", 0.25))
    if not 0 <= proactive_probability <= 1:
        raise ConfigError("proactive.probability 必须在 0 到 1 之间")

    typing_min = float(behavior.get("typing_min_seconds", 0.8))
    typing_max = float(behavior.get("typing_max_seconds", 4.5))
    direct_typing_min = float(behavior.get("direct_typing_min_seconds", 0.3))
    direct_typing_max = float(behavior.get("direct_typing_max_seconds", 1.4))
    active_typing_min = float(behavior.get("active_typing_min_seconds", 0.15))
    active_typing_max = float(behavior.get("active_typing_max_seconds", 0.8))
    recent_typing_min = float(behavior.get("recent_typing_min_seconds", 0.1))
    recent_typing_max = float(behavior.get("recent_typing_max_seconds", 0.5))
    delay_ranges = (
        (typing_min, typing_max),
        (direct_typing_min, direct_typing_max),
        (active_typing_min, active_typing_max),
        (recent_typing_min, recent_typing_max),
    )
    if any(minimum < 0 or maximum < minimum for minimum, maximum in delay_ranges):
        raise ConfigError("打字延迟范围无效")

    examples_raw = persona.get("examples", []) or []
    examples: list[dict[str, str]] = []
    for item in examples_raw:
        if isinstance(item, dict) and item.get("user") and item.get("assistant"):
            examples.append({"user": str(item["user"]), "assistant": str(item["assistant"])})

    def root_path(value: Any, default: str) -> Path:
        path = Path(str(value or default))
        return path if path.is_absolute() else root / path

    return AppConfig(
        root=root,
        telegram=TelegramConfig(
            api_id=api_id,
            api_hash=os.getenv("TELEGRAM_API_HASH", "").strip(),
            phone=os.getenv("TELEGRAM_PHONE", "").strip(),
            session_path=session_path,
            allow_private=bool(telegram.get("allow_private", False)),
        ),
        telegram_web=TelegramWebConfig(
            url=str(telegram_web.get("url", "https://web.telegram.org/k/")),
            profile_path=root_path(telegram_web.get("profile_path"), "data/telegram-web-profile"),
            login_screenshot_path=root_path(
                telegram_web.get("login_screenshot_path"), "data/telegram-web-login.png"
            ),
            error_screenshot_path=root_path(
                telegram_web.get("error_screenshot_path"), "data/telegram-web-error.png"
            ),
            headless=bool(telegram_web.get("headless", True)),
            poll_seconds=max(2.0, float(telegram_web.get("poll_seconds", 5))),
            allowed_chat_ids=tuple(
                str(item).strip().lstrip("#")
                for item in (telegram_web.get("allowed_chat_ids", []) or [])
                if str(item).strip().lstrip("#").lstrip("-").isdigit()
            ),
            allowed_chat_titles=tuple(
                str(item).strip() for item in (telegram_web.get("allowed_chat_titles", []) or []) if str(item).strip()
            ),
            mention_keywords=tuple(
                str(item).strip().casefold() for item in (telegram_web.get("mention_keywords", []) or []) if str(item).strip()
            ),
            self_names=tuple(
                str(item).strip().casefold() for item in (telegram_web.get("self_names", []) or []) if str(item).strip()
            ),
        ),
        deepseek=DeepSeekConfig(
            api_key=api_key,
            base_url=str(deepseek.get("base_url", "https://api.deepseek.com")).rstrip("/"),
            model=str(deepseek.get("model", "deepseek-flash")),
            thinking_enabled=bool(deepseek.get("thinking_enabled", False)),
            temperature=float(deepseek.get("temperature", 1.15)),
            max_tokens=int(deepseek.get("max_tokens", 220)),
            timeout_seconds=float(deepseek.get("timeout_seconds", 45)),
        ),
        trigger=TriggerConfig(
            default_mode=_mode(trigger.get("default_mode", "mention")),
            free_reply_probability=probability,
            free_decision_enabled=bool(trigger.get("free_decision_enabled", True)),
            min_message_chars=max(1, int(trigger.get("min_message_chars", 2))),
            cooldown_seconds=max(0, int(trigger.get("cooldown_seconds", 45))),
            direct_cooldown_seconds=max(0, int(trigger.get("direct_cooldown_seconds", 4))),
            fast_cooldown_seconds=max(0, int(trigger.get("fast_cooldown_seconds", 2))),
            max_replies_per_hour=max(0, int(trigger.get("max_replies_per_hour", 0))),
            max_replies_per_chat_per_day=max(
                0, int(trigger.get("max_replies_per_chat_per_day", 0))
            ),
            target_replies_per_chat_per_day_min=max(
                0, int(trigger.get("target_replies_per_chat_per_day_min", 0))
            ),
            target_replies_per_chat_per_day_max=max(
                0, int(trigger.get("target_replies_per_chat_per_day_max", 0))
            ),
        ),
        proactive=ProactiveConfig(
            enabled=bool(proactive.get("enabled", False)),
            active_only=bool(proactive.get("active_only", True)),
            probability=proactive_probability,
            min_interval_seconds=max(0, int(proactive.get("min_interval_seconds", 600))),
            max_replies_per_hour=max(0, int(proactive.get("max_replies_per_hour", 4))),
            open_prompt_keywords=tuple(
                str(item).strip().casefold()
                for item in (proactive.get("open_prompt_keywords", []) or [])
                if str(item).strip()
            ),
        ),
        context=ContextConfig(
            message_count=max(3, min(50, int(context.get("message_count", 18)))),
            max_message_chars=max(50, int(context.get("max_message_chars", 500))),
        ),
        behavior=BehaviorConfig(
            typing_enabled=bool(behavior.get("typing_enabled", True)),
            typing_min_seconds=typing_min,
            typing_max_seconds=typing_max,
            direct_typing_min_seconds=direct_typing_min,
            direct_typing_max_seconds=direct_typing_max,
            active_typing_min_seconds=active_typing_min,
            active_typing_max_seconds=active_typing_max,
            recent_typing_min_seconds=recent_typing_min,
            recent_typing_max_seconds=recent_typing_max,
            active_window_seconds=max(5, int(behavior.get("active_window_seconds", 45))),
            active_message_threshold=max(2, int(behavior.get("active_message_threshold", 4))),
            recent_exchange_seconds=max(0, int(behavior.get("recent_exchange_seconds", 180))),
            reply_to_trigger=bool(behavior.get("reply_to_trigger", True)),
            max_reply_chars=max(20, int(behavior.get("max_reply_chars", 260))),
        ),
        persona=PersonaConfig(
            name=str(persona.get("name", "自然群友")),
            identity=str(persona.get("identity", "你是账号主人的群聊回复助手。")),
            tone=tuple(str(item) for item in (persona.get("tone", []) or [])),
            habits=tuple(str(item) for item in (persona.get("habits", []) or [])),
            boundaries=tuple(str(item) for item in (persona.get("boundaries", []) or [])),
            examples=tuple(examples),
        ),
    )

