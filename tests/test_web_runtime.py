from tgdeepseek.web_runtime import (
    WebMessage,
    chat_identity_matches,
    is_open_group_prompt,
    is_semantic_prompt_candidate,
    is_direct_trigger,
    messages_after,
    peer_id_variants,
    select_direct_message,
)


def test_direct_trigger_by_keyword() -> None:
    message = WebMessage("1", "群友", "小助手 在吗", False)
    assert is_direct_trigger(message, ("小助手",), (), False)


def test_direct_trigger_by_reply_name() -> None:
    message = WebMessage("2", "群友", "这个呢", False, "回复 张三：上一条消息")
    assert is_direct_trigger(message, (), ("张三",), False)


def test_direct_trigger_is_safe_by_default() -> None:
    message = WebMessage("3", "群友", "大家觉得呢", False)
    assert not is_direct_trigger(message, (), (), False)


def test_peer_id_variants_accept_user_facing_group_id() -> None:
    assert peer_id_variants("1234567890") == (
        "-1234567890",
        "1234567890",
        "-1001234567890",
    )


def test_peer_id_variants_preserve_web_peer_id() -> None:
    assert peer_id_variants("#-1234567890") == ("-1234567890",)


def test_chat_identity_requires_matching_title_and_peer() -> None:
    ids = ("-1234567890", "1234567890", "-1001234567890")
    assert chat_identity_matches(ids, "目标群", "-1234567890", "#-1234567890", "目标群")
    assert not chat_identity_matches(ids, "目标群", "-9876543210", "#-9876543210", "另一个群")
    assert not chat_identity_matches(ids, "目标群", "-1234567890", "#-1234567890", "另一个群")
    assert chat_identity_matches(ids, "目标群", "", "#-1234567890", "")


def test_messages_after_keeps_all_messages_from_a_burst() -> None:
    messages = [
        WebMessage("1", "甲", "普通消息", False),
        WebMessage("2", "乙", "@example_user 在吗", False),
        WebMessage("3", "丙", "后续消息", False),
    ]
    assert [message.mid for message in messages_after(messages, "1")] == ["2", "3"]


def test_messages_after_does_not_replay_stale_backlog() -> None:
    messages = [
        WebMessage("8", "甲", "旧消息", False),
        WebMessage("9", "乙", "最新消息", False),
    ]
    assert [message.mid for message in messages_after(messages, "missing")] == ["9"]


def test_select_direct_message_finds_mention_before_newer_chatter() -> None:
    messages = [
        WebMessage("2", "乙", "@example_user 在吗", False),
        WebMessage("3", "丙", "后续消息", False),
    ]
    selected = select_direct_message(messages, ("@example_user",), (), False)
    assert selected is not None
    assert selected.mid == "2"


def test_select_direct_message_uses_badge_as_fallback() -> None:
    messages = [WebMessage("4", "乙", "在吗", False)]
    assert select_direct_message(messages, (), (), True) == messages[0]


def test_open_group_prompt_matches_public_question() -> None:
    message = WebMessage("5", "群友", "有没有人在？", False)
    assert is_open_group_prompt(message, ("有没有人", "大家觉得"))


def test_open_group_prompt_ignores_long_unrelated_text() -> None:
    message = WebMessage("6", "群友", "这是一条普通聊天消息", False)
    assert not is_open_group_prompt(message, ("有没有人", "大家觉得"))


def test_semantic_prompt_candidate_prefilters_questions() -> None:
    assert is_semantic_prompt_candidate(WebMessage("7", "群友", "这个怎么处理比较好", False))
    assert not is_semantic_prompt_candidate(WebMessage("8", "群友", "今天天气不错", False))
    assert not is_semantic_prompt_candidate(WebMessage("9", "群友", "@张三 这个怎么处理", False))
