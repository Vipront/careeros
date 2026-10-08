import contextlib
import html
import os
import sys
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE)


OUTPUT = ROOT / "output"
LOG_FILE = ROOT / "run_daily.log"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
_SCAN_IN_PROGRESS: bool = False

def is_authorized_user(user_id, chat_id=None):
    """Ensure incoming requests strictly originate from the configured operator."""
    if not CHAT_ID:
        return False
    try:
        configured_id = int(str(CHAT_ID).strip())
        operator = os.getenv("TELEGRAM_OPERATOR_ID") or (str(configured_id) if configured_id > 0 else "")
        if not operator or not user_id or int(user_id) != int(operator):
            return False
        return chat_id is None or int(chat_id) == configured_id
    except (ValueError, TypeError):
        pass
    return False

def get_db():
    from src.db import get_connection
    con = get_connection()
    # con.row_factory is not strictly needed as TursoCursor rows support dict-like access
    return con

def get_pending_review_jobs(limit=50):
    from src.eligibility.gate import evaluate_job_eligibility
    from src.eligibility.profile_adapter import get_candidate_profile_facts

    con = get_db()
    table_cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()]
    if not table_cols:
        table_cols = [
            "id", "title", "company", "location", "url", "profile_type",
            "llm_score", "final_score", "status", "is_easy_apply",
            "workplace_type", "employment_type", "description",
            "education_requirements", "experience_requirements", "eligibility_text",
            "liveness_status", "liveness_http_code", "liveness_detail", "liveness_checked_at",
        ]
    sel_query = f"SELECT {', '.join(table_cols)} FROM jobs WHERE status = 'ready_for_review'"
    rows = con.execute(sel_query).fetchall()
    con.close()

    candidate = get_candidate_profile_facts()
    eligible_jobs = []

    for r in rows:
        job_dict = dict(zip(table_cols, r, strict=False)) if not isinstance(r, dict) else dict(r)
        # Evaluate current shared gate for every full job row
        gate_decision = evaluate_job_eligibility(job_dict, candidate)
        # Include ONLY can_notify true (requires fresh OPEN, current profile, all criteria MET)
        if gate_decision.can_notify:
            job_dict["gate_decision"] = gate_decision
            eligible_jobs.append(job_dict)

    # Sort by score descending
    eligible_jobs.sort(
        key=lambda j: float(j.get("llm_score") or j.get("final_score") or 0.0),
        reverse=True,
    )

    # Apply LIMIT after filtering
    return eligible_jobs[:limit]

def mark_job_status(job_id, status, note=""):
    from src.db import transition
    transition(job_id, status, note=note or f"User marked as {status} via Telegram bot")

    if status == "rejected":
        import shutil
        if OUTPUT.exists():
            for p in OUTPUT.glob(f"*/*_{job_id}"):
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
            for d in list(OUTPUT.iterdir()):
                if d.is_dir() and not any(d.iterdir()):
                    with contextlib.suppress(OSError):
                        d.rmdir()

def send_telegram_request(method, payload):
    if not TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=30)
        r.raise_for_status()
        return require_telegram_success(r.json())
    except Exception as e:
        print(f"Telegram API Error ({method}): {type(e).__name__}")
        return None

def require_telegram_success(result):
    """Reject API-level failures, including HTTP 200 responses with ok=false."""
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Telegram API did not confirm success")
    return result


def _send_review_message(payload):
    require_telegram_success(send_telegram_request("sendMessage", payload))


def send_review_cards():
    if not CHAT_ID:
        print("TELEGRAM_CHAT_ID not set.")
        return

    jobs = get_pending_review_jobs(limit=10)
    if not jobs:
        msg = "<b>📋 Başvuru Bekleyen İlanlar</b>\n\n<i>Şu an başvuru bekleyen aktif bir fırsat bulunmuyor. Tüm ilanları tamamladın! 🎉</i>"
        _send_review_message({
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        })
        return

    intro = f"<b>📋 Başvuru Bekleyen Aktif İlanlar (Toplam {len(jobs)} İlan)</b>\n<i>İlanları inceleyip altındaki butonlarla tek tıkla durumunu güncelleyebilirsin:</i>"
    _send_review_message({
        "chat_id": CHAT_ID,
        "text": intro,
        "parse_mode": "HTML"
    })

    emojis = ["🔥", "✨", "⭐", "🎯", "💫"]
    for idx, job in enumerate(jobs):
        jid = job.get("id")
        title = html.escape(job.get("title") or "")
        comp = html.escape(job.get("company") or "")
        loc = html.escape(job.get("location") or "")
        prof = html.escape(job.get("profile_type") or "")
        score = float(job.get("llm_score") or job.get("final_score") or 0.0)
        url = job.get("url") or f"https://www.linkedin.com/jobs/view/{jid}/"
        icon = emojis[idx] if idx < len(emojis) else "•"

        doc_folders = list(OUTPUT.glob(f"*/*_{jid}")) if OUTPUT.exists() else []
        from src.ops.artifact_qa import application_package_error
        package_ready = any(application_package_error(folder) is None for folder in doc_folders)
        doc_status = "CV ve Ön Yazı Hazır ✅" if package_ready else "Belgeler Hazırlanıyor ⏳"

        meta_tokens = []
        if job.get("is_easy_apply") == 1:
            meta_tokens.append("⚡ Kolay Başvuru")
        if job.get("workplace_type"):
            meta_tokens.append(f"📍 {html.escape(job['workplace_type'])}")
        if job.get("employment_type"):
            meta_tokens.append(f"💼 {html.escape(job['employment_type'])}")
        meta_line = f"• <b>Etiketler:</b> {' | '.join(meta_tokens)}\n" if meta_tokens else ""

        card_text = (
            f"{icon} <b>#{jid} • {comp} — {loc}</b>\n"
            f"• <b>Rol:</b> {title}\n"
            f"• <b>Uyum:</b> %{score:.0f} Uyumlu ({prof})\n"
            f"{meta_line}"
            f"• <b>Dosyalar:</b> {doc_status}\n"
            f'• 🔗 <a href="{html.escape(url, quote=True)}">Hemen İlana Git</a>'
        )

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "📄 CV (.docx)", "callback_data": f"docxcv_{jid}"},
                    {"text": "✉️ Ön Yazı (.docx)", "callback_data": f"docxcl_{jid}"}
                ],
                [
                    {"text": "✅ Başvurdum", "callback_data": f"apply_{jid}"},
                    {"text": "❌ İlgilenmiyorum", "callback_data": f"reject_{jid}"}
                ]
            ]
        }

        _send_review_message({
            "chat_id": CHAT_ID,
            "text": card_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": reply_markup
        })
        time.sleep(0.1) # Prevent Telegram flood wait limits when sending up to 50 cards

def send_document_to_chat(chat_id, file_path, caption):
    if not TOKEN or not Path(file_path).exists():
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            r = requests.post(url, data={"chat_id": chat_id, "caption": caption}, files={"document": f}, timeout=60)
            return r.json().get("ok", False)
    except Exception as e:
        print(f"Error sending document: {e}")
        return False


def _notify_scan_result(chat_id, returncode):
    """Send a user-facing message for each documented daily-scan exit code."""
    if returncode == 0:
        message = "🎉 <b>Tarama başarıyla tamamlandı!</b>\n\n<i>Sonuçları ve CV'leri görmek için /ilanlar yazabilirsiniz.</i>"
    elif returncode == 2:
        message = "⏳ <b>Tarama zaten başka bir işlem/cron tarafından yürütülüyor.</b> Lütfen mevcut işlemin bitmesini bekleyin."
    elif returncode == 3:
        message = (
            "⚠️ <b>Tarama tamamlanamadı; iş kuyruğunda ilanlar kaldı.</b>\n"
            "Ayrıntılar ve kalan iş sayısı çalışma raporuna kaydedildi."
        )
    else:
        message = "⚠️ <b>Tarama sırasında bir sorun oluştu.</b>\nDetaylar log dosyasına kaydedildi."
    send_telegram_request("sendMessage", {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    })

def process_updates():
    if not TOKEN:
        return
    # Get last updates from Telegram
    res = send_telegram_request("getUpdates", {"limit": 50, "timeout": 2})
    if not res or not res.get("ok"):
        return

    updates = res.get("result", [])
    max_update_id = 0

    for u in updates:
        update_id = u["update_id"]
        if update_id > max_update_id:
            max_update_id = update_id

        # Handle button clicks (callback_query)
        if "callback_query" in u:
            cb = u["callback_query"]
            cb_id = cb["id"]
            data = cb.get("data", "")
            msg = cb.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            from_user_id = cb.get("from", {}).get("id")
            message_id = msg.get("message_id")
            text = msg.get("text", "")

            if not is_authorized_user(from_user_id, chat_id):
                send_telegram_request("answerCallbackQuery", {
                    "callback_query_id": cb_id,
                    "text": "⛔ Yetkisiz erişim: Bu bot yalnızca yetkili operatör tarafından yönetilebilir.",
                    "show_alert": True
                })
                continue

            if data.startswith("docxcv_"):
                job_id = int(data.split("_")[1])
                doc_folders = list(OUTPUT.glob(f"*/*_{job_id}")) if OUTPUT.exists() else []
                sent = False
                if doc_folders:
                    target_file = doc_folders[0] / "tailored_cv.docx"
                    if target_file.exists():
                        sent = send_document_to_chat(chat_id, target_file, f"📄 İlan #{job_id} Özelleştirilmiş CV (.docx)")

                send_telegram_request("answerCallbackQuery", {
                    "callback_query_id": cb_id,
                    "text": "📄 CV Word dosyası gönderildi!" if sent else "⚠️ Word dosyası bulunamadı. Lütfen önce üretilmesini bekleyin."
                })

            elif data.startswith("docxcl_"):
                job_id = int(data.split("_")[1])
                doc_folders = list(OUTPUT.glob(f"*/*_{job_id}")) if OUTPUT.exists() else []
                sent = False
                if doc_folders:
                    target_file = doc_folders[0] / "cover_letter.docx"
                    if target_file.exists():
                        sent = send_document_to_chat(chat_id, target_file, f"✉️ İlan #{job_id} Ön Yazı (.docx)")

                send_telegram_request("answerCallbackQuery", {
                    "callback_query_id": cb_id,
                    "text": "✉️ Ön Yazı Word dosyası gönderildi!" if sent else "⚠️ Word dosyası bulunamadı. Lütfen önce üretilmesini bekleyin."
                })

            elif data.startswith("apply_"):
                job_id = int(data.split("_")[1])
                try:
                    mark_job_status(job_id, "applied")
                    send_telegram_request("answerCallbackQuery", {
                        "callback_query_id": cb_id,
                        "text": f"✅ İlan #{job_id} 'Başvuruldu' olarak işaretlendi!"
                    })
                    # Edit original message to show updated status
                    new_text = text + "\n\n<b>STATUS:</b> ✅ Başvuruldu olarak kaydedildi."
                    send_telegram_request("editMessageText", {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": new_text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True
                    })
                except ValueError as ve:
                    send_telegram_request("answerCallbackQuery", {
                        "callback_query_id": cb_id,
                        "text": f"⚠️ Durum güncellenemedi: {ve}",
                        "show_alert": True
                    })
                except Exception as exc:
                    send_telegram_request("answerCallbackQuery", {
                        "callback_query_id": cb_id,
                        "text": f"❌ Beklenmeyen hata: {exc}",
                        "show_alert": True
                    })

            elif data.startswith("reject_"):
                job_id = int(data.split("_")[1])
                # Show quick 2-row reason selector keyboard with option to skip
                reason_markup = {
                    "inline_keyboard": [
                        [
                            {"text": "📍 Lokasyon", "callback_data": f"rreason_{job_id}_lokasyon"},
                            {"text": "🗣️ Dil", "callback_data": f"rreason_{job_id}_dil"}
                        ],
                        [
                            {"text": "💼 Deneyim/Teknik", "callback_data": f"rreason_{job_id}_deneyim"},
                            {"text": "🎓 Eğitim/Derece", "callback_data": f"rreason_{job_id}_egitim"}
                        ],
                        [
                            {"text": "🛂 Vize/Sponsorluk", "callback_data": f"rreason_{job_id}_vize"},
                            {"text": "💰 Maaş/Koşullar", "callback_data": f"rreason_{job_id}_maas"}
                        ],
                        [
                            {"text": "🏢 Şirket/Tercih", "callback_data": f"rreason_{job_id}_sirket"},
                            {"text": "❓ Diğer", "callback_data": f"rreason_{job_id}_diger"}
                        ],
                        [
                            {"text": "⚡ Neden Belirtmeden Ele", "callback_data": f"rreason_{job_id}_skip"}
                        ]
                    ]
                }
                send_telegram_request("answerCallbackQuery", {
                    "callback_query_id": cb_id,
                    "text": "Ret nedeni seçin (isteğe bağlı):"
                })
                send_telegram_request("editMessageReplyMarkup", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": reason_markup
                })

            elif data.startswith("rreason_"):
                parts = data.split("_")
                job_id = int(parts[1])
                reason_key = parts[2] if len(parts) > 2 else "skip"

                reason_map = {
                    "lokasyon": "Lokasyon",
                    "dil": "Dil",
                    "deneyim": "Deneyim / Teknik Uyum",
                    "egitim": "Eğitim / Derece",
                    "vize": "Çalışma İzni / Sponsorluk",
                    "maas": "Maaş / Koşullar",
                    "sirket": "Şirket / Kişisel Tercih",
                    "diger": "Diğer",
                    "skip": None
                }
                chosen_reason = reason_map.get(reason_key)
                try:
                    mark_job_status(job_id, "rejected", note=chosen_reason)
                    reason_label = f" (Neden: {chosen_reason})" if chosen_reason else ""
                    send_telegram_request("answerCallbackQuery", {
                        "callback_query_id": cb_id,
                        "text": f"❌ İlan #{job_id} elendi{reason_label}."
                    })
                    new_text = text + f"\n\n<b>STATUS:</b> ❌ İlgilenilmiyor (Elendi{reason_label})."
                    send_telegram_request("editMessageText", {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": new_text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True
                    })
                except ValueError as ve:
                    send_telegram_request("answerCallbackQuery", {
                        "callback_query_id": cb_id,
                        "text": f"⚠️ Durum güncellenemedi: {ve}",
                        "show_alert": True
                    })
                except Exception as exc:
                    send_telegram_request("answerCallbackQuery", {
                        "callback_query_id": cb_id,
                        "text": f"❌ Beklenmeyen hata: {exc}",
                        "show_alert": True
                    })

        # Handle text commands (/ilanlar, /tara, /durum, /help)
        elif "message" in u:
            m = u["message"]
            chat_id = m.get("chat", {}).get("id")
            from_user_id = m.get("from", {}).get("id")
            cmd = (m.get("text") or "").strip().lower()

            if not is_authorized_user(from_user_id, chat_id):
                send_telegram_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": "⛔ <b>Erişim Engellendi</b>\nBu bot yalnızca yetkili CareerOS yöneticisine açıktır.",
                    "parse_mode": "HTML"
                })
                continue

            if cmd in ["/ilanlar", "/review", "/jobs"]:
                send_review_cards()

            elif cmd in ["/durum", "/stats", "/status"]:
                try:
                    con = get_db()
                    total = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                    ready = con.execute("SELECT COUNT(*) FROM jobs WHERE status = 'ready_for_review'").fetchone()[0]
                    applied = con.execute("SELECT COUNT(*) FROM jobs WHERE status='applied'").fetchone()[0]
                    rejected = con.execute("SELECT COUNT(*) FROM jobs WHERE status='rejected'").fetchone()[0]
                    con.close()

                    stats_msg = (
                        f"📊 <b>İlan Avcısı Anlık Durum Raporu</b>\n\n"
                        f"• <b>Toplam Kayıtlı İlan:</b> {total}\n"
                        f"• <b>İnceleme Bekleyen / CV Hazır:</b> {ready} adet 🎯\n"
                        f"• <b>Başvurulan:</b> {applied} adet ✅\n"
                        f"• <b>Elenen / Pas Geçilen:</b> {rejected} adet ❌\n\n"
                        f"<i>İnceleme bekleyenleri görmek için /ilanlar yazabilirsin.</i>"
                    )
                    send_telegram_request("sendMessage", {
                        "chat_id": chat_id or CHAT_ID,
                        "text": stats_msg,
                        "parse_mode": "HTML"
                    })
                except Exception as exc:
                    send_telegram_request("sendMessage", {
                        "chat_id": chat_id or CHAT_ID,
                        "text": f"⚠️ Durum bilgisi alınamadı: {exc}"
                    })

            elif cmd in ["/tara", "/baslat", "/run", "/scan"]:
                import threading
                import subprocess

                global _SCAN_IN_PROGRESS
                if "_SCAN_IN_PROGRESS" not in globals():
                    _SCAN_IN_PROGRESS = False

                if _SCAN_IN_PROGRESS:
                    send_telegram_request("sendMessage", {
                        "chat_id": chat_id or CHAT_ID,
                        "text": "⏳ <b>Şu anda devam eden bir tarama işlemi var!</b> Lütfen bitmesini bekleyin.",
                        "parse_mode": "HTML"
                    })
                else:
                    def run_scan_async(target_chat_id=None):
                        global _SCAN_IN_PROGRESS
                        _SCAN_IN_PROGRESS = True
                        effective_chat_id = target_chat_id or CHAT_ID
                        try:
                            send_telegram_request("sendMessage", {
                                "chat_id": effective_chat_id,
                                "text": "🚀 <b>LinkedIn Taraması Başlatıldı!</b>\n\n<i>İlanlar taranıyor, Claude ile analiz ediliyor ve CV'ler hazırlanıyor. İşlem tamamlandığında haber vereceğim...</i>",
                                "parse_mode": "HTML"
                            })
                            with open(LOG_FILE, "a", encoding="utf-8") as lf:
                                lf.write(f"\n\n=== TELEGRAM MANUAL SCAN: {time.strftime('%Y-%m-%dT%H:%M:%S')} ===\n")
                                lf.flush()
                                res = subprocess.run(
                                    [sys.executable, str(ROOT / "run_daily.py")],
                                    stdout=lf,
                                    stderr=lf,
                                    cwd=str(ROOT),
                                    timeout=7200
                                )

                            _notify_scan_result(effective_chat_id, res.returncode)
                        except subprocess.TimeoutExpired:
                            send_telegram_request("sendMessage", {
                                "chat_id": effective_chat_id,
                                "text": "⏱️ <b>Tarama zaman aşımına uğradı (Timeout - 2 saat).</b> Lütfen logları kontrol edin.",
                                "parse_mode": "HTML"
                            })
                        except Exception as exc:
                            send_telegram_request("sendMessage", {
                                "chat_id": effective_chat_id,
                                "text": f"❌ Tarama hatası: {exc}"
                            })
                        finally:
                            _SCAN_IN_PROGRESS = False

                    thread = threading.Thread(target=run_scan_async, args=(chat_id or CHAT_ID,), daemon=True)
                    thread.start()

            elif cmd.startswith("/ekle") or cmd.startswith("/add"):
                from src.ingestion.manual_ingestion import process_manual_job, validate_url
                parts = (m.get("text") or "").strip().split(maxsplit=1)
                if len(parts) < 2:
                    send_telegram_request("sendMessage", {
                        "chat_id": chat_id or CHAT_ID,
                        "text": "ℹ️ <b>Kullanım:</b> <code>/ekle &lt;İlan URL'i veya İlan Metni&gt;</code>\n\nÖrnek:\n<code>/ekle https://careers.embl.org/job/12345</code>",
                        "parse_mode": "HTML"
                    })
                else:
                    raw_input = parts[1].strip()
                    send_telegram_request("sendMessage", {
                        "chat_id": chat_id or CHAT_ID,
                        "text": "⏳ <b>İlan alınıyor ve analiz ediliyor...</b>\n<i>Lütfen bekleyin, knockout ve eşleşme değerlendirmesi yapılıyor.</i>",
                        "parse_mode": "HTML"
                    })

                    is_url, url_err = validate_url(raw_input)
                    if is_url:
                        res = process_manual_job(url=raw_input)
                    else:
                        res = process_manual_job(text=raw_input)

                    if not res.success:
                        if res.is_duplicate:
                            msg = f"⚠️ <b>Mükerrer İlan:</b> {html.escape(res.reason or '')}"
                        else:
                            msg = f"❌ <b>İlan İşlenemedi:</b> {html.escape(res.error or res.reason or '')}"
                    else:
                        if res.status == "rejected":
                            msg = f"🚫 <b>İlan Elendi:</b> İlan temel kriterlere uymadığı için elendi.\n<b>Neden:</b> {html.escape(res.reason or '')}"
                        elif res.status == "ready_for_review":
                            msg = (
                                f"🎉 <b>İlan Başarıyla Eklendi ve Onaylandı!</b>\n\n"
                                f"• <b>İlan ID:</b> #{res.job_id}\n"
                                f"• <b>Uyum Skoru:</b> %{res.final_score:.0f}\n"
                                f"• <b>Durum:</b> İnceleme Bekliyor (CV Hazırlanabilir)\n\n"
                                f"<i>CV ve Ön Yazı butonlarını getirmek için /ilanlar yazabilirsiniz.</i>"
                            )
                        elif res.status == "evaluated":
                            msg = (
                                f"⏳ <b>İlan Kaydedildi:</b> #{res.job_id}\n"
                                f"• <b>Ön Skor:</b> %{float(res.final_score or 0):.0f}\n"
                                "• <b>Durum:</b> Tam değerlendirme kuyruğunda\n"
                                "<i>Semantik ve LLM değerlendirmesi sonraki taramada tamamlanacak.</i>"
                            )
                        else:
                            msg = (
                                f"ℹ️ <b>İlan Kaydedildi (Düşük Öncelik):</b>\n\n"
                                f"• <b>İlan ID:</b> #{res.job_id}\n"
                                f"• <b>Skor:</b> %{res.final_score:.0f} (Eşik altı)\n"
                                f"• <b>Durum:</b> {res.status}"
                            )

                    send_telegram_request("sendMessage", {
                        "chat_id": chat_id or CHAT_ID,
                        "text": msg,
                        "parse_mode": "HTML"
                    })

            elif cmd in ["/yardim", "/help", "/start"]:
                help_msg = (
                    "🤖 <b>İlan Otomasyonu Telegram Asistanı Komutları:</b>\n\n"
                    "• <b>/ekle &lt;URL&gt;</b> — Dışarıdan bulduğunuz ilanı anında analiz edip kaydeder.\n"
                    "• <b>/ilanlar</b> — Başvuruya hazır fırsatları ve CV/Ön Yazı butonlarını getirir.\n"
                    "• <b>/tara</b> — LinkedIn'i anında baştan sona tarayıp yeni CV'ler üretir.\n"
                    "• <b>/durum</b> — Veritabanındaki toplam ve bekleyen ilan istatistiklerini gösterir.\n"
                    "• <b>/yardim</b> — Bu komut listesini gösterir."
                )
                send_telegram_request("sendMessage", {
                    "chat_id": chat_id or CHAT_ID,
                    "text": help_msg,
                    "parse_mode": "HTML"
                })

    # Confirm processed updates by calling getUpdates with offset
    if max_update_id > 0:
        send_telegram_request("getUpdates", {"offset": max_update_id + 1, "timeout": 1})

def run_polling(duration_seconds=0):
    print("Starting Telegram Bot listener...")
    start_time = time.time()
    while True:
        process_updates()
        if duration_seconds > 0 and (time.time() - start_time) >= duration_seconds:
            break
        time.sleep(2)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--send":
        send_review_cards()
    elif len(sys.argv) > 1 and sys.argv[1] == "--poll":
        run_polling(duration_seconds=int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    else:
        process_updates()
