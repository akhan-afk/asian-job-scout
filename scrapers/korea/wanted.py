from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, JobListing, DEBUG


class WantedScraper(BaseScraper):
    """
    Scraper for Wanted (원티드), one of Korea's most popular job platforms.
    Site: https://www.wanted.co.kr

    Targets the main job listing page sorted by most recent.
    Individual listing URLs follow: https://www.wanted.co.kr/wd/XXXXX
    """

    source_name = "wanted"
    BASE_URL    = "https://www.wanted.co.kr"
    LIST_URL    = "https://www.wanted.co.kr/wdlist"

    async def get_listing_urls(self) -> list[str]:
        await self.page.goto(self.LIST_URL, wait_until="commit", timeout=60000)
        try:
            await self.page.wait_for_selector("a[href]", timeout=12000)
        except Exception:
            pass
        await self.page.wait_for_timeout(5000)

        title = await self.page.title()
        print(f"[wanted] Page loaded: {title}")

        html = await self.page.content()
        soup = BeautifulSoup(html, "html.parser")
        self._debug_links(soup, "wanted")

        links = []
        seen: set[str] = set()
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if not href:
                continue
            # Wanted job detail pages: /wd/XXXXX (numeric ID, 4–6 digits)
            if not href.startswith("/wd/") and "/wd/" not in href:
                continue
            # Skip non-numeric suffixes (category pages like /wdlist/...)
            path = href.split("?")[0].rstrip("/")
            job_id = path.split("/wd/")[-1]
            if not job_id.isdigit():
                continue
            full = self.BASE_URL + "/wd/" + job_id
            if full not in seen:
                seen.add(full)
                links.append(full)

        return links[:20]

    _debug_detail_done = False

    async def parse_listing(self, url: str) -> JobListing:
        await self.page.goto(url, wait_until="commit", timeout=60000)
        try:
            await self.page.wait_for_selector("h1", timeout=12000)
        except Exception:
            pass
        await self.page.wait_for_timeout(4000)

        html = await self.page.content()
        soup = BeautifulSoup(html, "html.parser")

        if DEBUG and not WantedScraper._debug_detail_done:
            self._debug_detail(soup)
            WantedScraper._debug_detail_done = True

        def get(*selectors: str) -> str:
            for sel in selectors:
                el = soup.select_one(sel)
                if el:
                    return self._clean(el.get_text())
            return ""

        def find_after_label(*keywords: str) -> str:
            """Find value text that follows a label element containing any of the keywords."""
            for keyword in keywords:
                # <dt>keyword</dt> → <dd>value</dd>
                for dt in soup.find_all("dt"):
                    if keyword in dt.get_text():
                        dd = dt.find_next_sibling("dd")
                        if dd:
                            text = self._clean(dd.get_text())
                            if text and len(text) > 1:
                                return text

                # <th>keyword</th> → <td>value</td>
                for th in soup.find_all("th"):
                    if keyword in th.get_text():
                        td = th.find_next_sibling("td")
                        if td:
                            text = self._clean(td.get_text())
                            if text and len(text) > 1:
                                return text

                # Fallback: text node containing keyword → next sibling
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

        title = get(
            '[class*="JobHeader_JobHeader__title"]',
            '[class*="position_title"]',
            '[class*="job_title"]',
            "h1",
        )
        company = get(
            '[class*="JobHeader_JobHeader__company"]',
            '[class*="company_name"]',
            '[class*="JobHeader"] a',
        )
        location = find_after_label("근무지역", "근무위치", "위치")
        salary   = find_after_label("급여", "연봉", "월급", "보상")
        deadline = find_after_label("마감일", "접수마감", "지원마감")
        description = get(
            '[class*="JobDescription"]',
            '[class*="job_description"]',
            '[class*="position_desc"]',
            "article",
            "main",
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
