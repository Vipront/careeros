from src.db import get_connection
import asyncio
import hashlib
import re
import sys
import urllib.parse
from pathlib import Path
from playwright.async_api import async_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from src.db import utc_now as now_iso

ROOT = Path(__file__).resolve().parents[2]


# 6 Consolidated Master Search Queries (Covering 100% of keywords with zero redundancy)
SEARCH_TARGETS = [
    # 1. Germany - Wet Lab, Working Student, Internship & Entry Research
    {
        "keywords": '("Molecular Biology" OR Genetics OR Biotechnology) AND ("Working Student" OR Intern OR Praktikum OR Assistant OR Technician)',
        "location": "Germany"
    },
    # 2. Germany - Bioinformatics, Computational Biology, Genomics & Drug Design (Excluding executive/senior noise)
    {
        "keywords": '(Bioinformatics OR "Computational Biology" OR "Drug Discovery" OR "Drug Design" OR Genomics) NOT (Senior OR Director OR Lead)',
        "location": "Germany"
    },
    # 3. European Union - Regional & Remote Research, Trainee & Lab Roles
    {
        "keywords": '("Molecular Biology" OR Bioinformatics OR "Drug Discovery" OR Genomics) AND (Intern OR Trainee OR Assistant OR Technician)',
        "location": "European Union"
    },
    # 4. European Union - Computational Biology, Bioinformatics, Drug Design & Molecular Docking
    {
        "keywords": '("Computational Biology" OR Bioinformatics OR "Drug Design" OR "Molecular Docking" OR Transcriptomics)',
        "location": "European Union"
    },
    # 5. Istanbul & Turkey - Moleküler Biyoloji, Genetik, Biyoteknoloji & Laboratuvar (Domain odaklı, sanayi Ar-Ge hariç)
    {
        "keywords": '("Moleküler Biyoloji" OR "Molecular Biology" OR Genetik OR Genetics OR Biyoteknoloji) AND (Laboratuvar OR Laboratory OR Biyoloji)',
        "location": "Istanbul, Turkey"
    },
    # 6. Istanbul & Turkey - Biyoinformatik, Hesaplamalı Biyoloji & Biyolojik Veri Analizi (Domain kısıtlı Veri Analizi)
    {
        "keywords": '(Biyoinformatik OR Bioinformatics OR "Computational Biology" OR ("Veri Analizi" AND (Biyoloji OR Genetik OR Sağlık OR Tıp OR Klinik)))',
        "location": "Istanbul, Turkey"
    },
]

def make_fingerprint(title, company, location=""):
    raw = f"{title.lower().strip()}|{company.lower().strip()}|{location.lower().strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def upsert_job(con, job, source_query=""):
    title = (job.get("title") or "").strip()
    company = (job.get("company") or "").strip()
    location = (job.get("location") or "").strip()
    url = (job.get("url") or "").strip()
    desc = (job.get("description") or "").strip()

    if not title or not company:
        return False

    fp = make_fingerprint(title, company, location)
    stamp = now_iso()

    is_easy_apply = int(job.get("is_easy_apply") or 0)

    existing = con.execute("SELECT id, status FROM jobs WHERE fingerprint = ?", (fp,)).fetchone()
    if existing:
        job_id, status = existing
        con.execute(
            """
            UPDATE jobs
            SET url = COALESCE(NULLIF(url, ''), ?),
                location = COALESCE(NULLIF(location, ''), ?),
                source_query = COALESCE(NULLIF(source_query, ''), ?),
                is_easy_apply = CASE WHEN is_easy_apply = 1 THEN 1 ELSE ? END,
                updated_at = ?
            WHERE id = ?
            """,
            (url, location, source_query, is_easy_apply, stamp, job_id)
        )
        return False
    else:
        con.execute(
            """
            INSERT INTO jobs(
                fingerprint, title, company, location, url, source,
                date_found, created_at, updated_at, description,
                status, is_active, source_query, source_query_status,
                description_available, is_easy_apply
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fp, title, company, location, url, "linkedin_direct_search",
                stamp, stamp, stamp, desc,
                "new", 1, source_query, "attributed",
                1 if desc else 0, is_easy_apply
            )
        )
        return True

async def scrape_target_all_pages(page, target, max_pages=8):
    """
    Crawls ALL pages (Page 1, 2, 3... start=0, 25, 50...) for a specific target query
    using LinkedIn's native Past Week (7 Days) filter: f_TPR=r604800.
    Returns (all_jobs, anomaly_flag).
    """
    keywords = target["keywords"]
    location = target["location"]
    encoded_kw = urllib.parse.quote_plus(keywords)
    encoded_loc = urllib.parse.quote_plus(location)

    all_jobs = []
    seen_ids: set[str] = set()
    anomaly = None

    print(f"[LinkedIn Crawler] Searching: '{keywords}' in '{location}' (Browsing all pages - Past 7 Days)...")

    for page_idx in range(max_pages):
        start_offset = len(seen_ids)
        if start_offset == 0:
            page_url = f"https://www.linkedin.com/jobs/search?keywords={encoded_kw}&location={encoded_loc}&f_TPR=r604800&position=1&pageNum={page_idx}"
        else:
            page_url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={encoded_kw}&location={encoded_loc}&f_TPR=r604800&start={start_offset}"

        try:
            resp = await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)

            # HTTP Status anomaly detection
            if resp and resp.status in (403, 429, 999):
                anomaly = f"HTTP_{resp.status}"
                print(f"  [Crawler Anomaly] Block or rate-limit detected for '{keywords}': HTTP {resp.status}")
                break
            elif resp and resp.status >= 500:
                anomaly = f"HTTP_{resp.status}"
                print(f"  [Crawler Anomaly] Server error detected for '{keywords}': HTTP {resp.status}")
                break

            # Authwall / Checkpoint / Login challenge detection
            current_url = (page.url or "").lower()
            if "authwall" in current_url:
                anomaly = "AUTHWALL"
                print(f"  [Crawler Anomaly] Authentication wall detected for '{keywords}'")
                break
            elif "checkpoint" in current_url:
                anomaly = "CHECKPOINT"
                print(f"  [Crawler Anomaly] Security checkpoint challenge detected for '{keywords}'")
                break
            elif "login" in current_url:
                anomaly = "LOGIN"
                print(f"  [Crawler Anomaly] Login redirect detected for '{keywords}'")
                break

            await asyncio.sleep(1.8)

            # Scroll to load dynamic cards on current page
            for _ in range(2):
                await page.evaluate("window.scrollBy(0, 1000)")
                await asyncio.sleep(0.6)

            card_elements = await page.query_selector_all("li div.base-card, div.job-search-card, ul.jobs-search__results-list li")
            if not card_elements:
                # No more job cards found on this page -> Reached the last page!
                break

            page_jobs_count = 0
            for card in card_elements:
                try:
                    title_el = await card.query_selector("h3.base-search-card__title, h3")
                    comp_el = await card.query_selector("h4.base-search-card__subtitle, a.hidden-nested-link")
                    loc_el = await card.query_selector("span.job-search-card__location")
                    link_el = await card.query_selector("a.base-card__full-link, a")

                    if not title_el or not link_el:
                        continue

                    title = (await title_el.inner_text()).strip()
                    company = (await comp_el.inner_text()).strip() if comp_el else "Unknown Company"
                    job_loc = (await loc_el.inner_text()).strip() if loc_el else location
                    href = await link_el.get_attribute("href") or ""

                    id_match = re.search(r"view/(\d+)", href) or re.search(r"currentJobId=(\d+)", href) or re.search(r"-(\d+)\?", href)
                    jid = id_match.group(1) if id_match else ""
                    canonical_url = f"https://www.linkedin.com/jobs/view/{jid}/" if jid else href.split("?")[0]

                    if jid and jid in seen_ids:
                        continue
                    if jid:
                        seen_ids.add(jid)

                    # Easy Apply indicator check
                    easy_apply = False
                    try:
                        easy_el = await card.query_selector(".job-search-card__easy-apply-label, .base-card__easy-apply, [aria-label*='Easy Apply'], [aria-label*='Kolay Başvuru']")
                        if easy_el:
                            easy_apply = True
                        else:
                            card_text = (await card.inner_text()) or ""
                            if "easy apply" in card_text.lower() or "kolay başvuru" in card_text.lower():
                                easy_apply = True
                    except Exception:
                        easy_apply = False

                    all_jobs.append({
                        "title": title,
                        "company": company,
                        "location": job_loc,
                        "url": canonical_url,
                        "description": "",
                        "is_easy_apply": 1 if easy_apply else 0,
                    })
                    page_jobs_count += 1
                except Exception as exc:
                    print(
                        f"[Crawler] Skipping malformed job card on page {page_idx + 1}: "
                        f"{type(exc).__name__}"
                    )
                    continue

            # If this page yielded 0 new unique jobs, we have reached the end of results
            if page_jobs_count == 0:
                break
            skipped_pages = start_offset // 25
            print(f"  -> Page {page_idx + 1} (Skipped ~{skipped_pages} pages / {start_offset} jobs): {page_jobs_count} new job cards collected.")
            await asyncio.sleep(1.0)

        except TimeoutError as texc:
            anomaly = "TIMEOUT"
            print(f"  [Crawler Anomaly] Timeout on Page {page_idx + 1} for '{keywords}': {texc}")
            break
        except Exception as exc:
            err_str = str(exc)
            if "timeout" in err_str.lower():
                anomaly = "TIMEOUT"
            else:
                anomaly = f"EXCEPTION_{type(exc).__name__}"
            print(f"  [Notice] Page {page_idx + 1} note for '{keywords}': {exc}")
            break

    print(f"  [OK] Finished target '{keywords}' in '{location}': Total {len(all_jobs)} jobs found across all pages. (Anomaly: {anomaly or 'None'})")
    return all_jobs, anomaly

async def run_all_targets():
    con = get_connection()
    con.execute("PRAGMA foreign_keys = ON")
    total_imported = 0
    total_cards_seen = 0
    target_anomalies = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="en-US"
            )
            page = await context.new_page()

            for target in SEARCH_TARGETS:
                jobs, anomaly = await scrape_target_all_pages(page, target, max_pages=8)
                if anomaly:
                    target_anomalies.append((target["keywords"], anomaly))

                cards_for_target = len(jobs)
                total_cards_seen += cards_for_target

                query_str = f"{target['keywords']}|{target['location']}"
                imported_for_target = 0
                for j in jobs:
                    if upsert_job(con, j, source_query=query_str):
                        imported_for_target += 1
                total_imported += imported_for_target
                con.commit()
                print(f"  Target summary: '{target['keywords'][:40]}...' cards={cards_for_target} imported={imported_for_target} anomaly={anomaly or 'None'}")
                await asyncio.sleep(1.2)

            await browser.close()

        print(f"\n[Crawler Summary] Total raw cards seen across all targets: {total_cards_seen} | Total new imported: {total_imported} | Anomalies: {len(target_anomalies)}")

        # Zero-Yield Detection: If no raw job cards were found across all targets, raise critical anomaly!
        if total_cards_seen == 0:
            anomaly_summary = ", ".join([f"{kw}: {an}" for kw, an in target_anomalies]) if target_anomalies else "Selector breakage or silent block"
            error_msg = f"[Crawler Critical] Zero-Yield Anomaly: 0 jobs found across all targets! Details: {anomaly_summary}"
            print(error_msg, file=sys.stderr)
            raise RuntimeError(error_msg)

        print(f"LINKEDIN AUTONOMOUS CRAWLER: PASS | Total new 7-day jobs imported: {total_imported}")
        return total_imported
    finally:
        con.close()

def run():
    return asyncio.run(run_all_targets())

if __name__ == "__main__":
    run()
