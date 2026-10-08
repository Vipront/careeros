from src.db import get_connection
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from bs4 import BeautifulSoup
from src.config import env
from src.db import utc_now as now

ROOT = Path(__file__).resolve().parents[2]


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TIMEOUT = 8
MAX_CANDIDATES = 10

MAX_ENRICH_ATTEMPTS = 3
FIRST_RETRY_DAYS = 2
SECOND_RETRY_DAYS = 7

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/151 Safari/537.36 "
        "CareerOS/2.0"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US,en;q=0.8,de;q=0.7",
}

PREFERRED_HOSTS = [
    "tum.de", "lmu.de", "embl.org", "euraxess.ec.europa.eu",
    "eures.europa.eu", "academictransfer.com", "impactpool.org",
    "boards.greenhouse.io", "jobs.lever.co", "smartrecruiters.com",
    "myworkdayjobs.com", "jobs.ashbyhq.com", "nature.com", "science.org",
    "gsk.wd5.myworkdayjobs.com", "orjincrs.com"
]

TR_MAP = str.maketrans({
    "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
    "Ç": "C", "Ğ": "G", "İ": "I", "Ö": "O", "Ş": "S", "Ü": "U",
})


def normalize(text):
    text = unquote(text or "").translate(TR_MAP).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def normalize_employment_type(val):
    if not val:
        return None
    s = str(val).strip().upper().replace("-", "_").replace(" ", "_")
    mapping = {
        "FULL_TIME": "Full-time",
        "FULLTIME": "Full-time",
        "PART_TIME": "Part-time",
        "PARTTIME": "Part-time",
        "CONTRACT": "Contract",
        "CONTRACTOR": "Contract",
        "TEMPORARY": "Temporary",
        "INTERN": "Internship",
        "INTERNSHIP": "Internship",
        "VOLUNTEER": "Volunteer",
        "OTHER": "Other",
    }
    return mapping.get(s, None)

def normalize_workplace_type(val, desc_text=""):
    if val:
        s = str(val).strip().upper().replace("-", "_").replace(" ", "_")
        if "TELECOMMUTE" in s or "REMOTE" in s:
            return "Remote"
        if "HYBRID" in s:
            return "Hybrid"
        if "ONSITE" in s or "ON_SITE" in s:
            return "On-site"
    if desc_text:
        d = desc_text.lower()
        if re.search(r"\b(?:fully\s+remote|100%\s+remote|work\s+from\s+home|uzaktan\s+çalışma)\b", d):
            return "Remote"
        elif re.search(r"\b(?:hybrid|hibrit)\b", d):
            return "Hybrid"
    return None

def tokens(text, min_len=3):
    return [t for t in normalize(text).split() if len(t) >= min_len]

def linkedin_job_id(url):
    m = re.search(r"/jobs/view/(\d+)", url or "")
    return m.group(1) if m else None

def fetch_diagnostic(url):
    try:
        r = requests.get(
            url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True
        )
        if r.status_code != 200:
            return None, None, f"http_{r.status_code}"
        if "text/html" not in r.headers.get("content-type", "").lower():
            return None, None, "not_html"
        return r.url, r.text, "ok"
    except requests.RequestException as exc:
        return None, None, f"request_error:{type(exc).__name__}"


def fetch(url):
    try:
        r = requests.get(
            url, headers=HEADERS, timeout=TIMEOUT,
            allow_redirects=True
        )
        if r.status_code != 200:
            return None, None
        if "text/html" not in r.headers.get("content-type", "").lower():
            return None, None
        return r.url, r.text
    except requests.RequestException:
        return None, None

def clean_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    lines = [re.sub(r"\s+", " ", x).strip() for x in soup.get_text("\n").splitlines()]
    return soup, "\n".join(x for x in lines if x)

def extract_jsonld(soup):
    import json
    found = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception as exc:
            print(f"[Enrichment] Skipping malformed JSON-LD block: {type(exc).__name__}")
            continue
        items = data if isinstance(data, list) else [data]
        expanded = list(items)
        for item in items:
            if isinstance(item, dict) and "@graph" in item:
                expanded.extend(item["@graph"])
        for item in expanded:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                found.append(item)
    return found

def identity_score(title, company, location, visible, final_url, jsonld_items):
    t = normalize(visible)
    host = urlparse(final_url).netloc.lower()

    title_tokens = set(tokens(title, 4))
    company_tokens = set(tokens(company, 4))
    location_tokens = set(tokens(location, 4))

    score = 0.0

    if title and normalize(title) in t:
        score += 7
    else:
        overlap = len(title_tokens & set(t.split()))
        score += min(5, overlap * 1.5)

    if company and normalize(company) in t:
        score += 6
    else:
        overlap = len(company_tokens & set(t.split()))
        score += min(5, overlap * 1.5)

    loc_overlap = len(location_tokens & set(t.split()))
    score += min(3, loc_overlap)

    if any(h in host for h in PREFERRED_HOSTS):
        score += 2

    if jsonld_items:
        score += 3

    # Do not require the exact LinkedIn job ID.
    return score

def first_matching_line(text, terms):
    for line in (text or "").splitlines():
        low = line.lower()
        if any(term in low for term in terms):
            return line.strip()[:1500]
    return ""

def extract_job_data(final_url, html, expected_title, expected_company, expected_location):
    soup, visible = clean_text(html)
    jsonld = extract_jsonld(soup)

    valid, reason = candidate_url_is_valid(
        final_url, expected_title, expected_company, jsonld, visible
    )
    if not valid:
        return None, reason

    score = identity_score(
        expected_title, expected_company, expected_location,
        visible, final_url, jsonld
    )

    if score < 8:
        return None

    if jsonld:
        job = jsonld[0]
        desc = BeautifulSoup(
            job.get("description", "") or "",
            "html.parser"
        ).get_text(" ", strip=True)

        if len(desc) < 250:
            return None

        org = job.get("hiringOrganization") or {}
        company = org.get("name") if isinstance(org, dict) else ""
        company = company or expected_company

        return {
            "description": desc[:30000],
            "requirements_text": desc[:15000],
            "education_requirements": first_matching_line(
                desc, ["phd", "ph.d", "master", "bachelor", "degree", "msc", "bsc",
                       "lisans", "yüksek lisans", "doktora"]
            ),
            "experience_requirements": first_matching_line(
                desc, ["years of experience", "experience in", "experience with",
                       "yıl deneyim", "deneyim"]
            ),
            "eligibility_text": first_matching_line(
                desc, ["visa", "work authorization", "work permit",
                       "citizenship", "language", "çalışma izni", "vatandaşlık",
                       "almanca", "ingilizce"]
            ),
            "deadline": job.get("validThrough"),
            "date_posted": job.get("datePosted"),
            "employment_type": normalize_employment_type(job.get("employmentType")),
            "workplace_type": normalize_workplace_type(job.get("jobLocationType"), desc),
            "description_source": final_url,
            "source_quality": reason,
            "source_score": score,
        }

    # Generic public-page fallback.
    if len(visible) < 500:
        return None

    return {
        "description": visible[:30000],
        "requirements_text": visible[:15000],
        "education_requirements": first_matching_line(
            visible, ["phd", "ph.d", "master", "bachelor", "degree", "msc", "bsc",
                      "lisans", "yüksek lisans", "doktora"]
        ),
        "experience_requirements": first_matching_line(
            visible, ["years of experience", "experience in", "experience with",
                      "yıl deneyim", "deneyim"]
        ),
        "eligibility_text": first_matching_line(
            visible, ["visa", "work authorization", "work permit",
                      "citizenship", "language", "çalışma izni", "vatandaşlık",
                      "almanca", "ingilizce"]
        ),
        "deadline": first_matching_line(
            visible, ["deadline", "application deadline", "apply by", "son başvuru"]
        ),
        "date_posted": None,
        "description_source": final_url,
        "source_quality": reason,
        "source_score": score,
    }

def locale_for_location(location):
    """Return Google language/country hints derived from the job location."""
    s = normalize(location)

    if any(x in s for x in (
        "turkey", "turkiye", "türkiye", "istanbul", "ankara", "izmir"
    )):
        return "tr", "tr"
    if any(x in s for x in (
        "germany", "almanya", "deutschland", "berlin", "munich", "münchen"
    )):
        return "de", "de"
    if any(x in s for x in (
        "france", "fransa", "paris", "lyon", "toulouse"
    )):
        return "fr", "fr"
    if any(x in s for x in (
        "denmark", "dänemark", "danmark", "copenhagen", "kobenhavn", "kopenhag"
    )):
        return "en", "dk"
    if any(x in s for x in (
        "lithuania", "litauen", "vilnius", "kaunas"
    )):
        return "lt", "lt"
    if any(x in s for x in (
        "netherlands", "hollanda", "nederland", "amsterdam", "utrecht"
    )):
        return "nl", "nl"
    if any(x in s for x in (
        "belgium", "belgique", "belgie", "brussels", "bruxelles"
    )):
        return "en", "be"
    if any(x in s for x in (
        "austria", "österreich", "vienna", "wien"
    )):
        return "de", "at"
    if any(x in s for x in (
        "switzerland", "schweiz", "zurich", "zürich", "geneva"
    )):
        return "de", "ch"

    return "en", "us"


def diagnostic_reason_summary(search_stats, fetch_counts,
                              identity_failures, short_description_failures):
    reasons = []
    if search_stats.get("organic_results", 0) == 0:
        reasons.append("search_no_results")
    if search_stats.get("query_errors", 0) > 0:
        reasons.append("search_query_error")
    if fetch_counts.get("request_error", 0) or fetch_counts.get("not_html", 0):
        reasons.append("fetch_failed")
    if identity_failures:
        reasons.append("identity_failed")
    if short_description_failures:
        reasons.append("description_too_short")
    if fetch_counts.get("linkedin_profile", 0):
        reasons.append("linkedin_profile")
    return ",".join(reasons) if reasons else "no_accepted_candidate"


def queries_for(title, company, location, jid):
    variants = [
        f'"{title}" "{company}" "{location}"',
        f'"{title}" "{company}"',
        f'"{title}" {company} careers',
        f'"{title}" {company} jobs',
        f'"{company}" "{title}"',
    ]

    if jid:
        variants += [
            f'"{jid}" "{company}"',
            f'"{jid}" "{title}"',
        ]

    out, seen = [], set()
    for q in variants:
        if q not in seen:
            out.append(q)
            seen.add(q)
    return out

def tavily_search_candidates(title, company, location, jid):
    key = env("TAVILY_API_KEY")
    if not key:
        return [], {"provider": "tavily", "search_count": 0, "result_count": 0,
                    "query_errors": 1, "hl": None, "gl": None}

    lang, country = locale_for_location(location)
    results, seen = [], set()
    stats = {
        "provider": "tavily",
        "search_count": 0,
        "result_count": 0,
        "query_errors": 0,
        "hl": lang,
        "gl": country,
    }

    for q in queries_for(title, company, location, jid):
        stats["search_count"] += 1
        try:
            r = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": key,
                    "query": q,
                    "search_depth": "basic",
                    "max_results": 10,
                    "include_answer": False,
                    "include_raw_content": False,
                    "country": country,
                },
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            stats["query_errors"] += 1
            continue

        for item in data.get("results", []) or []:
            stats["result_count"] += 1
            url = item.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)

            blob = " ".join([
                str(item.get("title", "")),
                str(item.get("content", "")),
            ])
            score = (
                int(normalize(title) in normalize(blob)) * 7
                + int(normalize(company) in normalize(blob)) * 6
            )
            results.append({
                "url": url,
                "search_title": item.get("title", ""),
                "search_content": item.get("content", ""),
                "score": score,
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:MAX_CANDIDATES], stats


def serper_search_candidates(title, company, location, jid):
    key = env("SERPER_API_KEY")
    if not key:
        return [], {"provider": "serper", "search_count": 0, "result_count": 0,
                    "query_errors": 1, "hl": None, "gl": None}

    lang, country = locale_for_location(location)
    results, seen = [], set()
    stats = {
        "provider": "serper",
        "search_count": 0,
        "result_count": 0,
        "query_errors": 0,
        "hl": lang,
        "gl": country,
    }

    for q in queries_for(title, company, location, jid):
        stats["search_count"] += 1
        try:
            r = requests.post(
                "https://google.serper.dev/search",
                headers={
                    "X-API-KEY": key,
                    "Content-Type": "application/json",
                },
                json={"q": q, "gl": country, "hl": lang, "num": 10},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            stats["query_errors"] += 1
            continue

        for item in data.get("organic", []) or []:
            stats["result_count"] += 1
            url = item.get("link", "")
            if not url or url in seen:
                continue
            seen.add(url)

            blob = " ".join([
                str(item.get("title", "")),
                str(item.get("snippet", "")),
            ])
            score = (
                int(normalize(title) in normalize(blob)) * 7
                + int(normalize(company) in normalize(blob)) * 6
            )
            results.append({
                "url": url,
                "search_title": item.get("title", ""),
                "search_content": item.get("snippet", ""),
                "score": score,
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:MAX_CANDIDATES], stats


def search_candidates(title, company, location, jid):
    candidates, _ = tavily_search_candidates(title, company, location, jid)
    return [x["url"] for x in candidates]

def normalize_company_for_url(company):
    s = normalize(company)
    # Keep a compact alphanumeric token set for comparing URL slugs.
    return [t for t in s.split() if len(t) >= 3]


def url_is_linkedin_profile(url):
    host = urlparse(url or "").netloc.lower()
    path = urlparse(url or "").path.lower()
    return "linkedin.com" in host and path.startswith("/in/")


def linkedin_job_company_matches(url, expected_company):
    """Reject LinkedIn job pages whose URL slug identifies another company."""
    host = urlparse(url or "").netloc.lower()
    path = urlparse(url or "").path.lower()

    if "linkedin.com" not in host or "/jobs/view/" not in path:
        return True

    # LinkedIn job URLs commonly contain '-at-company-' in the slug.
    m = re.search(r"-at-([^/]+?)-(\d+)(?:/)?$", path)
    if not m:
        # We cannot reliably infer the company from this URL shape.
        return True

    slug_company = re.sub(r"[-_]+", " ", m.group(1))
    expected_tokens = set(normalize_company_for_url(expected_company))
    slug_tokens = set(normalize(slug_company).split())

    if not expected_tokens:
        return True

    # Require meaningful overlap; single-token companies still work.
    overlap = len(expected_tokens & slug_tokens)
    required = 1 if len(expected_tokens) == 1 else min(2, len(expected_tokens))
    return overlap >= required


def structured_company_matches(jsonld_items, expected_company):
    """Return True when structured hiringOrganization is present and matches."""
    expected = normalize(expected_company)
    for item in jsonld_items or []:
        org = item.get("hiringOrganization") if isinstance(item, dict) else None
        if isinstance(org, dict):
            name = normalize(org.get("name", ""))
            if name:
                return expected in name or name in expected
    return None


def fetch_direct_linkedin_job(url: str):
    """Directly fetch full job description from public LinkedIn job URL."""
    if not url or "linkedin.com/jobs/view" not in url:
        return None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8,tr;q=0.7",
    }
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200:
            if r.status_code == 429:
                print(f"[Dynamic Enricher] Rate limited (429) fetching LinkedIn job: {url}")
            else:
                print(f"[Dynamic Enricher] HTTP {r.status_code} fetching LinkedIn job: {url}")
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        desc_el = (
            soup.find("div", class_="show-more-less-html__markup")
            or soup.find("section", class_="show-more-less-html")
            or soup.find("div", class_="description__text")
        )
        if not desc_el:
            return None
        text = desc_el.get_text("\n", strip=True)
        if len(text) < 120:
            return None

        posted_el = soup.find("span", class_="posted-time-ago__text")
        date_posted = posted_el.get_text(strip=True) if posted_el else None

        return {
            "description": text,
            "requirements_text": text,
            "education_requirements": None,
            "experience_requirements": None,
            "eligibility_text": None,
            "deadline": None,
            "date_posted": date_posted,
            "description_source": url,
            "source_score": 100.0,
            "source_quality": "direct_linkedin_page",
        }
    except Exception as e:
        print(f"[Dynamic Enricher] Exception fetching {url}: {e}")
        return None


def source_path_quality(final_url):
    """Classify the URL path without assuming a specific company's URL scheme."""
    path = urlparse(final_url or "").path.lower().rstrip("/")
    parts = [p for p in path.split("/") if p]

    if not parts:
        return "root_page"

    category_segments = {
        "department", "departments", "category", "categories",
        "teams", "team", "search", "results"
    }
    landing_segments = {
        "jobs", "careers", "career", "opportunities",
        "vacancies", "open-positions"
    }

    # Category/department/search pages are listing pages even when the exact
    # title happens to appear somewhere in their rendered text.
    if any(seg in category_segments for seg in parts):
        return "generic_listing_page"

    # A bare careers/jobs landing page is also not an exact job posting.
    if len(parts) == 1 and parts[0] in landing_segments:
        return "generic_listing_page"

    return "public_detail_candidate"


def candidate_url_is_valid(final_url, expected_title, expected_company,
                           jsonld_items, visible):
    """Validate source identity and distinguish exact job pages from listings."""
    if url_is_linkedin_profile(final_url):
        return False, "linkedin_profile"

    if not linkedin_job_company_matches(final_url, expected_company):
        return False, "linkedin_company_mismatch"

    structured_match = structured_company_matches(jsonld_items, expected_company)
    if structured_match is False:
        return False, "structured_company_mismatch"

    quality = source_path_quality(final_url)
    if quality == "generic_listing_page":
        return False, "generic_listing_page"

    # A JobPosting structured record is strong evidence of a specific posting.
    if jsonld_items:
        return True, "job_posting"

    title_norm = normalize(expected_title)
    visible_norm = normalize(visible)
    exact_title_present = bool(title_norm and title_norm in visible_norm)

    if quality == "job_detail_candidate" and exact_title_present:
        return True, "exact_job_page"

    if quality == "public_detail_candidate" and exact_title_present:
        return True, "exact_job_page"

    return False, "no_exact_job_evidence"

def extract_job_data_diagnostic(final_url, html, expected_title,
                                expected_company, expected_location):
    soup, visible = clean_text(html)
    jsonld = extract_jsonld(soup)

    # Precision gate MUST run before identity scoring. Otherwise a LinkedIn
    # person profile or another company's job page can still score highly
    # based on generic title/company words in the rendered page.
    valid, precision_reason = candidate_url_is_valid(
        final_url, expected_title, expected_company, jsonld, visible
    )
    if not valid:
        return None, precision_reason

    score = identity_score(
        expected_title, expected_company, expected_location,
        visible, final_url, jsonld
    )

    if score < 8:
        return None, "identity_failed"

    if jsonld:
        job = jsonld[0]
        desc = BeautifulSoup(
            job.get("description", "") or "", "html.parser"
        ).get_text(" ", strip=True)

        if len(desc) < 250:
            return None, "description_too_short"

        return {
            "description": desc[:30000],
            "requirements_text": desc[:15000],
            "education_requirements": first_matching_line(
                desc, ["phd", "ph.d", "master", "bachelor", "degree", "msc", "bsc",
                       "lisans", "yüksek lisans", "doktora"]
            ),
            "experience_requirements": first_matching_line(
                desc, ["years of experience", "experience in", "experience with",
                       "yıl deneyim", "deneyim"]
            ),
            "eligibility_text": first_matching_line(
                desc, ["visa", "work authorization", "work permit",
                       "citizenship", "language", "çalışma izni", "vatandaşlık",
                       "almanca", "ingilizce"]
            ),
            "deadline": job.get("validThrough"),
            "date_posted": job.get("datePosted"),
            "employment_type": normalize_employment_type(job.get("employmentType")),
            "workplace_type": normalize_workplace_type(job.get("jobLocationType"), desc),
            "description_source": final_url,
            "source_score": score,
        }, "ok"

    if len(visible) < 500:
        return None, "description_too_short"

    return {
        "description": visible[:30000],
        "requirements_text": visible[:15000],
        "education_requirements": first_matching_line(
            visible, ["phd", "ph.d", "master", "bachelor", "degree", "msc", "bsc",
                      "lisans", "yüksek lisans", "doktora"]
        ),
        "experience_requirements": first_matching_line(
            visible, ["years of experience", "experience in", "experience with",
                      "yıl deneyim", "deneyim"]
        ),
        "eligibility_text": first_matching_line(
            visible, ["visa", "work authorization", "work permit",
                      "citizenship", "language", "çalışma izni", "vatandaşlık",
                      "almanca", "ingilizce"]
        ),
        "deadline": first_matching_line(
            visible, ["deadline", "application deadline", "apply by", "son başvuru"]
        ),
        "date_posted": None,
        "description_source": final_url,
        "source_score": score,
    }, "ok"


def tavily_extract(url, title, company, location):
    key = env("TAVILY_API_KEY")
    if not key:
        return None, "tavily_key_missing"

    try:
        r = requests.post(
            "https://api.tavily.com/extract",
            json={
                "api_key": key,
                "urls": [url],
                "extract_depth": "advanced",
                "format": "markdown",
            },
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        return None, f"tavily_extract_error:{type(exc).__name__}"

    results = data.get("results", []) or []
    if not results:
        return None, "tavily_extract_empty"

    content = results[0].get("raw_content") or results[0].get("content") or ""
    if not isinstance(content, str):
        content = str(content)

    visible = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content)
    visible = re.sub(r"[*#_>`]", " ", visible)
    visible = re.sub(r"\s+", " ", visible).strip()

    if len(visible) < 500:
        return None, "description_too_short"

    valid, reason = candidate_url_is_valid(
        url, title, company, [], visible
    )
    if not valid:
        return None, reason

    score = identity_score(title, company, location, visible, url, [])
    if score < 8:
        return None, "identity_failed"

    return {
        "description": visible[:30000],
        "requirements_text": visible[:15000],
        "education_requirements": first_matching_line(
            visible, ["phd", "ph.d", "master", "bachelor", "degree", "msc", "bsc",
                      "lisans", "yüksek lisans", "doktora"]
        ),
        "experience_requirements": first_matching_line(
            visible, ["years of experience", "experience in", "experience with",
                      "yıl deneyim", "deneyim"]
        ),
        "eligibility_text": first_matching_line(
            visible, ["visa", "work authorization", "work permit",
                      "citizenship", "language", "çalışma izni", "vatandaşlık",
                      "almanca", "ingilizce"]
        ),
        "deadline": first_matching_line(
            visible, ["deadline", "application deadline", "apply by", "son başvuru"]
        ),
        "date_posted": None,
        "description_source": url,
        "source_quality": reason,
        "source_score": score,
    }, "ok"


def extract_candidate_provider(candidate, title, company, location, provider):
    if provider == "tavily":
        return tavily_extract(
            candidate["url"], title, company, location
        )

    final_url, html, fetch_reason = fetch_diagnostic(candidate["url"])
    if not final_url or not html:
        return None, fetch_reason

    parsed, reason = extract_job_data_diagnostic(
        final_url, html, title, company, location
    )
    if not parsed:
        return None, reason or "parse_failed"

    return parsed, "ok"


def enrichment_cache_key(title, company, location):
    return normalize(" | ".join([title or "", company or "", location or ""]))

def iso_after_days(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

def should_attempt(status, next_attempt_at, terminal):
    if terminal:
        return False
    if status in (None, "", "not_attempted"):
        return True
    if status != "not_found":
        return False
    if not next_attempt_at:
        return True
    return next_attempt_at <= now()

def apply_cache_hit(con, job_id, cache_row, cache_key):
    (
        status, description, requirements_text, education_requirements,
        experience_requirements, eligibility_text, deadline, date_posted,
        description_source, source_score, attempts, fetched_at, next_retry_at,
        terminal, notes
    ) = cache_row

    stamp = now()
    if status == "found":
        con.execute("""
            UPDATE jobs SET
                description=?,
                requirements_text=?,
                education_requirements=?,
                experience_requirements=?,
                eligibility_text=?,
                deadline=?,
                date_posted=?,
                description_source=?,
                description_available=1,
                enrichment_status='enriched',
                enriched_at=?,
                enrichment_attempts=?,
                enrichment_next_attempt_at=NULL,
                enrichment_terminal=0,
                enrichment_cache_key=?,
                enrichment_notes=?
            WHERE id=?
        """, (
            description or "",
            requirements_text or "",
            education_requirements or "",
            experience_requirements or "",
            eligibility_text or "",
            deadline,
            date_posted,
            description_source or "",
            stamp,
            attempts,
            cache_key or "",
            f"Cache hit from enrichment_cache; source_score={source_score}",
            job_id,
        ))
        return True

    con.execute("""
        UPDATE jobs SET
            enrichment_status='not_found',
            enriched_at=?,
            enrichment_attempts=?,
            enrichment_next_attempt_at=?,
            enrichment_terminal=?,
            enrichment_cache_key=?,
            enrichment_notes=?
        WHERE id=?
    """, (
        stamp, attempts, next_retry_at, terminal, cache_key,
        "Cached enrichment miss; no SerpAPI call this run.",
        job_id,
    ))
    return False


def run(limit=40):
    con = get_connection()
    con.execute("PRAGMA foreign_keys=ON")

    rows = con.execute("""
        SELECT id,title,company,location,url,
               enrichment_status,enrichment_next_attempt_at,enrichment_terminal
        FROM jobs
        WHERE status IN ('new','evaluated')
          AND (description_available=0 OR description_available IS NULL)
          AND (enrichment_status IS NULL OR enrichment_status IN ('not_attempted','not_found'))
          AND COALESCE(enrichment_terminal,0)=0
          AND (
                enrichment_status IS NULL
                OR enrichment_status='not_attempted'
                OR enrichment_next_attempt_at IS NULL
                OR enrichment_next_attempt_at <= ?
          )
        ORDER BY id DESC
        LIMIT ?
    """, (now(), limit)).fetchall()

    processed = found = 0

    for (
        job_id, title, company, location, url,
        _status, _next_attempt_at, _terminal
    ) in rows:
        processed += 1
        cache_key = enrichment_cache_key(title, company, location)

        cache = con.execute("""
            SELECT status,description,requirements_text,education_requirements,
                   experience_requirements,eligibility_text,deadline,date_posted,
                   description_source,source_score,attempts,fetched_at,
                   next_retry_at,terminal,notes
            FROM enrichment_cache
            WHERE cache_key=?
        """, (cache_key,)).fetchone()

        if cache:
            hit = apply_cache_hit(con, job_id, cache, cache_key)
            if hit:
                found += 1
                print(f"CACHE ENRICHED {job_id}: {title}")
            else:
                print(f"CACHE NOT FOUND {job_id}: {title} — retry scheduled")
            con.commit()
            continue

        jid = linkedin_job_id(url)
        best = None
        best_key = (-1.0, -1)
        search_stats = {}
        fetch_counts = {}
        identity_failures = 0
        short_description_failures = 0
        precision_failures = {}

        # 1. First priority: Direct instant fetch from LinkedIn job page
        if url and "linkedin.com/jobs/view" in url:
            direct = fetch_direct_linkedin_job(url)
            if direct:
                best = direct
                provider = "linkedin_direct"

        def consider_candidates(
            candidate_list,
            provider_name,
            *,
            title_snapshot=title,
            company_snapshot=company,
            location_snapshot=location,
            fetch_counts=fetch_counts,
            precision_failures=precision_failures,
        ):
            nonlocal best, best_key, identity_failures, short_description_failures
            for candidate in candidate_list:
                parsed, parse_reason = extract_candidate_provider(
                    candidate, title_snapshot, company_snapshot, location_snapshot, provider_name
                )

                fetch_counts[parse_reason] = fetch_counts.get(parse_reason, 0) + 1

                if parse_reason == "identity_failed":
                    identity_failures += 1
                elif parse_reason == "description_too_short":
                    short_description_failures += 1
                elif parse_reason in {
                    "linkedin_profile",
                    "linkedin_company_mismatch",
                    "structured_company_mismatch",
                    "generic_careers_page",
                    "generic_listing_page",
                    "no_exact_job_evidence",
                }:
                    precision_failures[parse_reason] = (
                        precision_failures.get(parse_reason, 0) + 1
                    )

                if parsed:
                    host = urlparse(parsed["description_source"]).netloc.lower()
                    preferred = int(any(h in host for h in PREFERRED_HOSTS))
                    key = (parsed.get("source_score", 0), preferred)

                    if key > best_key:
                        best = parsed
                        best_key = key

        if best is None:
            provider = "tavily"
            candidates, search_stats = tavily_search_candidates(
                title, company, location, jid
            )
            consider_candidates(candidates, "tavily")

        # Serper is used only when Tavily fails to produce a validated job.
        if best is None:
            fallback_candidates, fallback_stats = serper_search_candidates(
                title, company, location, jid
            )
            if fallback_stats.get("search_count", 0):
                provider = "serper"
                search_stats = {
                    **fallback_stats,
                    "fallback_from": "tavily",
                    "tavily_search_count": search_stats.get("search_count", 0),
                    "tavily_result_count": search_stats.get("result_count", 0),
                }
            consider_candidates(fallback_candidates, "serper")

        stamp = now()
        previous_attempts = int(
            con.execute(
                "SELECT COALESCE(enrichment_attempts,0) FROM jobs WHERE id=?",
                (job_id,)
            ).fetchone()[0]
        )
        attempts = previous_attempts + 1

        if best:
            con.execute("""
                INSERT OR REPLACE INTO enrichment_cache(
                    cache_key,status,description,requirements_text,
                    education_requirements,experience_requirements,
                    eligibility_text,deadline,date_posted,description_source,
                    source_score,attempts,fetched_at,next_retry_at,terminal,notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                cache_key, "found", best["description"],
                best["requirements_text"], best["education_requirements"],
                best["experience_requirements"], best["eligibility_text"],
                best["deadline"], best["date_posted"],
                best["description_source"], best.get("source_score", 0),
                attempts, stamp, None, 0,
                "Dynamic public-page enrichment cache",
            ))
            con.execute("""
                UPDATE jobs
                SET
                    description=?,
                    requirements_text=?,
                    education_requirements=?,
                    experience_requirements=?,
                    eligibility_text=?,
                    deadline=?,
                    date_posted=?,
                    employment_type=?,
                    workplace_type=?,
                    description_source=?,
                    description_available=1,
                    enrichment_status='enriched',
                    enriched_at=?,
                    enrichment_attempts=?,
                    enrichment_next_attempt_at=NULL,
                    enrichment_terminal=0,
                    enrichment_cache_key=?,
                    enrichment_notes=?
                WHERE id=?
            """, (
                best.get("description") or "",
                best.get("requirements_text") or "",
                best.get("education_requirements") or "",
                best.get("experience_requirements") or "",
                best.get("eligibility_text") or "",
                best.get("deadline"),
                best.get("date_posted"),
                best.get("employment_type"),
                best.get("workplace_type"),
                best.get("description_source") or "",
                stamp,
                attempts,
                cache_key or "",
                f"Dynamic public-page enrichment V30; provider={provider}; source_quality={best.get('source_quality','unknown')}; identity_score={best.get('source_score',0):.1f}",
                job_id,
            ))
            found += 1
            print(f"ENRICHED {job_id}: {title} -> {best['description_source']} | provider={provider}")
        else:
            terminal_now = attempts >= MAX_ENRICH_ATTEMPTS
            if terminal_now:
                next_retry = None
                reason_summary = diagnostic_reason_summary(
                    search_stats, fetch_counts,
                    identity_failures, short_description_failures
                )
                note = (
                    f"No reliable public job page found after {attempts} attempts; "
                    f"diagnostic={reason_summary}; hl={search_stats.get('hl')}; "
                    f"gl={search_stats.get('gl')}; provider={provider}; terminal for this cache key."
                )
            elif attempts == 1:
                reason_summary = diagnostic_reason_summary(
                    search_stats, fetch_counts,
                    identity_failures, short_description_failures
                )
                next_retry = iso_after_days(FIRST_RETRY_DAYS)
                note = (
                    f"No reliable public job page found; retry after {FIRST_RETRY_DAYS} days; "
                    f"diagnostic={reason_summary}; hl={search_stats.get('hl')}; "
                    f"gl={search_stats.get('gl')}; provider={provider}."
                )
            else:
                reason_summary = diagnostic_reason_summary(
                    search_stats, fetch_counts,
                    identity_failures, short_description_failures
                )
                next_retry = iso_after_days(SECOND_RETRY_DAYS)
                note = (
                    f"No reliable public job page found; retry after {SECOND_RETRY_DAYS} days; "
                    f"diagnostic={reason_summary}; hl={search_stats.get('hl')}; "
                    f"gl={search_stats.get('gl')}; provider={provider}."
                )

            con.execute("""
                INSERT OR REPLACE INTO enrichment_cache(
                    cache_key,status,description,requirements_text,
                    education_requirements,experience_requirements,
                    eligibility_text,deadline,date_posted,description_source,
                    source_score,attempts,fetched_at,next_retry_at,terminal,notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                cache_key, "not_found", "", "", "", "", "", None, None,
                None, None, attempts, stamp, next_retry,
                int(terminal_now), note,
            ))

            con.execute("""
                UPDATE jobs
                SET enrichment_status='not_found',
                    enriched_at=?,
                    enrichment_attempts=?,
                    enrichment_next_attempt_at=?,
                    enrichment_terminal=?,
                    enrichment_cache_key=?,
                    enrichment_notes=?
                WHERE id=?
            """, (
                stamp, attempts, next_retry, int(terminal_now), cache_key,
                note, job_id,
            ))
            reason_summary = diagnostic_reason_summary(
                search_stats, fetch_counts,
                identity_failures, short_description_failures
            )
            precision_summary = ",".join(
                f"{k}={v}" for k, v in sorted(precision_failures.items())
            )
            print(
                f"NOT FOUND {job_id}: {title} — {company} | "
                f"reason={reason_summary} | "
                f"precision={precision_summary or 'none'} | "
                f"hl={search_stats.get('hl')} gl={search_stats.get('gl')}"
            )

        con.commit()
        time.sleep(0.2)

    con.close()

    print(
        f"DYNAMIC ENRICHMENT V30: processed={processed}, "
        f"found={found}, not_found={processed-found}"
    )

if __name__ == "__main__":
    run()
