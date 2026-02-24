"""Rank, filter, and queue discovered jobs."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Set

from agent import States
from agent.db import AgentDB

logger = logging.getLogger("agent.ranker")


class JobRanker:
    """Score jobs against the user's profile and push passing ones to queue."""

    def __init__(self, db: AgentDB, config: Dict[str, Any]):
        self.db = db
        self.cfg = config
        self.fit_threshold = config.get("fit_threshold", 40)
        self.company_weekly_cap = config.get("company_weekly_cap", 2)
        self.company_monthly_cap = config.get("company_monthly_cap", 6)

        tax_path = Path(config.get("taxonomy_path", "data/skill_taxonomy.json"))
        with open(tax_path, "r", encoding="utf-8") as f:
            self.taxonomy = json.load(f)

        self.role_keywords: Dict[str, List[str]] = self.taxonomy.get(
            "role_keywords", {}
        )
        self.skill_keywords: List[str] = self.taxonomy.get("skill_keywords", [])
        self.exclude_kw: List[str] = self.taxonomy.get("exclude_keywords", [])
        self.title_exclude: List[str] = self.taxonomy.get(
            "title_exclude_patterns", []
        )

        cv_path = Path(config.get("master_cv_path", "data/master_cv.json"))
        with open(cv_path, "r", encoding="utf-8") as f:
            cv = json.load(f)
        self.my_skills: Set[str] = set()
        for group in cv.get("skills", {}).values():
            if isinstance(group, list):
                self.my_skills.update(s.lower() for s in group)
        logger.info("Loaded %d personal skills for matching", len(self.my_skills))

    def rank_discovered(self) -> int:
        jobs = self.db.get_jobs_by_status(States.DISCOVERED)
        if not jobs:
            return 0
        queued = 0
        for job in jobs:
            if self._evaluate(job) == "QUEUE":
                queued += 1
        logger.info("Ranked %d discovered → %d queued", len(jobs), queued)
        return queued

    def _evaluate(self, job: Dict) -> str:
        title_raw = job.get("title") or ""
        title = title_raw.lower()
        desc = (job.get("description") or "").lower()
        company = job.get("company", "")
        job_id = job["job_id"]

        logger.info("  Evaluating: '%s' at '%s' (%d chars)",
                     title_raw, company, len(desc))

        # 1) Title exclusion (senior, director, etc.)
        for pat in self.title_exclude:
            if pat.lower() in title:
                self.db.update_job_status(job_id, States.SKIPPED_LOW_FIT, fit_score=0)
                self.db.log_event(job_id, States.SKIPPED_LOW_FIT,
                                  f"Title excluded: '{pat}'")
                logger.info("    → SKIP title-exclude '%s'", pat)
                return "SKIP"

        # 2) Content exclusion
        for kw in self.exclude_kw:
            if kw.lower() in desc:
                self.db.update_job_status(job_id, States.SKIPPED_LOW_FIT, fit_score=0)
                self.db.log_event(job_id, States.SKIPPED_LOW_FIT,
                                  f"Excluded kw: '{kw}'")
                logger.info("    → SKIP excluded-kw '%s'", kw)
                return "SKIP"

        # 3) Role relevance – must match at least 1 category
        matched_cats = self._find_matching_categories(title, desc)
        if not matched_cats:
            self.db.update_job_status(job_id, States.SKIPPED_LOW_FIT, fit_score=0)
            self.db.log_event(job_id, States.SKIPPED_LOW_FIT,
                              "No role keyword match")
            logger.info("    → SKIP no role-keyword match")
            return "SKIP"

        logger.info("    Matched categories: %s", matched_cats)

        # 4) Repost
        loc = job.get("location", "")
        if self.db.repost_exists(company, title_raw, loc):
            self.db.update_job_status(job_id, States.SKIPPED_DUPLICATE)
            self.db.log_event(job_id, States.SKIPPED_DUPLICATE, "Repost")
            return "SKIP"

        # 5) Company cooldown
        weekly = self.db.company_applications_since(company, 7)
        monthly = self.db.company_applications_since(company, 30)
        if weekly >= self.company_weekly_cap or monthly >= self.company_monthly_cap:
            self.db.update_job_status(job_id, States.SKIPPED_COOLDOWN)
            self.db.log_event(job_id, States.SKIPPED_COOLDOWN,
                              f"Cooldown: {weekly}/wk {monthly}/mo")
            return "SKIP"

        # 6) Compute fit
        fit = self._compute_fit(title, desc, matched_cats)
        if fit < self.fit_threshold:
            self.db.update_job_status(job_id, States.SKIPPED_LOW_FIT, fit_score=fit)
            self.db.log_event(job_id, States.SKIPPED_LOW_FIT,
                              f"Fit {fit:.0f} < {self.fit_threshold}")
            logger.info("    → SKIP fit %.0f < %d", fit, self.fit_threshold)
            return "SKIP"

        # 7) Queue
        self.db.update_job_status(job_id, States.QUEUED, fit_score=fit)
        self.db.enqueue(job_id, priority=int(fit))
        self.db.log_event(job_id, States.QUEUED, f"Queued fit={fit:.0f}")
        logger.info("    ✓ QUEUED fit=%.0f '%s' at %s", fit, title_raw, company)
        return "QUEUE"

    def _find_matching_categories(self, title: str, desc: str) -> List[str]:
        combined = f"{title} {desc}"
        matched = []
        for cat_name, keywords in self.role_keywords.items():
            for kw in keywords:
                if kw.lower() in combined:
                    matched.append(cat_name)
                    break
        return matched

    def _compute_fit(self, title: str, desc: str,
                     matched_cats: List[str]) -> float:
        """
        0-100 fit score.
        Base 30 for ≥1 category + bonus per extra + up to 55 from skill overlap.
        Graduate/intern matches get +10 bonus.
        """
        combined = f"{title} {desc}"
        desc_skills: Set[str] = set()
        for sk in self.skill_keywords:
            if sk.lower() in combined:
                desc_skills.add(sk.lower())
        overlap = desc_skills & self.my_skills
        skill_ratio = len(overlap) / len(desc_skills) if desc_skills else 0.3

        n = len(matched_cats)
        role_score = 30.0 + min((n - 1) * 5.0, 15.0)
        skill_score = skill_ratio * 55.0

        # Bonus for grad/intern/junior roles
        bonus = 0.0
        if "graduate_intern" in matched_cats:
            bonus = 10.0

        total = role_score + skill_score + bonus

        logger.info("    FIT: cats=%d(%.0f) skills=%d/%d overlap=%d "
                     "ratio=%.2f(%.0f) bonus=%.0f total=%.0f",
                     n, role_score, len(desc_skills), len(self.skill_keywords),
                     len(overlap), skill_ratio, skill_score, bonus, total)

        return min(100.0, max(0.0, total))
