"""CareerOS Sage Theme and Presentation Helpers.

Provides stylesheet loading, approved CareerOS SVG marks, prototype icons,
navigation mapping, and structured HTML components for the sage design palette.
"""

from pathlib import Path
import html
from urllib.parse import quote

# Approved custom inline CareerOS SVG mark matching prototype
CAREEROS_SVG_MARK = (
    '<svg class="os-mark" viewBox="0 0 40 40" fill="none" aria-hidden="true">'
    '<path d="M29 10a14 14 0 1 0 0 20" stroke="currentColor" stroke-width="3.2" stroke-linecap="round"/>'
    '<path d="M17 26V14l12 6-12 6Z" fill="currentColor"/>'
    '<circle cx="31" cy="9" r="3" fill="#b8d4b2"/>'
    '</svg>'
)

# Prototype SVG icon paths
SVG_ICON_PATHS = {
    "settings": '<path d="m9.5 3-.5 2-2 .9-1.8-.6-2 3.4 1.4 1.4v2.4l-1.4 1.4 2 3.4 1.8-.6 2 .9.5 2h4l.5-2 2-.9 1.8.6 2-3.4-1.4-1.4v-2.4l1.4-1.4-2-3.4-1.8.6-2-.9-.5-2Z"/><circle cx="11.5" cy="11.5" r="3"/>',
    "filter": '<path d="M4 6h16M7 12h10M10 18h4"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    "pin": '<path d="M18 10c0 5-6 10-6 10S6 15 6 10a6 6 0 1 1 12 0Z"/><circle cx="12" cy="10" r="2"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "cross": '<path d="m7 7 10 10M7 17 17 7"/>',
    "pending": '<path d="M7 3h10M7 21h10M8 3v5l8 8v5M16 3v5l-8 8v5"/>',
    "arrow": '<path d="M4 12h15m-5-5 5 5-5 5"/>',
    "external": '<path d="M14 4h6v6m0-6-9 9M10 5H5a1 1 0 0 0-1 1v13a1 1 0 0 0 1 1h13a1 1 0 0 0 1-1v-5"/>',
    "bookmark": '<path d="M6 4h12v17l-6-4-6 4Z"/>',
    "file": '<path d="M14 3H6v18h12V7Zm0 0v5h4M9 12h6m-6 4h6"/>',
    "briefcase": '<rect x="3" y="7" width="18" height="14" rx="3"/><path d="M8 7V4h8v3M3 12c6 4 12 4 18 0M10 14h4"/>',
    "grid": '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 10h18m-13 4h2m4 0h2m-8 3h2"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>',
}


def render_svg_icon(name: str, css_class: str = "os-icon") -> str:
    """Render a clean SVG icon by prototype name without any emoji decoration."""
    path_d = SVG_ICON_PATHS.get(name, SVG_ICON_PATHS["info"])
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="{css_class}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="1.65" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{path_d}</svg>'
    )


# Native Streamlit navigation options & label mapping (5 functional modes)
WORKSPACE_MODES = [
    "Genel Bakış",
    "İlan Değerlendirme",
    "Başvurular",
    "Belgelerim",
    "Performans & Analiz",
]

MODE_LABELS = {
    "Genel Bakış": "Bugün",
    "İlan Değerlendirme": "İlan Masası",
    "Başvurular": "Başvurular",
    "Belgelerim": "Belgeler",
    "Performans & Analiz": "Analiz",
}

THEME_CSS_PATH = Path(__file__).resolve().parent / "dashboard_sage.css"


def get_theme_css() -> str:
    """Read and return the sage stylesheet."""
    if THEME_CSS_PATH.exists():
        css = THEME_CSS_PATH.read_text(encoding="utf-8")
        selectors = {
            ".st-key-nav_today button": "sun",
            ".st-key-nav_jobs button": "grid",
            ".st-key-nav_applications button": "briefcase",
            ".st-key-nav_documents button": "file",
            '.st-key-top_navbar_container [data-testid="stPopoverButton"]': "settings",
            ".stLinkButton a": "external",
            "[class*='st-key-btn_act_apply_'] button": "check",
            ".stDownloadButton button": "file",
            '.st-key-advanced_filters [data-testid="stPopoverButton"]': "filter",
        }
        for selector, name in selectors.items():
            svg = render_svg_icon(name).replace('stroke="currentColor"', 'stroke="black"')
            uri = "data:image/svg+xml," + quote(svg, safe="")
            css += f'\n{selector}::before {{ content:""; display:inline-block; flex-shrink:0; width:16px; height:16px; background:currentColor; mask:url("{uri}") center/contain no-repeat; }}'
        return css
    return ""


def inject_theme(st_module) -> None:
    """Inject the loaded stylesheet into the Streamlit app."""
    css = get_theme_css()
    if css:
        st_module.markdown(f"<style>\n{css}\n</style>", unsafe_allow_html=True)


def format_mode_label(mode: str) -> str:
    """Format an internal workspace mode string into its approved display label."""
    return MODE_LABELS.get(mode, mode)


def render_brand_header() -> str:
    """Render the top bar brand lockup matching prototype."""
    return (
        '<div class="layout-b-brand">'
        f'  <span class="layout-b-brand-logo">{CAREEROS_SVG_MARK}</span>'
        '  <div class="layout-b-brand-text">'
        '    <strong>CareerOS</strong>'
        '    <span>Kariyer çalışma masası</span>'
        '  </div>'
        '</div>'
    )


def render_desk_heading(eyebrow: str = "FIRSATLARIN", title: str = "İlan çalışma masası", subtitle: str = "Hazır fırsatlar, bekleyen kontroller ve kararların tek yerde.") -> str:
    """Render prototype desk heading with eyebrow, 28px title and status keys."""
    return (
        f'<div class="desk-heading">'
        f'  <div>'
        f'    <span class="eyebrow">{html.escape(eyebrow)}</span>'
        f'    <h1>{html.escape(title)}<span class="heading-period">.</span></h1>'
        f'    <p>{html.escape(subtitle)}</p>'
        f'  </div>'
        f'</div>'
    )


def render_login_header() -> str:
    """Render the approved centered login header with mark, CareerOS and welcoming subtitle."""
    return (
        f'<div class="login-header-group">'
        f'  <div class="login-mark-wrap">{CAREEROS_SVG_MARK}</div>'
        f'  <div class="login-title">CareerOS</div>'
        f'  <div class="login-subtitle">Kariyer alanına hoş geldin.</div>'
        f'</div>'
    )
