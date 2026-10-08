import contextlib
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from src.db import get_connection

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE)


def build_message(con=None):
    owns_connection = con is None
    con = get_connection() if owns_connection else con
    with contextlib.suppress(Exception):
        from src.eligibility.gate import ensure_eligibility_schema
        ensure_eligibility_schema(con)

    total = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    today_found = con.execute("""
        SELECT COUNT(*) FROM jobs
        WHERE date(date_found)=date('now')
    """).fetchone()[0]
    qualified = con.execute("""
        SELECT COUNT(*) FROM jobs
        WHERE llm_judge_status='success'
    """).fetchone()[0]
    from src.eligibility.gate import evaluate_job_eligibility
    from src.eligibility.profile_adapter import get_candidate_profile_facts

    candidate = get_candidate_profile_facts()
    table_cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()]

    ready_rows = con.execute(f"""
        SELECT {', '.join(table_cols)} FROM jobs
        WHERE status = 'ready_for_review'
    """).fetchall()
    ready_count = 0
    for r in ready_rows:
        job_dict = dict(zip(table_cols, r, strict=False)) if not isinstance(r, dict) else dict(r)
        dec = evaluate_job_eligibility(job_dict, candidate)
        if dec.can_notify:
            ready_count += 1

    applied_count = con.execute("""
        SELECT COUNT(*) FROM jobs
        WHERE status = 'applied'
    """).fetchone()[0]

    # Calculate total unique output packages currently ready for ACTIVE unapplied jobs
    out_dir = ROOT / "output"
    total_packages = 0
    if out_dir.exists():
        unique_jids = set()
        for p in list(out_dir.rglob("tailored_cv.docx")) + list(out_dir.rglob("tailored_cv.pdf")):
            parts = p.parent.name.split("_")
            if parts and parts[-1].isdigit():
                unique_jids.add(int(parts[-1]))
        if unique_jids:
            active_rows = con.execute(f"""
                SELECT {', '.join(table_cols)} FROM jobs
                WHERE status NOT IN ('applied', 'rejected', 'withdrawn')
            """).fetchall()
            valid_active_ids = set()
            for r in active_rows:
                job_dict = dict(zip(table_cols, r, strict=False)) if not isinstance(r, dict) else dict(r)
                dec = evaluate_job_eligibility(job_dict, candidate)
                if dec.can_generate_documents:
                    valid_active_ids.add(job_dict.get("id"))
            total_packages = len(unique_jids.intersection(valid_active_ids))

    if owns_connection:
        con.close()

    now_str = datetime.now().strftime("%d.%m.%Y • %H:%M")

    lines = [
        "<b>İlan Bulma Otomasyonu ➜ Günlük Rapor</b>",
        f"📅 <i>{now_str}</i>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "📊 <b>Günün Özeti:</b>",
        f"• Veritabanındaki Toplam İlan: <b>{total}</b>",
        f"• Bugün Bulunan Yeni İlan: <b>{today_found}</b>",
        f"• LLM Tarafından İncelenen: <b>{qualified}</b>",
        f"• Başvurulan İlanlar: <b>{applied_count}</b>",
        f"• İnceleme Bekleyen Aktif İlan: <b>{ready_count} İlan</b>",
        f"• Başvuru Bekleyen Hazır Belgeler: <b>{total_packages} Paket (CV+Mektup)</b>",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "<i>Bekleyen ilan kartları birazdan aşağıda listelenecek... 👇</i>"
    ]

    return "\n".join(lines)

def send():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("TELEGRAM: skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)")
        return

    import requests
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": build_message(),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    from src.telegram_bot import require_telegram_success, send_review_cards

    failures = []
    try:
        response = requests.post(
            url,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        require_telegram_success(response.json())
        print("TELEGRAM: SUMMARY PASS")
    except Exception as e:
        failures.append("summary")
        print(f"TELEGRAM: failed to send summary message ({type(e).__name__})")

    try:
        send_review_cards()
        print("TELEGRAM: CARDS PASS")
    except Exception as e:
        failures.append("cards")
        print(f"TELEGRAM: failed to send review cards ({type(e).__name__})")

    # Incoming commands belong exclusively to the polling bot service.
    if failures:
        raise RuntimeError("Telegram delivery failed: " + ", ".join(failures))

if __name__ == "__main__":
    send()
