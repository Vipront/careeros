import streamlit as st
import pandas as pd
import altair as alt
import os
import html
import textwrap
import functools
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from src.ops.login_security import LOGIN_ATTEMPTS
from src.observability.telemetry import safe_log, sanitize_text
from src.db import transition, get_connection, get_connection_provider
from src.dashboard_data import JOB_COLUMNS, fetch_description
from src.dashboard_cache import get_dashboard_jobs, patch_cached_status, refresh_dashboard_cache, start_dashboard_refresh, SnapshotUnavailable
from src.dashboard_theme import inject_theme, render_login_header, render_brand_header, render_desk_heading, render_svg_icon
from src.dashboard_workspace import render_workspace, render_today, render_applications


def clean_html(raw_str: str) -> str:
    """Safely dedent and strip HTML strings so Streamlit Markdown does not parse indented lines as code blocks."""
    return textwrap.dedent(raw_str).strip()

# ==============================================================================
# 1. CONFIG & INITIALIZATION
# ==============================================================================
st.set_page_config(
    page_title="CareerOS",
    layout="wide",
    initial_sidebar_state="collapsed",
)

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE)
start_dashboard_refresh()

# ==============================================================================
# 2. SAGE THEME STYLESHEET INJECTION
# ==============================================================================
inject_theme(st)

# ==============================================================================
# 3. EXECUTIVE LOGIN & SECURITY GATE (PROFESSIONAL PORTAL EXPERIENCE)
# ==============================================================================
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
if not DASHBOARD_PASSWORD:
    st.error("Erişim devre dışı bırakıldı: DASHBOARD_PASSWORD ortam değişkeni yapılandırılmamış.")
    st.stop()

if not st.session_state.get("authenticated", False):
    login_key = "global"
    if LOGIN_ATTEMPTS.is_limited(login_key):
        st.error("Çok fazla başarısız giriş denemesi. Bir süre sonra yeniden deneyin.")
        st.stop()
    with st.container(key="login_shell"):
        st.markdown(render_login_header(), unsafe_allow_html=True)
        with st.form("login_form"):
            pwd_input = st.text_input("Şifre", type="password", placeholder="Şifrenizi girin...")
            if st.form_submit_button("Giriş yap", use_container_width=True, type="primary"):
                import hmac
                valid_password = hmac.compare_digest(str(pwd_input or ""), str(DASHBOARD_PASSWORD or ""))
                accepted, limited = LOGIN_ATTEMPTS.check_attempt(login_key, valid_password)
                if accepted:
                    st.session_state["authenticated"] = True
                    st.rerun()
                elif limited:
                    st.error("Çok fazla başarısız giriş denemesi. Bir süre sonra yeniden deneyin.")
                else:
                    st.error("Şifre hatalı.")
    st.stop()


# ==============================================================================
# 4. TEST-VERIFIED CORE HELPERS & DATA UTILITIES
# ==============================================================================
@functools.lru_cache(maxsize=256)
def get_avatar_class(company_name):
    c = company_name.strip()[:1].upper() if company_name else "B"
    classes = {
        "B": "av-b", "I": "av-i", "G": "av-g", "S": "av-s", "N": "av-n",
        "D": "av-d", "H": "av-h", "T": "av-d", "R": "av-b", "F": "av-g",
    }
    return classes.get(c, "av-b"), c


@functools.lru_cache(maxsize=512)
def parse_job_sections(job_id, title, company, requirements_text, description):
    raw_text = (requirements_text or description or "").strip()
    if not raw_text:
        return f"{company} bünyesindeki {title} pozisyonu.", ["Pozisyon nitelikleri ve teknik gereksinimler."]

    cleaned_desc = raw_text.replace("\\n", " ").replace("\\r", " ").strip()
    overview = cleaned_desc[:280]
    if len(cleaned_desc) > 280:
        last_period = overview.rfind(".")
        if last_period > 100:
            overview = overview[: last_period + 1]
        else:
            overview += "..."

    bullets = []
    lines = [ln.strip() for ln in raw_text.split("\\n") if ln.strip()]
    for ln in lines:
        if any(
            ln.startswith(prefix)
            for prefix in ["-", "*", "•", "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.", "10."]
        ):
            clean_b = ln.strip("-*• 0123456789.:")
            if len(clean_b) > 5 and clean_b not in bullets:
                bullets.append(clean_b)

    if not bullets:
        sentences = [s.strip() for s in raw_text.split(".") if len(s.strip()) > 15]
        bullets = sentences[:5]

    return overview, bullets[:6]


@functools.lru_cache(maxsize=512)
def get_job_skills_tags(title, company, loc, req_text, desc_text):
    text_lower = f"{title} {company} {loc} {req_text} {desc_text}".lower()
    tags = []
    skill_map = [
        ("ngs", "NGS"),
        ("bioinformatics", "Bioinformatics"),
        ("python", "Python"),
        (" r ", "R"),
        ("linux", "Linux"),
        ("qpcr", "qPCR"),
        ("pcr", "PCR"),
        ("rna", "RNA QC"),
        ("cell culture", "Cell Culture"),
        ("fermentation", "Fermentation"),
        ("bioprocess", "Bioprocessing"),
        ("western blot", "Western Blot"),
        ("bioassay", "Bioassay"),
        ("gmp", "GMP"),
        ("microarray", "Microarray"),
        ("genetics", "Genetics"),
    ]
    for key, label in skill_map:
        if key in text_lower and label not in tags:
            tags.append(label)
    if not tags:
        tags = ["Molecular Biology", "Biotechnology"]
    return tags[:5]


def get_job_evidence_checklist(job_row):
    text_lower = f"{job_row.get('title', '')} {job_row.get('company', '')} {job_row.get('requirements_text', '')} {job_row.get('description', '')}".lower()
    evidence_items = []

    # 1. Degree & Academic Background
    if any(k in text_lower for k in ["bioinformatics", "computational", "biyoinformatik"]):
        evidence_items.append({
            "title": "BSc / MSc Biyoinformatik & MBG",
            "desc": "Biyoinformatik ve Moleküler Biyoloji lisans/yüksek lisans eğitiminiz pozisyonla örtüşüyor.",
            "status": "match",
        })
    else:
        evidence_items.append({
            "title": "Moleküler Biyoloji / Biyoteknoloji Lisans",
            "desc": "Moleküler biyoloji ve yaşam bilimleri akademik geçmişiniz doğrulanmıştır.",
            "status": "match",
        })

    # 2. Lab & Technical Capabilities
    if any(k in text_lower for k in ["pcr", "qpcr", "rna", "wet lab", "assay", "fermentation", "cell culture"]):
        evidence_items.append({
            "title": "Islak-Lab PCR, RNA QC & Deney Becerileri",
            "desc": "PCR, RNA izolasyonu, kalite kontrol ve laboratuvar metodolojisi uyumludur.",
            "status": "match",
        })
    elif any(k in text_lower for k in ["python", " r ", "pipeline", "sequencing", "ngs"]):
        evidence_items.append({
            "title": "Python NGS & Biyoinformatik Veri Analizi",
            "desc": "Python ve Linux tabanlı omics veri analizi yetkinlikleriniz eşleşmektedir.",
            "status": "match",
        })
    else:
        evidence_items.append({
            "title": "Teknik & Metodolojik Yetkinlik",
            "desc": "Pozisyon için gerekli temel bilimsel analiz ve dokümantasyon becerisi uyumludur.",
            "status": "match",
        })

    # 3. Computational & QC
    if any(k in text_lower for k in ["python", " r ", "bioinformatics", "data analysis", "computational"]):
        evidence_items.append({
            "title": "Python / R Biyolojik Veri Analitiği",
            "desc": "Biyolojik veri setlerinin istatistiksel ve algoritmik işlenmesi yetkinliği eşleşiyor.",
            "status": "match",
        })
    elif any(k in text_lower for k in ["qc", "quality control", "documentation", "gmp"]):
        evidence_items.append({
            "title": "Kalite Kontrol (QC) ve Dokümantasyon",
            "desc": "Prosedürlere uygun deney takibi ve kalite kontrol standartları doğrulanmıştır.",
            "status": "match",
        })
    else:
        evidence_items.append({
            "title": "Analitik Düşünce ve Problem Çözme",
            "desc": "Bilimsel araştırma ve problem çözme altyapısı pozisyona uygundur.",
            "status": "match",
        })

    # 4. Language Requirement
    if any(
        k in text_lower
        for k in [
            "deutschkenntnisse", "german language", "deutsch in wort",
            "german b2", "german c1", "fließend deutsch",
        ]
    ):
        evidence_items.append({
            "title": "Almanca Dil Şartı / İletişim",
            "desc": "İlan metninde Almanca dil yetkinliği aranmakta veya tercih edilmektedir.",
            "status": "warning",
        })
    else:
        evidence_items.append({
            "title": "İngilizce Çalışma Ortamı",
            "desc": "Uluslararası standartlarda İngilizce çalışma ve raporlama yetkinliğiniz uygundur.",
            "status": "match",
        })

    # 5. Linux / HPC or Specific Advantage
    if any(k in text_lower for k in ["linux", "bash", "hpc", "cluster", "cloud", "aws", "docker"]):
        evidence_items.append({
            "title": "Linux / HPC & Pipeline Deneyimi",
            "desc": "Sunucu ve Linux ortamlarında pipeline çalıştırma tecrübeniz artı değer sağlamaktadır.",
            "status": "advantage",
        })
    elif any(k in text_lower for k in ["entry level", "student", "intern", "praktik", "berufsanfänger"]):
        evidence_items.append({
            "title": "Kariyer / Giriş Seviyesi Uygunluğu",
            "desc": "Pozisyon yeni mezun / stajyer ve araştırmacı profiline tam olarak açıktır.",
            "status": "match",
        })
    else:
        evidence_items.append({
            "title": "Gelişime Açık Profil",
            "desc": "Sürekli öğrenme ve yeni teknikleri hızla edinme yetkinliği eşleşiyor.",
            "status": "match",
        })

    return evidence_items


@st.cache_data(ttl=60, show_spinner=False)
def get_job_timeline_events(job_id):
    con = None
    try:
        con = get_connection()
        rows = con.execute(
            """
            SELECT event_type, event_time, note
            FROM events
            WHERE job_id = ?
            ORDER BY id ASC
        """,
            (job_id,),
        ).fetchall()
        return rows
    except Exception as exc:
        safe_log(f"Dashboard timeline query failed: {sanitize_text(str(exc))}")
        st.error("Etkinlik geçmişi şu anda yüklenemiyor.")
        return []
    finally:
        if con is not None:
            try:
                con.close()
            except Exception as exc:
                safe_log(f"Dashboard timeline connection close failed: {sanitize_text(str(exc))}")


@st.cache_data(ttl=120, show_spinner=False)
def get_job_assets_index():
    assets = {}
    out_dir = ROOT / "output"
    if out_dir.exists():
        for d in sorted(out_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
            if d.is_dir():
                for sub in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
                    if sub.is_dir():
                        parts = sub.name.split("_")
                        if len(parts) >= 2 and parts[-1].isdigit():
                            try:
                                j_id = int(parts[-1])
                                assets[j_id] = sub
                            except ValueError:
                                pass
    return assets


def format_relative_date(date_str):
    if not date_str:
        return "Tarih Belirtilmemiş"
    try:
        clean_str = str(date_str).split(".")[0].replace("Z", "")
        dt = datetime.fromisoformat(clean_str)
        diff = datetime.now() - dt
        days = diff.days
        if days <= 0:
            return "Bugün"
        elif days == 1:
            return "Dün"
        elif days < 30:
            return f"{days}g önce"
        else:
            return dt.strftime("%d.%m.%Y")
    except Exception:
        return str(date_str)[:10]


@st.fragment(run_every="2s")
def wait_for_dashboard_data(include_rejected=False):
    try:
        get_dashboard_jobs(include_rejected)
    except SnapshotUnavailable:
        st.info("Güncel ilanlar hazırlanıyor; ekran otomatik olarak açılacak…")
        return
    st.rerun()


def load_data(include_rejected: bool = False):
    try:
        return get_dashboard_jobs(include_rejected)
    except SnapshotUnavailable:
        wait_for_dashboard_data(include_rejected)
        st.stop()
    except Exception as exc:
        st.error(f"Veritabanı bağlantı hatası: {exc}")
        return pd.DataFrame(columns=JOB_COLUMNS)


@st.cache_data(ttl=600, show_spinner=False)
def get_job_description(job_id: int) -> str:
    try:
        return fetch_description(job_id)
    except Exception as exc:
        st.error(f"İlan açıklaması okunamadı: {exc}")
        return ""


@st.cache_data(ttl=60, show_spinner=False)
def load_analytics_funnel_counts():
    try:
        con = get_connection()
        row = con.execute("""
            SELECT
                COUNT(*),
                SUM(CASE WHEN semantic_score > 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN status IN ('ready_for_review', 'applied', 'interview', 'offer') THEN 1 ELSE 0 END),
                SUM(CASE WHEN status IN ('applied', 'interview', 'offer') THEN 1 ELSE 0 END),
                SUM(CASE WHEN status IN ('interview', 'offer') THEN 1 ELSE 0 END)
            FROM jobs
        """).fetchone()
        con.close()
        return {
            "total_scraped": (row[0] or 0) if row else 0,
            "passed_semantic": (row[1] or 0) if row else 0,
            "shortlisted": (row[2] or 0) if row else 0,
            "applied": (row[3] or 0) if row else 0,
            "interviews": (row[4] or 0) if row else 0,
        }
    except Exception:
        return {"total_scraped": 0, "passed_semantic": 0, "shortlisted": 0, "applied": 0, "interviews": 0}


def update_job_status(job_id, new_status, note=None, allow_unarchive=False):
    try:
        transition(job_id, new_status, note=note, allow_unarchive=allow_unarchive)
        patch_cached_status(job_id, new_status)
        st.cache_data.clear()
        st.toast(f"İlan #{job_id} durumu güncellendi: {new_status.upper()}")
        return True
    except Exception as e:
        st.error(f"Durum güncellenemedi: {e}")
        return False


# ==============================================================================
# 5. GLOBAL DATA LOAD & TOP NAVIGATION
# ==============================================================================
db_provider_label = get_connection_provider()

def navigate_workspace(mode):
    st.session_state["app_workspace_mode"] = mode
    st.session_state["mobile_detail_open"] = False


def logout_workspace():
    st.session_state["authenticated"] = False
    st.session_state["mobile_detail_open"] = False


if "pending_workspace_mode" in st.session_state:
    st.session_state["app_workspace_mode"] = st.session_state.pop("pending_workspace_mode")
selected_mode = st.session_state.setdefault("app_workspace_mode", "Genel Bakış")
with st.container(key="top_navbar_container"):
    brand, navigation, account = st.columns([1.2, 6.6, 0.35], vertical_alignment="center")
    with brand:
        st.markdown(render_brand_header(), unsafe_allow_html=True)
    with navigation:
        with st.container(key="primary_navigation", horizontal=True, vertical_alignment="center"):
            for mode, label, key in [("Genel Bakış", "Bugün", "today"), ("İlan Değerlendirme", "İlan Masası", "jobs"),
                                     ("Başvurular", "Başvurular", "applications"), ("Belgelerim", "Belgeler", "documents")]:
                st.button(label, key=f"nav_{key}", type="primary" if selected_mode == mode else "secondary",
                          on_click=navigate_workspace, args=(mode,), width="content")
    with account:
        with st.popover("Ayarlar", use_container_width=True):
            st.caption(f"Veri kaynağı: {db_provider_label}")
            st.button("Analiz", key="nav_analytics", on_click=navigate_workspace, args=("Performans & Analiz",))
            if st.button("Senkronize Et", key="btn_refresh_top"):
                refresh_dashboard_cache()
                st.cache_data.clear()
                st.session_state["last_refresh_time"] = datetime.now().strftime("%H:%M:%S")
                st.toast("İlanlar yenilendi.")
                st.rerun()
            st.button("Çıkış Yap", key="btn_logout_top", on_click=logout_workspace)


# ==============================================================================
# MODE 1: GENEL BAKIŞ (SİTUATİON & ÖNE ÇIKAN İLANLAR)
# ==============================================================================
if selected_mode == "Genel Bakış":
    df_all_active = load_data(include_rejected=False)
    render_today(globals())


elif selected_mode == "İlan Değerlendirme":
    render_workspace(globals())

elif selected_mode == "Başvurular":
    df_all_active = load_data(include_rejected=False)
    render_applications(globals())


# ==============================================================================
# MODE 3: PERFORMANS & ANALİZ (HUNİ ANALİTİĞİ & MODEL DOĞRULUĞU)
# ==============================================================================
elif selected_mode == "Performans & Analiz":
    st.markdown(render_desk_heading("SÜRECİN", "Analiz", "Tarama ve başvuru akışının güncel görünümü."), unsafe_allow_html=True)
    st.markdown(
        clean_html(
            """
            <div class='section-headline'>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="20" x2="18" y2="10"></line><line x1="12" y1="20" x2="12" y2="4"></line><line x1="6" y1="20" x2="6" y2="14"></line></svg>
                BAŞVURU DÖNÜŞÜM ANALİTİĞİ & MODEL PERFORMANSI
            </div>
            """
        ),
        unsafe_allow_html=True,
    )

    with st.spinner("Analiz istatistikleri yükleniyor…"):
        f_counts = load_analytics_funnel_counts()
    total_s = max(f_counts["total_scraped"], 1)
    fn_df = pd.DataFrame([
        {"Aşama": "1. Tarama", "Sayı": f_counts["total_scraped"], "Oran": 100.0, "Sıra": 1},
        {"Aşama": "2. Semantik Eşik", "Sayı": f_counts["passed_semantic"], "Oran": round((f_counts["passed_semantic"] / total_s) * 100, 1), "Sıra": 2},
        {"Aşama": "3. Havuzda İnceleme", "Sayı": f_counts["shortlisted"], "Oran": round((f_counts["shortlisted"] / total_s) * 100, 1), "Sıra": 3},
        {"Aşama": "4. Başvuru / Mülakat", "Sayı": f_counts["applied"], "Oran": round((f_counts["applied"] / total_s) * 100, 1), "Sıra": 4},
    ])

    fn_bars = alt.Chart(fn_df).mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, height=22).encode(
        x=alt.X("Sayı:Q", title="Toplam İlan Hacmi"),
        y=alt.Y("Aşama:N", sort=alt.SortField("Sıra", order="ascending"), title=""),
        color=alt.Color(
            "Aşama:N",
            scale=alt.Scale(
                domain=["1. Tarama", "2. Semantik Eşik", "3. Havuzda İnceleme", "4. Başvuru / Mülakat"],
                range=["#889880", "#315e52", "#b39a61", "#3d6244"],
            ),
            legend=None,
        ),
        tooltip=[alt.Tooltip("Aşama:N"), alt.Tooltip("Sayı:Q", format=","), alt.Tooltip("Oran:Q", title="Dönüşüm (%)", format=".1f")],
    )
    fn_text = fn_bars.mark_text(align="left", baseline="middle", dx=8, color="#253c32", fontSize=12, fontWeight="bold").encode(
        text=alt.Text("Sayı:Q", format=",")
    )
    fn_chart = (fn_bars + fn_text).properties(height=160, background="transparent").configure_view(strokeWidth=0).configure_axis(
        labelColor="#657568",
        titleColor="#253c32",
        gridColor="#dce3d7",
        domainColor="#c8d4c2",
    )
    st.altair_chart(fn_chart, use_container_width=True, theme=None)

    st.markdown("<hr style='border-color:var(--border-hairline); margin:16px 0;'>", unsafe_allow_html=True)

    try:
        from src.evaluation.calibration import compute_outcome_calibration

        cal_rep = compute_outcome_calibration()
        st.markdown("### Başvuru sonuçları")

        # Nullable metric counts and conversion rate show "Ölçülmedi" (Olculmedi) when null
        def _format_outcome_count(val):
            return "Ölçülmedi" if val is None else val

        def _format_outcome_rate(val):
            return "Ölçülmedi" if val is None else f"%{val:.1f}"

        rate = cal_rep.get("interview_conversion_rate")
        submitted_val = cal_rep.get("total_submitted_applications")
        interview_val = cal_rep.get("total_interviews_secured")

        submitted_col, interview_col, rate_col = st.columns(3)
        submitted_col.metric("Gönderilen başvuru", _format_outcome_count(submitted_val))
        interview_col.metric("Mülakata ulaşan", _format_outcome_count(interview_val))
        rate_col.metric("Mülakat oranı", _format_outcome_rate(rate))

        # Rejection breakdown: postsubmission vs preapplication vs unknown rejections
        st.markdown("<div style='height: 8px;'></div>", unsafe_allow_html=True)
        st.markdown(
            "#### Ret dağılımı",
            help="Başvuru sonrası retler, başvuru öncesi elemeler ve bilinmeyen / geçmiş retler",
        )
        post_rej = cal_rep.get("total_postsubmission_rejections")
        pre_rej = cal_rep.get("total_preapplication_rejections")
        unk_rej = cal_rep.get("total_unknown_legacy_rejections")

        rej_col1, rej_col2, rej_col3 = st.columns(3)
        rej_col1.metric(
            "Başvuru sonrası ret",
            _format_outcome_count(post_rej),
            help="Başvuru yapıldığı doğrulanan ilanlardaki retler.",
        )
        rej_col2.metric(
            "Başvuru öncesi eleme",
            _format_outcome_count(pre_rej),
            help="Başvuru yapılmadan önce elenen ilanlar.",
        )
        rej_col3.metric(
            "Geçmişi belirsiz ret",
            _format_outcome_count(unk_rej),
            help="Başvuru yapılıp yapılmadığı geçmiş kayıtlardan belirlenemeyen retler.",
        )

        st.caption(cal_rep.get("calibration_insight", ""))
        st.caption("Bu rapor başvuru geçmişini özetler; modelin doğruluğunu ölçmez.")
        if cal_rep.get("model_error_status") == "DATA_UNAVAILABLE":
            st.warning("Başvuru geçmişi okunamadı; sonuçlar başarı ölçümü olarak kullanılamaz.")

    except Exception as e:
        st.caption(f"Başvuru sonuçları yüklenemedi: {e}")


# ==============================================================================
# MODE 4: BELGELERİM (GÜVENLİ CV VE BAŞVURU DOSYALARI)
# ==============================================================================
elif selected_mode == "Belgelerim":
    st.markdown(render_desk_heading("BELGELERİN", "Kariyer belgelerin", "CV, referanslar ve başvuru belgelerin."), unsafe_allow_html=True)
    vault_dir = ROOT / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    existing_files = sorted(
        list(vault_dir.glob("*.pdf")) + list(vault_dir.glob("*.png")) + list(vault_dir.glob("*.jpg"))
    )

    v_c1, v_c2 = st.columns([3, 1.2])
    with v_c1:
        if not existing_files:
            st.markdown('<div class="workspace-empty"><strong>Henüz belge bulunmuyor</strong><p>CV veya referans belgeni yükleyebilirsin.</p></div>', unsafe_allow_html=True)
        for index, file in enumerate(existing_files):
            with st.container(key=f"document_card_{index}"):
                filename = html.escape(file.name)
                size_kb = file.stat().st_size / 1024
                st.markdown(f'<div class="document-heading"><span class="document-symbol">{render_svg_icon("file")}</span><div><h3>{filename}</h3><p>{file.suffix[1:].upper()} · {size_kb:.1f} KB</p></div></div>', unsafe_allow_html=True)
                st.download_button("Belgeyi indir", file.read_bytes(), file_name=file.name, key=f"dl_v_{file.name}")

    with v_c2, st.container(key="document_upload_panel"):
        st.markdown(
            clean_html(
                """
                <div class='studio-frame'>
                    <div class='section-headline'>YENİ BELGE YÜKLE</div>
                    <div style='font-size:0.72rem; color:var(--text-muted); margin-bottom:10px;'>Desteklenen: PDF, PNG, JPG (Maks. 200MB)</div>
                </div>
                """
            ),
            unsafe_allow_html=True,
        )
        up = st.file_uploader(
            "Dosya seç (PDF/Resim)",
            type=["pdf", "png", "jpg"],
            label_visibility="collapsed",
            key="vault_file_uploader",
        )
        if up is not None:
            uploaded_token = f"{up.name}_{up.size}"
            if st.session_state.get("last_uploaded_token") != uploaded_token:
                buf = up.getbuffer()
                header = bytes(buf[:8])

                # MIME Magic byte validation (PDF: %PDF, PNG: \x89PNG, JPG: \xff\xd8\xff)
                is_valid_type = (
                    header.startswith(b"%PDF")
                    or header.startswith(b"\x89PNG\r\n\x1a\n")
                    or header.startswith(b"\xff\xd8\xff")
                )

                if not is_valid_type:
                    st.error("Güvenlik Uyarısı: Yüklenen dosyanın içeriği belirtilen formatla (PDF/PNG/JPG) eşleşmiyor.")
                else:
                    safe_filename = Path(up.name).name
                    save_p = (vault_dir / safe_filename).resolve()
                    if not str(save_p).startswith(str(vault_dir.resolve())):
                        st.error("Güvenlik Uyarısı: Geçersiz dosya adı tespit edildi.")
                        st.stop()
                    with open(save_p, "wb") as of:
                        of.write(buf)
                    st.session_state["last_uploaded_token"] = uploaded_token
                    st.cache_data.clear()
                    st.toast(f"{safe_filename} başarıyla yüklendi!")
                    st.rerun()

        total_docs = len(existing_files)
        total_bytes = sum(f.stat().st_size for f in existing_files) if existing_files else 0
        total_mb = round(total_bytes / (1024 * 1024), 2)
        st.markdown(
            clean_html(
                f"""
                <div class='studio-frame' style='margin-top:12px;'>
                    <div class='section-headline'>DEPOLAMA ÖZETİ</div>
                    <div style='font-size:0.75rem; color:var(--text-body); line-height:1.8;'>
                        <div><strong style='color:var(--text-pure);'>Toplam Belge:</strong> <code>{total_docs}</code> adet</div>
                        <div><strong style='color:var(--text-pure);'>Toplam Boyut:</strong> <code>{total_mb} MB</code></div>
                        <div><strong style='color:var(--text-pure);'>Depolama:</strong> Güvenli Yerel Depolama</div>
                    </div>
                </div>
                """
            ),
            unsafe_allow_html=True,
        )
