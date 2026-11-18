# Telegram Web DeepSeek Assistant

一个基于 Playwright 和 Telegram Web K 的个人账号群聊助手，使用 DeepSeek 判断是否参与对话并生成回复。无需 Telegram `api_id`、`api_hash` 或 BotFather Token；首次运行通过手机 Telegram 扫码登录。

> Telegram Web 的 DOM 和登录流程可能变化。请先在测试群验证，并遵守 Telegram 规则及所在群组的约定。

## 功能

- Telegram Web 扫码登录与浏览器会话持久化
- 按群 ID 或精确群名设置白名单
- `mention`、`free`、`off` 三种触发模式
- 可编辑人格、语气、边界与示例回复
- 最近消息上下文、随机输入延迟和多级冷却
- 活跃群聊、连续对话与公开提问识别
- 广告内容识别与跳过
- 单群每日目标、硬上限和主动参与频率控制
- 发送前多次校验群身份，兼容 Telegram 将数字 hash 改写为 `#@username`
- 可选的 HTTPS 登录页与管理面板

## 要求

- Python 3.11+
- 可访问 Telegram Web 和 DeepSeek API 的网络
- Linux/macOS；常驻部署推荐 Linux

## 快速开始

```bash
git clone https://github.com/XiaoShuonb/telegram-web-deepseek-assistant.git
cd telegram-web-deepseek-assistant
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
PLAYWRIGHT_BROWSERS_PATH=data/ms-playwright .venv/bin/playwright install chromium
cp .env.example .env
cp config/config.example.yaml config/config.yaml
```

在 `.env` 中填写 DeepSeek API Key：

```dotenv
DEEPSEEK_API_KEY=your_api_key_here
```

然后编辑 `config/config.yaml`。默认示例的群白名单为空，因此不会读取或发送任何群消息。

## Telegram Web 登录

```bash
PLAYWRIGHT_BROWSERS_PATH=data/ms-playwright .venv/bin/python -m tgdeepseek web-login
```

程序会持续更新 `data/login-view/telegram-web-login.png`。用手机 Telegram 打开“设置 → 设备 → 连接桌面设备”后扫码。

浏览器资料保存在 `data/telegram-web-profile/`。该目录等同于登录凭证，禁止上传、分享或提交到 Git。

## 配置群聊

推荐填写 Telegram Web 地址栏 `#` 后的聊天 ID：

```yaml
telegram_web:
  allowed_chat_ids:
    - "-1001234567890"
  allowed_chat_titles: []
  mention_keywords:
    - "@your_username"
    - "你的称呼"
  self_names:
    - "你的显示名"
```

- `mention`：点名、@、引用本人或连续对话时回复。
- `free`：在直接触发之外，按概率并经模型判断后低频参与。
- `off`：停止自动回复。

`proactive` 控制主动参与；`trigger.max_replies_per_chat_per_day` 是单群每日硬上限。设置为 `0` 表示不限制，但仍受冷却和主动频率控制。

## 检查与运行

```bash
.venv/bin/python -m tgdeepseek doctor
PLAYWRIGHT_BROWSERS_PATH=data/ms-playwright .venv/bin/python -m tgdeepseek run
```

## systemd 常驻

```bash
sed "s|__APP_DIR__|$PWD|g" deploy/tgdeepseek.service.template \
  > ~/.config/systemd/user/tgdeepseek.service
systemctl --user daemon-reload
systemctl --user enable --now tgdeepseek.service
```

如果需要退出 SSH 后继续运行，可让管理员为当前 Linux 用户启用 linger：

```bash
sudo loginctl enable-linger "$USER"
```

无法使用 systemd 用户服务时，可通过环境变量启动守护脚本：

```bash
APP_DIR="$PWD" nohup setsid deploy/run-forever.sh >/dev/null 2>&1 < /dev/null &
```

## Docker

镜像不包含真实配置和登录会话。运行时需挂载 `config/config.yaml`、`data/` 和 `.env`：

```bash
docker build -t telegram-web-deepseek-assistant .
docker run --rm --env-file .env \
  -v "$PWD/config/config.yaml:/app/config/config.yaml:ro" \
  -v "$PWD/data:/app/data" \
  telegram-web-deepseek-assistant
```

## 管理面板（可选）

管理面板使用 HTTP Basic Auth，并通过 HTTPS 提供服务。至少设置：

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace_with_a_long_random_password
ADMIN_PROJECT_ROOT=/absolute/path/to/project
ADMIN_CONFIG_PATH=/absolute/path/to/project/config/config.yaml
```

管理面板可以查看状态、近期日志、单群回复统计并修改频率参数。不要把面板直接暴露在没有防火墙或反向代理保护的公网端口上。

## 测试

```bash
.venv/bin/pytest -q
.venv/bin/python tests/check_identity_guard.py
```

## 数据与隐私

以下内容已由 `.gitignore` 排除，任何情况下都不应提交：

- `.env`
- `config/config.yaml` 和本地配置
- `data/telegram-web-profile/`
- SQLite 数据库、日志、截图和 Playwright 浏览器文件
- 管理面板密码与 TLS 私钥

公开部署前建议执行一次密钥扫描，并检查 `git status` 与 `git diff --cached`。

## License

[MIT](LICENSE)
