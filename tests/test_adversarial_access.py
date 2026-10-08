from unittest.mock import patch

import pytest

from src import telegram_bot


@pytest.mark.parametrize("actor,chat", [(999, 123), (123, 999), (None, 123)])
def test_forged_telegram_callbacks_cannot_modify_jobs(actor, chat):
    update = {"update_id": 1, "callback_query": {
        "id": "forged", "data": "apply_1", "from": {"id": actor},
        "message": {"message_id": 1, "chat": {"id": chat}},
    }}
    with patch.object(telegram_bot, "TOKEN", "fictional-test-token"), \
            patch.object(telegram_bot, "CHAT_ID", "123"), \
            patch.dict("os.environ", {"TELEGRAM_OPERATOR_ID": "123"}), \
            patch.object(telegram_bot, "send_telegram_request") as api, \
            patch.object(telegram_bot, "mark_job_status") as change, \
            patch.object(telegram_bot, "get_db") as database:
        api.return_value = {"ok": True, "result": [update]}
        telegram_bot.process_updates()
        change.assert_not_called()
        database.assert_not_called()
        assert any(call.args[0] == "answerCallbackQuery" and call.args[1].get("show_alert")
                   for call in api.call_args_list)


def test_dashboard_rejects_missing_and_wrong_password(monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("DASHBOARD_PASSWORD", "fictional-strong-test-password")
    with patch("src.dashboard_cache.start_dashboard_refresh"), \
            patch("src.dashboard_theme.inject_theme"):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src/dashboard.py"))
        app.run(timeout=20)
        assert not app.exception
        assert len(app.text_input) == 1
        app.text_input[0].set_value("wrong-password")
        app.button[0].click().run(timeout=20)
        assert not app.exception
        assert "authenticated" not in app.session_state or not app.session_state["authenticated"]
        assert any("hatalı" in error.value for error in app.error)
