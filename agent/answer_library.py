"""Answer library for tricky form questions.

Matches textarea / input labels to prepared answers using regex patterns.
Falls back to the truth.json form_defaults when no specific match is found.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("agent.answer_library")


class AnswerLibrary:
    """Match form field labels to prepared answers."""

    # ── Static patterns (checked first) ──
    STATIC_PATTERNS: List[Tuple[str, str]] = [
        # Visa / sponsorship / work permit
        (r"(visa|sponsorship|uppehållstillstånd|work\s*permit|arbetstillstånd"
         r"|sponsr|tillstånd.*arbet)",
         "Not required — I hold a valid Swedish residence permit"),

        # Notice period / availability / start date
        (r"(notice.?period|uppsägningstid|start.?dat|tillträd|när.*börja"
         r"|earliest.*start|available.*start|tillgänglig)",
         "Immediate — available to start right away"),

        # Salary
        (r"(salary|lön|löneanspråk|löneönskemål|compensation|wage"
         r"|lönekrav|desired.?salary|expected.?salary)",
         "35000"),

        # Relocation
        (r"(relocat|flytta|ort.*jobba|geographical|omplacering|willing.*move"
         r"|open.*location|beredd.*flytta)",
         "Yes, open to relocating anywhere in Sweden"),

        # Years of experience
        (r"(years?.?of?.?experience|år.*erfarenhet|how.?long.*work"
         r"|professional.*experience|arbetslivserfarenhet)",
         "1"),

        # Education level
        (r"(education.*level|utbildningsnivå|highest.*degree"
         r"|högsta.*utbildning|qualification)",
         "Master's degree in Computer Science, "
         "Your University, 202X"),

        # Current role / title
        (r"(current.*role|nuvarande.*tjänst|current.*position"
         r"|current.*title|senaste.*tjänst)",
         "Recent MSc Computer Science graduate from "
         "Your University"),

        # Driving license
        (r"(driv.*licen|körkort|driver)",
         "Nej / No"),

        # Swedish level (text field, not radio)
        (r"(swedish.*level|svenska.*nivå|swedish.*proficien)",
         "Basic — completed Introduction to Swedish (Grade B) "
         "at Your University"),

        # English level (text field)
        (r"(english.*level|engelska.*nivå|english.*proficien)",
         "Fluent — all university courses completed in English"),

        # Citizenship
        (r"(citizenship|medborgarskap|nationality|nationalitet)",
         "Valid Swedish work permit holder"),

        # How did you hear about us
        (r"(how.*hear|hur.*hitta|var.*såg|where.*find|källa"
         r"|source|how.*find.*us|hur.*fick.*reda)",
         "Arbetsförmedlingen (Swedish Public Employment Service)"),

        # Gender (text field)
        (r"(gender|kön)\b",
         "Man / Male"),

        # Date of birth
        (r"(date.*birth|födelsedatum|born|födelseår)",
         "2003-01-16"),
    ]

    # ── Project descriptions keyed by domain ──
    PROJECT_DOMAINS: Dict[str, str] = {
        "ai_ml": (
            "Master's thesis: Developed an AI-driven optimization framework "
            "for construction site ecosystems, integrating predictive "
            "maintenance and real-time resource allocation using Python, "
            "TensorFlow, and data analytics pipelines. Also implemented "
            "knowledge distillation for neural network compression on "
            "CIFAR-10, achieving effective knowledge transfer."
        ),
        "security": (
            "Hands-on coursework in malware analysis, software security, "
            "and secure software engineering. Projects include buffer "
            "overflow exploitation, YARA rule development for malware "
            "detection, DLL side-loading analysis, and threat modeling "
            "with STRIDE methodology for LLM-based architectures."
        ),
        "fullstack": (
            "Developed a full-stack bookstore web application using the "
            "MERN stack (MongoDB, Express.js, React, Node.js) with Docker "
            "containerization and Kubernetes orchestration for deployment. "
            "Also built an AI-powered electronic device recognition system "
            "using MobileNetV2 for e-waste recycling guidance."
        ),
        "cloud_devops": (
            "Applied Cloud Computing and Big Data coursework with "
            "hands-on experience in scalable deployment and data "
            "processing pipelines. Experience with Docker, Kubernetes, "
            "CI/CD practices, and cloud infrastructure management."
        ),
        "data": (
            "Knowledge distillation pipeline for neural network "
            "compression on CIFAR-10. Built decision support systems "
            "for travel optimization and port traffic management using "
            "data analytics, queuing theory, and predictive modeling."
        ),
        "embedded": (
            "Coursework in applied cloud computing and software "
            "engineering with a focus on system-level programming. "
            "Familiar with C/C++ from university projects and "
            "hands-on experience with Linux systems and networking."
        ),
        "general": (
            "Recent Computer Science master's graduate with a thesis "
            "on AI-driven optimization. Practical experience across "
            "AI/ML, cybersecurity, full-stack development, and cloud "
            "computing. Built 30+ projects spanning multiple domains "
            "during my studies."
        ),
    }

    # Domain keyword mapping
    DOMAIN_KEYWORDS: Dict[str, List[str]] = {
        "ai_ml": ["ai", "artificial intelligence", "machine learning",
                   "deep learning", "neural", "tensorflow", "pytorch",
                   "nlp", "computer vision", "ml engineer"],
        "security": ["security", "cyber", "penetration", "malware",
                     "vulnerability", "soc", "threat", "forensic",
                     "encryption", "säkerhet"],
        "fullstack": ["fullstack", "full-stack", "frontend", "backend",
                      "react", "node", "web", "javascript", "typescript",
                      "angular", "vue"],
        "cloud_devops": ["cloud", "devops", "aws", "azure", "gcp",
                         "docker", "kubernetes", "ci/cd", "terraform",
                         "infrastructure"],
        "data": ["data engineer", "data scientist", "analytics",
                 "etl", "sql", "spark", "hadoop", "warehouse",
                 "big data", "bi"],
        "embedded": ["embedded", "firmware", "rtos", "c/c++",
                     "microcontroller", "fpga", "hardware",
                     "iot", "signal processing"],
    }

    def __init__(self, truth: Dict[str, Any]):
        self.truth = truth
        self.links = truth.get("links", {})

    def match_field(self, label: str, job_keywords: List[str] = None
                    ) -> Optional[str]:
        """Try to find an answer for a form field label.
        
        Returns the answer string, or None if no match.
        """
        label_lower = label.lower().strip()

        # ── LinkedIn / GitHub URL fields ──
        if re.search(r"\b(linkedin|linked.?in)\b", label_lower):
            url = self.links.get("linkedin", "")
            if url:
                return url

        if re.search(r"\b(github|git.?hub|portfolio|website|hemsida|"
                      r"webbplats|personal.?url)\b", label_lower):
            url = (self.links.get("github", "")
                   or self.links.get("portfolio", "")
                   or self.links.get("linkedin", ""))
            if url:
                return url

        # Generic URL / profile field
        if re.search(r"\b(url|profil.?link|social.?media)\b", label_lower):
            if not re.search(r"(e-post|email|foto|image|photo|bild)",
                             label_lower):
                url = (self.links.get("linkedin", "")
                       or self.links.get("github", ""))
                if url:
                    return url

        # ── Static patterns ──
        for pattern, answer in self.STATIC_PATTERNS:
            if re.search(pattern, label_lower, re.IGNORECASE):
                logger.debug("  📖 Answer match: '%s' → pattern", label_lower[:40])
                return answer

        # ── Project / experience description (dynamic) ──
        if re.search(r"(describe.*project|beskriv.*projekt|relevant.*experience"
                      r"|tell.*about.*yourself|berätta.*om.*dig"
                      r"|why.*interested|varför.*intresserad"
                      r"|motivat|cover.*letter|personligt\s*brev)",
                      label_lower, re.IGNORECASE):
            domain = self._best_domain(job_keywords or [])
            logger.debug("  📖 Project match: domain=%s", domain)
            return self.PROJECT_DOMAINS.get(domain, self.PROJECT_DOMAINS["general"])

        return None

    def match_dropdown(self, label: str, options: List[str]) -> Optional[str]:
        """Try to pick the best option from a dropdown.
        
        Returns the option value/label to select, or None.
        """
        label_lower = label.lower()

        # Birth year
        if re.search(r"(födelseår|birth.*year|born)", label_lower):
            return "2003"

        # Gender
        if re.search(r"(kön|gender)", label_lower):
            for o in options:
                if o.lower() in ("man", "male", "he/him"):
                    return o
            return None

        # Experience years dropdown
        if re.search(r"(year.*experience|år.*erfarenhet)", label_lower):
            for o in options:
                if any(k in o.lower() for k in ["0-1", "0-2", "1", "<1", "junior"]):
                    return o
            # pick the first/lowest option
            return options[0] if options else None

        # Education level dropdown
        if re.search(r"(education|utbildning)", label_lower):
            for o in options:
                if any(k in o.lower() for k in ["master", "msc", "magister"]):
                    return o
            return None

        # Framtidenkontor
        if re.search(r"framtidenkontor", label_lower):
            return "Malmö"

        # Title prefix (Ingen = None)
        if re.search(r"(titel|title)\b", label_lower) and len(options) < 10:
            for o in options:
                if o.lower() in ("ingen", "none", "mr", "mr."):
                    return o

        return None

    def _best_domain(self, keywords: List[str]) -> str:
        """Pick the best project domain based on job keywords."""
        if not keywords:
            return "general"

        kw_lower = {k.lower() for k in keywords}
        scores: Dict[str, int] = {}

        for domain, domain_kws in self.DOMAIN_KEYWORDS.items():
            score = sum(1 for dk in domain_kws if dk in kw_lower)
            # Also check partial matches
            for kw in kw_lower:
                for dk in domain_kws:
                    if dk in kw or kw in dk:
                        score += 1
            scores[domain] = score

        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return best
        return "general"

    def has_unknown_mandatory(self, label: str) -> bool:
        """Check if a field label is unknown and might be mandatory.
        
        Used to decide whether to fall back to ASSIST mode.
        """
        label_lower = label.lower().strip()

        # These are known field types we can handle
        known_patterns = [
            r"(förnamn|first.?name|fname|firstname)",
            r"(efternamn|last.?name|lname|lastname|surname)",
            r"(e-post|email|e-mail)",
            r"(mobil|phone|tel)",
            r"(adress|address|street|gatuadress)",
            r"(stad|city|postort|\bort\b)",
            r"(postnummer|postal|zip|postkod)",
            r"(linkedin|linked.?in|github|git.?hub|portfolio|website|hemsida|webbplats)",
            r"(personligt\s*brev|cover.?letter|meddelande)",
            r"(salary|lön|löneanspråk)",
            r"(namn|name)\b",
            r"(stat|provins|province|state)",
            r"(framtidenkontor)",
            r"\b(url|profil.?link|social.?media)\b",
        ]

        # Also check our static patterns
        for pattern, _ in self.STATIC_PATTERNS:
            known_patterns.append(pattern)

        for p in known_patterns:
            if re.search(p, label_lower, re.IGNORECASE):
                return False

        # If we get here, it's an unknown field
        return True