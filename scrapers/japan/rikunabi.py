from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, JobListing, DEBUG


class RikunabiScraper(BaseScraper):
    """
    Scraper for Rikunabi 2026 (リクナビ2026), Japan's new-graduate job board.

    Rikunabi relaunched on 2026-04-01 with a new URL structure:
      Old: /n/?mode=selection  →  /n/selection/job_descriptions/...
      New: /2026/s/            →  /2026/company/rXXXXX/
    """

    source_name = "rikunabi"
    BASE_URL    = "https://job.rikunabi.com"
    LIST_URL    = "https://job.rikunabi.com/2026/s/"

    async def get_listing_urls(self) -> list[str]:
        await self.page.goto(self.LIST_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            await self.page.wait_for_selector("a[href]", timeout=10000)
        except Exception:
            pass
        await self.page.wait_for_timeout(4000)

        html = await self.page.content()
        soup = BeautifulSoup(html, "html.parser")
        self._debug_links(soup, "rikunabi-list")

        links = []
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            # Match company pages: /2026/company/rXXXXX/
            if href and "/2026/company/r" in href:
                full = href if href.startswith("http") else self.BASE_URL + href
                # Normalise to the root company page (no sub-paths)
                base = full.split("/2026/company/")[0] + "/2026/company/"
                company_id = full.split("/2026/company/")[1].split("/")[0]
                company_url = base + company_id + "/"
                if company_url not in links:
                    links.append(company_url)

        return links[:20]

    _debug_detail_done = False

    async def parse_listing(self, url: str) -> JobListing:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await self.page.wait_for_selector("h1", timeout=10000)
        except Exception:
            pass
        await self.page.wait_for_timeout(3000)

        html = await self.page.content()
        soup = BeautifulSoup(html, "html.parser")

        if DEBUG and not RikunabiScraper._debug_detail_done:
            self._debug_detail(soup)
            self._debug_links(soup, "rikunabi-detail")
            RikunabiScraper._debug_detail_done = True

        def get(*selectors: str) -> str:
            for sel in selectors:
                el = soup.select_one(sel)
                if el:
                    return self._clean(el.get_text())
            return ""

        def find_after_label(*keywords: str) -> str:
            for keyword in keywords:
                for label_el in soup.find_all(string=lambda t, k=keyword: t and k in t):
                    parent = label_el.parent
                    sibling = parent.find_next_sibling()
                    if sibling:
                        text = self._clean(sibling.get_text())
                        if text and len(text) > 1:
                            return text
                    grandparent = parent.parent
                    if grandparent:
                        next_el = grandparent.find_next_sibling()
                        if next_el:
                            text = self._clean(next_el.get_text())
                            if text and len(text) > 1:
                                return text
            return ""

        # Company name lives in h1 on the company profile; job title can be
        # "採用情報" (Recruitment Information) or a specific role title.
        company  = get("h1", '[class*="companyName"]', '[class*="company-name"]', '[class*="Corp"]')
        title    = (
            get('[class*="jobTitle"]', '[class*="job-title"]', '[class*="position"]')
            or find_after_label("職種", "募集職種", "採用職種")
            or company  # fall back to company name
        )
        location = find_after_label("勤務地", "勤務場所", "就業場所")
        salary   = find_after_label("給与", "月給", "年収", "賃金", "給料")
        deadline = find_after_label("締切", "応募締切", "エントリー締切", "エントリー期間")
        description = get(
            "article",
            "main",
            '[class*="companyDetail"]',
            '[class*="jobDetail"]',
            '[class*="description"]',
            '[class*="Overview"]',
        )

        return JobListing(
            source      = self.source_name,
            url         = url,
            title       = title,
            company     = company,
            location    = location,
            salary      = salary,
            deadline    = deadline,
            description = description[:1500],
        )
