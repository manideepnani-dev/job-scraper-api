"""
JobScraper Pro v4.0 — 7 Real Platform Scrapers (India Edition)
==============================================================
Changes from v3.0:
  • All searches scoped to India by default
  • New query param: work_type  — remote | hybrid | onsite | any
  • New query param: listing_type — jobs | internships | both
  • Job model: apply_link field added
  • posted_date surfaced consistently across all scrapers

Platforms:
  1. LinkedIn       — aiohttp, server-side rendered public search
  2. Internshala    — aiohttp, HTML parsing (jobs + internships)
  3. Naukri         — aiohttp, JSON-LD + HTML fallback
  4. Indeed         — aiohttp first → Playwright stealth fallback
  5. Glassdoor      — Playwright stealth (JS-heavy, blocks bots)
  6. Company Careers— Greenhouse API + Lever API (real public APIs)
  7. Startups       — Wellfound scrape + Remotive public API fallback

Install:
  pip install fastapi uvicorn aiohttp beautifulsoup4 fake-useragent lxml
  pip install playwright playwright-stealth
  playwright install chromium
"""

from fastapi import FastAPI, Query
from dataclasses import dataclass, asdict
from typing import List, Optional
from datetime import datetime
from urllib.parse import quote, urljoin
import asyncio, aiohttp, json, re, time, random, logging
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("jobscraper")

app = FastAPI(
    title="🚀 JobScraper Pro v4.0 — India Edition",
    version="4.0",
    description="Real jobs & internships from 7 platforms, India-focused",
)

# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────

INDIA_LOCATION = "India"

WORK_TYPE_KEYWORDS = {
    "remote":  ["remote", "work from home", "wfh", "telecommute"],
    "hybrid":  ["hybrid"],
    "onsite":  ["onsite", "on-site", "in-office", "office"],
}


def _matches_work_type(location: str, title: str, description: str, work_type: str) -> bool:
    """Return True if the job matches the requested work_type filter."""
    if work_type == "any":
        return True
    text = f"{location} {title} {description}".lower()
    keywords = WORK_TYPE_KEYWORDS.get(work_type, [])
    return any(kw in text for kw in keywords)


# ─────────────────────────────────────────────────────────────────────────────
#  Data Model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Job:
    title: str
    company: str
    location: str
    remote: bool
    work_type: str          # remote | hybrid | onsite | unknown
    job_type: str           # full-time | internship | part-time | contract
    description: str
    url: str
    apply_link: str         # direct apply URL (same as url when not separately available)
    posted_date: str
    platform: str


def _detect_work_type(location: str, title: str = "", description: str = "") -> str:
    text = f"{location} {title} {description}".lower()
    if any(kw in text for kw in WORK_TYPE_KEYWORDS["remote"]):
        return "remote"
    if any(kw in text for kw in WORK_TYPE_KEYWORDS["hybrid"]):
        return "hybrid"
    if any(kw in text for kw in WORK_TYPE_KEYWORDS["onsite"]):
        return "onsite"
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
#  Core Stealth Scraper
# ─────────────────────────────────────────────────────────────────────────────

class StealthScraper:
    CHROME_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
    MAC_UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    def __init__(self):
        self.ua = UserAgent()
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(ssl=False, limit=15)
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self

    async def __aexit__(self, *_):
        if self.session and not self.session.closed:
            await self.session.close()

    def _headers(self, referer: str = None, ua: str = None) -> dict:
        h = {
            "User-Agent": ua or self.ua.random,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none" if not referer else "same-origin",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
        if referer:
            h["Referer"] = referer
        return h

    def _api_headers(self) -> dict:
        return {
            "User-Agent": self.CHROME_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Origin": "https://www.google.com",
        }

    async def _fetch(
        self,
        url: str,
        headers: dict = None,
        params: dict = None,
        as_json: bool = False,
        retries: int = 3,
    ):
        if headers is None:
            headers = self._headers()
        for attempt in range(retries):
            await asyncio.sleep(random.uniform(0.4, 1.2))
            try:
                async with self.session.get(
                    url, headers=headers, params=params, allow_redirects=True
                ) as resp:
                    if resp.status == 200:
                        if as_json:
                            return await resp.json(content_type=None)
                        return await resp.text()
                    if resp.status == 429:
                        wait = 2 ** attempt * 3
                        logger.warning(f"Rate-limited on {url}, waiting {wait}s")
                        await asyncio.sleep(wait)
                    else:
                        logger.warning(f"HTTP {resp.status} — {url}")
                        return None
            except asyncio.TimeoutError:
                logger.warning(f"Timeout on {url} (attempt {attempt + 1})")
            except Exception as e:
                logger.error(f"Fetch error on {url}: {e}")
            await asyncio.sleep(1)
        return None

    # ─────────────────────────────────────────────────────────────────────────
    #  1. LinkedIn  (India)
    # ─────────────────────────────────────────────────────────────────────────

    async def scrape_linkedin(
        self,
        keywords: str,
        listing_type: str = "jobs",
    ) -> List[Job]:
        """
        LinkedIn public job search scoped to India.
        f_WT=2 → remote only (added when work_type=remote at route level via filtering).
        """
        jobs: List[Job] = []

        # LinkedIn: f_E=1 → internship, default → all
        extra_params = {}
        if listing_type == "internships":
            extra_params["f_E"] = "1"   # Entry level / internship filter

        params = {
            "keywords": keywords,
            "location": "India",
            "f_TPR": "r604800",   # Past 7 days
            "position": 1,
            "pageNum": 0,
            "sortBy": "DD",
            **extra_params,
        }
        html = await self._fetch(
            "https://www.linkedin.com/jobs/search",
            headers=self._headers(ua=self.CHROME_UA),
            params=params,
        )
        if not html:
            logger.warning("LinkedIn: no response")
            return jobs

        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("div.base-card, li.jobs-search__results-list > div")[:20]

        for card in cards:
            try:
                title_el   = card.select_one("h3.base-search-card__title, h3.job-search-card__title")
                company_el = card.select_one("h4.base-search-card__subtitle, a.job-search-card__company-name")
                loc_el     = card.select_one("span.job-search-card__location")
                link_el    = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
                time_el    = card.select_one("time")

                if not title_el:
                    continue

                title    = title_el.get_text(strip=True)
                company  = company_el.get_text(strip=True) if company_el else "N/A"
                location = loc_el.get_text(strip=True) if loc_el else INDIA_LOCATION
                href     = link_el["href"].split("?")[0] if link_el else "https://linkedin.com/jobs"
                posted   = time_el.get("datetime", "")[:10] if time_el else datetime.now().strftime("%Y-%m-%d")

                # Ensure India-scoped — skip if location clearly outside India
                if location and not any(
                    x in location.lower() for x in ("india", "remote", "wfh", "work from home")
                ) and "," in location:
                    # LinkedIn sometimes returns global results; skip non-India ones
                    country_part = location.split(",")[-1].strip().lower()
                    if country_part not in ("india", "in", ""):
                        continue

                desc = f"{title} at {company} ({location})"
                jtype = "internship" if listing_type == "internships" else "full-time"

                jobs.append(Job(
                    title=title,
                    company=company,
                    location=location,
                    remote="remote" in location.lower() or "wfh" in location.lower(),
                    work_type=_detect_work_type(location, title, desc),
                    job_type=jtype,
                    description=desc,
                    url=href,
                    apply_link=href,
                    posted_date=posted or datetime.now().strftime("%Y-%m-%d"),
                    platform="LinkedIn",
                ))
            except Exception as e:
                logger.debug(f"LinkedIn card error: {e}")

        logger.info(f"LinkedIn: {len(jobs)} results")
        return jobs

    # ─────────────────────────────────────────────────────────────────────────
    #  2. Internshala  (India-native)
    # ─────────────────────────────────────────────────────────────────────────

    async def scrape_internshala(
        self,
        keywords: str,
        listing_type: str = "both",
    ) -> List[Job]:
        """
        Internshala jobs + internships pages.
        listing_type controls which URLs are scraped.
        """
        jobs: List[Job] = []
        kw_encoded = quote(keywords)

        targets = []
        if listing_type in ("jobs", "both"):
            targets.append((f"https://internshala.com/jobs/keywords-{kw_encoded}/", "full-time"))
        if listing_type in ("internships", "both"):
            targets.append((f"https://internshala.com/internships/keywords-{kw_encoded}/", "internship"))

        for url, jtype in targets:
            html = await self._fetch(url, headers=self._headers("https://internshala.com"))
            if not html:
                continue

            soup = BeautifulSoup(html, "lxml")
            cards = soup.select(".individual_internship, .internship-listing-card")[:12]

            for card in cards:
                try:
                    title_el   = card.select_one(".profile, .job-internship-name, h3.job-internship-name")
                    company_el = card.select_one(".company_name, a.link_display_like_text")
                    loc_el     = card.select_one(".location_link, .locations_link, .location span")
                    link_el    = card.select_one("a[href]")
                    stipend_el = card.select_one(".stipend, .salary")
                    date_el    = card.select_one(".posted-on, .status-info .status-li:last-child")

                    if not title_el:
                        continue

                    title    = title_el.get_text(strip=True)
                    company  = company_el.get_text(strip=True) if company_el else "N/A"
                    location = loc_el.get_text(strip=True) if loc_el else INDIA_LOCATION
                    href     = urljoin("https://internshala.com", link_el["href"]) if link_el else url
                    stipend  = stipend_el.get_text(strip=True) if stipend_el else ""
                    posted   = date_el.get_text(strip=True) if date_el else datetime.now().strftime("%Y-%m-%d")
                    is_remote = any(k in location.lower() for k in ("work from home", "remote", "wfh"))
                    desc     = f"{title} at {company}" + (f" | {stipend}" if stipend else "")

                    jobs.append(Job(
                        title=title,
                        company=company,
                        location=location if location else INDIA_LOCATION,
                        remote=is_remote,
                        work_type=_detect_work_type(location, title, desc),
                        job_type=jtype,
                        description=desc,
                        url=href,
                        apply_link=href,
                        posted_date=posted,
                        platform="Internshala",
                    ))
                except Exception as e:
                    logger.debug(f"Internshala card error: {e}")

        logger.info(f"Internshala: {len(jobs)} results")
        return jobs

    # ─────────────────────────────────────────────────────────────────────────
    #  3. Naukri  (India-native)
    # ─────────────────────────────────────────────────────────────────────────

    async def scrape_naukri(
        self,
        keywords: str,
        listing_type: str = "jobs",
    ) -> List[Job]:
        """
        Naukri.com — India's largest job board.
        listing_type: internships uses the internship-specific URL segment.
        """
        jobs: List[Job] = []
        slug = re.sub(r"[^a-z0-9]+", "-", keywords.lower()).strip("-")

        if listing_type == "internships":
            url = f"https://www.naukri.com/{slug}-internship-jobs"
        else:
            url = f"https://www.naukri.com/{slug}-jobs-in-india"

        headers = self._headers("https://www.naukri.com/", ua=self.CHROME_UA)
        html = await self._fetch(url, headers=headers)
        if not html:
            logger.warning("Naukri: no response")
            return jobs

        soup = BeautifulSoup(html, "lxml")

        # ── Strategy A: JSON-LD structured data ─────────────────────────────
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "JobPosting":
                        loc = (item.get("jobLocation") or {})
                        loc_name = (loc.get("address") or {}).get("addressLocality", INDIA_LOCATION)
                        job_url = item.get("url", url)
                        desc_raw = BeautifulSoup(item.get("description", ""), "lxml").get_text()[:200]
                        jtype = "internship" if listing_type == "internships" else (
                            (item.get("employmentType") or "full-time").lower()
                        )
                        jobs.append(Job(
                            title=item.get("title", "N/A"),
                            company=(item.get("hiringOrganization") or {}).get("name", "N/A"),
                            location=loc_name,
                            remote="remote" in item.get("title", "").lower(),
                            work_type=_detect_work_type(loc_name, item.get("title", ""), desc_raw),
                            job_type=jtype,
                            description=desc_raw,
                            url=job_url,
                            apply_link=job_url,
                            posted_date=(item.get("datePosted") or datetime.now().isoformat())[:10],
                            platform="Naukri",
                        ))
            except Exception:
                continue

        # ── Strategy B: HTML job cards (fallback) ────────────────────────────
        if not jobs:
            cards = soup.select(
                "article.jobTuple, div.job-tuple-wrapper, .cust-job-tuple, div[type='tuple']"
            )[:15]
            for card in cards:
                try:
                    title_el   = card.select_one("a.title, .jobtitle, h2 a, .row1 a")
                    company_el = card.select_one("a.subTitle, .comp-name, .companyInfo a")
                    loc_el     = card.select_one("li.location span, .locWdth, .loc span, .location")
                    date_el    = card.select_one(".job-post-day, .freshness, .postedDate")

                    if not title_el:
                        continue

                    title    = title_el.get_text(strip=True)
                    company  = company_el.get_text(strip=True) if company_el else "N/A"
                    location = loc_el.get_text(strip=True) if loc_el else INDIA_LOCATION
                    href     = title_el.get("href", url)
                    posted   = date_el.get_text(strip=True) if date_el else datetime.now().strftime("%Y-%m-%d")
                    full_url = href if href.startswith("http") else f"https://www.naukri.com{href}"
                    desc     = f"{title} at {company}"
                    jtype    = "internship" if listing_type == "internships" else "full-time"

                    jobs.append(Job(
                        title=title,
                        company=company,
                        location=location,
                        remote="remote" in location.lower(),
                        work_type=_detect_work_type(location, title, desc),
                        job_type=jtype,
                        description=desc,
                        url=full_url,
                        apply_link=full_url,
                        posted_date=posted,
                        platform="Naukri",
                    ))
                except Exception as e:
                    logger.debug(f"Naukri card error: {e}")

        logger.info(f"Naukri: {len(jobs)} results")
        return jobs

    # ─────────────────────────────────────────────────────────────────────────
    #  4. Indeed India
    # ─────────────────────────────────────────────────────────────────────────

    async def scrape_indeed(
        self,
        keywords: str,
        listing_type: str = "jobs",
    ) -> List[Job]:
        jobs = await self._indeed_aiohttp(keywords, listing_type)
        if not jobs:
            logger.info("Indeed aiohttp failed, trying Playwright stealth...")
            jobs = await self._indeed_playwright(keywords, listing_type)
        return jobs

    async def _indeed_aiohttp(self, keywords: str, listing_type: str = "jobs") -> List[Job]:
        jobs: List[Job] = []
        # Use Indeed India
        base_url = "https://in.indeed.com/jobs"
        params = {
            "q": keywords + (" internship" if listing_type == "internships" else ""),
            "l": "India",
            "fromage": "7",
            "sort": "date",
        }

        headers = self._headers(ua=self.CHROME_UA)
        headers["Cookie"] = "CTK=1; LC=co=IN&lang=en; indeed_rcc=CTK"

        html = await self._fetch(base_url, headers=headers, params=params)
        if not html or "captcha" in html.lower() or len(html) < 8000:
            return []

        soup = BeautifulSoup(html, "lxml")

        # ── Strategy A: embedded mosaic JSON ────────────────────────────────
        for script in soup.find_all("script"):
            raw = script.string or ""
            if "mosaic" not in raw and "jobKeysWithTitles" not in raw:
                continue
            match = re.search(r'"jobKeysWithTitles"\s*:\s*(\{[^}]+\})', raw)
            if match:
                try:
                    kv = json.loads(match.group(1))
                    for jk, title in kv.items():
                        apply_url = f"https://in.indeed.com/viewjob?jk={jk}"
                        jtype = "internship" if listing_type == "internships" else "full-time"
                        jobs.append(Job(
                            title=title,
                            company="Indeed Listing",
                            location=INDIA_LOCATION,
                            remote=False,
                            work_type="unknown",
                            job_type=jtype,
                            description=f"{title} (see link for details)",
                            url=apply_url,
                            apply_link=apply_url,
                            posted_date=datetime.now().strftime("%Y-%m-%d"),
                            platform="Indeed",
                        ))
                    if jobs:
                        break
                except Exception:
                    pass

        # ── Strategy B: HTML job cards ───────────────────────────────────────
        if not jobs:
            cards = soup.select("div[data-jk], .job_seen_beacon, td.resultContent")[:15]
            for card in cards:
                try:
                    title_el   = card.select_one("h2.jobTitle span[title], h2.jobTitle a span")
                    company_el = card.select_one('[data-testid="company-name"], .companyName span')
                    loc_el     = card.select_one('[data-testid="text-location"], .companyLocation')
                    date_el    = card.select_one('[data-testid="myJobsStateDate"], .date')
                    jk         = card.get("data-jk") or (
                        card.select_one("a[data-jk]") or {}
                    ).get("data-jk")

                    if not title_el:
                        continue

                    title    = title_el.get("title") or title_el.get_text(strip=True)
                    company  = company_el.get_text(strip=True) if company_el else "N/A"
                    location = loc_el.get_text(strip=True) if loc_el else INDIA_LOCATION
                    posted   = date_el.get_text(strip=True) if date_el else datetime.now().strftime("%Y-%m-%d")
                    apply_url = f"https://in.indeed.com/viewjob?jk={jk}" if jk else "https://in.indeed.com"
                    desc     = f"{title} at {company} — {location}"
                    jtype    = "internship" if listing_type == "internships" else "full-time"

                    jobs.append(Job(
                        title=title,
                        company=company,
                        location=location,
                        remote="remote" in location.lower(),
                        work_type=_detect_work_type(location, title, desc),
                        job_type=jtype,
                        description=desc,
                        url=apply_url,
                        apply_link=apply_url,
                        posted_date=posted,
                        platform="Indeed",
                    ))
                except Exception as e:
                    logger.debug(f"Indeed card error: {e}")

        logger.info(f"Indeed aiohttp: {len(jobs)} results")
        return jobs

    async def _indeed_playwright(self, keywords: str, listing_type: str = "jobs") -> List[Job]:
        try:
            from playwright.async_api import async_playwright
            from playwright_stealth import stealth_async
        except ImportError:
            logger.warning("playwright / playwright-stealth not installed. Skipping Indeed Playwright.")
            return []

        jobs: List[Job] = []
        query = keywords + (" internship" if listing_type == "internships" else "")
        url = f"https://in.indeed.com/jobs?q={quote(query)}&l=India&fromage=7&sort=date"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1366,768",
            ])
            ctx = await browser.new_context(
                user_agent=self.CHROME_UA,
                viewport={"width": 1366, "height": 768},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            page = await ctx.new_page()
            await stealth_async(page)

            try:
                await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(2, 4))

                for sel in ['button[id*="close"]', 'button[aria-label*="close"]', "#onetrust-accept-btn-handler"]:
                    try:
                        btn = page.locator(sel)
                        if await btn.count():
                            await btn.first.click()
                            await asyncio.sleep(0.5)
                    except Exception:
                        pass

                try:
                    await page.wait_for_selector("div[data-jk], .job_seen_beacon", timeout=12000)
                except Exception:
                    pass

                cards = await page.query_selector_all("div[data-jk], .job_seen_beacon")

                for card in cards[:15]:
                    try:
                        title_el   = await card.query_selector("h2.jobTitle span[title], h2.jobTitle a span")
                        company_el = await card.query_selector('[data-testid="company-name"]')
                        loc_el     = await card.query_selector('[data-testid="text-location"]')
                        date_el    = await card.query_selector('[data-testid="myJobsStateDate"]')
                        jk         = await card.get_attribute("data-jk")

                        if not title_el:
                            continue

                        title    = (await title_el.get_attribute("title")) or (await title_el.inner_text())
                        company  = await company_el.inner_text() if company_el else "N/A"
                        location = await loc_el.inner_text() if loc_el else INDIA_LOCATION
                        posted   = await date_el.inner_text() if date_el else datetime.now().strftime("%Y-%m-%d")
                        apply_url = f"https://in.indeed.com/viewjob?jk={jk}" if jk else "https://in.indeed.com"
                        desc     = f"{title} at {company}"
                        jtype    = "internship" if listing_type == "internships" else "full-time"

                        jobs.append(Job(
                            title=title.strip(),
                            company=company.strip(),
                            location=location.strip(),
                            remote="remote" in location.lower(),
                            work_type=_detect_work_type(location, title, desc),
                            job_type=jtype,
                            description=desc,
                            url=apply_url,
                            apply_link=apply_url,
                            posted_date=posted.strip(),
                            platform="Indeed",
                        ))
                    except Exception as e:
                        logger.debug(f"Indeed PW card error: {e}")

            except Exception as e:
                logger.error(f"Indeed Playwright error: {e}")
            finally:
                await browser.close()

        logger.info(f"Indeed Playwright: {len(jobs)} results")
        return jobs

    # ─────────────────────────────────────────────────────────────────────────
    #  5. Glassdoor India
    # ─────────────────────────────────────────────────────────────────────────

    async def scrape_glassdoor(
        self,
        keywords: str,
        listing_type: str = "jobs",
    ) -> List[Job]:
        try:
            from playwright.async_api import async_playwright
            from playwright_stealth import stealth_async
        except ImportError:
            logger.warning("playwright / playwright-stealth not installed. Skipping Glassdoor.")
            return []

        jobs: List[Job] = []
        kw_slug = keywords.replace(" ", "-")
        # Scope to India using Glassdoor's location parameter
        url = (
            f"https://www.glassdoor.co.in/Job/{kw_slug}-jobs-SRCH_KO0,{len(kw_slug)}.htm"
            f"?locT=N&locId=115&countryRedirect=true"
        )

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ])
            ctx = await browser.new_context(
                user_agent=self.MAC_UA,
                viewport={"width": 1440, "height": 900},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            page = await ctx.new_page()
            await stealth_async(page)

            try:
                await page.goto(url, timeout=35000, wait_until="networkidle")
                await asyncio.sleep(random.uniform(2, 4))

                dismiss_selectors = [
                    'button[data-test="welcome-back-continue-btn"]',
                    "button.CloseButton",
                    '[aria-label="Close"]',
                    "#onetrust-accept-btn-handler",
                    'button:has-text("Continue")',
                    'button:has-text("Accept")',
                ]
                for sel in dismiss_selectors:
                    try:
                        btn = page.locator(sel)
                        if await btn.count():
                            await btn.first.click()
                            await asyncio.sleep(0.8)
                    except Exception:
                        pass

                card_selectors = [
                    'li[data-test="jobListing"]',
                    "article.JobCard",
                    "li.react-job-listing",
                ]
                cards = []
                for sel in card_selectors:
                    try:
                        await page.wait_for_selector(sel, timeout=8000)
                        cards = await page.query_selector_all(sel)
                        if cards:
                            break
                    except Exception:
                        continue

                for card in cards[:12]:
                    try:
                        title_el = (
                            await card.query_selector('[data-test="job-title"]')
                            or await card.query_selector(".JobCard_jobTitle__GLyJ1")
                            or await card.query_selector("a[data-test='job-title']")
                        )
                        company_el = (
                            await card.query_selector('[data-test="employer-name"]')
                            or await card.query_selector(".EmployerProfile_compactEmployerName__LE242")
                        )
                        loc_el = (
                            await card.query_selector('[data-test="emp-location"]')
                            or await card.query_selector(".JobCard_location__N_iYE")
                        )
                        link_el = (
                            await card.query_selector("a[href*='/job-listing/']")
                            or await card.query_selector("a[class*='JobCard_trackingLink']")
                        )
                        date_el = await card.query_selector('[data-test="job-age"], .JobCard_listingAge__KuaxZ')

                        if not title_el:
                            continue

                        title    = (await title_el.inner_text()).strip()
                        company  = (await company_el.inner_text()).strip() if company_el else "N/A"
                        location = (await loc_el.inner_text()).strip() if loc_el else INDIA_LOCATION
                        posted   = (await date_el.inner_text()).strip() if date_el else datetime.now().strftime("%Y-%m-%d")
                        href     = await link_el.get_attribute("href") if link_el else ""
                        link     = f"https://www.glassdoor.co.in{href}" if href.startswith("/") else (href or "https://glassdoor.co.in")
                        desc     = f"{title} at {company} — {location}"
                        jtype    = "internship" if listing_type == "internships" else "full-time"

                        jobs.append(Job(
                            title=title,
                            company=company,
                            location=location,
                            remote="remote" in location.lower(),
                            work_type=_detect_work_type(location, title, desc),
                            job_type=jtype,
                            description=desc,
                            url=link,
                            apply_link=link,
                            posted_date=posted,
                            platform="Glassdoor",
                        ))
                    except Exception as e:
                        logger.debug(f"Glassdoor card error: {e}")

            except Exception as e:
                logger.error(f"Glassdoor error: {e}")
            finally:
                await browser.close()

        logger.info(f"Glassdoor: {len(jobs)} results")
        return jobs

    # ─────────────────────────────────────────────────────────────────────────
    #  6. Company Career Pages  (Greenhouse + Lever — filtered to India)
    # ─────────────────────────────────────────────────────────────────────────

    async def scrape_company_careers(
        self,
        keywords: str,
        listing_type: str = "jobs",
    ) -> List[Job]:
        jobs: List[Job] = []
        kw_tokens = keywords.lower().split()

        def kw_match(text: str) -> bool:
            t = text.lower()
            return any(k in t for k in kw_tokens)

        def india_match(location: str) -> bool:
            """Accept jobs in India or Remote (could work from India)."""
            loc = location.lower()
            return any(x in loc for x in ("india", "remote", "anywhere", "worldwide", ""))

        # ── Greenhouse boards ─────────────────────────────────────────────────
        greenhouse_slugs = [
            "airbnb", "stripe", "dropbox", "shopify", "hubspot",
            "zendesk", "twilio", "datadog", "hashicorp", "figma",
            "gitlab", "mongodb", "elastic", "cloudflare", "confluent",
        ]

        async def _greenhouse(slug: str) -> List[Job]:
            data = await self._fetch(
                f"https://api.greenhouse.io/v1/boards/{slug}/jobs",
                headers=self._api_headers(),
                as_json=True,
            )
            results = []
            if not data or "jobs" not in data:
                return results
            for j in data["jobs"]:
                if not kw_match(j.get("title", "")):
                    continue
                loc = (j.get("location") or {}).get("name", "N/A")
                if not india_match(loc):
                    continue
                job_url = j.get("absolute_url", f"https://boards.greenhouse.io/{slug}")
                desc_raw = BeautifulSoup(j.get("content") or "", "lxml").get_text()[:250]
                jtype = "internship" if listing_type == "internships" else "full-time"
                results.append(Job(
                    title=j.get("title", "N/A"),
                    company=slug.capitalize(),
                    location=loc,
                    remote="remote" in loc.lower(),
                    work_type=_detect_work_type(loc, j.get("title", ""), desc_raw),
                    job_type=jtype,
                    description=desc_raw,
                    url=job_url,
                    apply_link=job_url,
                    posted_date=(j.get("updated_at") or datetime.now().isoformat())[:10],
                    platform="Company Careers",
                ))
            return results[:4]

        # ── Lever boards ──────────────────────────────────────────────────────
        lever_slugs = [
            "netflix", "reddit", "discord", "notion", "linear",
            "razorpay", "swiggy", "meesho", "groww", "zepto",
        ]

        async def _lever(slug: str) -> List[Job]:
            data = await self._fetch(
                f"https://api.lever.co/v0/postings/{slug}",
                headers=self._api_headers(),
                params={"mode": "json"},
                as_json=True,
            )
            results = []
            if not isinstance(data, list):
                return results
            for j in data:
                if not kw_match(j.get("text", "")):
                    continue
                cats = j.get("categories") or {}
                loc  = cats.get("location", "N/A")
                if not india_match(loc):
                    continue
                posted_ms = j.get("createdAt", 0)
                posted = (
                    datetime.fromtimestamp(posted_ms / 1000).strftime("%Y-%m-%d")
                    if posted_ms else datetime.now().strftime("%Y-%m-%d")
                )
                apply_url = j.get("applyUrl") or j.get("hostedUrl", f"https://jobs.lever.co/{slug}")
                jtype = "internship" if listing_type == "internships" else cats.get("commitment", "full-time")
                desc  = (j.get("descriptionPlain") or "")[:250]
                results.append(Job(
                    title=j.get("text", "N/A"),
                    company=slug.capitalize(),
                    location=loc,
                    remote="remote" in loc.lower() or "remote" in j.get("text", "").lower(),
                    work_type=_detect_work_type(loc, j.get("text", ""), desc),
                    job_type=jtype,
                    description=desc,
                    url=j.get("hostedUrl", f"https://jobs.lever.co/{slug}"),
                    apply_link=apply_url,
                    posted_date=posted,
                    platform="Company Careers",
                ))
            return results[:4]

        gh_results = await asyncio.gather(*[_greenhouse(s) for s in greenhouse_slugs], return_exceptions=True)
        lv_results = await asyncio.gather(*[_lever(s) for s in lever_slugs], return_exceptions=True)

        for r in (*gh_results, *lv_results):
            if isinstance(r, list):
                jobs.extend(r)

        logger.info(f"Company Careers: {len(jobs)} results")
        return jobs

    # ─────────────────────────────────────────────────────────────────────────
    #  7. Startup Jobs  (Wellfound + Remotive API fallback)
    # ─────────────────────────────────────────────────────────────────────────

    async def scrape_startup_jobs(
        self,
        keywords: str,
        listing_type: str = "jobs",
    ) -> List[Job]:
        jobs = await self._wellfound(keywords, listing_type)
        if not jobs:
            logger.info("Wellfound empty, falling back to Remotive API")
            jobs = await self._remotive(keywords, listing_type)
        return jobs

    async def _wellfound(self, keywords: str, listing_type: str = "jobs") -> List[Job]:
        jobs: List[Job] = []
        params: dict = {"q": keywords, "country": "IN"}
        if listing_type == "internships":
            params["type"] = "internship"

        headers = self._headers("https://wellfound.com", ua=self.CHROME_UA)
        html = await self._fetch("https://wellfound.com/jobs", headers=headers, params=params)
        if not html:
            return jobs

        soup = BeautifulSoup(html, "lxml")
        script = soup.find("script", id="__NEXT_DATA__")
        if script:
            try:
                data = json.loads(script.string or "")
                job_list = (
                    data.get("props", {})
                        .get("pageProps", {})
                        .get("jobs", [])
                )
                for j in job_list[:15]:
                    startup  = j.get("startup") or {}
                    locs     = j.get("locations") or ["India"]
                    location = ", ".join(locs) if isinstance(locs, list) else str(locs)
                    # Filter to India / remote
                    if not any(x in location.lower() for x in ("india", "remote", "anywhere", "worldwide")):
                        continue
                    apply_url = j.get("applyUrl") or f"https://wellfound.com/jobs/{j.get('slug', '')}"
                    jtype     = "internship" if listing_type == "internships" else j.get("jobType", "full-time")
                    desc      = (j.get("description") or "")[:250]
                    jobs.append(Job(
                        title=j.get("title", "N/A"),
                        company=startup.get("name", "N/A"),
                        location=location,
                        remote=j.get("remote", False),
                        work_type=_detect_work_type(location, j.get("title", ""), desc),
                        job_type=jtype,
                        description=desc,
                        url=f"https://wellfound.com/jobs/{j.get('slug', '')}",
                        apply_link=apply_url,
                        posted_date=datetime.now().strftime("%Y-%m-%d"),
                        platform="Wellfound",
                    ))
            except Exception as e:
                logger.debug(f"Wellfound __NEXT_DATA__ parse error: {e}")

        if not jobs:
            cards = soup.select(".styles_component__2qRiz, [data-test='StartupResult']")[:10]
            for card in cards:
                try:
                    title_el   = card.select_one("a[data-test='job-title'], h2")
                    company_el = card.select_one("[data-test='startup-name'], h3")
                    loc_el     = card.select_one("[data-test='location']")
                    link_el    = card.select_one("a[href*='/jobs/']")

                    if not title_el:
                        continue

                    title    = title_el.get_text(strip=True)
                    company  = company_el.get_text(strip=True) if company_el else "N/A"
                    location = loc_el.get_text(strip=True) if loc_el else INDIA_LOCATION
                    href     = link_el["href"] if link_el else ""
                    link     = urljoin("https://wellfound.com", href) if href else "https://wellfound.com"
                    desc     = f"{title} at {company}"
                    jtype    = "internship" if listing_type == "internships" else "full-time"

                    jobs.append(Job(
                        title=title, company=company, location=location,
                        remote="remote" in location.lower(),
                        work_type=_detect_work_type(location, title, desc),
                        job_type=jtype,
                        description=desc,
                        url=link,
                        apply_link=link,
                        posted_date=datetime.now().strftime("%Y-%m-%d"),
                        platform="Wellfound",
                    ))
                except Exception:
                    continue

        logger.info(f"Wellfound: {len(jobs)} results")
        return jobs

    async def _remotive(self, keywords: str, listing_type: str = "jobs") -> List[Job]:
        """Remotive public REST API — remote jobs only, worldwide."""
        data = await self._fetch(
            "https://remotive.com/api/remote-jobs",
            headers=self._api_headers(),
            params={"search": keywords, "limit": 20},
            as_json=True,
        )
        jobs: List[Job] = []
        if not data or "jobs" not in data:
            return jobs

        for j in data["jobs"]:
            jtype = "internship" if listing_type == "internships" else j.get("job_type", "full-time")
            desc  = BeautifulSoup(j.get("description", ""), "lxml").get_text()[:250]
            apply_url = j.get("url", "https://remotive.com")
            jobs.append(Job(
                title=j.get("title", "N/A"),
                company=j.get("company_name", "N/A"),
                location=j.get("candidate_required_location", "Worldwide / Remote"),
                remote=True,
                work_type="remote",
                job_type=jtype,
                description=desc,
                url=apply_url,
                apply_link=apply_url,
                posted_date=(j.get("publication_date") or datetime.now().isoformat())[:10],
                platform="Remotive",
            ))

        logger.info(f"Remotive: {len(jobs)} results")
        return jobs


# ─────────────────────────────────────────────────────────────────────────────
#  Platform registry
# ─────────────────────────────────────────────────────────────────────────────

PLATFORM_MAP = {
    "linkedin":        "scrape_linkedin",
    "internshala":     "scrape_internshala",
    "naukri":          "scrape_naukri",
    "indeed":          "scrape_indeed",
    "glassdoor":       "scrape_glassdoor",
    "company-careers": "scrape_company_careers",
    "startups":        "scrape_startup_jobs",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "message": "🚀 JobScraper Pro v4.0 — India Edition",
        "docs": "/docs",
        "platforms": list(PLATFORM_MAP.keys()),
        "examples": {
            "all jobs":          "/jobs?role=python+developer&platforms=all",
            "remote only":       "/jobs?role=data+engineer&work_type=remote",
            "internships only":  "/jobs?role=machine+learning&listing_type=internships",
            "hybrid jobs":       "/jobs?role=backend+developer&work_type=hybrid&listing_type=jobs",
            "naukri internships":"/jobs?role=frontend&platforms=naukri&listing_type=internships",
        },
    }


@app.get("/jobs")
async def get_jobs(
    role: str = Query(..., description="Job title / keywords, e.g. 'python developer'"),
    platforms: str = Query(
        "all",
        description="Comma-separated platforms or 'all'. "
                    f"Available: {', '.join(PLATFORM_MAP.keys())}",
    ),
    limit: int = Query(50, ge=1, le=200, description="Max total jobs to return"),
    work_type: str = Query(
        "any",
        description="Filter by work arrangement: remote | hybrid | onsite | any",
        regex="^(remote|hybrid|onsite|any)$",
    ),
    listing_type: str = Query(
        "jobs",
        description="What to scrape: jobs | internships | both",
        regex="^(jobs|internships|both)$",
    ),
):
    start = time.time()

    # ── Resolve platforms ────────────────────────────────────────────────────
    if platforms.strip().lower() == "all":
        selected_keys = list(PLATFORM_MAP.keys())
    else:
        selected_keys = [
            p.strip().lower()
            for p in platforms.split(",")
            if p.strip().lower() in PLATFORM_MAP
        ]

    if not selected_keys:
        return {"error": f"No valid platforms. Choose from: {list(PLATFORM_MAP.keys())}"}

    # ── Run scrapers concurrently ────────────────────────────────────────────
    async with StealthScraper() as s:
        tasks = [
            getattr(s, PLATFORM_MAP[key])(role, listing_type)
            for key in selected_keys
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_jobs: List[Job] = []
    errors: dict = {}

    for key, result in zip(selected_keys, results):
        if isinstance(result, Exception):
            errors[key] = str(result)
            logger.error(f"{key} scraper raised: {result}")
        elif isinstance(result, list):
            all_jobs.extend(result)

    # ── Apply work_type filter ───────────────────────────────────────────────
    if work_type != "any":
        all_jobs = [
            j for j in all_jobs
            if _matches_work_type(j.location, j.title, j.description, work_type)
        ]

    # ── Deduplicate by URL ───────────────────────────────────────────────────
    seen_urls: set = set()
    unique_jobs: List[Job] = []
    for job in all_jobs:
        if job.url not in seen_urls:
            seen_urls.add(job.url)
            unique_jobs.append(job)

    # ── Sort by posted_date descending (newest first) ────────────────────────
    def _parse_date(d: str) -> str:
        # Keep raw string for sort; ISO dates sort correctly as strings
        return d or "0000-00-00"

    unique_jobs.sort(key=lambda j: _parse_date(j.posted_date), reverse=True)
    unique_jobs = unique_jobs[:limit]

    return {
        "jobs": [asdict(j) for j in unique_jobs],
        "total": len(unique_jobs),
        "filters": {
            "role": role,
            "work_type": work_type,
            "listing_type": listing_type,
            "location": "India",
        },
        "platforms_scraped": len(selected_keys),
        "platforms": selected_keys,
        "execution_time_seconds": round(time.time() - start, 2),
        "errors": errors,
    }


@app.get("/platforms")
async def list_platforms():
    return {
        "platforms": list(PLATFORM_MAP.keys()),
        "query_params": {
            "work_type":    "remote | hybrid | onsite | any (default: any)",
            "listing_type": "jobs | internships | both (default: jobs)",
        },
        "notes": {
            "indeed":          "aiohttp → Playwright stealth fallback — uses in.indeed.com (India)",
            "glassdoor":       "Playwright stealth only — uses glassdoor.co.in",
            "linkedin":        "aiohttp, server-side rendered — location=India param",
            "internshala":     "aiohttp — India-native, best for internships",
            "naukri":          "aiohttp + JSON-LD — India-native, uses /jobs-in-india slug",
            "company-careers": "Greenhouse + Lever public APIs — filtered to India/remote",
            "startups":        "Wellfound (country=IN) + Remotive API fallback",
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)