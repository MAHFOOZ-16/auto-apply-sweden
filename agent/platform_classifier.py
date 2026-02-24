"""Detect which ATS / career platform a job page uses.

Returns a platform dict with:
  name:        str   – canonical name
  mode:        str   – AUTO / TRY_AUTO / ASSIST
  multi_step:  bool  – whether the form typically has multiple pages
  max_steps:   int   – expected number of form steps
"""

import logging
from typing import Any, Dict

from playwright.sync_api import Page

logger = logging.getLogger("agent.platform_classifier")

# ═══════════════════════════════════════════════════════
#  PLATFORM SIGNATURES
# ═══════════════════════════════════════════════════════

PLATFORMS: Dict[str, Dict[str, Any]] = {
    "teamtailor": {
        "url_patterns": [
            "teamtailor.com", "career.", "careers.",
            "/jobs/", "apply.teamtailor",
        ],
        "html_signals": [
            "teamtailor", "Kandidatinformation",
            "data-controller=\"application-form\"",
            "powered by Teamtailor",
        ],
        "mode": "AUTO",
        "multi_step": True,
        "max_steps": 3,
    },
    "varbi": {
        "url_patterns": ["varbi.com"],
        "html_signals": [
            "varbi", "Rekryteringsverktyg",
            "varbi-application",
        ],
        "mode": "AUTO",
        "multi_step": False,
        "max_steps": 1,
    },
    "framtiden_zoho": {
        "url_patterns": [
            "framtiden.se", "zoho.com/recruit",
            "jobs.zoho", "recruit.zoho",
        ],
        "html_signals": [
            "Framtidenkontor", "Zoho Recruit",
            "Enkel ansökan", "zoho.com/recruit",
        ],
        "mode": "AUTO",
        "multi_step": True,
        "max_steps": 2,
    },
    "reachmee": {
        "url_patterns": ["reachmee.com"],
        "html_signals": ["ReachMee", "reachmee"],
        "mode": "AUTO",
        "multi_step": False,
        "max_steps": 1,
    },
    "jobylon": {
        "url_patterns": ["jobylon.com"],
        "html_signals": ["jobylon"],
        "mode": "AUTO",
        "multi_step": False,
        "max_steps": 1,
    },
    "workday": {
        "url_patterns": [
            "myworkday", "wd3.", "wd5.", "wd1.",
            "workday.com",
        ],
        "html_signals": ["workday", "Workday"],
        "mode": "TRY_AUTO",
        "multi_step": True,
        "max_steps": 5,
    },
    "successfactors": {
        "url_patterns": [
            "successfactors", "jobs.sap.com",
            "performancemanager",
        ],
        "html_signals": ["SuccessFactors", "SAP"],
        "mode": "TRY_AUTO",
        "multi_step": True,
        "max_steps": 4,
    },
    "smartrecruiters": {
        "url_patterns": ["smartrecruiters.com", "jobs.smartrecruiters"],
        "html_signals": ["SmartRecruiters"],
        "mode": "TRY_AUTO",
        "multi_step": True,
        "max_steps": 3,
    },
    "lever": {
        "url_patterns": ["lever.co", "jobs.lever"],
        "html_signals": ["lever.co", "Lever"],
        "mode": "TRY_AUTO",
        "multi_step": False,
        "max_steps": 1,
    },
    "greenhouse": {
        "url_patterns": ["greenhouse.io", "boards.greenhouse"],
        "html_signals": ["greenhouse", "Greenhouse"],
        "mode": "TRY_AUTO",
        "multi_step": True,
        "max_steps": 3,
    },
    "linkedin": {
        "url_patterns": [
            "linkedin.com", "linkedin.se",
            "lnkd.in",
        ],
        "html_signals": ["linkedin"],
        "mode": "SKIP",
        "multi_step": False,
        "max_steps": 0,
    },
}

UNKNOWN_PLATFORM: Dict[str, Any] = {
    "name": "unknown",
    "mode": "TRY_AUTO",
    "multi_step": True,   # assume multi-step to be safe
    "max_steps": 3,
}


def classify_platform(page: Page) -> Dict[str, Any]:
    """Detect the ATS platform from page URL and HTML content.
    
    Returns a dict: {name, mode, multi_step, max_steps}
    """
    url = page.url.lower()

    # ── Check URL patterns first (fast) ──
    for name, sig in PLATFORMS.items():
        for pattern in sig["url_patterns"]:
            if pattern in url:
                logger.info("  🏷  Platform: %s (url: %s)", name, pattern)
                return {
                    "name": name,
                    "mode": sig["mode"],
                    "multi_step": sig["multi_step"],
                    "max_steps": sig["max_steps"],
                }

    # ── Check HTML signals (slower, but catches embedded platforms) ──
    try:
        html = page.content()[:10000].lower()
    except Exception:
        html = ""

    for name, sig in PLATFORMS.items():
        for signal in sig["html_signals"]:
            if signal.lower() in html:
                logger.info("  🏷  Platform: %s (html: %s)", name, signal)
                return {
                    "name": name,
                    "mode": sig["mode"],
                    "multi_step": sig["multi_step"],
                    "max_steps": sig["max_steps"],
                }

    # ── Check meta tags ──
    try:
        meta = page.locator("meta[name='generator'], meta[property='og:site_name']").all()
        for m in meta:
            content = (m.get_attribute("content") or "").lower()
            for name, sig in PLATFORMS.items():
                for signal in sig["html_signals"]:
                    if signal.lower() in content:
                        logger.info("  🏷  Platform: %s (meta: %s)", name, content)
                        return {
                            "name": name,
                            "mode": sig["mode"],
                            "multi_step": sig["multi_step"],
                            "max_steps": sig["max_steps"],
                        }
    except Exception:
        pass

    logger.info("  🏷  Platform: unknown")
    return dict(UNKNOWN_PLATFORM)