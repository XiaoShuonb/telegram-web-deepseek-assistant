from tgdeepseek.config import PersonaConfig
from tgdeepseek.prompting import (
    advertising_decision_prompt,
    clean_reply,
    decision_system_prompt,
    open_prompt_decision_prompt,
    system_prompt,
)


def test_clean_reply_removes_label_and_quotes() -> None:
    assert clean_reply('回复：“确实，有点离谱”', 100) == "确实，有点离谱"


def test_clean_reply_limits_length() -> None:
    assert len(clean_reply("很" * 500, 80)) == 80


def test_system_prompt_contains_honesty_boundary() -> None:
    prompt = system_prompt(PersonaConfig())
    assert "不要冒充" not in prompt or "身份边界" in prompt
    assert "按真人账号使用者的口吻自然短答" in prompt
    assert "不要每次使用同一句" in prompt
    assert "我是AI助手" in prompt


def test_system_prompt_uses_configured_name() -> None:
    prompt = system_prompt(PersonaConfig(name="小川"))
    assert "人格名称：小川" in prompt
    assert "我叫小川" in prompt


def test_decision_prompt_is_strict() -> None:
    assert "REPLY 或 SKIP" in decision_system_prompt()


def test_decision_prompt_prefers_natural_group_participation() -> None:
    from tgdeepseek.prompting import decision_prompt

    prompt = decision_prompt("测试群", "甲：这个说法也太逗了\n乙：哈哈哈")
    assert "默认倾向回答 REPLY" in prompt
    assert "简短接梗" in prompt
    assert "不要协助执行" in prompt


def test_open_prompt_decision_distinguishes_group_question() -> None:
    prompt = open_prompt_decision_prompt("测试群", "甲：这个怎么处理比较好？")
    assert "整个群公开提问" in prompt
    assert "指定的人" in prompt


def test_advertising_prompt_distinguishes_ads_from_discussion() -> None:
    prompt = advertising_decision_prompt("测试群", "甲：怎么天天有人来群里打广告")
    assert "最后一条消息本身" in prompt
    assert "吐槽别人发广告" in prompt
    assert "只能输出 AD 或 CHAT" in prompt

