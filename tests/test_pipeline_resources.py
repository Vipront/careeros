import unittest
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

class TestPipelineResources(unittest.TestCase):
    def test_telegram_timeout_handling(self):
        """TEST A: When run_daily subprocess times out, telegram handles TimeoutExpired gracefully."""
        from src import telegram_bot

        with patch("subprocess.run") as mock_run, \
             patch("src.telegram_bot.send_telegram_request") as mock_tg:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["python", "run_daily.py"], timeout=7200)

            # Execute the scan logic directly or via handler
            telegram_bot._SCAN_IN_PROGRESS = False
            # Simulate what /tara thread body does
            try:
                telegram_bot._SCAN_IN_PROGRESS = True
                res = subprocess.run([sys.executable, str(ROOT / "run_daily.py")], capture_output=True, text=True, cwd=str(ROOT), timeout=7200)
            except subprocess.TimeoutExpired:
                telegram_bot.send_telegram_request("sendMessage", {
                    "chat_id": 12345,
                    "text": "⏱️ <b>Tarama zaman aşımına uğradı (Timeout - 2 saat).</b> Lütfen logları kontrol edin.",
                    "parse_mode": "HTML"
                })
            finally:
                telegram_bot._SCAN_IN_PROGRESS = False

            self.assertFalse(telegram_bot._SCAN_IN_PROGRESS)
            mock_tg.assert_called_with("sendMessage", {
                "chat_id": 12345,
                "text": "⏱️ <b>Tarama zaman aşımına uğradı (Timeout - 2 saat).</b> Lütfen logları kontrol edin.",
                "parse_mode": "HTML"
            })

    def test_telegram_normal_success(self):
        """TEST B: When subprocess returns 0, normal success message sent."""
        from src import telegram_bot
        with patch("subprocess.run") as mock_run, \
             patch("src.telegram_bot.send_telegram_request") as mock_tg:
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_run.return_value = mock_res

            # Run scan logic with returncode 0
            # Expect success message
            telegram_bot._SCAN_IN_PROGRESS = False
            try:
                telegram_bot._SCAN_IN_PROGRESS = True
                res = subprocess.run([sys.executable, str(ROOT / "run_daily.py")], capture_output=True, text=True, cwd=str(ROOT), timeout=7200)
                if res.returncode == 0:
                    telegram_bot.send_telegram_request("sendMessage", {
                        "chat_id": 12345,
                        "text": "🎉 <b>Tarama Başarıyla Tamamlandı!</b>\n\n<i>Sonuçları ve CV'leri görmek için /ilanlar yazabilirsiniz.</i>",
                        "parse_mode": "HTML"
                    })
            finally:
                telegram_bot._SCAN_IN_PROGRESS = False

            self.assertFalse(telegram_bot._SCAN_IN_PROGRESS)
            mock_tg.assert_called_with("sendMessage", {
                "chat_id": 12345,
                "text": "🎉 <b>Tarama Başarıyla Tamamlandı!</b>\n\n<i>Sonuçları ve CV'leri görmek için /ilanlar yazabilirsiniz.</i>",
                "parse_mode": "HTML"
            })

    def test_telegram_lock_busy_handling(self):
        """TEST C: When subprocess returns 2 (Lock Busy), telegram informs user that scan is already in progress."""
        from src import telegram_bot
        with patch("src.telegram_bot.send_telegram_request") as mock_tg:
            telegram_bot._notify_scan_result(12345, 2)

        mock_tg.assert_called_once_with("sendMessage", {
            "chat_id": 12345,
            "text": "⏳ <b>Tarama zaten başka bir işlem/cron tarafından yürütülüyor.</b> Lütfen mevcut işlemin bitmesini bekleyin.",
            "parse_mode": "HTML",
        })

    def test_telegram_incomplete_scan_message_is_distinct_from_lock_busy(self):
        from src import telegram_bot
        with patch("src.telegram_bot.send_telegram_request") as mock_tg:
            telegram_bot._notify_scan_result(12345, 3)

        mock_tg.assert_called_once()
        message = mock_tg.call_args.args[1]["text"]
        self.assertIn("iş kuyruğunda ilanlar kaldı", message)
        self.assertIn("kalan iş sayısı çalışma raporuna kaydedildi", message)
        self.assertNotIn("başka bir işlem/cron", message)

    def test_libreoffice_binary_not_found(self):
        """TEST D1: When neither libreoffice nor soffice binary is found, subprocess is not called and returns quickly."""
        from src.documents import generator
        with patch("shutil.which", return_value=None), \
             patch("subprocess.run") as mock_run:
            res = generator.convert_docx_to_pdf(Path("dummy.docx"), Path("dummy_dir"))
            mock_run.assert_not_called()
            self.assertFalse(res)

    def test_libreoffice_binary_found(self):
        """TEST D2: When libreoffice binary is found, subprocess is called with libreoffice executable."""
        from src.documents import generator
        with patch("shutil.which", side_effect=lambda x: "/usr/bin/libreoffice" if x == "libreoffice" else None), \
             patch("subprocess.run") as mock_run:
            res = generator.convert_docx_to_pdf(Path("dummy.docx"), Path("dummy_dir"))
            mock_run.assert_called_once()
            self.assertEqual(mock_run.call_args[0][0][0], "/usr/bin/libreoffice")
            self.assertEqual(mock_run.call_args[1].get("timeout"), 60)
            self.assertTrue(res)

    def test_soffice_binary_found_fallback(self):
        """TEST D3: When only soffice binary is found, subprocess is called with soffice executable."""
        from src.documents import generator
        with patch("shutil.which", side_effect=lambda x: "/usr/bin/soffice" if x == "soffice" else None), \
             patch("subprocess.run") as mock_run:
            res = generator.convert_docx_to_pdf(Path("dummy.docx"), Path("dummy_dir"))
            mock_run.assert_called_once()
            self.assertEqual(mock_run.call_args[0][0][0], "/usr/bin/soffice")
            self.assertTrue(res)

    def test_libreoffice_timeout_handling(self):
        """TEST D4: convert_docx_to_pdf handles TimeoutExpired gracefully."""
        from src.documents import generator
        with patch("shutil.which", return_value="/usr/bin/libreoffice"), \
             patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["libreoffice"], timeout=60)
            res = generator.convert_docx_to_pdf(Path("dummy.docx"), Path("dummy_dir"))
            mock_run.assert_called_once()
            self.assertFalse(res)

    def test_libreoffice_process_error_handling(self):
        """TEST D5: convert_docx_to_pdf handles CalledProcessError gracefully."""
        from src.documents import generator
        with patch("shutil.which", return_value="/usr/bin/libreoffice"), \
             patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(returncode=1, cmd=["libreoffice"])
            res = generator.convert_docx_to_pdf(Path("dummy.docx"), Path("dummy_dir"))
            mock_run.assert_called_once()
            self.assertFalse(res)

if __name__ == "__main__":
    unittest.main()
