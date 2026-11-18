"""Temporary, authenticated HTTPS viewer for a server-side Telegram login."""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import signal
import ssl
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .cli import project_root
from .config import load_config
from .web_runtime import TelegramWebSession

PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram 服务器登录</title><style>
body{font:16px system-ui;background:#eef4f8;color:#182a38;max-width:800px;margin:32px auto;padding:20px}
main{background:white;padding:24px;border-radius:18px}img{width:100%;border-radius:12px}p{line-height:1.7}
</style><main><h2>Telegram 服务器登录</h2>
<p>扫码将授权这台服务器登录你的 Telegram 账号。登录会话保存在服务器，服务器管理员可以使用该会话访问账号。仅在信任管理员并同意授权时操作。</p>
<p>手机 Telegram → 设置 → 设备 → 连接桌面设备。二维码每两秒更新。可随时在手机“设备”中撤销服务器会话。</p>
<p id="state">正在准备二维码…</p><img id="qr" hidden alt="Telegram 登录二维码">
</main><script>
async function tick(){try{const r=await fetch('/status',{cache:'no-store'});if(!r.ok)throw Error();
const s=await r.json();document.getElementById('state').textContent=s.message;
const q=document.getElementById('qr');q.hidden=!s.ready;
if(s.ready)q.src='/qr.png?t='+Date.now();else q.removeAttribute('src');
if(s.finished)return;}catch(e){document.getElementById('state').textContent='连接暂时中断，正在重试…';}
setTimeout(tick,2000)}tick();</script></html>"""


class Portal:
    def __init__(self, password: str):
        self.credentials = base64.b64encode(('login:' + password).encode()).decode()
        self.lock = threading.Lock()
        self.png: bytes | None = None
        self.message = '正在连接 Telegram…'
        self.finished = False

    def update(self, message: str, png: bytes | None = None, finished: bool = False):
        with self.lock:
            self.message, self.png, self.finished = message, png, finished


def handler_for(portal: Portal):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            expected = 'Basic ' + portal.credentials
            if not hmac.compare_digest(self.headers.get('Authorization', ''), expected):
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Telegram login", charset="UTF-8"')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                return
            route = self.path.split('?', 1)[0]
            with portal.lock:
                if route == '/':
                    body, mime, code = PAGE.encode(), 'text/html; charset=utf-8', 200
                elif route == '/status':
                    body = json.dumps(dict(message=portal.message, ready=portal.png is not None,
                                           finished=portal.finished), ensure_ascii=False).encode()
                    mime, code = 'application/json; charset=utf-8', 200
                elif route == '/qr.png' and portal.png:
                    body, mime, code = portal.png, 'image/png', 200
                else:
                    body, mime, code = b'Not found', 'text/plain', 404
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store, max-age=0')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(body)

    return Handler


async def capture(portal: Portal):
    config = load_config(project_root(), require_deepseek=False)
    session = TelegramWebSession(config)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await session.start()
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            if await session.is_logged_in():
                portal.update('已进入账号，正在保存并重新打开验证，请稍候…')
                await session.close()
                session = TelegramWebSession(config)
                await session.start()
                if not await session.wait_until_logged_in(timeout_seconds=45):
                    portal.update('重开验证未通过，请重新扫码。')
                    continue
                # Closing Chromium flushes Local Storage, IndexedDB and cookies.
                await session.close()
                profile = config.telegram_web.profile_path
                profile.chmod(0o700)
                backup_dir = config.root / 'data' / 'session-backups'
                backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                backup_dir.chmod(0o700)
                archive = backup_dir / ('telegram-session-' + time.strftime('%Y%m%d-%H%M%S') + '.tar.gz')
                with tarfile.open(archive, 'w:gz') as target:
                    target.add(profile, arcname=profile.name)
                archive.chmod(0o600)
                marker = config.root / 'data' / 'login-portal' / 'verified.json'
                marker.write_text(json.dumps({'verified': True, 'verified_at': int(time.time())}))
                marker.chmod(0o600)
                print('LOGIN_SESSION_SAVED_AND_REOPEN_VERIFIED', flush=True)
                portal.update('登录成功！完整会话已保存并备份，重新打开验证通过，可以关闭页面。', finished=True)
                return
            assert session.page
            portal.update('请扫描下方当前二维码，登录窗口有效期 30 分钟。',
                          await session.page.screenshot(type='png'))
            await asyncio.sleep(2)
        portal.update('登录窗口已到期，请联系管理员重新开启。', finished=True)
    except Exception as exc:
        portal.update('连接 Telegram 失败，请联系管理员重启登录页面。', finished=True)
        print('LOGIN_PORTAL_ERROR', type(exc).__name__, flush=True)
    except asyncio.CancelledError:
        portal.update('登录页面已关闭。', finished=True)
    finally:
        await session.close()


def main():
    password = os.environ['LOGIN_PORTAL_PASSWORD']
    if len(password) < 16:
        raise ValueError('Login password must contain at least 16 characters')
    portal = Portal(password)
    server = ThreadingHTTPServer(('0.0.0.0', 18765), handler_for(portal))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain('/app/data/login-portal/cert.pem', '/app/data/login-portal/key.pem')
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print('LOGIN_PORTAL_HTTPS_READY', flush=True)
    try:
        asyncio.run(capture(portal))
        time.sleep(300)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
