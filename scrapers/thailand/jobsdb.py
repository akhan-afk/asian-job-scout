from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, JobListing, DEBUG


class JobsDBScraper(BaseScraper):
    """
    Scraper for JobsDB Thailand (th.jobsdb.com), operated by SEEK.
    Thailand's leading English-language job board.

    Listing page renders server-side — fast, no heavy JS wait required.
    Individual listing URLs follow: https://th.jobsdb.com/job/XXXXXXXX
    """

    source_name = "jobsdb_thailand"
    BASE_URL    = "https://th.jobsdb.com"
    # Sort by most recently listed so we always get the freshest jobs
    LIST_URL    = "https://th.jobsdb.com/jobs?sortmode=ListedDate"

    async def get_listing_urls(self) -> list[str]:
        await self.page.goto(self.LIST_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            await self.page.wait_for_selector("a[href]", timeout=10000)
        except Exception:
            pass
        await self.page.wait_for_timeout(3000)

        title = await self.page.title()
        print(f"[jobsdb_thailand] Page loaded: {title}")

        html = await self.page.content()
        soup = BeautifulSoup(html, "html.parser")
        self._debug_links(soup, "jobsdb_thailand")

        links = []
        seen: set[str] = set()
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if not href:
                continue
            # JobsDB job detail URLs: /job/XXXXXXXX (8-digit numeric ID)
            path = href.split("?")[0].rstrip("/")
            if "/job/" not in path:
                continue
            job_id = path.split("/job/")[-1]
            if not job_id.isdigit():
                continue
            full = f"{self.BASE_URL}/job/{job_id}"
            if full not in seen:
                seen.add(full)
                links.append(full)

        return links[:20]

    _debug_detail_done = False

    async def parse_listing(self, url: str) -> JobListing:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await self.page.wait_for_selector("h1", timeout=10000)
        except Exception:
            pass
        await self.page.wait_for_timeout(2000)

        html = await self.page.content()
        soup = BeautifulSoup(html, "html.parser")

        if DEBUG and not JobsDBScraper._debug_detail_done:
            self._debug_detail(soup)
            JobsDBScraper._debug_detail_done = True

        def get(*selectors: str) -> str:
            for sel in selectors:
                el = soup.select_one(sel)
                if el:
                    return self._clean(el.get_text())
            return ""

        import re as _re
        # SVG viewBox coordinates and other numeric arrays get picked up by
        # generic text searches — reject anything that looks like [n,n,n,n]
        _garbage = _re.compile(r"^\[\d[\d,\s]+\]$")

        def find_after_label(*keywords: str) -> str:
            """Find value text that follows a label containing any of the keywords."""
            for keyword in keywords:
                # Standard dt/dd pattern
                for dt in soup.find_all("dt"):
                    if keyword.lower() in dt.get_text().lower():
                        dd = dt.find_next_sibling("dd")
                        if dd:
                            text = self._clean(dd.get_text())
                            if text and len(text) > 1 and not _garbage.match(text):
                                return text

                # Fallback: label text → next sibling
                for label_el in soup.find_all(
                    string=lambda t, k=keyword.lower(): t and k in t.lower()
                ):
                    parent = label_el.parent
                    sibling = parent.find_next_sibling()
                    if sibling:
                        text = self._clean(sibling.get_text())
                        if text and len(text) > 1 and not _garbage.match(text):
                            return text
                    grandparent = parent.parent
                    if grandparent:
                        next_el = grandparent.find_next_sibling()
                        if next_el:
                            text = self._clean(next_el.get_text())
                            if text and len(text) > 1 and not _garbage.match(text):
                                return text
            return ""

        # JobsDB/SEEK detail page uses data-automation attributes extensively.
        # Salary and deadline rely solely on data-automation — the label-based
        # fallback was picking up SVG viewBox coordinates as garbage values.
        title = get(
            '[data-automation="job-detail-title"]',
            "h1",
        )
        company = get(
            '[data-automation="job-detail-company"]',
            '[data-automation="advertiser-name"]',
            '[class*="company"]',
        )
        location = (
            get('[data-automation="job-detail-location"]')
            or find_after_label("Location", "Work Location")
        )
        salary = get(
            '[data-automation="job-detail-salary"]',
            '[data-automation="salary"]',
        )
        deadline = get(
            '[data-automation="job-detail-expiry"]',
            '[data-automation="job-expiry-date"]',
        )
        description = (
            get('[data-automation="jobAdDetails"]')
            or get('[data-automation="job-detail-description"]')
            or get("article", "main")
        )

        return JobListing(
            source=self.source_name,
            url=url,
            title=title,
            company=company,
            location=location,
            salary=salary,
            deadline=deadline,
            description=description[:1500],
        )
