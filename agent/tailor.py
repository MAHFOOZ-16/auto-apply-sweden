"""Tailor resume and cover letter content for each job (LaTeX output)."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from agent import States
from agent.db import AgentDB

logger = logging.getLogger("agent.tailor")


def _latex_escape(text: str) -> str:
    """Escape special LaTeX characters in user-data strings."""
    if not text:
        return ""
    replacements = [
        ("\\", r"\textbackslash{}"),
        ("&", r"\&"),
        ("%", r"\%"),
        ("$", r"\$"),
        ("#", r"\#"),
        ("_", r"\_"),
        ("{", r"\{"),
        ("}", r"\}"),
        ("~", r"\textasciitilde{}"),
        ("^", r"\textasciicircum{}"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


# ── Location → Address mapping ──
MALMO_CITIES = {"malmö", "malmo", "lund", "helsingborg", "landskrona",
                "trelleborg", "ystad", "eslöv", "staffanstorp", "lomma",
                "kävlinge", "höganäs", "skåne", "burlöv", "svedala"}
STOCKHOLM_CITIES = {"stockholm", "solna", "sundbyberg", "kista", "nacka",
                    "huddinge", "södertälje", "järfälla", "täby", "haninge",
                    "lidingö", "upplands väsby", "sigtuna"}


def _pick_address(job_location: str, truth: Dict) -> str:
    """Pick the right address based on job location."""
    addresses = truth.get("addresses", {})
    loc_lower = (job_location or "").lower()

    for city in MALMO_CITIES:
        if city in loc_lower:
            return addresses.get("malmo", {}).get("full", "Malmö, Sweden")
    for city in STOCKHOLM_CITIES:
        if city in loc_lower:
            return addresses.get("stockholm", {}).get("full", "Stockholm, Sweden")
    # Default to Malmö
    return addresses.get("default", {}).get("full", "Sweden")


def _pick_city(job_location: str, truth: Dict) -> str:
    """Pick city name for form filling."""
    loc_lower = (job_location or "").lower()
    for city in STOCKHOLM_CITIES:
        if city in loc_lower:
            return truth.get("addresses", {}).get("stockholm", {}).get("city", "Stockholm")
    return truth.get("addresses", {}).get("default", {}).get("city", "Malmö")


def _pick_postal(job_location: str, truth: Dict) -> str:
    loc_lower = (job_location or "").lower()
    for city in STOCKHOLM_CITIES:
        if city in loc_lower:
            return truth.get("addresses", {}).get("stockholm", {}).get("postal_code", "191 65")
    return truth.get("addresses", {}).get("default", {}).get("postal_code", "217 66")


def _pick_street(job_location: str, truth: Dict) -> str:
    loc_lower = (job_location or "").lower()
    for city in STOCKHOLM_CITIES:
        if city in loc_lower:
            return truth.get("addresses", {}).get("stockholm", {}).get("street", "Kungsgatan 5")
    return truth.get("addresses", {}).get("default", {}).get("street", "Storgatan 12")


class Tailor:
    """Generate ATS-optimized resume data and cover letter text per job."""

    THESIS_URL = "https://urn.kb.se/resolve?urn=urn:nbn:se:bth-28884"

    def __init__(self, db: AgentDB, config: Dict[str, Any]):
        self.db = db
        self.cfg = config

        cv_path = Path(config.get("master_cv_path", "data/master_cv.json"))
        with open(cv_path, "r", encoding="utf-8") as f:
            self.cv = json.load(f)

        tax_path = Path(config.get("taxonomy_path", "data/skill_taxonomy.json"))
        with open(tax_path, "r", encoding="utf-8") as f:
            self.taxonomy = json.load(f)

        truth_path = Path(config.get("truth_path", "data/truth.json"))
        with open(truth_path, "r", encoding="utf-8") as f:
            self.truth = json.load(f)

    # ──────────────────────────────────────────────
    def tailor_for_job(self, job: Dict) -> Dict[str, Any]:
        job_id = job["job_id"]
        self.db.update_job_status(job_id, States.TAILORING)
        self.db.log_event(job_id, States.TAILORING, "Tailoring started")

        title = job.get("title", "Software Engineer")
        company = job.get("company", "the company")
        location = job.get("location", "Sweden")
        description = job.get("description", "")

        keywords = self._extract_keywords(description)
        resume_data = self._build_resume_data(title, description, keywords)
        cover_data = self._build_cover_letter_data(
            title, company, location, description, keywords
        )
        # Also produce plain-text cover letter for "Personligt brev" textarea
        cover_text = self._build_cover_letter_plaintext(
            title, company, location, description, keywords
        )
        job_json = {
            **job,
            "tailored_at": datetime.utcnow().isoformat(),
            "matched_keywords": keywords,
        }

        logger.info("Tailored for %s at %s (%d kw)", title, company, len(keywords))
        return {
            "resume_data": resume_data,
            "cover_letter_data": cover_data,
            "cover_letter_text": cover_text,
            "extracted_keywords": keywords,
            "job_json": job_json,
        }

    # ──────────────────────────────────────────────
    def _extract_keywords(self, description: str) -> List[str]:
        desc_lower = description.lower()
        found: List[str] = []
        for sk in self.taxonomy.get("skill_keywords", []):
            if sk.lower() in desc_lower:
                found.append(sk)
        for _cat, kws in self.taxonomy.get("role_keywords", {}).items():
            for kw in kws:
                if kw.lower() in desc_lower and kw not in found:
                    found.append(kw)
        return sorted(set(found))

    # ──────────────────────────────────────────────
    def _build_resume_data(self, job_title: str, description: str,
                           keywords: List[str]) -> Dict[str, Any]:
        cv = self.cv
        contact = cv.get("contact", {})
        skills = self._organise_skills(cv.get("skills", {}), keywords)
        experience = self._prioritise_experience(cv.get("experience", []), keywords)
        projects = self._prioritise_projects(cv.get("projects", []), keywords)

        # Build achievements with hyperlinked paper
        achievements = []
        for a in cv.get("achievements", []):
            if "AI-driven Optimization Framework" in a:
                esc = _latex_escape(a)
                # Replace the title with a hyperlink
                esc = esc.replace(
                    "'AI-driven Optimization Framework for Construction Site Ecosystems'",
                    r"\href{" + self.THESIS_URL + r"}{AI-driven Optimization Framework for Construction Site Ecosystems}"
                )
                achievements.append(esc)
            else:
                achievements.append(_latex_escape(a))

        return {
            "name": _latex_escape(cv.get("name", "")),
            "cv_email": contact.get("email", ""),
            "phone": contact.get("phone", ""),
            "location": _latex_escape(contact.get("location", "")),
            "linkedin": contact.get("linkedin", ""),
            "linkedin_url": contact.get("linkedin_url", ""),
            "github": contact.get("github", ""),
            "github_url": contact.get("github_url", ""),
            "skills": skills,
            "education": [
                {
                    "institution": _latex_escape(e.get("institution", "")),
                    "location": _latex_escape(e.get("location", "")),
                    "degree": _latex_escape(e.get("degree", "")),
                    "start": e.get("start", ""),
                    "end": e.get("end", ""),
                }
                for e in cv.get("education", [])
            ],
            "experience": experience,
            "projects": projects,
            "certifications": [_latex_escape(c) for c in cv.get("certifications", [])],
            "achievements": achievements,
            "extracurricular": [_latex_escape(e) for e in cv.get("extracurricular", [])],
        }

    # ──────────────────────────────────────────────
    def _build_cover_letter_data(self, job_title: str, company: str,
                                 location: str, description: str,
                                 keywords: List[str]) -> Dict[str, Any]:
        contact = self.cv.get("contact", {})
        _form = self.truth.get("form_defaults", {})
        top_skills = keywords[:5] if keywords else ["Python", "machine learning"]
        address = _pick_address(location, self.truth)

        return {
            "name": _latex_escape(self.cv.get("name", "")),
            "email": self.truth.get("personal", {}).get("cv_email",
                     contact.get("email", "")),
            "phone": contact.get("phone", ""),
            "address": _latex_escape(address),
            "today_date": datetime.utcnow().strftime("%B %d, %Y"),
            "company": _latex_escape(company),
            "job_location": _latex_escape(location),
            "job_title": _latex_escape(job_title),
            "primary_domain": _latex_escape(self._infer_domain(job_title)),
            "top_skills": [_latex_escape(s) for s in top_skills],
            "tailored_paragraph_1": _latex_escape(self._gen_para_1(job_title, keywords)),
            "tailored_paragraph_2": _latex_escape(self._gen_para_2(description, keywords)),
            "company_appeal": _latex_escape(self._gen_company_appeal(company, description)),
            "experience_highlight": _latex_escape(self._gen_experience_highlight(keywords)),
        }

    # ──────────────────────────────────────────────
    def _build_cover_letter_plaintext(self, job_title: str, company: str,
                                      location: str, description: str,
                                      keywords: List[str]) -> str:
        """Plain-text cover letter for 'Personligt brev' textareas."""
        _form = self.truth.get("form_defaults", {})
        contact = self.cv.get("contact", {})
        top_skills = keywords[:5] if keywords else ["Python", "machine learning"]
        address = _pick_address(location, self.truth)
        name = self.cv.get("name", "YOUR_NAME")
        email = self.truth.get("personal", {}).get("cv_email", contact.get("email", ""))

        p1 = self._gen_para_1(job_title, keywords)
        p2 = self._gen_para_2(description, keywords)
        company_appeal = self._gen_company_appeal(company, description)
        exp_highlight = self._gen_experience_highlight(keywords)

        return f"""{name}
{email}
{contact.get('phone', '')}
{address}

{datetime.utcnow().strftime('%B %d, %Y')}

Hiring Manager
{company}
{location}

Dear Hiring Manager,

I am writing to express my strong interest in the {job_title} position at {company}. With my background in {self._infer_domain(job_title)} and practical experience with {', '.join(top_skills)}, I am confident I can contribute meaningfully to your team.

{p1}

{p2}

I am particularly drawn to {company} because of {company_appeal}. My experience in {exp_highlight} aligns well with the requirements outlined in this role, and I am eager to bring my skills to your organization.

I am available to start immediately and I am open to relocating anywhere in Sweden.

Thank you for considering my application. I look forward to the opportunity to discuss how my experience and skills can contribute to {company}'s success.

Sincerely,
{name}"""

    # ── Skills reordering ──
    @staticmethod
    def _organise_skills(skills: Dict[str, List[str]],
                         keywords: List[str]) -> Dict[str, List[str]]:
        kw_lower = {k.lower() for k in keywords}
        organised: Dict[str, List[str]] = {}
        for group_name, items in skills.items():
            matched = [s for s in items if s.lower() in kw_lower]
            others = [s for s in items if s.lower() not in kw_lower]
            label = group_name.replace("_", " ").title()
            organised[label] = [_latex_escape(s) for s in (matched + others)]
        return organised

    # ── Experience reordering ──
    @staticmethod
    def _prioritise_experience(experience: List[Dict],
                               keywords: List[str]) -> List[Dict]:
        kw_set = {k.lower() for k in keywords}
        result = []
        for exp in experience:
            bullets = exp.get("bullets", [])
            scored = []
            for b in bullets:
                hits = sum(1 for k in kw_set if k in b.lower())
                scored.append((hits, b))
            scored.sort(key=lambda x: x[0], reverse=True)
            result.append({
                "role": _latex_escape(exp.get("role", "")),
                "company": _latex_escape(exp.get("company", "")),
                "location": _latex_escape(exp.get("location", "")),
                "start": exp.get("start", ""),
                "end": exp.get("end", ""),
                "bullets": [_latex_escape(b) for _, b in scored],
            })
        return result

    # ── Projects reordering ──
    @staticmethod
    def _prioritise_projects(projects: List[Dict],
                             keywords: List[str]) -> List[Dict]:
        kw_set = {k.lower() for k in keywords}

        def proj_score(p: Dict) -> int:
            text = (f"{p.get('name', '')} {p.get('tech', '')} "
                    f"{' '.join(p.get('bullets', []))}").lower()
            return sum(1 for k in kw_set if k in text)

        sorted_projects = sorted(projects, key=proj_score, reverse=True)
        return [
            {
                "name": _latex_escape(p.get("name", "")),
                "tech": _latex_escape(p.get("tech", "")),
                "date": p.get("date", ""),
                "bullets": [_latex_escape(b) for b in p.get("bullets", [])],
            }
            for p in sorted_projects
        ]

    # ── Paragraph generators (NO "hands-on", NO salary) ──
    def _gen_para_1(self, job_title: str, keywords: List[str]) -> str:
        exp = self.cv.get("experience", [])
        if exp:
            latest = exp[0]
            role = latest.get("role", "AI Researcher")
            company = latest.get("company", "YOUR_COMPANY")
            bullets = latest.get("bullets", [])
            highlight = (bullets[0].lower().rstrip(".")
                         if bullets else "developing AI-driven solutions")
            kw_str = (", ".join(keywords[:3])
                      if keywords else "AI, Python, and software development")
            return (
                f"In my recent role as {role} at {company}, I focused on "
                f"{highlight}. This experience has prepared me well for the "
                f"{job_title} position, where I can apply my expertise in "
                f"{kw_str}."
            )
        return (
            f"My background in computer science has equipped me with strong "
            f"skills in {', '.join(keywords[:3]) if keywords else 'software development'}, "
            f"making me a strong candidate for this {job_title} role."
        )

    def _gen_para_2(self, description: str, keywords: List[str]) -> str:
        if len(keywords) >= 3:
            return (
                f"I have practical experience with {keywords[0]}, {keywords[1]}, "
                f"and {keywords[2]}, applied across multiple projects and production "
                f"environments. I am passionate about writing clean, maintainable "
                f"code and continuously improving software quality through testing "
                f"and code reviews."
            )
        return (
            "Throughout my career, I have delivered robust software solutions while "
            "collaborating effectively with cross-functional teams. I take pride in "
            "writing clean, well-tested code and staying current with best practices."
        )

    @staticmethod
    def _gen_company_appeal(company: str, description: str) -> str:
        dl = description.lower()
        if "startup" in dl:
            return f"the innovative and fast-paced environment at {company}"
        if "enterprise" in dl or "large" in dl:
            return f"the scale and impact of {company}'s engineering challenges"
        return (f"the opportunity to contribute to {company}'s mission "
                f"and grow alongside the team")

    def _gen_experience_highlight(self, keywords: List[str]) -> str:
        kw_str = " ".join(keywords).lower()
        domains = []
        if any(k in kw_str for k in ["python", "java", "react", "node"]):
            domains.append("full-stack development")
        if any(k in kw_str for k in ["ml", "ai", "machine learning", "pytorch",
                                      "tensorflow"]):
            domains.append("AI and machine learning")
        if any(k in kw_str for k in ["aws", "docker", "kubernetes", "terraform"]):
            domains.append("cloud infrastructure")
        if any(k in kw_str for k in ["security", "owasp", "penetration",
                                      "nmap", "siem"]):
            domains.append("cybersecurity")
        return ", ".join(domains) if domains else "software engineering and AI"

    @staticmethod
    def _infer_domain(job_title: str) -> str:
        t = job_title.lower()
        if "data" in t:
            return "data engineering"
        if "ml" in t or "machine learning" in t or "ai" in t:
            return "AI and machine learning"
        if "devops" in t or "sre" in t or "platform" in t:
            return "DevOps and cloud infrastructure"
        if "security" in t or "cyber" in t:
            return "cybersecurity"
        return "software engineering"