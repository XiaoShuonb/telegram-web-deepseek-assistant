from datetime import datetime
from zoneinfo import ZoneInfo

from tgdeepseek.web_runtime import shanghai_day_start


def test_shanghai_day_start_uses_china_midnight() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    now = int(datetime(2026, 10, 31, 18, 30, tzinfo=timezone).timestamp())
    expected = int(datetime(2026, 10, 31, 0, 0, tzinfo=timezone).timestamp())
    assert shanghai_day_start(now) == expected
