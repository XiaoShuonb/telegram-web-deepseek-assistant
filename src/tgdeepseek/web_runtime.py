from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .config import AppConfig
from .deepseek import DeepSeekClient, DeepSeekError
from .prompting import (
    advertising_decision_prompt,
    advertising_decision_system_prompt,
    clean_reply,
    decision_prompt,
    decision_system_prompt,
    generation_prompt,
    open_prompt_decision_prompt,
    open_prompt_decision_system_prompt,
    system_prompt,
)
from .storage import Storage

logger = logging.getLogger(__name__)

SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


class ChatIdentityMismatch(RuntimeError):
    """Raised when Telegram Web is not showing the chat we intend to send to."""


def shanghai_day_start(now: int) -> int:
    local_now = datetime.fromtimestamp(now, SHANGHAI_TIMEZONE)
    return int(local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


@dataclass(frozen=True)
class WebMessage:
    mid: str
    sender: str
    text: str
    is_out: bool
    reply_text: str = ""


def is_direct_trigger(
    message: WebMessage,
    mention_keywords: tuple[str, ...],
    self_names: tuple[str, ...],
    had_mention_badge: bool,
) -> bool:
    if had_mention_badge:
        return True
    text = message.text.casefold()
    if any(keyword in text for keyword in mention_keywords):
        return True
    reply_text = message.reply_text.casefold()
    return bool(reply_text and any(name in reply_text for name in self_names))


def peer_id_variants(peer_id: str) -> tuple[str, ...]:
    normalized = peer_id.strip().lstrip("#")
    if normalized.startswith("-"):
        return (normalized,)
    return (f"-{normalized}", normalized, f"-100{normalized}")


_TITLE_SPACES = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Fold a chat title so cosmetic edits cannot break the identity check."""
    text = unicodedata.normalize("NFKC", title or "")
    text = _TITLE_SPACES.sub("", text)
    return text.strip().casefold()


def hash_username(url_hash: str) -> str:
    """Return "@publicname" when Telegram canonicalised the hash to a username."""
    value = (url_hash or "").strip().lstrip("#").strip()
    return value.casefold() if value.startswith("@") else ""


def chat_identity_matches(
    expected_ids: tuple[str, ...],
    expected_title: str,
    active_peer_id: str,
    url_hash: str,
    visible_title: str,
    bound_usernames: frozenset[str] | set[str] = frozenset(),
) -> bool:
    """Decide whether Telegram Web is really showing the chat we are about to act on.

    Evidence, strongest first:
      1. the active chatlist row carries the peer id we expect;
      2. the URL hash still holds that peer id;
      3. Telegram canonicalised the hash to "@publicname" because the peer owns a
         username. That username is either already bound to this peer id, or the
         rendered header title proves which chat is open.
    A contradicting non-empty title always loses, so a wrong chat never slips in.
    """
    expected = {item.strip().lstrip("#") for item in expected_ids}
    active = active_peer_id.strip().lstrip("#")
    hashed = url_hash.strip().lstrip("#")
    id_matches = bool((active and active in expected) or (hashed and hashed in expected))

    wanted_norm = normalize_title(expected_title)
    current_norm = normalize_title(visible_title)

    hash_name = hash_username(url_hash)
    username_matches = False
    if hash_name:
        known = {item.casefold() for item in bound_usernames}
        username_matches = hash_name in known or (
            bool(wanted_norm) and current_norm == wanted_norm
        )

    # Telegram renders headers lazily; an absent title is not a mismatch, but a
    # present title must agree with the chat we intended to touch.
    title_ok = not wanted_norm or not current_norm or current_norm == wanted_norm

    return bool(title_ok and (id_matches or username_matches))


def messages_after(messages: list[WebMessage], last_mid: str) -> list[WebMessage]:
    """Return unseen messages without replaying a stale backlog when the cursor fell out of context."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].mid == last_mid:
            return messages[index + 1 :]
    return messages[-1:]


def select_direct_message(
    messages: list[WebMessage],
    mention_keywords: tuple[str, ...],
    self_names: tuple[str, ...],
    had_mention_badge: bool,
) -> WebMessage | None:
    incoming = [message for message in messages if not message.is_out]
    for message in reversed(incoming):
        if is_direct_trigger(message, mention_keywords, self_names, False):
            return message
    return incoming[-1] if had_mention_badge and incoming else None


def is_open_group_prompt(message: WebMessage, keywords: tuple[str, ...]) -> bool:
    """Recognize short questions addressed to the whole group rather than a named person."""
    text = " ".join(message.text.casefold().split())
    return bool(text and len(text) <= 120 and any(keyword in text for keyword in keywords))


def is_semantic_prompt_candidate(message: WebMessage) -> bool:
    """Cheap prefilter so semantic classification is not called for every group message."""
    text = " ".join(message.text.casefold().split())
    if not 2 <= len(text) <= 120 or "@" in text:
        return False
    question_hints = ("？", "?", "吗", "呢", "谁", "怎么", "什么", "啥", "哪", "求助", "请教", "推荐")
    return any(hint in text for hint in question_hints)


class TelegramWebSession:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.chat_pages: dict[str, Page] = {}
        self._bound_usernames: dict[str, set[str]] = {}

    async def start(self) -> None:
        cfg = self.config.telegram_web
        cfg.profile_path.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(cfg.profile_path),
            headless=cfg.headless,
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
            args=["--disable-dev-shm-usage", "--no-first-run", "--disable-background-timer-throttling"],
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self.page.goto(cfg.url, wait_until="domcontentloaded", timeout=60000)

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()

    async def is_logged_in(self) -> bool:
        assert self.page
        return await self.page.locator("#page-chats").is_visible() or await self.page.locator(
            "#chatlist-container .chatlist-chat[data-peer-id]"
        ).count() > 0

    async def wait_until_logged_in(self, timeout_seconds: float = 30) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if await self.is_logged_in():
                return True
            await asyncio.sleep(0.5)
        return False

    async def capture_login_until_ready(self, timeout_seconds: int = 180) -> bool:
        assert self.page
        path = self.config.telegram_web.login_screenshot_path
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if await self.is_logged_in():
                await self._atomic_screenshot(path)
                return True
            await self._atomic_screenshot(path)
            await asyncio.sleep(2)
        return False

    async def _atomic_screenshot(self, path: Path) -> None:
        """Publish complete PNGs so the live viewer never reads a half-written file."""
        assert self.page
        temp_path = path.with_name(f".{path.name}.tmp")
        await self.page.screenshot(path=str(temp_path), full_page=True, type="png")
        temp_path.replace(path)

    def _chat_row(self, title: str) -> Locator:
        assert self.page
        rows = self.page.locator("#chatlist-container .chatlist-chat[data-peer-id]")
        return rows.filter(has=self.page.locator(".peer-title", has_text=title)).first

    async def _current_chat_identity(self) -> dict[str, str]:
        assert self.page
        return await self.page.evaluate(
            """() => {
                const active = document.querySelector(
                    '#chatlist-container .chatlist-chat.active[data-peer-id]'
                );
                const title = document.querySelector(
                    '#column-center .chat-info .peer-title, #column-center .topbar .peer-title'
                );
                let hash = location.hash || '';
                try { hash = decodeURIComponent(hash); } catch (_) {}
                return {
                    activePeerId: active?.getAttribute('data-peer-id') || '',
                    urlHash: hash,
                    visibleTitle: title?.textContent?.trim() || '',
                };
            }"""
        )

    def _bound_for(self, expected_ids: tuple[str, ...]) -> set[str]:
        names: set[str] = set()
        for item in expected_ids:
            names |= self._bound_usernames.get(item.strip().lstrip("#"), set())
        return names

    def _bind_username(self, expected_ids: tuple[str, ...], url_hash: str) -> None:
        name = hash_username(url_hash)
        if not name:
            return
        for item in expected_ids:
            bucket = self._bound_usernames.setdefault(item.strip().lstrip("#"), set())
            if name not in bucket:
                bucket.add(name)
                logger.info("已绑定群的用户名哈希：%s -> #%s", item, name)

    async def _wait_for_chat_identity(
        self, expected_ids: tuple[str, ...], expected_title: str, timeout_seconds: float = 10
    ) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            identity = await self._current_chat_identity()
            if chat_identity_matches(
                expected_ids,
                expected_title,
                identity["activePeerId"],
                identity["urlHash"],
                identity["visibleTitle"],
                self._bound_for(expected_ids),
            ):
                self._bind_username(expected_ids, identity["urlHash"])
                return True
            await asyncio.sleep(0.15)
        return False

    async def open_chat(self, title: str) -> tuple[str, bool] | None:
        assert self.page
        row = self._chat_row(title)
        if await row.count() == 0:
            return None
        actual_title = (await row.locator(".peer-title").first.inner_text()).strip()
        if actual_title != title:
            return None
        peer_id = await row.get_attribute("data-peer-id") or title
        had_mention = await row.locator(".dialog-subtitle-badge-mention").count() > 0
        await row.click()
        if not await self._wait_for_chat_identity(peer_id_variants(peer_id), actual_title):
            logger.error("群切换校验失败，拒绝读取或发送：%s (%s)", actual_title, peer_id)
            return None
        try:
            await self.page.locator("#column-center .bubble[data-mid]").last.wait_for(
                state="visible", timeout=10000
            )
        except PlaywrightTimeoutError:
            logger.warning("群 %s 打开后没有可见消息", title)
        return peer_id, had_mention

    async def open_chat_by_id(
        self, peer_id: str, expected_title: str | None = None
    ) -> tuple[str, str, bool] | None:
        """Open each allowlisted chat in a dedicated page to prevent cross-chat races."""
        assert self.context
        normalized = peer_id.strip().lstrip("#")
        variants = peer_id_variants(normalized)
        page = self.chat_pages.get(normalized)
        if page is None or page.is_closed():
            page = await self.context.new_page()
            self.chat_pages[normalized] = page
        self.page = page
        base_url = self.config.telegram_web.url.split("#", 1)[0]
        wanted_title = (expected_title or "").strip()

        # Fast path. Telegram rewrites "#-123456" into "#@publicname" for peers
        # that own a username, so comparing raw hashes would force a navigation
        # on every poll, starve the other chats and keep resetting the render.
        if wanted_title and await self._wait_for_chat_identity(variants, wanted_title, 1.5):
            identity = await self._current_chat_identity()
            return f"id:{normalized}", identity["visibleTitle"] or wanted_title, False

        for candidate in variants:
            target_url = f"{base_url}#{candidate}"
            current_hash = self.page.url.split("#", 1)[1] if "#" in self.page.url else ""
            if current_hash != candidate:
                await self.page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            if not await self._wait_for_chat_identity(variants, wanted_title, 8):
                identity = await self._current_chat_identity()
                logger.warning(
                    "群身份未就绪：目标=%s title=%r，当前 active=%r hash=%r title=%r",
                    variants,
                    wanted_title,
                    identity["activePeerId"],
                    identity["urlHash"],
                    identity["visibleTitle"],
                )
                continue
            identity = await self._current_chat_identity()
            if wanted_title and chat_identity_matches(
                variants,
                wanted_title,
                identity["activePeerId"],
                identity["urlHash"],
                identity["visibleTitle"],
                self._bound_for(variants),
            ):
                return f"id:{normalized}", wanted_title, False
        return None

    async def messages(self, limit: int) -> list[WebMessage]:
        assert self.page
        bubbles = self.page.locator(
            "#column-center .bubble[data-mid]:not(.is-date):not(.service)"
        )
        snapshots = await bubbles.evaluate_all(
            """(nodes, maxItems) => nodes
                .slice(Math.max(0, nodes.length - maxItems))
                .map((bubble) => {
                    const text = bubble.querySelector('.message')?.textContent || '';
                    const sender = bubble.querySelector('.bubble-name, .peer-title')?.textContent || '群友';
                    const reply = bubble.querySelector('.reply, .reply-content')?.textContent || '';
                    return {
                        mid: bubble.getAttribute('data-mid') || '',
                        sender,
                        text,
                        isOut: bubble.classList.contains('is-out'),
                        replyText: reply,
                    };
                })""",
            limit,
        )
        result: list[WebMessage] = []
        for item in snapshots:
            mid = str(item.get("mid", "")).strip()
            if not mid:
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            sender = str(item.get("sender", "群友")).strip()
            reply_text = str(item.get("replyText", "")).strip()
            result.append(
                WebMessage(
                    mid=mid,
                    sender=sender or "群友",
                    text=" ".join(text.split()),
                    is_out=bool(item.get("isOut")),
                    reply_text=" ".join(reply_text.split()),
                )
            )
        return result

    async def send(
        self,
        text: str,
        delay_seconds: float,
        expected_peer_ids: tuple[str, ...],
        expected_title: str,
    ) -> None:
        assert self.page
        if not await self._wait_for_chat_identity(expected_peer_ids, expected_title, 2):
            raise ChatIdentityMismatch(
                f"发送前群身份不匹配：{expected_title} / {expected_peer_ids}"
            )
        editor = self.page.locator(
            "#column-center .input-message-input[contenteditable='true']"
        ).last
        await editor.wait_for(state="visible", timeout=10000)
        await asyncio.sleep(delay_seconds)
        if not await self._wait_for_chat_identity(expected_peer_ids, expected_title, 2):
            raise ChatIdentityMismatch(
                f"输入前群身份不匹配：{expected_title} / {expected_peer_ids}"
            )
        await editor.fill(text)
        if not await self._wait_for_chat_identity(expected_peer_ids, expected_title, 2):
            await editor.fill("")
            raise ChatIdentityMismatch(
                f"发送瞬间群身份不匹配：{expected_title} / {expected_peer_ids}"
            )
        await editor.press("Enter")

    async def screenshot_error(self) -> None:
        if self.page:
            path = self.config.telegram_web.error_screenshot_path
            path.parent.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(path), full_page=True)


class WebUserBot:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.web = TelegramWebSession(config)
        self.ai = DeepSeekClient(config.deepseek)
        self.storage = Storage(config.database_path)
        self._activity: dict[str, deque[float]] = defaultdict(deque)

    async def run(self) -> None:
        await self.web.start()
        try:
            if not await self.web.wait_until_logged_in():
                raise RuntimeError("Telegram Web 尚未登录，请先运行：python -m tgdeepseek web-login")
            ids = self.config.telegram_web.allowed_chat_ids
            titles = self.config.telegram_web.allowed_chat_titles
            if not ids and not titles:
                logger.warning("群 ID 和群名白名单均为空：服务保持安全待机，不读取或发送群消息")
            while True:
                for peer_id in ids:
                    await self._process_chat(peer_id=peer_id)
                for title in titles:
                    await self._process_chat(title=title)
                await asyncio.sleep(self.config.telegram_web.poll_seconds)
        finally:
            await self.ai.close()
            self.storage.close()
            await self.web.close()

    async def _process_chat(self, title: str | None = None, peer_id: str | None = None) -> None:
        try:
            if peer_id is not None:
                normalized_key = f"id:{peer_id.strip().lstrip('#')}"
                title_hint = self.storage.get_web_chat_title(normalized_key)
                opened_by_id = await self.web.open_chat_by_id(peer_id, title_hint)
                if opened_by_id is None:
                    logger.warning("无法打开白名单群 ID：%s", peer_id)
                    return
                chat_key, title, had_mention = opened_by_id
            else:
                assert title is not None
                opened = await self.web.open_chat(title)
                if opened is None:
                    logger.warning("左侧聊天列表未找到白名单群：%s（请先置顶）", title)
                    return
                chat_key, had_mention = opened
            messages = await self.web.messages(self.config.context.message_count)
            if not messages:
                return
            latest = messages[-1]
            seen = self.storage.get_web_last_mid(chat_key)
            self.storage.set_web_last_mid(chat_key, title, latest.mid)
            if seen is None or seen == latest.mid:
                return
            unseen = messages_after(messages, seen)
            incoming = [message for message in unseen if not message.is_out]
            if not incoming:
                return
            high_activity = self._record_activity(chat_key, len(incoming))
            target = select_direct_message(
                unseen,
                self.config.telegram_web.mention_keywords,
                self.config.telegram_web.self_names,
                had_mention,
            )
            trigger_kind = "direct" if target is not None else "free"
            now = int(time.time())
            if target is None:
                continuation = incoming[-1]
                continuation_last_at = self.storage.web_last_sender_reply_at(
                    chat_key, continuation.sender
                )
                if (
                    continuation_last_at is not None
                    and now - continuation_last_at <= self.config.behavior.recent_exchange_seconds
                ):
                    target = continuation
                    trigger_kind = "continuation"
            if target is None and is_open_group_prompt(
                incoming[-1], self.config.proactive.open_prompt_keywords
            ):
                target = incoming[-1]
                trigger_kind = "open_prompt"
            if target is None and is_semantic_prompt_candidate(incoming[-1]):
                semantic_context = messages[-8:]
                semantic_transcript = "\n".join(
                    f"{'我' if item.is_out else item.sender}：{item.text[: self.config.context.max_message_chars]}"
                    for item in semantic_context
                )
                if await self.ai.should_join(
                    open_prompt_decision_system_prompt(),
                    open_prompt_decision_prompt(title, semantic_transcript),
                ):
                    target = incoming[-1]
                    trigger_kind = "semantic_prompt"
            direct = target is not None
            mode = self.config.trigger.default_mode
            if mode == "off":
                return
            proactive = False
            if target is None:
                target = incoming[-1]
                if mode == "mention":
                    if not self._proactive_allowed(chat_key, high_activity, now):
                        return
                    if random.random() > self._adaptive_proactive_probability(chat_key, now):
                        return
                    proactive = True
                    trigger_kind = "proactive"
            if len(target.text.strip()) < self.config.trigger.min_message_chars:
                return
            advertising_context = messages[-8:]
            advertising_transcript = "\n".join(
                f"{'我' if item.is_out else item.sender}：{item.text[: self.config.context.max_message_chars]}"
                for item in advertising_context
            )
            if await self.ai.is_advertising(
                advertising_decision_system_prompt(),
                advertising_decision_prompt(title, advertising_transcript),
            ):
                logger.info(
                    "跳过广告内容：群=%s，发送者=%s，触发方式=%s",
                    title,
                    target.sender,
                    trigger_kind,
                )
                return
            if (
                not direct
                and not proactive
                and random.random() > self.config.trigger.free_reply_probability
            ):
                return
            sender_last_at = self.storage.web_last_sender_reply_at(chat_key, target.sender)
            recent_exchange = bool(
                sender_last_at is not None
                and now - sender_last_at <= self.config.behavior.recent_exchange_seconds
            )
            # Only an explicit mention/reply gets the short direct-message cooldown.
            # Open prompts, semantic prompts and continuation chatter should respect
            # the normal group cooldown so the account does not dominate a busy chat.
            priority_direct = trigger_kind == "direct"
            fast_mode = priority_direct and (high_activity or recent_exchange)
            if not self._within_limits(chat_key, priority_direct, fast_mode):
                return

            target_index = next(
                (index for index, item in enumerate(messages) if item.mid == target.mid),
                len(messages) - 1,
            )
            context_messages = messages[: target_index + 1]
            transcript = "\n".join(
                f"{'我' if item.is_out else item.sender}：{item.text[: self.config.context.max_message_chars]}"
                for item in context_messages
            )
            base_system = system_prompt(self.config.persona)
            if proactive or (not direct and self.config.trigger.free_decision_enabled):
                if not await self.ai.should_join(
                    decision_system_prompt(), decision_prompt(title, transcript)
                ):
                    return
            reply = await self.ai.complete(
                base_system, generation_prompt(title, transcript, target.sender)
            )
            reply = clean_reply(reply, self.config.behavior.max_reply_chars)
            if not reply:
                return
            delay, pace = self._reply_delay(direct, high_activity, recent_exchange)
            expected_ids = peer_id_variants(peer_id) if peer_id is not None else (chat_key,)
            await self.web.send(reply, delay, expected_ids, title)
            self.storage.record_web_reply(chat_key, target.sender)
            if proactive:
                self.storage.record_web_proactive_reply(chat_key)
            logger.info(
                "已通过 Telegram Web 回复群 %s，触发方式=%s，速度档=%s，未处理消息=%s",
                title,
                trigger_kind,
                pace,
                len(unseen),
            )
        except (PlaywrightTimeoutError, DeepSeekError, ChatIdentityMismatch, OSError) as exc:
            logger.error("处理群 %s 失败：%s: %s", title, type(exc).__name__, exc)
            await self.web.screenshot_error()
        except Exception:
            logger.exception("处理群 %s 时出现未预期错误", title)
            await self.web.screenshot_error()

    def _record_activity(self, chat_key: str, incoming_count: int) -> bool:
        now = time.monotonic()
        activity = self._activity[chat_key]
        activity.extend([now] * incoming_count)
        cutoff = now - self.config.behavior.active_window_seconds
        while activity and activity[0] < cutoff:
            activity.popleft()
        return len(activity) >= self.config.behavior.active_message_threshold

    def _proactive_allowed(self, chat_key: str, high_activity: bool, now: int) -> bool:
        proactive = self.config.proactive
        if not proactive.enabled or (proactive.active_only and not high_activity):
            return False
        hourly_limit = proactive.max_replies_per_hour
        if (
            hourly_limit > 0
            and self.storage.web_proactive_replies_for_chat_since(chat_key, now - 3600)
            >= hourly_limit
        ):
            return False
        last_at = self.storage.web_last_proactive_reply_at(chat_key)
        return last_at is None or now - last_at >= proactive.min_interval_seconds

    def _adaptive_proactive_probability(self, chat_key: str, now: int) -> float:
        """Spread a soft daily target across the China-time day without forced posting."""
        base = self.config.proactive.probability
        minimum = self.config.trigger.target_replies_per_chat_per_day_min
        maximum = self.config.trigger.target_replies_per_chat_per_day_max
        if minimum <= 0 or maximum < minimum:
            return base

        day_start = shanghai_day_start(now)
        sent = self.storage.web_replies_for_chat_since(chat_key, day_start)
        elapsed = max(1, now - day_start)
        midpoint = (minimum + maximum) / 2
        expected = midpoint * min(elapsed, 86400) / 86400
        tolerance = max(10.0, midpoint * 0.025)

        if sent >= maximum:
            return min(base, 0.10)
        if sent > expected + tolerance:
            return max(0.12, base * 0.35)
        if sent < expected - tolerance:
            return min(0.85, base * 1.35)
        return base

    def _reply_delay(
        self, direct: bool, high_activity: bool, recent_exchange: bool
    ) -> tuple[float, str]:
        behavior = self.config.behavior
        if recent_exchange:
            limits = (behavior.recent_typing_min_seconds, behavior.recent_typing_max_seconds)
            pace = "recent"
        elif high_activity:
            limits = (behavior.active_typing_min_seconds, behavior.active_typing_max_seconds)
            pace = "active"
        elif direct:
            limits = (behavior.direct_typing_min_seconds, behavior.direct_typing_max_seconds)
            pace = "direct"
        else:
            limits = (behavior.typing_min_seconds, behavior.typing_max_seconds)
            pace = "normal"
        return random.uniform(*limits), pace

    def _within_limits(self, chat_key: str, direct: bool, fast_mode: bool) -> bool:
        now = int(time.time())
        daily_limit = self.config.trigger.max_replies_per_chat_per_day
        if (
            daily_limit > 0
            and self.storage.web_replies_for_chat_since(chat_key, shanghai_day_start(now))
            >= daily_limit
        ):
            return False
        hourly_limit = self.config.trigger.max_replies_per_hour
        if hourly_limit > 0 and self.storage.web_replies_since(now - 3600) >= hourly_limit:
            return False
        last_at = self.storage.web_last_reply_at(chat_key)
        if last_at is None:
            return True
        if direct and fast_mode:
            cooldown = self.config.trigger.fast_cooldown_seconds
        elif direct:
            cooldown = self.config.trigger.direct_cooldown_seconds
        else:
            cooldown = self.config.trigger.cooldown_seconds
        return now - last_at >= cooldown


async def web_login(config: AppConfig, timeout_seconds: int = 600) -> bool:
    session = TelegramWebSession(config)
    await session.start()
    try:
        if await session.is_logged_in():
            return True
        logger.info("二维码页面截图会持续更新：%s", config.telegram_web.login_screenshot_path)
        return await session.capture_login_until_ready(timeout_seconds)
    finally:
        await session.close()

