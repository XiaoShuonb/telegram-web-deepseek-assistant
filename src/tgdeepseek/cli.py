from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .web_runtime import WebUserBot, web_login


def project_root() -> Path:
    configured_root = os.getenv("APP_ROOT", "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    working_root = Path.cwd().resolve()
    if (working_root / "config" / "config.yaml").exists():
        return working_root
    return Path(__file__).resolve().parents[2]


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def login() -> None:
    config = load_config(project_root(), require_deepseek=False)
    if await web_login(config):
        print("Telegram Web 登录成功")
    else:
        print(f"登录等待超时。最新页面截图：{config.telegram_web.login_screenshot_path}")
        raise SystemExit(1)


async def run_bot() -> None:
    config = load_config(project_root(), require_deepseek=True)
    bot = WebUserBot(config)
    await bot.run()


async def doctor() -> None:
    config = load_config(project_root(), require_deepseek=False)
    checks = [
        ("配置文件", True),
        ("DeepSeek API Key", bool(os.getenv("DEEPSEEK_API_KEY", "").strip())),
        ("Telegram Web 浏览器配置", config.telegram_web.profile_path.exists()),
        ("data 目录可写", os.access(config.root / "data", os.W_OK)),
    ]
    for name, passed in checks:
        print(f"{'OK' if passed else 'MISSING'}  {name}")
    if not all(passed for _, passed in checks):
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram DeepSeek 个人账号助手")
    parser.add_argument("command", choices=["web-login", "run", "doctor"])
    args = parser.parse_args()
    configure_logging()
    try:
        if args.command == "web-login":
            asyncio.run(login())
        elif args.command == "doctor":
            asyncio.run(doctor())
        else:
            asyncio.run(run_bot())
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        pass

