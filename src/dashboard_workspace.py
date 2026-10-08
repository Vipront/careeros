"""The approved Sage B workspace, backed by the existing dashboard services."""
import html
import json
import math
from urllib.parse import urlsplit

import pandas as pd
import streamlit as st

from src.dashboard_data import explain_review
from src.dashboard_overview import get_overview_queues
from src.dashboard_theme import render_desk_heading, render_svg_icon
from src.final_ranking import READY_THRESHOLD
from src.review_feedback import record_feedback, request_retry


FILTERS = ["Tümü (Aktif İlanlar)", "Yüksek Eşleşme (%80+)", "Değerlendirme Bekleyen",
           "İşleme Kuyruğunda", "Düşük Öncelik / İzleme", "Başvurulanlar", "Mülakat & Teklif",
           "Kolay Başvuru", "Arşiv / Reddedilenler"]
GROUPS = {"Tümü": None, "Uygun": "eligible", "İnceleme": "review", "Engellendi": "ineligible"}


def text(value, fallback=""):
    return fallback if value is None or (isinstance(value, float) and math.isnan(value)) else str(value)


def escape(value):
    return html.escape(text(value), quote=True)


def verdict(row):
    workflow = {"applied": "Başvuruldu", "interview": "Mülakat", "offer": "Teklif",
                "rejected": "Arşivde", "withdrawn": "Arşivde"}.get(text(row.get("status")))
    if workflow:
        return "suitable", workflow, "briefcase"
    status = text(row.get("eligibility_status"))
    if status == "eligible":
        return "suitable", "Uygun", "check"
    if status == "ineligible":
        return "blocked", "Engellendi", "cross"
    return "pending", "İnceleme bekliyor", "pending"


def decision_evidence(row):
    """Use authoritative gate evidence; absent evidence is explicitly unknown."""
    try:
        decision = json.loads(text(row.get("eligibility_decision_json"), "{}"))
    except (ValueError, TypeError):
        decision = {}
    if not isinstance(decision, dict):
        decision = {}
    result = []
    for key, label in (("experience", "Deneyim"), ("language", "Dil"),
                       ("posting_language_preference", "İlan dili"), ("liveness", "Kaynak")):
        item = decision.get(key)
        item = item if isinstance(item, dict) else {}
        status = item.get("status", "unknown")
        kind = "suitable" if status in {"met", "open"} else "blocked" if status in {"unmet", "closed"} else "pending"
        result.append({"label": label, "kind": kind, "reason": text(item.get("reason"), "Bu kriter için doğrulanmış kanıt bulunmuyor."),
                       "spans": item.get("evidence_spans", [])})
    return result


def filter_jobs(frame, query="", status_filter=FILTERS[0], group="Tümü", sort="Skor (Azalan)", applications_only=False):
    if frame.empty:
        return frame.copy()
    result = frame.copy()
    if applications_only:
        result = result[result["status"].isin(["applied", "interview", "offer"])]
    if status_filter == FILTERS[1]:
        result = result[result["final_score"].fillna(0) >= 80]
    elif status_filter == FILTERS[2]:
        result = result[result["is_ready_recommendation"].fillna(False).astype(bool)]
    elif status_filter in (FILTERS[3], FILTERS[4], FILTERS[5], FILTERS[8]):
        target = {FILTERS[3]: "evaluated", FILTERS[4]: "low_priority", FILTERS[5]: "applied", FILTERS[8]: "rejected"}[status_filter]
        result = result[result["status"] == target]
    elif status_filter == FILTERS[6]:
        result = result[result["status"].isin(["interview", "offer"])]
    elif status_filter == FILTERS[7]:
        result = result[result["is_easy_apply"] == 1]
    if query.strip():
        mask = pd.Series(False, index=result.index)
        for column in ("title", "company", "location", "requirements_text"):
            if column in result:
                mask |= result[column].fillna("").astype(str).str.contains(query.strip(), case=False, regex=False)
        result = result[mask]
    if GROUPS.get(group):
        result = result[result["eligibility_status"].fillna("review") == GROUPS[group]]
    if group == "İnceleme":
        result = result[~result["status"].isin(["applied", "interview", "offer"])]
    columns, ascending = {"Skor (Azalan)": (["final_score", "id"], [False, False]),
                          "Tarih (En Yeni)": (["created_at", "id"], [False, False]),
                          "Şirket (A-Z)": (["company", "title"], [True, True])}[sort]
    return result.sort_values(columns, ascending=ascending)


def reset_filters():
    st.session_state.update(triage_search_query="", triage_filter_status=FILTERS[0], workbench_status_filter="Tümü", desk_view="İlanlar", desk_high_match=False)


def select_job(job_id):
    st.session_state.update(selected_job_id=job_id, mobile_detail_open=True)


def close_mobile():
    st.session_state["mobile_detail_open"] = False


def open_workspace(job_id=None, status_filter=FILTERS[0], view=None):
    reset_filters()
    if job_id is not None:
        st.session_state["pending_job_page"] = job_id
    st.session_state.update(app_workspace_mode="İlan Değerlendirme", selected_job_id=job_id,
                            triage_filter_status=status_filter, desk_view=view or ("İncelenecek" if status_filter == FILTERS[2] else "İlanlar"), mobile_detail_open=job_id is not None)


def render_today(services):
    frame = services["df_all_active"]
    st.markdown(render_desk_heading("BUGÜN", "Bugünün odağı", "İnceleme kuyruğun, başvuruların ve sıradaki adımların."), unsafe_allow_html=True)
    ready, watchlist = get_overview_queues(frame, limit=max(len(frame), 1))
    pending = desk_jobs(frame, view="İncelenecek") if not frame.empty else frame
    applications = frame[frame["status"].isin(["applied", "interview", "offer"])] if not frame.empty else frame
    tasks = [("grid", f"{len(ready)} ilan incelemeye hazır", "Uygunluk kontrolünü geçen fırsatları incele.", ready, FILTERS[0]),
             ("pending", f"{len(pending)} ilanın kontrolü bekliyor", "Belirsiz kriterleri ve değerlendirme durumunu görüntüle.", pending, FILTERS[2]),
             ("briefcase", f"{len(applications)} başvurun süreçte", "Başvuru, mülakat ve teklif aşamalarını takip et.", applications, FILTERS[0])]
    for index, (icon, title, description, rows, status_filter) in enumerate(tasks):
        with st.container(key=f"today_task_{index}"):
            st.markdown(f'<div class="task-top"><span class="task-symbol">{render_svg_icon(icon)}</span><div><h3>{title}</h3><p>{description}</p></div></div>', unsafe_allow_html=True)
            if index == 2:
                st.button("Başvuruları aç", key="today_applications", on_click=services["navigate_workspace"], args=("Başvurular",))
            else:
                st.button("İlan masasına git", key=f"today_open_{index}", on_click=open_workspace,
                          args=(int(rows.iloc[0]["id"]) if not rows.empty else None, status_filter))
    if not watchlist.empty:
        st.caption("Düşük öncelikli fırsatlar hazır önerilerden ayrı tutulur.")
        st.button("İzleme listesini aç", key="btn_open_low_priority_watchlist", on_click=open_workspace, args=(None, FILTERS[4]))


def render_applications(services):
    frame = filter_jobs(services["df_all_active"], applications_only=True, sort="Tarih (En Yeni)")
    st.markdown(render_desk_heading("BAŞVURULARIN", "Başvuruların", "Başvurudan mülakata, süreçlerini tek yerden takip et."), unsafe_allow_html=True)
    if frame.empty:
        st.markdown('<div class="workspace-empty"><strong>Henüz takip edilen başvuru yok</strong><p>İlan detayındaki Başvurdum düğmesiyle süreci takip etmeye başlayabilirsin.</p></div>', unsafe_allow_html=True)
    names = {"applied": "Başvuruldu", "interview": "Mülakat", "offer": "Teklif"}
    next_steps = {"applied": "Yanıt geldiğinde başvuru durumunu güncelleyebilirsin.", "interview": "Görüşme için başvuru belgelerini ve ilan detaylarını açabilirsin.", "offer": "Teklif sürecini ilan detayından takip edebilirsin."}
    for _, row in frame.iterrows():
        job_id = int(row["id"])
        status = text(row.get("status"))
        with st.container(key=f"application_card_{job_id}"):
            st.markdown(f'<div class="application-heading"><div><h3>{escape(row.get("title"))}</h3><p>{escape(row.get("company"))} · {escape(row.get("location"))}</p></div>'
                        f'<span class="chip chip-suitable">{names[status]}</span></div>'
                        f'<div class="application-next">{render_svg_icon("calendar")} {next_steps[status]}</div>', unsafe_allow_html=True)
            st.button("İlan detayına git", key=f"application_open_{job_id}", on_click=open_workspace, args=(job_id, FILTERS[0], "Başvurulanlar"))


def desk_jobs(frame, query="", view="İlanlar", sort="Skor (Azalan)", high_match=False):
    result = filter_jobs(frame, query=query, sort=sort)
    if result.empty:
        return result
    if high_match:
        result = result[pd.to_numeric(result["final_score"], errors="coerce").ge(80)]
    if view == "Başvurulanlar":
        return result[result["status"].isin(["applied", "interview", "offer"])]
    if view == "Arşiv":
        return result[result["status"].isin(["rejected", "withdrawn"])]
    result = result[~result["status"].isin(["applied", "interview", "offer", "rejected", "withdrawn"])]
    if view == "İncelenecek":
        result = result[result["eligibility_status"].fillna("review").eq("review")
                        & pd.to_numeric(result["final_score"], errors="coerce").ge(READY_THRESHOLD)]
    return result


def render_workspace(services, applications_only=False):
    st.markdown(render_desk_heading("BAŞVURULARIN" if applications_only else "FIRSATLARIN",
                "Başvuruların" if applications_only else "İlan çalışma masası",
                "Başvurudan mülakata, süreçlerini tek yerden takip et." if applications_only else
                "İlanı incele, başvuruya git ve başvurunu kaydet."), unsafe_allow_html=True)
    source = services["load_data"](include_rejected=st.session_state.get("desk_view") == "Arşiv")
    views = ["İlanlar", "İncelenecek", "Başvurulanlar", "Arşiv"]
    counts = {name: len(desk_jobs(source, view=name)) for name in views}
    with st.container(key="desk_tabs"):
        view = st.radio("İlan listesi", views, horizontal=True, label_visibility="collapsed", key="desk_view",
                        format_func=lambda name: f"{name} ({counts[name]})" if name != "Arşiv" else name)
    with st.container(key="workbench_toolbar"):
        search_col, score_col, sort_col = st.columns([2.8, 1.1, 1], vertical_alignment="center")
        with search_col:
            query = st.text_input("İlan ara", placeholder="İlan başlığı, şirket veya nitelik…", key="triage_search_query", label_visibility="collapsed")
        with score_col:
            high_match = st.checkbox("Yüksek eşleşme (%80+)", key="desk_high_match")
        with sort_col:
            sort = st.selectbox("Sıralama", ["Skor (Azalan)", "Tarih (En Yeni)", "Şirket (A-Z)"],
                                key="desk_sort", label_visibility="collapsed")
    filtered = desk_jobs(source, query, "Başvurulanlar" if applications_only else view, sort, high_match)

    page_count = max(1, (len(filtered) + 29) // 30)
    page_signature = (query, view, sort, high_match, applications_only)
    if st.session_state.get("workbench_page_signature") != page_signature:
        st.session_state["workbench_page"] = 1
        st.session_state["workbench_page_signature"] = page_signature
    pending_job = st.session_state.pop("pending_job_page", None)
    if pending_job in filtered.get("id", pd.Series(dtype=int)).values:
        st.session_state["workbench_page"] = list(filtered["id"]).index(pending_job) // 30 + 1
    st.session_state["workbench_page"] = min(max(1, st.session_state.get("workbench_page", 1)), page_count)
    page = st.session_state["workbench_page"]
    visible = filtered.iloc[(page - 1) * 30:page * 30]
    selected = st.session_state.get("selected_job_id")
    if not visible.empty and selected not in visible["id"].values:
        selected = int(visible.iloc[0]["id"])
    elif filtered.empty:
        selected = None
        st.session_state["mobile_detail_open"] = False
    st.session_state["selected_job_id"] = selected
    if st.session_state.get("mobile_detail_open"):
        st.markdown('<span class="mobile-detail-state" aria-hidden="true"></span>', unsafe_allow_html=True)
    with st.container(key="workbench_split"):
        master_col, detail_col = st.columns([0.85, 1.65], gap="medium")
        with master_col:
            with st.container(key="workbench_master_pane", height=560, border=False):
                st.markdown(f'<div class="master-list-header"><span>İLAN LİSTESİ</span><span class="master-list-count">{len(filtered)} ilan</span></div>', unsafe_allow_html=True)
                if filtered.empty:
                    message = "Seçtiğin arama veya puan filtresine uygun ilan yok. Aramayı temizleyebilir veya puan filtresini kapatabilirsin." if query or high_match else {
                        "İlanlar": "İncelenecek yeni ilan bulunmuyor. Başvurularını Başvurulanlar bölümünde görebilirsin.",
                        "İncelenecek": "İncelemene hazır yeni öneri bulunmuyor.",
                        "Başvurulanlar": "Başvurdum dediğin ilanlar burada görünecek.", "Arşiv": "Kaldırdığın ilanlar burada saklanır."}[view]
                    st.markdown(f'<div class="workspace-empty"><strong>Liste boş</strong><p>{message}</p></div>', unsafe_allow_html=True)
                if page_count > 1:
                    st.selectbox("Liste sayfası", range(1, page_count + 1), key="workbench_page",
                                 format_func=lambda number: f"Sayfa {number} / {page_count}")
                for index, (_, row) in enumerate(visible.iterrows()):
                    job_id = int(row["id"])
                    kind, label, icon = verdict(row)
                    company = text(row.get("company"), "Şirket belirtilmemiş")
                    initials = "".join(word[0] for word in company.split()[:2]).upper()
                    active = " selected" if selected == job_id else ""
                    score = pd.to_numeric(row.get("final_score"), errors="coerce")
                    score_label = f'%{score:.0f}' if pd.notna(score) else '—'
                    with st.container(key=f"master_card_{job_id}"):
                        st.markdown(f'<div class="master-card-wrapper{active}">'
                            f'<span class="company-avatar avatar-{index % 3}">{escape(initials)}</span>'
                            f'<div class="master-job-top"><span class="master-job-title">{escape(row.get("title"))}</span><span class="chip chip-score">{score_label}</span></div>'
                            f'<div class="master-job-company">{escape(company)} · {escape(row.get("location"))}</div>'
                            f'<div class="master-job-meta-row"><span class="chip chip-{kind}">{render_svg_icon(icon)} {label}</span>'
                            f'<span>{escape(services["format_relative_date"](row.get("created_at")))}</span></div></div>', unsafe_allow_html=True)
                        st.button(f'{text(row.get("title"))} — {company}', key=f"sel_m_{job_id}", on_click=select_job, args=(job_id,), use_container_width=True)
        with detail_col:
            with st.container(key="workbench_detail_pane", height=560, border=False):
                st.button("Listeye dön", key="close_mobile_detail", on_click=close_mobile)
                if selected is None:
                    st.markdown('<div class="workspace-empty"><strong>İlan seçilmedi</strong><p>Detaylarını görmek için listeden bir ilan seç.</p></div>', unsafe_allow_html=True)
                else:
                    render_detail(filtered[filtered["id"] == selected].iloc[0], services)


def render_detail(row, services):
    job_id = int(row["id"])
    status = text(row.get("status"))
    kind, label, icon = verdict(row)
    score = pd.to_numeric(row.get("final_score"), errors="coerce")
    score_label = f'%{score:.0f}' if pd.notna(score) else '—'
    review = explain_review(row)
    st.markdown(f'<div class="detail-top-bar"><span class="chip chip-{kind}">{render_svg_icon(icon)} {label}</span>'
        f'<span class="chip chip-score">Eşleşme indeksi: {score_label}</span></div>'
        f'<h2 class="detail-title">{escape(row.get("title"))}</h2>'
        f'<div class="detail-company-line"><span class="detail-company-name">{escape(row.get("company"))}</span>'
        f'<span>·</span>{render_svg_icon("pin")}<span>{escape(row.get("location"))}</span></div>', unsafe_allow_html=True)
    url = text(row.get("url"))
    parsed = urlsplit(url)
    safe_url = url if parsed.scheme in {"http", "https"} and parsed.netloc else None
    with st.container(key="detail_actions"):
        actions = st.columns([1.1, 1.1, 0.8])
    with actions[0]:
        if safe_url:
            st.link_button("Başvuruya Git", safe_url, type="primary", use_container_width=True)
    with actions[1]:
        transitions = {"evaluated": ("Başvurdum", "applied"), "ready_for_review": ("Başvurdum", "applied"), "low_priority": ("Başvurdum", "applied"),
                       "applied": ("Mülakata Alındı", "interview"), "interview": ("Teklif Alındı", "offer")}
        if status in transitions:
            name, target = transitions[status]
            st.button(name, key=f"btn_act_apply_{job_id}" if target == "applied" else f"btn_act_{target}_{job_id}",
                      on_click=services["update_job_status"], args=(job_id, target), use_container_width=True)
        else:
            st.caption({"evaluated": "Değerlendirme sürüyor", "offer": "Teklif aşamasında", "rejected": "Arşivde"}.get(status, status))
    with actions[2]:
        if status not in {"applied", "interview", "offer", "rejected", "withdrawn"}:
            st.button("Listeden kaldır", key=f"archive_{job_id}", on_click=services["update_job_status"],
                      args=(job_id, "rejected"), use_container_width=True)
        else:
            with st.popover("Diğer işlemler", use_container_width=True):
                previous = {"applied": "ready_for_review", "interview": "applied", "offer": "interview"}.get(status)
                if previous:
                    st.button("Geri Al", key=f"btn_act_revert_{job_id}", on_click=services["update_job_status"], args=(job_id, previous))
                if status in {"rejected", "withdrawn"}:
                    st.button("Arşivden Çıkar", key=f"btn_unarchive_{job_id}", on_click=services["update_job_status"],
                              args=(job_id, "ready_for_review"), kwargs={"allow_unarchive": True})
                else:
                    st.button("Arşive kaldır", key=f"archive_{job_id}", on_click=services["update_job_status"], args=(job_id, "rejected"))
    st.markdown('<div class="detail-header-divider"></div>', unsafe_allow_html=True)
    summary = text(row.get("requirements_text"))
    if summary:
        st.markdown("**İlan özeti**")
        st.write(summary[:1200] + ("…" if len(summary) > 1200 else ""))
    panel = st.expander("Değerlendirme ayrıntıları", key=f"detail_evaluation_{job_id}", on_change="rerun")
    if panel.open:
        with panel:
            st.caption(review["stage"])
            evidence = decision_evidence(row)
            if review.get("reason"):
                st.write(review["reason"])
            for item in evidence:
                if item["label"] == "Kaynak" and item["kind"] == "pending":
                    continue
                result = {"suitable": "Uygun", "blocked": "Karşılanmıyor", "pending": "Belirsiz"}[item["kind"]]
                st.write(f'{item["label"]} · {result}: {item["reason"]}')
            for field, label in (("semantic_score", "Semantik uyum"), ("llm_score", "Kriter değerlendirmesi")):
                value = pd.to_numeric(row.get(field), errors="coerce")
                st.write(f'{label}: %{value * (100 if field == "semantic_score" else 1):.0f}' if pd.notna(value) else f'{label}: Henüz değerlendirilmedi')
            for uncertainty in review["uncertainties"]:
                st.write(f"Belirsiz: {text(uncertainty)}")
            for field, label in (("deadline", "Son başvuru"), ("retry_at", "Yeniden deneme")):
                if review[field]:
                    st.caption(f'{label}: {review[field]}')
            if row.get("pipeline_status") == "FAILED":
                st.warning(f'Belge hazırlığı tamamlanamadı: {text(row.get("last_error"), "Yeniden hazırlanmalı.")}')
            if status == "evaluated" and (row.get("pipeline_status") == "FAILED" or row.get("llm_judge_status") in {"failed", "rate_limited"}):
                if st.button("Yeniden denemeyi kuyruğa al", key=f"retry_{job_id}"):
                    try:
                        request_retry(job_id)
                        st.cache_data.clear()
                        st.success("Yeniden deneme günlük işlem kuyruğuna alındı.")
                    except ValueError as exc:
                        st.error(str(exc))
            for item in evidence:
                if item["spans"]:
                    st.write(item["label"] + ": " + "; ".join(map(str, item["spans"])))
    panel = st.expander("İlan metni ve görevler", key=f"detail_description_{job_id}", on_change="rerun")
    if panel.open:
        with panel:
            description = text(row.get("description")) or services["get_job_description"](job_id)
            st.write(description or text(row.get("requirements_text"), "İlan metni henüz alınamadı."))
            if safe_url:
                st.link_button("Orijinal kaynağı görüntüle", safe_url)
    panel = st.expander("Başvuru belgeleri", key=f"detail_documents_{job_id}", on_change="rerun")
    if panel.open:
        with panel:
            folder = services["get_job_assets_index"]().get(job_id)
            files = sorted(f for f in folder.iterdir() if f.is_file() and not f.is_symlink() and f.resolve().parent == folder.resolve() and f.suffix in {".pdf", ".docx", ".txt", ".json"}) if folder and folder.exists() else []
            if not files:
                st.caption("Bu ilan için henüz özel CV veya ön yazı üretilmemiş.")
            for file in files:
                st.download_button(file.name, file.read_bytes(), file_name=file.name, key=f"dl_st_{job_id}_{file.name}")
    panel = st.expander("Öneri hakkında geri bildirim", key=f"detail_feedback_{job_id}", on_change="rerun")
    if panel.open:
        with panel:
            st.caption("Bu not öneriyi değerlendirmek için kaydedilir; otomatik uygunluk sonucunu değiştirmez.")
            events = services["get_job_timeline_events"](job_id)
            for event_type, event_time, event_note in reversed(events):
                if event_type != "review_feedback":
                    continue
                try:
                    saved = json.loads(event_note)
                    if not isinstance(saved, dict) or type(saved.get("is_match")) is not bool:
                        continue
                except (ValueError, TypeError):
                    continue
                st.success("Kayıtlı geri bildirimin: " + ("Uygun" if saved["is_match"] else "Uygun değil"))
                st.write(text(saved.get("reason")))
                st.caption(f'Son kayıt: {text(event_time)[:16]}. Yeni gönderim son geri bildirimin olarak kullanılır.')
                break
            with st.form(f"review_feedback_{job_id}", clear_on_submit=True):
                choice = st.radio("Bu ilan sana uygun mu?", ["Uygun", "Uygun değil"], horizontal=True)
                reason = st.text_area("Gerekçe", max_chars=4000)
                if st.form_submit_button("Geri bildirimi kaydet"):
                    try:
                        record_feedback(job_id, choice == "Uygun", reason)
                        st.cache_data.clear()
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
    panel = st.expander("İşlem geçmişi", key=f"detail_history_{job_id}", on_change="rerun")
    if panel.open:
        with panel:
            events = services["get_job_timeline_events"](job_id)
            if not events:
                st.caption("Geçmiş durum kaydı bulunmuyor.")
            for event_type, event_time, event_note in events[-10:]:
                st.write(f'{text(event_time)[:16]} · {text(event_type)} · {text(event_note)}')
