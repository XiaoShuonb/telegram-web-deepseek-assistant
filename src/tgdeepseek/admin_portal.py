"""Small authenticated HTTPS dashboard for the Telegram Web assistant."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(os.getenv("ADMIN_PROJECT_ROOT", Path.cwd())).resolve()
CONFIG_PATH = Path(
    os.getenv("ADMIN_CONFIG_PATH", ROOT / "config" / "config.yaml")
).resolve()
DB_PATH = ROOT / "data" / "state.db"
BOT_CONTAINER = os.getenv("ADMIN_BOT_CONTAINER", "tgdeepseek")
PORT = int(os.getenv("ADMIN_PORT", "18765"))
TIMEZONE = ZoneInfo("Asia/Shanghai")

EDITABLE: dict[str, tuple[str, str, type, float, float]] = {
    "daily_min": ("trigger", "target_replies_per_chat_per_day_min", int, 0, 5000),
    "daily_max": ("trigger", "target_replies_per_chat_per_day_max", int, 0, 5000),
    "daily_cap": ("trigger", "max_replies_per_chat_per_day", int, 0, 5000),
    "cooldown": ("trigger", "cooldown_seconds", int, 0, 3600),
    "probability": ("proactive", "probability", float, 0, 1),
    "proactive_interval": ("proactive", "min_interval_seconds", int, 0, 86400),
    "proactive_hourly": ("proactive", "max_replies_per_hour", int, 0, 1000),
    "recent_exchange": ("behavior", "recent_exchange_seconds", int, 0, 3600),
}


def _run(*args: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


def _scalar(value: str) -> Any:
    value = value.strip().split(" #", 1)[0].strip()
    if value in {"true", "false"}:
        return value == "true"
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value.strip('"\'')


def read_yaml_scalars(path: Path) -> dict[tuple[str, str], Any]:
    result: dict[tuple[str, str], Any] = {}
    section = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        top = re.match(r"^([A-Za-z_][\w-]*):\s*$", line)
        if top:
            section = top.group(1)
            continue
        item = re.match(r"^  ([A-Za-z_][\w-]*):\s*(.+?)\s*$", line)
        if item and section:
            result[(section, item.group(1))] = _scalar(item.group(2))
    return result


def patch_yaml_scalars(path: Path, updates: dict[tuple[str, str], Any]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    section = ""
    remaining = set(updates)
    output: list[str] = []
    for line in lines:
        top = re.match(r"^([A-Za-z_][\w-]*):\s*$", line.rstrip("\r\n"))
        if top:
            section = top.group(1)
        item = re.match(r"^(  )([A-Za-z_][\w-]*):(.*?)(\r?\n)?$", line)
        key = (section, item.group(2)) if item else None
        if item and key in updates:
            value = updates[key]
            rendered = str(value).lower() if isinstance(value, bool) else str(value)
            line = f"  {item.group(2)}: {rendered}{item.group(4) or ''}"
            remaining.discard(key)
        output.append(line)
    if remaining:
        raise ValueError(f"配置项不存在：{sorted(remaining)!r}")
    temp = path.with_suffix(path.suffix + ".admin.tmp")
    temp.write_text("".join(output), encoding="utf-8")
    os.chmod(temp, 0o644)
    os.replace(temp, path)


def public_settings() -> dict[str, Any]:
    values = read_yaml_scalars(CONFIG_PATH)
    return {name: values.get((section, key)) for name, (section, key, *_rest) in EDITABLE.items()}


def today_start() -> int:
    now = datetime.now(TIMEZONE)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def group_stats() -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT s.chat_key, s.title, s.updated_at,
                   COUNT(r.id) AS replies,
                   MAX(r.created_at) AS last_reply
            FROM web_chat_state s
            LEFT JOIN web_reply_log r
              ON r.chat_key = s.chat_key AND r.created_at >= ?
            GROUP BY s.chat_key, s.title, s.updated_at
            ORDER BY s.title
            """,
            (today_start(),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def bot_state() -> dict[str, Any]:
    result = _run("docker", "inspect", BOT_CONTAINER, "--format", "{{json .State}}")
    if result.returncode != 0:
        return {"status": "missing", "error": result.stderr.strip()[-200:]}
    state = json.loads(result.stdout)
    return {
        "status": state.get("Status", "unknown"),
        "running": bool(state.get("Running")),
        "started_at": state.get("StartedAt"),
        "restart_count": state.get("RestartCount", 0),
        "oom_killed": bool(state.get("OOMKilled")),
    }


def dashboard_status() -> dict[str, Any]:
    return {
        "server_time": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        "bot": bot_state(),
        "groups": group_stats(),
        "settings": public_settings(),
    }


def recent_logs() -> str:
    result = _run("docker", "logs", "--tail", "120", BOT_CONTAINER)
    text = (result.stdout + result.stderr).strip()
    return "\n".join(text.splitlines()[-120:])


def validate_values(payload: dict[str, Any]) -> dict[tuple[str, str], Any]:
    unknown = set(payload) - set(EDITABLE)
    if unknown:
        raise ValueError(f"未知配置项：{', '.join(sorted(unknown))}")
    normalized: dict[tuple[str, str], Any] = {}
    friendly: dict[str, Any] = {}
    for name, raw in payload.items():
        section, key, kind, low, high = EDITABLE[name]
        try:
            value = kind(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 格式不正确") from exc
        if not low <= value <= high:
            raise ValueError(f"{name} 必须在 {low} 到 {high} 之间")
        normalized[(section, key)] = value
        friendly[name] = value
    combined = {**public_settings(), **friendly}
    if combined["daily_min"] > combined["daily_max"]:
        raise ValueError("每日目标下限不能大于上限")
    if combined["daily_max"] > combined["daily_cap"]:
        raise ValueError("每日目标上限不能大于硬上限")
    return normalized


def update_settings(payload: dict[str, Any]) -> None:
    updates = validate_values(payload)
    backup_dir = ROOT / "data" / "admin-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"config-{stamp}.yaml"
    shutil.copy2(CONFIG_PATH, backup)
    try:
        patch_yaml_scalars(CONFIG_PATH, updates)
        check = _run(
            "docker", "exec", BOT_CONTAINER, "python", "-c",
            "from pathlib import Path; from tgdeepseek.config import load_config; load_config(Path('/app'))",
        )
        if check.returncode != 0:
            raise RuntimeError("配置校验失败：" + (check.stderr or check.stdout).strip()[-300:])
        restart = _run("docker", "restart", "-t", "20", BOT_CONTAINER, timeout=40)
        if restart.returncode != 0:
            raise RuntimeError("重启失败：" + restart.stderr.strip()[-300:])
    except Exception:
        shutil.copy2(backup, CONFIG_PATH)
        raise


PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Telegram Assistant · 控制台</title>
<style>
:root{color-scheme:dark;--bg:#090d12;--panel:#111820;--line:#22303c;--text:#ecf4f8;--muted:#8799a7;--cyan:#57e5ca;--blue:#67a9ff;--red:#ff6e7a}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0,#173138 0,transparent 33%),var(--bg);color:var(--text);font:14px/1.5 ui-sans-serif,system-ui,-apple-system}
main{max-width:1180px;margin:auto;padding:28px 18px 56px}.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px}.brand{font-size:23px;font-weight:750}.sub{color:var(--muted)}
.pill{border:1px solid var(--line);border-radius:99px;padding:7px 12px;background:#0d141b}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;background:var(--red)}.dot.on{background:var(--cyan);box-shadow:0 0 14px var(--cyan)}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.card{grid-column:span 4;background:linear-gradient(145deg,#131d25,#0e151c);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 12px 35px #0004}.wide{grid-column:span 8}.full{grid-column:1/-1}
h2{font-size:13px;color:var(--muted);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin:0 0 14px}.big{font-size:30px;font-weight:780;margin:4px 0}.row{display:flex;gap:10px;align-items:center;justify-content:space-between}.groups{display:grid;gap:12px}.group{border:1px solid var(--line);border-radius:13px;padding:13px;background:#0b1117}.bar{height:8px;background:#1b2731;border-radius:9px;overflow:hidden;margin-top:10px}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--cyan),var(--blue));border-radius:9px}
button{border:1px solid #2b3c49;background:#17242e;color:var(--text);padding:9px 13px;border-radius:10px;cursor:pointer}button:hover{border-color:var(--cyan)}button.danger{color:#ff9ba3}.actions{display:flex;gap:8px;flex-wrap:wrap}
form{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}label{color:var(--muted);font-size:12px}input{width:100%;margin-top:5px;background:#0a1117;border:1px solid var(--line);border-radius:9px;color:var(--text);padding:10px}form button{align-self:end;height:39px}
pre{background:#080c10;border:1px solid var(--line);border-radius:12px;padding:14px;height:320px;overflow:auto;white-space:pre-wrap;color:#b9cad5;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}.notice{min-height:22px;color:var(--cyan);margin-top:8px}
@media(max-width:850px){.card,.wide{grid-column:1/-1}form{grid-template-columns:repeat(2,1fr)}}@media(max-width:520px){form{grid-template-columns:1fr}.top{align-items:flex-start;gap:12px;flex-direction:column}}
</style></head><body><main><div class="top"><div><div class="brand">Telegram Assistant · 控制台</div><div class="sub">Telegram Web 智能群聊管理</div></div><div class="pill"><span id="dot" class="dot"></span><span id="state">连接中</span></div></div>
<div class="grid"><section class="card"><h2>服务状态</h2><div class="big" id="service">—</div><div class="sub" id="started">—</div><div class="actions" style="margin-top:16px"><button onclick="control('restart')">重启</button><button onclick="control('start')">启动</button><button class="danger" onclick="control('stop')">停止</button></div></section>
<section class="card wide"><h2>今日群聊进度</h2><div id="groups" class="groups"><div class="sub">读取中…</div></div></section>
<section class="card full"><h2>活跃度设置</h2><form id="settings">
<label>每日目标下限<input name="daily_min" type="number"></label><label>每日目标上限<input name="daily_max" type="number"></label><label>每日硬上限<input name="daily_cap" type="number"></label><label>普通冷却（秒）<input name="cooldown" type="number"></label>
<label>主动概率（0–1）<input name="probability" type="number" min="0" max="1" step="0.05"></label><label>主动间隔（秒）<input name="proactive_interval" type="number"></label><label>主动每小时上限<input name="proactive_hourly" type="number"></label><label>连续对话窗口（秒）<input name="recent_exchange" type="number"></label><button type="submit">保存并重启</button></form><div id="notice" class="notice"></div></section>
<section class="card full"><div class="row"><h2>最近日志</h2><button onclick="loadLogs()">刷新日志</button></div><pre id="logs">读取中…</pre></section></div></main>
<script>const CSRF='__CSRF__';let first=true;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,opt={}){opt.headers={...(opt.headers||{}),'X-CSRF-Token':CSRF};const r=await fetch(url,opt);const j=await r.json();if(!r.ok)throw Error(j.error||'请求失败');return j}
async function refresh(){try{const d=await api('/api/status');const on=d.bot.running;document.querySelector('#dot').className='dot '+(on?'on':'');state.textContent=on?'运行中':'已停止';service.textContent=on?'ONLINE':'OFFLINE';started.textContent='服务器时间 '+d.server_time.replace('T',' ');const cap=d.settings.daily_cap||1000;groups.innerHTML=d.groups.map(g=>{const p=Math.min(100,Math.round((g.replies||0)/cap*100));return `<div class="group"><div class="row"><b>${esc(g.title)}</b><span>${g.replies||0} / ${cap}</span></div><div class="bar"><i style="width:${p}%"></i></div></div>`}).join('')||'<div class="sub">暂无群聊数据</div>';if(first){for(const [k,v] of Object.entries(d.settings)){const e=document.querySelector(`[name="${k}"]`);if(e)e.value=v}first=false}}catch(e){state.textContent='连接失败';notice.textContent=e.message}}
async function loadLogs(){try{logs.textContent=(await api('/api/logs')).logs||'暂无日志';logs.scrollTop=logs.scrollHeight}catch(e){logs.textContent=e.message}}
async function control(action){if(action==='stop'&&!confirm('确定停止机器人？'))return;notice.textContent='正在执行…';try{await api('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});notice.textContent='操作成功';setTimeout(refresh,1200)}catch(e){notice.textContent=e.message}}
settings.onsubmit=async e=>{e.preventDefault();notice.textContent='正在校验并应用…';const body=Object.fromEntries(new FormData(settings));try{await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});notice.textContent='配置已保存，机器人已重启';first=true;setTimeout(refresh,1200)}catch(e){notice.textContent=e.message}};
refresh();loadLogs();setInterval(refresh,5000);</script></body></html>"""


class Dashboard:
    def __init__(self, username: str, password: str) -> None:
        if not 8 <= len(username) <= 64 or not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
            raise ValueError("ADMIN_USERNAME 格式不正确")
        if len(password) < 16:
            raise ValueError("ADMIN_PASSWORD 至少需要 16 个字符")
        self.expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.csrf = hmac.new(password.encode(), b"tg-admin-csrf", hashlib.sha256).hexdigest()
        self.failures: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def authorized(self, address: str, value: str) -> bool:
        now = time.monotonic()
        with self.lock:
            queue = self.failures[address]
            while queue and queue[0] < now - 600:
                queue.popleft()
            if len(queue) >= 10:
                return False
            valid = hmac.compare_digest(value, self.expected)
            if not valid:
                queue.append(now)
            return valid


def handler_for(app: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TGAdmin/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"ADMIN {self.client_address[0]} {fmt % args}", flush=True)

        def _headers(self, code: int, mime: str, length: int) -> None:
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()

        def _json(self, payload: dict[str, Any], code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self._headers(code, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def _auth(self, modifying: bool = False) -> bool:
            if not app.authorized(self.client_address[0], self.headers.get("Authorization", "")):
                body = b"Authentication required"
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Telegram Admin", charset="UTF-8"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return False
            if modifying and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), app.csrf):
                self._json({"error": "CSRF 校验失败，请刷新页面"}, 403)
                return False
            return True

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                raise ValueError("请求过大")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("请求格式错误")
            return value

        def do_GET(self) -> None:
            if not self._auth():
                return
            route = self.path.split("?", 1)[0]
            try:
                if route == "/":
                    body = PAGE.replace("__CSRF__", app.csrf).encode()
                    self._headers(200, "text/html; charset=utf-8", len(body))
                    self.wfile.write(body)
                elif route == "/api/status":
                    self._json(dashboard_status())
                elif route == "/api/logs":
                    self._json({"logs": recent_logs()})
                else:
                    self._json({"error": "Not found"}, 404)
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        def do_POST(self) -> None:
            if not self._auth(modifying=True):
                return
            route = self.path.split("?", 1)[0]
            try:
                payload = self._body()
                if route == "/api/control":
                    action = payload.get("action")
                    if action not in {"start", "stop", "restart"}:
                        raise ValueError("不支持的操作")
                    command = ["docker", action]
                    if action == "restart":
                        command += ["-t", "20"]
                    result = _run(*command, BOT_CONTAINER, timeout=40)
                    if result.returncode != 0:
                        raise RuntimeError(result.stderr.strip()[-300:])
                    self._json({"ok": True})
                elif route == "/api/settings":
                    update_settings(payload)
                    self._json({"ok": True, "settings": public_settings()})
                else:
                    self._json({"error": "Not found"}, 404)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    return Handler


def main() -> None:
    username = os.environ["ADMIN_USERNAME"]
    password = os.environ["ADMIN_PASSWORD"]
    app = Dashboard(username, password)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), handler_for(app))
    cert = ROOT / "data" / "login-portal" / "cert.pem"
    key = ROOT / "data" / "login-portal" / "key.pem"
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    print(f"ADMIN_HTTPS_READY port={PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
