"""Fetch jobs from the Jobtech / Arbetsförmedlingen JobSearch API."""

import logging
import time
from datetime import datetime, date
from typing import Any, Dict

import requests

from agent.db import AgentDB

logger = logging.getLogger("agent.fetcher")

# Data/IT occupation field in Arbetsförmedlingen taxonomy
OCCUPATION_FIELD_IT = "apaJ_2ja_LuF"

# Targeted search queries — one per role category for better results
SEARCH_QUERIES = [
    "software developer",
    "mjukvaruutvecklare",       # Swedish: software developer
    "systemutvecklare",          # Swedish: systems developer
    "backend developer",
    "frontend developer",
    "fullstack developer",
    "python developer",
    "java developer",
    "machine learning",
    "AI engineer",
    "data engineer",
    "data scientist",
    "devops engineer",
    "cloud engineer",
    "cybersecurity",
    "security engineer",
    "IT säkerhet",               # Swedish: IT security
    "web developer",
    "programmerare",             # Swedish: programmer
]


class JobFetcher:
    """Discovers jobs via the open JobSearch API (no key required)."""

    def __init__(self, db: AgentDB, config: Dict[str, Any]):
        self.db = db
        self.cfg = config
        self.base = config.get(
            "jobsearch_api_base", "https://jobsearch.api.jobtechdev.se"
        )
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "JobAgent/1.0 (private use)",
        })

    def fetch_round(self) -> int:
        """Run one discovery round with multiple targeted queries."""
        new_total = 0
        seen_queries = 0

        for query in SEARCH_QUERIES:
            new = self._search_single(query, limit=50)
            new_total += new
            seen_queries += 1

            # Polite pacing between queries
            if seen_queries < len(SEARCH_QUERIES):
                time.sleep(0.5)

        # Also do one broad IT-field search without keywords
        new_total += self._search_single("", limit=100, field_only=True)

        logger.info(
            "Fetch round complete – %d new jobs from %d queries",
            new_total, seen_queries + 1,
        )
        return new_total

    def _search_single(self, query: str, limit: int = 50,
                       field_only: bool = False) -> int:
        """Run one search query. Return count of new jobs inserted."""
        params: Dict[str, Any] = {
            "offset": 0,
            "limit": limit,
            "sort": "pubdate-desc",
        }

        if query:
            params["q"] = query

        # Always filter to IT/Data occupation field
        params["occupation-field"] = OCCUPATION_FIELD_IT

        url = f"{self.base}/search"
        try:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", [])
            total_available = data.get("total", {})
            if isinstance(total_available, dict):
                total_available = total_available.get("value", len(hits))

            new_count = 0
            for raw in hits:
                if self._insert_if_new(raw):
                    new_count += 1

            if new_count > 0 or query:
                logger.info(
                    "  Query '%s': %d hits, %d new",
                    query or "(field-only)", len(hits), new_count,
                )
            return new_count

        except requests.RequestException as exc:
            logger.error("API request failed for '%s': %s", query, exc)
            return 0

    def _insert_if_new(self, raw: Dict) -> bool:
        job_id = raw.get("id", "")
        if not job_id:
            return False

        webpage_url = raw.get("webpage_url", "")
        app_details = raw.get("application_details", {})
        application_url = ""
        if isinstance(app_details, dict):
            application_url = app_details.get("url", "")

        url = (
            application_url
            or webpage_url
            or f"https://arbetsformedlingen.se/platsbanken/annonser/{job_id}"
        )

        if self.db.job_exists(job_id) or self.db.url_exists(url):
            return False

        # Skip jobs whose application deadline has already passed
        if self._deadline_passed(raw):
            return False

        employer = raw.get("employer", {})
        if not isinstance(employer, dict):
            employer = {}
        workplace = raw.get("workplace_address", {})
        if not isinstance(workplace, dict):
            workplace = {}

        job = {
            "job_id": job_id,
            "url": url,
            "title": raw.get("headline", ""),
            "company": employer.get("name", "Unknown"),
            "location": (
                workplace.get("municipality", "")
                or workplace.get("region", "")
                or "Sweden"
            ),
            "language_hint": self._detect_language(raw),
            "description": self._extract_description(raw),
        }

        inserted = self.db.insert_job(job)
        if inserted:
            logger.debug(
                "    New: '%s' at '%s' (%s)",
                job["title"][:60], job["company"], job["location"],
            )
        return inserted

    @staticmethod
    def _extract_description(raw: Dict) -> str:
        desc = raw.get("description", {})
        if isinstance(desc, dict):
            # Prefer plain text, fall back to formatted
            return (
                desc.get("text", "")
                or desc.get("text_formatted", "")
                or ""
            )
        if isinstance(desc, str):
            return desc
        return ""

    @staticmethod
    def _detect_language(raw: Dict) -> str:
        desc_text = ""
        desc = raw.get("description", {})
        if isinstance(desc, dict):
            desc_text = desc.get("text", "") or ""
        en_markers = [
            "we are looking", "you will", "requirements",
            "qualifications", "about us", "the role",
            "what we offer", "your profile", "responsibilities",
        ]
        lower = desc_text.lower()
        en_hits = sum(1 for m in en_markers if m in lower)
        return "en" if en_hits >= 2 else "sv"

    @staticmethod
    def _deadline_passed(raw: Dict) -> bool:
        """Return True if the application deadline is in the past."""
        today = date.today()

        # 1) Check top-level API field
        deadline_str = raw.get("application_deadline", "")

        # 2) Check nested application_details.deadline
        if not deadline_str:
            app_details = raw.get("application_details", {})
            if isinstance(app_details, dict):
                deadline_str = app_details.get("deadline", "")

        # 3) Try to parse ISO date from API field
        if deadline_str:
            try:
                dl = deadline_str.replace("Z", "+00:00")
                # Handle both "2026-01-31" and "2026-01-31T23:59:59"
                if "T" in dl:
                    deadline_dt = datetime.fromisoformat(dl).date()
                else:
                    deadline_dt = date.fromisoformat(dl[:10])
                if deadline_dt < today:
                    logger.debug("    Skipping: deadline %s < today %s",
                                 deadline_dt, today)
                    return True
                return False
            except (ValueError, TypeError):
                pass

        # 4) Fallback: scan description text for deadline patterns
        desc = raw.get("description", {})
        desc_text = ""
        if isinstance(desc, dict):
            desc_text = desc.get("text", "") or ""
        elif isinstance(desc, str):
            desc_text = desc

        if desc_text:
            import re
            # Match patterns like "Application deadline: January 31st 2026"
            # or "Sista ansökningsdag: 2026-01-31"
            patterns = [
                # ISO date in text
                r"(?:deadline|sista\s+ans[öo]knings\w*)[:\s]*(\d{4}-\d{2}-\d{2})",
                # "January 31st 2026" or "January 31 2026"
                r"(?:deadline|sista\s+ans[öo]knings\w*)[:\s]*"
                r"(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})",
                # "31 January 2026" or "31 januari 2026"
                r"(?:deadline|sista\s+ans[öo]knings\w*)[:\s]*"
                r"(\d{1,2})\s+(\w+)\s+(\d{4})",
            ]
            lower_text = desc_text.lower()

            # Pattern 1: ISO
            m = re.search(patterns[0], lower_text, re.IGNORECASE)
            if m:
                try:
                    dl = date.fromisoformat(m.group(1))
                    if dl < today:
                        logger.debug("    Skipping: desc deadline %s < today", dl)
                        return True
                    return False
                except ValueError:
                    pass

            # Pattern 2: "Month Day Year"
            m = re.search(patterns[1], lower_text, re.IGNORECASE)
            if m:
                try:
                    month_str, day_str, year_str = m.group(1), m.group(2), m.group(3)
                    dl = _parse_month_day_year(month_str, int(day_str), int(year_str))
                    if dl and dl < today:
                        logger.debug("    Skipping: desc deadline %s < today", dl)
                        return True
                except (ValueError, TypeError):
                    pass

            # Pattern 3: "Day Month Year"
            m = re.search(patterns[2], lower_text, re.IGNORECASE)
            if m:
                try:
                    day_str, month_str, year_str = m.group(1), m.group(2), m.group(3)
                    dl = _parse_month_day_year(month_str, int(day_str), int(year_str))
                    if dl and dl < today:
                        logger.debug("    Skipping: desc deadline %s < today", dl)
                        return True
                except (ValueError, TypeError):
                    pass

        return False  # no deadline found or future = allow


def _parse_month_day_year(month_str: str, day: int, year: int):
    """Parse month name (English or Swedish) + day + year into date."""
    months = {
        "january": 1, "februari": 2, "february": 2, "mars": 3, "march": 3,
        "april": 4, "maj": 5, "may": 5, "juni": 6, "june": 6,
        "juli": 7, "july": 7, "augusti": 8, "august": 8,
        "september": 9, "oktober": 10, "october": 10,
        "november": 11, "december": 12,
    }
    m = months.get(month_str.lower())
    if m:
        try:
            return date(year, m, day)
        except ValueError:
            pass
    return None