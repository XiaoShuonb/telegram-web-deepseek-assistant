from __future__ import annotations

import asyncio
import logging
import random
import shlex
import time
from collections import defaultdict
from typing import Any

from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError, RPCError

from .config import AppConfig
from .deepseek import DeepSeekClient, DeepSeekError
from .prompting import (
    clean_reply,
    decision_prompt,
    decision_system_prompt,
    generation_prompt,
    system_prompt,
)
from .storage import ChatSetting, Storage

logger = logging.getLogger(__name__)


class UserBot:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        config.telegram.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            str(config.telegram.session_path),
            config.telegram.api_id,
            config.telegram.api_hash,
            sequential_updates=True,
        )
        self.ai = DeepSeekClient(config.deepseek)
        self.storage = Storage(config.database_path)
        self.me: Any = None
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def run(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram 尚未登录，请先运行：python -m tgdeepseek login")
        self.me = await self.client.get_me()
        self.client.add_event_handler(self._on_incoming, events.NewMessage(incoming=True))
        self.client.add_event_handler(
            self._on_control_command,
            events.NewMessage(outgoing=True, pattern=r"^\.ai(?:\s|$)"),
        )
        logger.info("已登录 Telegram：%s（ID %s）", utils.get_display_name(self.me), self.me.id)
        logger.info("默认不会启用未授权群；控制命令请发到 Telegram 收藏夹")
        try:
            await self.client.run_until_disconnected()
        finally:
            await self.ai.close()
            self.storage.close()

    def _setting_for(self, chat_id: int) -> ChatSetting | None:
        configured = self.storage.get_chat(chat_id)
        if configured is not None:
            return configured
        if chat_id in self.config.trigger.allowed_chat_ids:
            return ChatSetting(chat_id, True, self.config.trigger.default_mode, 0)
        return None

    async def _is_direct_trigger(self, event: events.NewMessage.Event) -> bool:
        if bool(getattr(event.message, "mentioned", False)):
            return True
        if not event.is_reply:
            return False
        try:
            replied = await event.get_reply_message()
            return bool(replied and replied.sender_id == self.me.id)
        except RPCError:
            return False

    async def _on_incoming(self, event: events.NewMessage.Event) -> None:
        text = (event.raw_text or "").strip()
        if not text or text.startswith(".ai"):
            return
        if not event.is_group and not self.config.telegram.allow_private:
            return
        chat_id = int(event.chat_id)
        setting = self._setting_for(chat_id)
        if setting is None or not setting.enabled or setting.mode == "off":
            return

        try:
            sender = await event.get_sender()
            if getattr(sender, "bot", False):
                return
            direct = await self._is_direct_trigger(event)
            if setting.mode == "mention" and not direct:
                return
            if not direct and len(text) < self.config.trigger.min_message_chars:
                return
            if not direct and random.random() > self.config.trigger.free_reply_probability:
                return
            async with self._locks[chat_id]:
                if not self._within_limits(chat_id, direct):
                    return
                chat = await event.get_chat()
                chat_title = getattr(chat, "title", None) or utils.get_display_name(chat) or str(chat_id)
                transcript, latest_sender = await self._transcript(event)
                base_system = system_prompt(self.config.persona)
                if not direct and self.config.trigger.free_decision_enabled:
                    should_join = await self.ai.should_join(
                        decision_system_prompt(),
                        decision_prompt(chat_title, transcript),
                    )
                    if not should_join:
                        return
                reply = await self.ai.complete(
                    base_system,
                    generation_prompt(chat_title, transcript, latest_sender),
                )
                reply = clean_reply(reply, self.config.behavior.max_reply_chars)
                if not reply:
                    return
                await self._send_reply(event, reply)
                self.storage.record_reply(chat_id)
                logger.info("已回复群 %s（%s），触发方式=%s", chat_title, chat_id, "direct" if direct else "free")
        except FloodWaitError as exc:
            logger.warning("Telegram 要求等待 %s 秒，当前消息跳过", exc.seconds)
        except (DeepSeekError, RPCError, OSError) as exc:
            logger.error("处理消息失败：%s: %s", type(exc).__name__, exc)
        except Exception:
            logger.exception("处理消息时出现未预期错误")

    def _within_limits(self, chat_id: int, direct: bool) -> bool:
        now = int(time.time())
        if self.storage.replies_since(now - 3600) >= self.config.trigger.max_replies_per_hour:
            logger.warning("达到每小时回复上限，跳过消息")
            return False
        last_at = self.storage.last_reply_at(chat_id)
        if last_at is not None:
            cooldown = self.config.trigger.cooldown_seconds
            if direct:
                cooldown = min(8, cooldown)
            if now - last_at < cooldown:
                return False
        return True

    async def _transcript(self, event: events.NewMessage.Event) -> tuple[str, str]:
        messages = await self.client.get_messages(
            event.chat_id,
            limit=self.config.context.message_count,
        )
        lines: list[str] = []
        latest_sender = "群友"
        for message in reversed(messages):
            text = (message.raw_text or "").strip()
            if not text or text.startswith(".ai"):
                continue
            text = " ".join(text.split())[: self.config.context.max_message_chars]
            if message.sender_id == self.me.id:
                name = "我"
            else:
                try:
                    sender = await message.get_sender()
                    name = utils.get_display_name(sender) or f"用户{message.sender_id}"
                except RPCError:
                    name = f"用户{message.sender_id}"
            if message.id == event.message.id:
                latest_sender = name
                lines.append(f"{name}（最新消息）：{text}")
            else:
                lines.append(f"{name}：{text}")
        if not lines:
            lines.append(f"{latest_sender}（最新消息）：{event.raw_text}")
        return "\n".join(lines), latest_sender

    async def _send_reply(self, event: events.NewMessage.Event, reply: str) -> None:
        delay = random.uniform(
            self.config.behavior.typing_min_seconds,
            self.config.behavior.typing_max_seconds,
        )
        if self.config.behavior.typing_enabled:
            async with self.client.action(event.chat_id, "typing"):
                await asyncio.sleep(delay)
        else:
            await asyncio.sleep(min(delay, 1.0))
        kwargs: dict[str, Any] = {"parse_mode": None, "link_preview": False}
        if self.config.behavior.reply_to_trigger:
            kwargs["reply_to"] = event.message.id
        await self.client.send_message(event.chat_id, reply, **kwargs)

    async def _on_control_command(self, event: events.NewMessage.Event) -> None:
        if self.me is None or int(event.chat_id) != int(self.me.id):
            return
        try:
            parts = shlex.split(event.raw_text)
        except ValueError:
            await event.reply("命令格式有误。发送 .ai help 查看帮助", parse_mode=None)
            return
        command = parts[1].lower() if len(parts) > 1 else "help"
        try:
            if command == "help":
                result = self._help_text()
            elif command == "status":
                result = self._status_text()
            elif command == "chats":
                result = await self._dialogs_text()
            elif command in {"enable", "on"}:
                chat_id = self._chat_id_arg(parts)
                mode = parts[3].lower() if len(parts) > 3 else "mention"
                self.storage.set_chat(chat_id, True, mode)
                result = f"已启用 {chat_id}，模式：{mode}"
            elif command in {"disable", "off"}:
                chat_id = self._chat_id_arg(parts)
                self.storage.set_chat(chat_id, False, "off")
                result = f"已关闭 {chat_id}"
            elif command == "mode":
                chat_id = self._chat_id_arg(parts)
                if len(parts) < 4:
                    raise ValueError("缺少模式")
                mode = parts[3].lower()
                self.storage.set_chat(chat_id, mode != "off", mode)
                result = f"已把 {chat_id} 设置为 {mode} 模式"
            else:
                result = "未知命令。发送 .ai help 查看帮助"
        except (ValueError, IndexError) as exc:
            result = f"命令参数错误：{exc}。发送 .ai help 查看帮助"
        await event.reply(result, parse_mode=None, link_preview=False)

    @staticmethod
    def _chat_id_arg(parts: list[str]) -> int:
        if len(parts) < 3:
            raise ValueError("缺少群 ID")
        return int(parts[2])

    def _help_text(self) -> str:
        return """DeepSeek 群聊助手控制命令（仅在收藏夹使用）：
.ai status
.ai chats
.ai enable <群ID> [mention|free]
.ai disable <群ID>
.ai mode <群ID> <mention|free|off>

mention：仅被@或被回复时回答
free：在mention基础上低概率主动参与
off：关闭"""

    def _status_text(self) -> str:
        rows = self.storage.list_chats()
        if not rows:
            chat_text = "尚未启用任何群"
        else:
            chat_text = "\n".join(
                f"{item.chat_id}: {'启用' if item.enabled else '关闭'} / {item.mode}" for item in rows[:30]
            )
        return f"账号：{utils.get_display_name(self.me)}\n模型：{self.config.deepseek.model}\n{chat_text}"

    async def _dialogs_text(self) -> str:
        lines = ["最近群聊（复制 ID 用于 enable）："]
        async for dialog in self.client.iter_dialogs(limit=60):
            if dialog.is_group or dialog.is_channel:
                lines.append(f"{dialog.name}: {dialog.id}")
            if len(lines) >= 31:
                break
        return "\n".join(lines)

