from tgdeepseek.storage import Storage


def test_chat_settings_and_reply_log(tmp_path) -> None:
    storage = Storage(tmp_path / "state.db")
    try:
        assert storage.get_chat(-100123) is None
        storage.set_chat(-100123, True, "mention")
        setting = storage.get_chat(-100123)
        assert setting is not None
        assert setting.enabled is True
        assert setting.mode == "mention"
        storage.record_reply(-100123)
        assert storage.last_reply_at(-100123) is not None
        assert storage.replies_since(0) == 1
    finally:
        storage.close()


def test_web_sender_reply_state(tmp_path) -> None:
    storage = Storage(tmp_path / "state.db")
    try:
        assert storage.web_last_sender_reply_at("id:1", "Alice") is None
        storage.record_web_reply("id:1", "Alice")
        storage.record_web_reply("id:2", "Bob")
        assert storage.web_last_sender_reply_at("id:1", "alice") is not None
        assert storage.web_replies_since(0) == 2
        assert storage.web_replies_for_chat_since("id:1", 0) == 1
        assert storage.web_replies_for_chat_since("id:2", 0) == 1
        assert storage.web_last_proactive_reply_at("id:1") is None
        storage.record_web_proactive_reply("id:1")
        assert storage.web_last_proactive_reply_at("id:1") is not None
        assert storage.web_proactive_replies_since(0) == 1
        assert storage.web_proactive_replies_for_chat_since("id:1", 0) == 1
        assert storage.web_proactive_replies_for_chat_since("id:2", 0) == 0
    finally:
        storage.close()


def test_web_chat_title_is_reused_as_identity_hint(tmp_path) -> None:
    storage = Storage(tmp_path / "state.db")
    try:
        assert storage.get_web_chat_title("id:-1") is None
        storage.set_web_last_mid("id:-1", "测试群", "100")
        assert storage.get_web_chat_title("id:-1") == "测试群"
    finally:
        storage.close()

