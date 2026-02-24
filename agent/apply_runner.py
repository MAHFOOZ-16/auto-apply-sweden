"""V2 Browser automation: multi-step forms, platform-aware, assist mode.

Key changes from V1:
  - Platform classification before filling
  - Multi-step form handler (Nästa ≠ Submit)
  - Confirmation verification after submit
  - Non-blocking Assist mode (tab stays open, agent continues)
  - Answer library for tricky questions
  - Single fill pass (no double-fill bug)
  - Extra documents: degree, transcript, sample_work
  - No max assist-tab limit (can open up to daily_cap tabs)
"""

import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import (
    BrowserContext, Page, Playwright, sync_playwright,
    TimeoutError as PlaywrightTimeout,
)

from agent import States
from agent.db import AgentDB
from agent.platform_classifier import classify_platform
from agent.answer_library import AnswerLibrary
from agent.tailor import _pick_city, _pick_postal, _pick_street

logger = logging.getLogger("agent.apply_runner")

# ═══════════════════════════════════════════════════════
#  STEALTH — hide Playwright/automation markers
# ═══════════════════════════════════════════════════════

STEALTH_JS = """
// 1) Hide navigator.webdriver (the #1 detection signal)
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
    configurable: true,
});

// 2) Fake window.chrome (missing in Playwright's Chromium)
if (!window.chrome) {
    window.chrome = {
        runtime: { onMessage: { addListener: () => {}, removeListener: () => {} },
                   onConnect: { addListener: () => {}, removeListener: () => {} },
                   sendMessage: () => {},
                   connect: () => ({}) },
        loadTimes: () => ({}),
        csi: () => ({}),
    };
}

// 3) Fake navigator.plugins (real Chrome has PDF viewer etc.)
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
          description: 'Portable Document Format', length: 1 },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
          description: '', length: 1 },
        { name: 'Native Client', filename: 'internal-nacl-plugin',
          description: '', length: 2 },
    ],
    configurable: true,
});

// 4) Fake navigator.languages (Playwright sometimes leaves this odd)
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-GB', 'en', 'sv'],
    configurable: true,
});

// 5) Hide automation-related properties
delete navigator.__proto__.webdriver;

// 6) Override permissions query (Playwright returns 'denied' for notifications)
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (params) =>
    params.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : origQuery(params);

// 7) Spoof WebGL renderer (some sites fingerprint this)
const getParam = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Intel Inc.';
    if (param === 37446) return 'Intel Iris OpenGL Engine';
    return getParam.call(this, param);
};
"""

# ═══════════════════════════════════════════════════════
#  SELECTORS
# ═══════════════════════════════════════════════════════

SEL_APPLY = [
    'a:has-text("Ansök")', 'a:has-text("Ansök nu")',
    'a:has-text("Apply")', 'a:has-text("Apply now")',
    'button:has-text("Ansök")', 'button:has-text("Ansök nu")',
    'button:has-text("Apply")', 'button:has-text("Apply now")',
    '[data-testid="apply-button"]', 'a[href*="apply"]',
]

# FINAL submit only — "Nästa"/"Next" is handled separately
SEL_SUBMIT_FINAL = [
    'button:has-text("Skicka ansökan")',
    'button:has-text("Skicka in ansökan")',
    'button:has-text("Submit application")',
    'button:has-text("Submit")',
    'button:has-text("Send application")',
    'button:has-text("Slutför")',
    'button:has-text("Registrera")',
    'button:has-text("Skicka")',
    'input[type="submit"][value*="Skicka"]',
    'input[type="submit"][value*="Submit"]',
    'input[type="submit"][value*="Registrera"]',
]

# Next-step buttons (proceed to next page, NOT submit)
SEL_NEXT_STEP = [
    'button:has-text("Nästa")',
    'button:has-text("Next")',
    'button:has-text("Continue")',
    'button:has-text("Fortsätt")',
    'button:has-text("Gå vidare")',
]

SEL_VERIFY = [
    'text="BankID"', 'text="Mobilt BankID"',
    '[class*="bankid"]', 'img[src*="bankid"]',
    'iframe[src*="captcha"]', 'iframe[src*="recaptcha"]',
    '[class*="captcha"]',
]

# Confirmation signals — proof the application was received
CONFIRM_SIGNALS = [
    "tack för din ansökan", "thank you for applying",
    "application received", "ansökan skickad",
    "we have received your application",
    "din ansökan har registrerats",
    "din ansökan har skickats",
    "thanks for applying", "your application has been submitted",
    "application submitted successfully",
    "tack för att du söker", "vi har tagit emot din ansökan",
    "roligt att du har sökt", "vi har mottagit din ansökan",
    "thanks for your interest", "we're excited you did",
]


class ApplyRunner:
    def __init__(self, db: AgentDB, config: Dict[str, Any], notifier):
        self.db = db
        self.cfg = config
        self.notifier = notifier
        self.headless = config.get("headless_browser", False)
        self.user_data_dir = config.get("browser_user_data_dir",
                                        "./secrets/browser_profile")

        truth_path = Path(config.get("truth_path", "data/truth.json"))
        with open(truth_path, "r") as f:
            self.truth = json.load(f)

        # Base directory for resolving relative paths in truth.json
        # e.g. if truth_path is "data/truth.json", base is "data/"
        self._base_dir = truth_path.parent

        # Extra documents: degree, transcript, sample_work
        self.extra_docs: List[str] = []
        for k in ("degree_certificate", "transcript", "sample_work"):
            p = self.truth.get("extra_documents", {}).get(k, "")
            if not p:
                continue
            # Try multiple path resolutions
            candidates = [
                Path(p),                          # absolute or CWD-relative
                self._base_dir / Path(p).name,    # same dir as truth.json
                self._base_dir / p,               # relative to truth.json dir
                Path(p).resolve(),                 # absolute resolved
            ]
            found = False
            for cp in candidates:
                if cp.exists():
                    resolved = str(cp.resolve())
                    self.extra_docs.append(resolved)
                    logger.info("  📎 Extra doc [%s]: %s", k, resolved)
                    found = True
                    break
            if not found:
                logger.warning("  ⚠️  Extra doc [%s] not found: %s "
                               "(tried: %s)", k, p,
                               ", ".join(str(c) for c in candidates))

        # Login credentials
        login = self.truth.get("login", {})
        self._login_email = login.get("email", "")
        self._login_password = login.get("password", "")

        # Answer library
        self.answers = AnswerLibrary(self.truth)

        # Track open assist tabs: {job_id: page}
        self.assist_tabs: Dict[str, Page] = {}

        self._pw: Optional[Playwright] = None
        self._ctx: Optional[BrowserContext] = None

    # ════════════════════════════════════════════════════════
    #  BROWSER LIFECYCLE
    # ════════════════════════════════════════════════════════

    def start_browser(self):
        Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir, headless=self.headless,
            viewport={"width": 1280, "height": 900},
            locale="en-GB", timezone_id="Europe/Stockholm",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                # Anti-detection: make Chromium look like real Chrome
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--no-first-run",
                "--no-default-browser-check",
                # Reduce fingerprinting surface
                "--disable-extensions-except=",
                "--disable-component-extensions-with-background-pages",
            ],
        )

        # ── Stealth: inject anti-detection JS on every new page ──
        self._ctx.add_init_script(STEALTH_JS)

        logger.info("Browser started (headless=%s, stealth=ON)", self.headless)

    # Domains we must NEVER visit with automation (will get account banned)
    BLOCKED_DOMAINS = [
        "linkedin.com", "www.linkedin.com",
        "linkedin.se", "www.linkedin.se",
    ]

    def stop_browser(self):
        try:
            self._ctx and self._ctx.close()
            self._pw and self._pw.stop()
        except Exception:
            pass

    @classmethod
    def _is_blocked_domain(cls, url: str) -> bool:
        """Check if a URL is on a domain we must never automate."""
        url_lower = url.lower()
        for domain in cls.BLOCKED_DOMAINS:
            if domain in url_lower:
                return True
        return False

    # ════════════════════════════════════════════════════════
    #  MAIN APPLY FLOW (V2)
    # ════════════════════════════════════════════════════════

    def apply_to_job(self, job: Dict, resume_path: Path, cover_path: Path,
                     artifact_dir: Path, cover_text: str = "",
                     job_keywords: List[str] = None) -> str:
        jid = job["job_id"]
        url = job["url"]
        loc = job.get("location", "Sweden")

        # ── BLOCK: Never automate on LinkedIn (account ban risk) ──
        if self._is_blocked_domain(url):
            logger.info("⛔ SKIPPED: %s — blocked domain (LinkedIn). "
                        "Apply manually at: %s",
                        job.get("title"), url)
            self.db.update_job_status(jid, States.FAILED_PERMANENT)
            self.db.log_event(jid, States.FAILED_PERMANENT,
                              "Blocked domain — LinkedIn automation forbidden")
            return States.FAILED_PERMANENT

        self.db.update_job_status(jid, States.APPLYING)
        self.db.log_event(jid, States.APPLYING, f"Opening {url}")
        page = self._ctx.new_page()
        ss = [0]

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            _delay(2, 4)

            # ⓪ REDIRECT CHECK: page might have redirected to LinkedIn
            if self._is_blocked_domain(page.url):
                logger.info("⛔ REDIRECT to blocked domain: %s → %s",
                            url[:50], page.url[:50])
                self.db.update_job_status(jid, States.FAILED_PERMANENT)
                self.db.log_event(jid, States.FAILED_PERMANENT,
                                  f"Redirected to blocked domain: {page.url}")
                page.close()
                return States.FAILED_PERMANENT

            # ① Dismiss cookie banners
            self._dismiss_cookies(page)

            # ② Handle login at initial load
            #    NEVER auto-login on LinkedIn (account ban risk)
            if self._is_login_page(page):
                if self._is_blocked_domain(page.url):
                    logger.info("⛔ Login page on blocked domain — skipping")
                    page.close()
                    return States.FAILED_PERMANENT
                if not self._do_login(page):
                    return self._assist(job, page, artifact_dir, ss,
                                        "Login needed")
                _delay(2, 3)
                self._dismiss_cookies(page)

            # ③ Detect platform
            platform = classify_platform(page)
            logger.info("  Platform: %s (mode=%s, multi_step=%s)",
                        platform["name"], platform["mode"],
                        platform["multi_step"])

            # SKIP mode — platform is blocked (e.g. LinkedIn)
            if platform["mode"] == "SKIP":
                logger.info("  ⛔ Platform %s is SKIP — closing",
                            platform["name"])
                self.db.update_job_status(jid, States.FAILED_PERMANENT)
                self.db.log_event(jid, States.FAILED_PERMANENT,
                                  f"Platform {platform['name']} blocked")
                page.close()
                return States.FAILED_PERMANENT

            _scroll(page)
            self._ss(page, artifact_dir, ss)

            if self._is_verify(page):
                return self._assist(job, page, artifact_dir, ss,
                                    "Verification/captcha detected")

            # ④ Click Apply button (if needed — some pages go straight to form)
            if not self._click_apply(page):
                if not self._has_form(page):
                    # No apply button AND no form → ASSIST (don't close tab)
                    return self._assist(job, page, artifact_dir, ss,
                                        "No apply button or form found")
            _delay(2, 5)
            self._dismiss_cookies(page)

            # ④b Handle login after apply click
            if self._is_login_page(page):
                if not self._do_login(page):
                    return self._assist(job, page, artifact_dir, ss,
                                        "Login after apply click")
                _delay(2, 3)

            self._ss(page, artifact_dir, ss)
            if self._is_verify(page):
                return self._assist(job, page, artifact_dir, ss,
                                    "Verification after apply")

            # ⑤ MULTI-STEP FORM LOOP
            result = self._fill_and_submit_loop(
                job, page, platform, resume_path, cover_path,
                artifact_dir, ss, loc, cover_text,
                job_keywords or [],
            )
            return result

        except PlaywrightTimeout as e:
            # Timeout → fall back to ASSIST (leave tab open for human)
            logger.warning("  ⏰ Timeout: %s — falling back to ASSIST", e)
            return self._assist(job, page, artifact_dir, ss,
                                "Timeout — page may still be usable")
        except Exception as e:
            # Any error → fall back to ASSIST (leave tab open for human)
            logger.warning("  ⚠️ Error: %s — falling back to ASSIST", e)
            try:
                # Only assist if page is still alive
                if not page.is_closed():
                    return self._assist(job, page, artifact_dir, ss,
                                        f"Error: {type(e).__name__}")
            except Exception:
                pass
            # Page is dead — truly failed
            self._fail(jid, artifact_dir, page, ss,
                       f"{type(e).__name__}: {e}")
            try:
                page.close()
            except Exception:
                pass
            return States.FAILED_RETRYABLE

    # ════════════════════════════════════════════════════════
    #  MULTI-STEP FORM LOOP — The core V2 engine
    # ════════════════════════════════════════════════════════

    def _fill_and_submit_loop(self, job, page, platform,
                              resume_path, cover_path, artifact_dir,
                              ss, loc, cover_text, job_keywords) -> str:
        """Fill form fields step by step, handle Nästa, then submit."""
        _ = job["job_id"]
        max_steps = platform.get("max_steps", 3)
        has_unknown_mandatory = False
        cover_textarea_used = False

        for step in range(max_steps + 2):   # +2 safety margin
            logger.info("  📝 Step %d/%d", step + 1, max_steps)

            # Dismiss cookies (some forms show new banners per step)
            self._dismiss_cookies(page)
            _delay(0.5, 1)

            # Scroll full page to trigger lazy-loaded fields, then back to top
            # (SINGLE scroll — no second fill pass like V1)
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                _delay(0.8, 1.2)
                page.evaluate("window.scrollTo(0, 0)")
                _delay(0.5, 0.8)
            except Exception:
                pass

            # Fill all visible fields (SINGLE PASS — no double-fill bug)
            unknown_count, step_cover_filled = self._fill_form(
                page, loc, cover_text, job_keywords)

            if step_cover_filled:
                cover_textarea_used = True

            if unknown_count > 0:
                has_unknown_mandatory = True
                logger.info("  ⚠️  %d unknown mandatory field(s)", unknown_count)

            _delay(0.8, 1.5)

            # Fill radio buttons / special selects
            self._fill_radios(page)
            _delay(0.3, 0.8)

            # Click "Skriv i formulär istället" to reveal textareas
            if cover_text:
                wrote = self._click_write_in_form(page, cover_text)
                if wrote:
                    cover_textarea_used = True
            _delay(0.3, 0.8)

            # Upload files
            self._upload_files(page, resume_path, cover_path,
                               cover_textarea_used)
            _delay(1, 2)

            # Tick consent checkboxes + click "Godkänn" links
            self._tick_consent(page)
            _delay(0.3, 0.8)

            self._ss(page, artifact_dir, ss)

            if self._is_verify(page):
                return self._assist(job, page, artifact_dir, ss,
                                    "Verification on form step")

            # ── Decision: Submit, Next, or Assist? ──

            final_btn = self._find_button(page, SEL_SUBMIT_FINAL)
            next_btn = self._find_button(page, SEL_NEXT_STEP)

            if final_btn:
                # If there are unknown mandatory fields → ASSIST
                if has_unknown_mandatory:
                    return self._assist(
                        job, page, artifact_dir, ss,
                        "Unknown mandatory field(s) — needs human review")

                # Click final submit
                logger.info("  🚀 Clicking FINAL submit")
                try:
                    final_btn.click()
                    _delay(3, 5)
                except Exception as e:
                    logger.error("  Submit click failed: %s", e)
                    return self._assist(
                        job, page, artifact_dir, ss,
                        "Submit button click failed — please submit manually")

                self._ss(page, artifact_dir, ss)
                return self._verify_submission(job, page, artifact_dir, ss)

            elif next_btn:
                # Nästa/Next — proceed to next step (NOT a submit!)
                logger.info("  ➡️  Clicking Nästa/Next (step %d)", step + 1)

                # If unknown mandatory fields before Nästa → ASSIST
                if has_unknown_mandatory:
                    return self._assist(
                        job, page, artifact_dir, ss,
                        "Unknown mandatory field(s) before Nästa")

                try:
                    next_btn.click()
                    _delay(2, 4)
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                    _delay(1, 2)
                except Exception as e:
                    logger.debug("  Next click issue: %s", e)
                    _delay(1, 2)

                # Reset unknown flag for new page
                has_unknown_mandatory = False
                continue   # go to next iteration of the step loop

            else:
                # No submit or next button found
                if self._has_form(page):
                    # Try generic submit button
                    generic = self._find_button(page, [
                        'button[type="submit"]', 'input[type="submit"]',
                    ])
                    if generic:
                        if has_unknown_mandatory:
                            return self._assist(
                                job, page, artifact_dir, ss,
                                "Unknown mandatory field(s) — generic submit")

                        logger.info("  🚀 Clicking generic submit")
                        try:
                            generic.click()
                            _delay(3, 5)
                            self._ss(page, artifact_dir, ss)
                            return self._verify_submission(
                                job, page, artifact_dir, ss)
                        except Exception as e:
                            logger.error("  Generic submit failed: %s", e)
                            return self._assist(
                                job, page, artifact_dir, ss,
                                "Generic submit click failed — please submit manually")

                # Fallback: if platform is TRY_AUTO → assist
                if platform["mode"] == "TRY_AUTO":
                    return self._assist(
                        job, page, artifact_dir, ss,
                        "No submit button — TRY_AUTO platform")

                # ALL platforms: fall back to ASSIST (never close a filled form)
                return self._assist(
                    job, page, artifact_dir, ss,
                    "No submit button found — please submit manually")

        # Exhausted all steps without finding a submit
        return self._assist(job, page, artifact_dir, ss,
                            f"Exceeded {max_steps} steps without submit")

    # ════════════════════════════════════════════════════════
    #  CONFIRMATION VERIFICATION
    # ════════════════════════════════════════════════════════

    def _verify_submission(self, job, page, artifact_dir, ss) -> str:
        """After clicking submit, check for confirmation OR validation errors."""
        jid = job["job_id"]

        try:
            body = page.inner_text("body")[:5000].lower()
        except Exception:
            body = ""

        # ── Check for confirmation first ──
        for signal in CONFIRM_SIGNALS:
            if signal in body:
                self.db.update_job_status(jid, States.CONFIRMED)
                self.db.log_event(jid, States.CONFIRMED,
                                  f"Confirmation: '{signal}'")
                self.db.increment_daily("applied")
                logger.info("✅ CONFIRMED: %s at %s (signal: '%s')",
                            job.get("title"), job.get("company"), signal)
                page.close()
                return States.CONFIRMED

        # ── Check for validation errors (form rejected the submit) ──
        validation_errors = self._detect_validation_errors(page, body)
        if validation_errors:
            logger.info("  ⚠️  Validation errors after submit: %s",
                        validation_errors[:100])
            return self._assist(
                job, page, artifact_dir, ss,
                f"Form validation failed: {validation_errors[:80]}")

        # ── Check if page URL changed (redirect = likely submitted) ──
        # If we're still on the same form page with no errors and no
        # confirmation, mark UNCERTAIN but still close (likely submitted)
        self.db.update_job_status(jid, States.UNCERTAIN)
        self.db.log_event(jid, States.UNCERTAIN,
                          "Submit clicked but no confirmation detected")
        self.db.increment_daily("applied")
        logger.info("❓ UNCERTAIN: %s at %s (submitted, no confirmation)",
                     job.get("title"), job.get("company"))
        self._ss(page, artifact_dir, ss)
        page.close()
        return States.UNCERTAIN

    @staticmethod
    def _detect_validation_errors(page: Page, body: str) -> str:
        """Detect form validation errors after a submit attempt.
        Returns error description or empty string if none found."""
        # Common validation error signals in Swedish and English
        error_signals = [
            "fyll i detta fält", "detta fält är obligatoriskt",
            "vänligen fyll i", "fältet är obligatoriskt",
            "obligatoriskt fält", "du måste fylla i",
            "this field is required", "please fill in",
            "required field", "please complete this field",
            "is required", "can't be blank", "cannot be blank",
            "must be filled", "field is mandatory",
        ]
        for sig in error_signals:
            if sig in body:
                return sig

        # Check for visible error elements (red borders, error messages)
        error_selectors = [
            '[class*="error"]:visible',
            '[class*="invalid"]:visible',
            '[class*="validation"]:visible',
            '.field-error:visible',
            '.form-error:visible',
            '[aria-invalid="true"]:visible',
            '[class*="required-error"]:visible',
        ]
        for sel in error_selectors:
            try:
                count = page.locator(sel).count()
                if count > 0:
                    # Try to get the error text
                    try:
                        txt = page.locator(sel).first.inner_text()[:80]
                        if txt.strip():
                            return f"Validation: {txt.strip()}"
                    except Exception:
                        pass
                    return f"Validation error elements found ({count})"
            except Exception:
                continue

        # Check if required inputs are still empty (form didn't navigate away)
        try:
            empty_required = 0
            for inp in page.locator(
                'input[required]:visible, textarea[required]:visible, '
                'select[required]:visible'
            ).all():
                try:
                    val = inp.input_value() or ""
                    if len(val.strip()) == 0:
                        empty_required += 1
                except Exception:
                    pass
            if empty_required > 0:
                return f"{empty_required} required field(s) still empty"
        except Exception:
            pass

        return ""

    # ════════════════════════════════════════════════════════
    #  COOKIE BANNER DISMISSAL
    # ════════════════════════════════════════════════════════

    @staticmethod
    def _dismiss_cookies(page: Page):
        for sel in [
            'button:has-text("Accept all cookies")',
            'button:has-text("Accept all")',
            'button:has-text("Acceptera alla")',
            'button:has-text("Acceptera alla cookies")',
            'button:has-text("Acceptera")',
            'button:has-text("Godkänn alla")',
            'button:has-text("Godkänn alla cookies")',
            'button:has-text("Tillåt alla")',
            'button:has-text("Tillåt alla cookies")',
            'button:has-text("Decline all non-necessary cookies")',
            'button:has-text("OK")',
            'button:has-text("Jag förstår")',
            'button:has-text("I agree")',
            'button:has-text("Got it")',
            'button:has-text("Stäng")',
            'button:has-text("Close")',
            'a:has-text("Accept all cookies")',
            'a:has-text("Accept all")',
            'a:has-text("Acceptera")',
            'a:has-text("Acceptera alla")',
            '[id*="cookie-accept"]', '[id*="cookieAccept"]',
            '[data-action="accept"]',
            '[id*="onetrust-accept"]', '#onetrust-accept-btn-handler',
            '.cookie-accept', '.cookie-consent-accept',
            'button.consent-give', '[data-cky-tag="accept-button"]',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=500):
                    btn.click()
                    _delay(0.5, 1)
                    logger.info("  ✓ Cookie dismissed: %s", sel[:45])
                    return
            except Exception:
                continue
        # Fallback: hide overlays via JS
        try:
            page.evaluate("""
                document.querySelectorAll(
                    '[class*="cookie"], [class*="consent"], [id*="cookie"], '
                    + '[class*="Cookie"], [class*="Consent"]'
                ).forEach(el => {
                    if (el && el.style) el.style.display = 'none';
                });
            """)
        except Exception:
            pass

    # ════════════════════════════════════════════════════════
    #  FORM FILLING (V2 — single pass, answer library)
    # ════════════════════════════════════════════════════════

    def _fill_form(self, page: Page, loc: str, cover_text: str,
                   job_keywords: List[str]) -> tuple:
        """Fill all visible fields.
        Returns (unknown_mandatory_count, cover_textarea_filled)."""
        p = self.truth.get("personal", {})
        first = p.get("first_name", "Jake")
        last = p.get("last_name", "Morrison")
        email = p.get("email", "your.email@example.com")
        phone_raw = p.get("phone", "+46 700000000")
        phone_local = p.get("phone_local", "700000000")
        phone_digits = re.sub(r"[^\d]", "", phone_raw)

        street = _pick_street(loc, self.truth)
        city = _pick_city(loc, self.truth)
        postal = _pick_postal(loc, self.truth)
        linkedin = self.truth.get("links", {}).get("linkedin", "")
        github = self.truth.get("links", {}).get("github", "")

        filled = 0
        unknown_mandatory = 0
        cover_filled = False

        for el in page.locator(
            "input:visible, textarea:visible, select:visible"
        ).all():
            try:
                tag = el.evaluate("el => el.tagName").lower()
                itype = (el.get_attribute("type") or "text").lower()
                if itype in ("hidden", "file", "submit", "button",
                             "checkbox", "radio", "image"):
                    continue

                # Gather identifying info
                lbl = _label(page, el).lower()
                name = (el.get_attribute("name") or "").lower()
                iid = (el.get_attribute("id") or "").lower()
                ph = (el.get_attribute("placeholder") or "").lower()
                aria = (el.get_attribute("aria-label") or "").lower()
                ids = f"{lbl} {name} {iid} {ph} {aria}"

                is_required = (
                    el.get_attribute("required") is not None
                    or el.get_attribute("aria-required") == "true"
                    or "*" in lbl
                )

                # URL-type inputs → try LinkedIn/GitHub
                if itype == "url":
                    if _m(ids, ["linkedin", "linked"]):
                        _type(el, linkedin)
                        filled += 1
                        continue
                    elif _m(ids, ["github", "git"]):
                        _type(el, github)
                        filled += 1
                        continue
                    elif linkedin:
                        _type(el, linkedin)
                        filled += 1
                        continue
                    continue

                # Skip already-filled fields
                if tag != "select":
                    cur = el.input_value() or ""
                    if len(cur) > 2:
                        continue

                # ── SELECT (dropdowns) ──
                if tag == "select":
                    self._fill_select(el, ids, lbl, job_keywords)
                    continue

                # ── TEXTAREA ──
                if tag == "textarea":
                    if _m(ids, ["personligt brev", "personal letter",
                                "cover letter", "meddelande", "brev",
                                "motivation"]):
                        if cover_text:
                            _fill_long(el, cover_text[:3000])
                            logger.info("  ✓ Personligt brev textarea")
                            filled += 1
                            cover_filled = True
                    else:
                        answer = self.answers.match_field(ids, job_keywords)
                        if answer:
                            _fill_long(el, answer[:3000])
                            logger.info("  ✓ Textarea '%s' → library",
                                        lbl[:30])
                            filled += 1
                        elif is_required:
                            unknown_mandatory += 1
                            logger.info("  ⚠️  Unknown textarea: '%s'",
                                        lbl[:40])
                    continue

                # ── Confirm email ──
                if _m(ids, ["bekräfta e-post", "bekräfta email",
                            "confirm email", "repeat email", "verify email",
                            "confirm_email"]):
                    _type(el, email)
                    filled += 1
                    continue

                # ── Email ──
                if _m(ids, ["e-postadress", "e-post", "email", "e-mail"]) \
                        and itype in ("email", "text", ""):
                    _type(el, email)
                    filled += 1
                    continue

                # ── First name ──
                if _m(ids, ["förnamn", "first name", "first_name",
                            "fname", "firstname", "given_name"]):
                    _type(el, first)
                    filled += 1
                    continue

                # ── Last name ──
                if _m(ids, ["efternamn", "last name", "last_name",
                            "lname", "lastname", "surname",
                            "family_name"]):
                    _type(el, last)
                    filled += 1
                    continue

                # ── Full name ──
                if (_m(ids, ["name", "namn"])
                    and not _m(ids, ["first", "last", "för", "efter",
                                     "fname", "lname", "user", "company",
                                     "företag", "firma"])):
                    _type(el, f"{first} {last}")
                    filled += 1
                    continue

                # ── Phone / Mobil ──
                if _m(ids, ["mobiltelefon", "mobil", "mobile",
                            "telefon", "phone", "tel"]):
                    has_cc = _has_country_code_sibling(el)
                    if has_cc:
                        _type(el, phone_local)
                    elif itype == "tel":
                        _type(el, phone_digits)
                    else:
                        _type(el, phone_raw)
                    filled += 1
                    continue

                # ── Address ──
                if (_m(ids, ["adress", "address", "gatuadress", "street"])
                    and not _m(ids, ["e-post", "email"])):
                    _type(el, street)
                    filled += 1
                    continue

                # ── City / Ort ──
                if _m(ids, ["stad", "city", "postort"]) or \
                        re.search(r'\bort\b', ids):
                    _type(el, city)
                    filled += 1
                    continue

                # ── Postal code ──
                if _m(ids, ["postnummer", "postal", "zip", "postkod"]):
                    _type(el, postal)
                    filled += 1
                    continue

                # ── LinkedIn ──
                if _m(ids, ["linkedin", "linked in", "linked-in",
                            "linkedinprofil", "linkedin url",
                            "linkedin profile"]) and linkedin:
                    _type(el, linkedin)
                    filled += 1
                    continue

                # ── GitHub / portfolio / website ──
                if _m(ids, ["github", "portfolio", "website",
                            "hemsida", "webbplats", "personal url",
                            "personal website", "web site"]):
                    url = github or linkedin
                    if url:
                        _type(el, url)
                        filled += 1
                        continue

                # ── Generic URL field (catch-all for social profiles) ──
                if (_m(ids, ["url", "profil", "profile link",
                             "social media"]) and
                    not _m(ids, ["e-post", "email", "foto", "image",
                                 "photo", "bild"])):
                    url = linkedin or github
                    if url:
                        _type(el, url)
                        filled += 1
                        continue

                # ── Stat/Provins/Province ──
                if _m(ids, ["stat/provins", "provins", "state",
                            "province", "region"]):
                    _type(el, "Skåne")
                    filled += 1
                    continue

                # ── Salary ──
                if _m(ids, ["salary", "lön", "löneanspråk",
                            "lönekrav", "compensation"]):
                    _type(el, "35000")
                    filled += 1
                    continue

                # ── Personnummer (only if it looks like it) ──
                if _m(ids, ["personnummer", "personal.*number",
                            "social security"]):
                    pnr = p.get("personnummer", "")
                    if pnr:
                        _type(el, pnr)
                        filled += 1
                        continue

                # ── Try answer library for anything else ──
                answer = self.answers.match_field(ids, job_keywords)
                if answer:
                    _type(el, answer)
                    logger.info("  ✓ '%s' → answer library", lbl[:30])
                    filled += 1
                    continue

                # Unknown field
                if is_required:
                    unknown_mandatory += 1
                    logger.info("  ⚠️  Unknown required: '%s' (%s)",
                                lbl[:50], name[:20])

            except Exception as exc:
                logger.debug("Fill err: %s", exc)

        logger.info("Filled %d fields (%d unknown mandatory, cover=%s)",
                     filled, unknown_mandatory, cover_filled)
        return unknown_mandatory, cover_filled

    # ────────────────── Select dropdowns ─────────────────

    def _fill_select(self, el, ids, lbl, job_keywords):
        """Fill a <select> dropdown."""
        if _m(ids, ["födelseår", "birth", "born"]):
            _sel_opt(el, "2003")
            return
        if _m(ids, ["kön", "gender"]):
            _sel_opt(el, "Man")
            return
        if _m(ids, ["framtidenkontor"]):
            _sel_opt(el, "Malmö")
            return
        if _m(ids, ["titel", "title"]) and not _m(ids, ["job", "position"]):
            pass  # leave default
            return

        # Try answer library for dropdown matching
        try:
            options = []
            for opt in el.locator("option").all():
                txt = (opt.inner_text() or "").strip()
                if txt:
                    options.append(txt)
            answer = self.answers.match_dropdown(lbl, options)
            if answer:
                _sel_opt(el, answer)
                logger.info("  ✓ Select '%s' → '%s'",
                            lbl[:20], answer[:20])
        except Exception:
            pass

    # ════════════════════════════════════════════════════════
    #  RADIO BUTTONS
    # ════════════════════════════════════════════════════════

    @staticmethod
    def _fill_radios(page: Page):
        body = ""
        try:
            body = page.inner_text("body")[:8000].lower()
        except Exception:
            return

        radio_map = [
            (["behärskar du svenska", "svenska språket", "swedish level"],
             ["Grundläggande", "Basic", "Beginner"]),
            (["english level", "engelska nivå", "english proficiency"],
             ["Flytande", "Fluent", "Native or bilingual"]),
            (["arbetstillstånd", "work permit", "right to work",
              "legally authorized"],
             ["Ja", "Yes"]),
            (["pratar ej svenska"],
             ["Pratar ej svenska"]),
        ]

        for triggers, targets in radio_map:
            if not any(t in body for t in triggers):
                continue
            for target in targets:
                for sel_fmt in ['text="{}"', 'label:has-text("{}")',
                                'span:has-text("{}")']:
                    sel = sel_fmt.format(target)
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=1500):
                            el.click()
                            _delay(0.3, 0.6)
                            logger.info("  ✓ Radio: %s", target)
                            break
                    except Exception:
                        continue
                else:
                    continue
                break  # matched one target, stop

    # ════════════════════════════════════════════════════════
    #  "Skriv i formulär istället" — reveals textareas
    # ════════════════════════════════════════════════════════

    @staticmethod
    def _click_write_in_form(page: Page, cover_text: str) -> bool:
        """Click 'Write in form' links, then fill revealed textareas.
        Returns True if a textarea was filled."""
        clicked = False
        for sel in [
            'a:has-text("Skriv i formulär istället")',
            'a:has-text("Skriv i formulär")',
            'button:has-text("Skriv i formulär")',
            'a:has-text("Write in form instead")',
            'a:has-text("Write in form")',
        ]:
            try:
                links = page.locator(sel).all()
                for link in links:
                    if link.is_visible(timeout=1000):
                        link.click()
                        _delay(0.5, 1)
                        clicked = True
                        logger.info("  ✓ Clicked: %s", sel[:40])
            except Exception:
                continue

        if not clicked:
            return False

        # Fill any newly revealed textareas
        _delay(0.5, 1)
        for el in page.locator("textarea:visible").all():
            try:
                cur = el.input_value() or ""
                if len(cur) > 10:
                    continue
                lbl = _label(page, el).lower()
                if _m(lbl, ["personligt brev", "personal letter",
                            "cover letter", "brev", "meddelande"]):
                    _fill_long(el, cover_text[:3000])
                    logger.info("  ✓ Personligt brev textarea (revealed)")
                    return True
            except Exception:
                pass
        return clicked

    # ════════════════════════════════════════════════════════
    #  FILE UPLOADS (V2 — section matching with fallbacks)
    # ════════════════════════════════════════════════════════

    def _upload_files(self, page: Page, resume: Path, cover: Path,
                      cover_textarea_used: bool = False):
        # First, click "Ladda upp fler" links to reveal extra file slots
        self._click_upload_more(page)

        fis = page.locator('input[type="file"]').all()
        n = len(fis)
        logger.info("Found %d file input(s)", n)
        if n == 0:
            self._handle_drag_drop(page, resume)
            return

        done_cv = done_cl = done_ex = False

        # ── Strategy 1: Match by section label ──
        for i, fi in enumerate(fis):
            try:
                sec = _section_label(page, fi).lower()
                logger.debug("  File %d section: '%s'", i, sec[:50])

                # Personligt brev first (more specific)
                if (_m(sec, ["personligt brev", "personal letter",
                             "cover letter"]) and not done_cl):
                    fi.set_input_files(str(cover))
                    done_cl = True
                    logger.info("  ✓ Cover → slot %d (sec: %s)",
                                i, sec[:30])
                    _delay(1, 2)

                # CV / Resume (exclude personligt / övriga)
                elif (("cv" in sec or "resume" in sec)
                      and "personligt" not in sec
                      and "övriga" not in sec
                      and "other" not in sec
                      and not done_cv):
                    fi.set_input_files(str(resume))
                    done_cv = True
                    logger.info("  ✓ Resume → slot %d (sec: %s)",
                                i, sec[:30])
                    _delay(1, 2)

                # Extras / Övriga — ONLY degree, transcript, sample_work
                # NEVER put cover_letter here (it goes in its own slot or textarea)
                elif (_m(sec, ["övriga dokument", "other document",
                               "övrigt", "bilaga", "additional"])
                      and not done_ex):
                    if self.extra_docs:
                        # Try batch upload first (works on some platforms)
                        try:
                            fi.set_input_files(self.extra_docs)
                            done_ex = True
                            logger.info("  ✓ Extras batch → slot %d (%d files: %s)",
                                        i, len(self.extra_docs),
                                        ", ".join(Path(p).name for p in self.extra_docs))
                        except Exception:
                            # Batch failed → upload first file here,
                            # remaining files will use individual upload below
                            try:
                                fi.set_input_files(self.extra_docs[0])
                                logger.info("  ✓ Extra[0] → slot %d (%s)",
                                            i, Path(self.extra_docs[0]).name)
                            except Exception as e2:
                                logger.debug("  Extra upload err: %s", e2)
                    _delay(1, 2)
            except Exception as e:
                logger.debug("  File %d err: %s", i, e)

        # ── Strategy 2: name/id attribute ──
        if not done_cv:
            for fi in fis:
                try:
                    nm = (fi.get_attribute("name") or "").lower()
                    fid = (fi.get_attribute("id") or "").lower()
                    if _m(f"{nm} {fid}", ["resume", "cv", "curriculum"]):
                        fi.set_input_files(str(resume))
                        done_cv = True
                        logger.info("  ✓ Resume via name/id")
                        _delay(1, 2)
                        break
                except Exception:
                    pass

        # ── Strategy 3: Positional fallback ──
        if not done_cv and n >= 1:
            try:
                fis[0].set_input_files(str(resume))
                done_cv = True
                logger.info("  ✓ Resume → first input (fallback)")
                _delay(1, 2)
            except Exception:
                pass

        if not done_cl and n >= 2:
            if cover_textarea_used:
                # Cover letter already in textarea — use slot for extras
                logger.info("  ⤷ Skipping cover file (textarea used)")
                if not done_ex and self.extra_docs:
                    try:
                        fis[1].set_input_files(self.extra_docs)
                        done_ex = True
                        logger.info("  ✓ Extras → slot 1 (%d files)",
                                    len(self.extra_docs))
                        _delay(1, 2)
                    except Exception:
                        pass
            else:
                try:
                    fis[1].set_input_files(str(cover))
                    done_cl = True
                    logger.info("  ✓ Cover → 2nd input (fallback)")
                    _delay(1, 2)
                except Exception:
                    pass

        # Slot 3+ → extra documents (degree, transcript, sample_work)
        if not done_ex and self.extra_docs:
            # Find the next available slot after CV and cover
            start_slot = 2 if (done_cv and done_cl) else (1 if done_cv else 0)
            for slot_idx in range(max(start_slot, 2), n):
                try:
                    fis[slot_idx].set_input_files(self.extra_docs)
                    done_ex = True
                    logger.info("  ✓ Extras → slot %d (%d files)",
                                slot_idx, len(self.extra_docs))
                    _delay(1, 2)
                    break
                except Exception:
                    continue

        # ── Individual extra doc upload (for Teamtailor-style one-file-per-slot) ──
        # If extras weren't fully uploaded, try clicking "Ladda upp fler" and
        # uploading each file individually
        if not done_ex and self.extra_docs:
            self._upload_extras_individually(page)

    def _handle_drag_drop(self, page: Page, resume: Path):
        """Handle drag-drop CV upload areas (Framtiden/Zoho)."""
        for text in ["Överför din CV", "Drop your file",
                     "Dra ditt cv", "Dra och släpp"]:
            try:
                area = page.locator(f'text="{text}"').first
                if area.is_visible(timeout=1500):
                    for xp in ["xpath=..", "xpath=../..",
                               "xpath=../../.."]:
                        try:
                            par = area.locator(xp)
                            fi = par.locator('input[type="file"]').first
                            if fi.count() > 0:
                                fi.set_input_files(str(resume))
                                logger.info("  ✓ CV drag-drop area")
                                _delay(1, 2)
                                return
                        except Exception:
                            continue
            except Exception:
                pass

    @staticmethod
    def _click_upload_more(page: Page):
        """Click 'Ladda upp fler' / 'Upload more' links to reveal extra
        file input slots (Teamtailor puts only 1 slot under Övriga dokument
        by default — you need to click this link to get more)."""
        for _ in range(3):  # Click up to 3 times for 3 extra docs
            clicked = False
            for sel in [
                'a:has-text("Ladda upp fler")',
                'button:has-text("Ladda upp fler")',
                'span:has-text("Ladda upp fler")',
                'a:has-text("Upload more")',
                'a:has-text("Add another file")',
                'a:has-text("Lägg till fil")',
                '[class*="add-more"]',
                '[class*="upload-more"]',
            ]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=800):
                        el.click()
                        _delay(0.5, 1)
                        clicked = True
                        logger.info("  ✓ Clicked: %s", sel[:40])
                        break
                except Exception:
                    continue
            if not clicked:
                break  # No more "upload more" links

    def _upload_extras_individually(self, page: Page):
        """Upload extra docs one at a time using 'Ladda upp fler' slots.
        
        Teamtailor creates a new file input each time you click
        'Ladda upp fler'. This method:
        1. Finds an unused file input under 'Övriga dokument'
        2. Uploads one file
        3. Clicks 'Ladda upp fler' to create next slot
        4. Repeats for remaining files
        """
        uploaded = set()
        
        # Check which files are already uploaded by scanning existing filenames
        try:
            existing = page.inner_text("body")[:8000]
            for doc_path in self.extra_docs:
                fname = Path(doc_path).name
                if fname in existing:
                    uploaded.add(doc_path)
                    logger.info("  ⤷ Already uploaded: %s", fname)
        except Exception:
            pass

        remaining = [d for d in self.extra_docs if d not in uploaded]
        if not remaining:
            return

        for doc_path in remaining:
            fname = Path(doc_path).name
            
            # Find an empty file input (one that hasn't been used yet)
            found_slot = False
            for fi in page.locator('input[type="file"]').all():
                try:
                    # Check if this input has already been used
                    # by looking at nearby text for uploaded filenames
                    parent_text = ""
                    try:
                        parent_text = fi.locator("xpath=../..").inner_text()[:200]
                    except Exception:
                        pass
                    
                    sec = _section_label(page, fi).lower()
                    # Only use slots under "övriga dokument" / "additional"
                    if not _m(sec + " " + parent_text.lower(),
                              ["övriga", "other", "additional", "bilaga",
                               "ladda upp"]):
                        continue
                    
                    # Check if slot seems empty (no filename visible nearby)
                    if any(existing_name in parent_text
                           for existing_name in
                           [Path(d).name for d in self.extra_docs
                            if d in uploaded]):
                        continue
                    
                    fi.set_input_files(doc_path)
                    uploaded.add(doc_path)
                    found_slot = True
                    logger.info("  ✓ Extra individual: %s", fname)
                    _delay(1, 1.5)
                    break
                except Exception:
                    continue
            
            if not found_slot:
                # Click "Ladda upp fler" to create a new slot
                clicked = False
                for sel in [
                    'a:has-text("Ladda upp fler")',
                    'button:has-text("Ladda upp fler")',
                    'a:has-text("Upload more")',
                    'a:has-text("Lägg till fil")',
                ]:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=800):
                            el.click()
                            _delay(0.8, 1.2)
                            clicked = True
                            break
                    except Exception:
                        continue
                
                if clicked:
                    # Try the new slot
                    try:
                        new_fis = page.locator('input[type="file"]').all()
                        if new_fis:
                            new_fis[-1].set_input_files(doc_path)
                            uploaded.add(doc_path)
                            logger.info("  ✓ Extra (new slot): %s", fname)
                            _delay(1, 1.5)
                    except Exception as e:
                        logger.debug("  Extra new slot err: %s", e)
                else:
                    logger.info("  ⤷ No more upload slots for: %s", fname)
        
        if uploaded:
            logger.info("  📎 Uploaded %d/%d extra docs",
                        len(uploaded), len(self.extra_docs))

    # ════════════════════════════════════════════════════════
    #  CONSENT CHECKBOXES + "Godkänn" LINKS
    # ════════════════════════════════════════════════════════

    @staticmethod
    def _tick_consent(page: Page):
        kws = ["agree", "accept", "godkänn", "samtycke", "samtycker",
               "villkor", "gdpr", "personuppgifter", "consent",
               "integritetspolicy", "policy", "hanteras",
               "behandling", "användarvillkor", "terms"]

        # Click "Godkänn ..." links
        for sel in ['a:has-text("Godkänn")', 'button:has-text("Godkänn")',
                    'span:has-text("Godkänn")']:
            try:
                for el in page.locator(sel).all():
                    txt = (el.inner_text() or "").lower()
                    if any(k in txt for k in kws):
                        el.click()
                        _delay(0.3, 0.6)
                        logger.info("  ✓ Clicked: %s", txt[:50])
            except Exception:
                pass

        # Tick consent checkboxes
        for cb in page.locator('input[type="checkbox"]').all():
            try:
                lbl = _label(page, cb).lower()
                nm = (cb.get_attribute("name") or "").lower()
                ctx = f"{lbl} {nm}"
                try:
                    ctx += " " + cb.locator("xpath=../..").inner_text(
                    )[:200].lower()
                except Exception:
                    pass
                if any(k in ctx for k in kws):
                    if not cb.is_checked():
                        try:
                            cb.check(force=True)
                        except Exception:
                            try:
                                cb.locator("xpath=..").click()
                            except Exception:
                                try:
                                    cb.click(force=True)
                                except Exception:
                                    pass
                        _delay(0.3, 0.5)
                        logger.info("  ✓ Checked: %s", lbl[:50])
            except Exception:
                pass

        # Required checkboxes
        for cb in page.locator(
            'input[type="checkbox"][required]'
        ).all():
            try:
                if not cb.is_checked():
                    cb.check(force=True)
                    _delay(0.2, 0.4)
            except Exception:
                pass

    # ════════════════════════════════════════════════════════
    #  LOGIN HANDLING
    # ════════════════════════════════════════════════════════

    def _is_login_page(self, page: Page) -> bool:
        try:
            u = page.url.lower()
            # NEVER detect LinkedIn as a login page (blocked domain)
            if self._is_blocked_domain(u):
                return False
            if any(k in u for k in [
                "varbi.com/login", "varbi.com/logga-in",
            ]):
                return True
            has_pw = page.locator(
                'input[type="password"]:visible').count() > 0
            if not has_pw:
                return False
            body = page.inner_text("body")[:3000].lower()
            return any(k in body for k in [
                "logga in", "sign in", "log in", "login"])
        except Exception:
            return False

    def _do_login(self, page: Page) -> bool:
        if not self._login_email or not self._login_password:
            return False
        url = page.url.lower()
        logger.info("  🔑 Login: %s", url[:60])

        # NEVER auto-login on LinkedIn — will get account banned
        if self._is_blocked_domain(url):
            logger.warning("  ⛔ LinkedIn login blocked — NEVER automate "
                           "LinkedIn login. Apply manually.")
            return False

        try:
            return self._login_generic(page)
        except Exception as e:
            logger.debug("Login error: %s", e)
            return False

    def _login_linkedin(self, page: Page) -> bool:
        """DISABLED — LinkedIn automation causes account bans.
        This function intentionally does nothing and returns False."""
        logger.warning("  ⛔ _login_linkedin called but DISABLED. "
                        "LinkedIn automation is forbidden.")
        return False

    def _login_generic(self, page: Page) -> bool:
        for sel in ['input[type="email"]:visible',
                    'input[name*="email"]:visible',
                    'input[name*="user"]:visible']:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=2000):
                    _type(el, self._login_email)
                    break
            except Exception:
                continue
        try:
            pw = page.locator('input[type="password"]:visible').first
            if pw.is_visible(timeout=2000):
                _type(pw, self._login_password)
        except Exception:
            return False
        for sel in ['button[type="submit"]',
                    'button:has-text("Logga in")',
                    'button:has-text("Log in")']:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    _delay(3, 5)
                    break
            except Exception:
                continue
        _delay(2, 3)
        return page.locator(
            'input[type="password"]:visible').count() == 0

    # ════════════════════════════════════════════════════════
    #  BUTTON FINDER
    # ════════════════════════════════════════════════════════

    @staticmethod
    def _find_button(page: Page, selectors: List[str]):
        """Return the first visible button matching any selector."""
        for s in selectors:
            try:
                btn = page.locator(s).first
                if btn.is_visible(timeout=1500):
                    return btn
            except Exception:
                continue
        return None

    def _click_apply(self, page: Page) -> bool:
        btn = self._find_button(page, SEL_APPLY)
        if btn:
            try:
                btn.click()
                _delay(2, 4)
                return True
            except Exception:
                pass
        return False

    @staticmethod
    def _has_form(page: Page) -> bool:
        try:
            return (page.locator('input[type="file"]').count() > 0
                    or page.locator(
                        'input[type="email"]:visible').count() > 0
                    or page.locator("textarea:visible").count() > 0
                    or page.locator(
                        "input[type='text']:visible").count() > 2)
        except Exception:
            return False

    @staticmethod
    def _is_verify(page: Page) -> bool:
        for s in SEL_VERIFY:
            try:
                if page.locator(s).count() > 0:
                    return True
            except Exception:
                continue
        try:
            body = page.inner_text("body")[:4000].lower()
            if "bankid" in body:
                return True
        except Exception:
            pass
        return False

    # ════════════════════════════════════════════════════════
    #  ASSIST MODE — non-blocking (tab stays open)
    # ════════════════════════════════════════════════════════

    def _assist(self, job, page, ad, ss, reason) -> str:
        """Leave the tab open for the human. Return immediately.
        Prints a cheat sheet with job requirements and writes to CSV."""
        jid = job["job_id"]
        self._ss(page, ad, ss)

        # Pre-fill what we can
        try:
            self._tick_consent(page)
        except Exception:
            pass

        self.db.update_job_status(jid, States.ASSIST)
        self.db.log_event(jid, States.ASSIST, reason)
        self.notifier.notify_human_needed(
            job_title=job.get("title", "?"),
            company=job.get("company", "?"),
            reason=reason, artifact_dir=str(ad))

        self.assist_tabs[jid] = page

        # ── Extract job context for the cheat sheet ──
        title = job.get("title", "?")
        company = job.get("company", "?")
        url = job.get("url", "")
        description = job.get("description", "")
        location = job.get("location", "")

        # Extract key requirements from description
        key_reqs = _extract_requirements(description)
        why_suitable = _generate_suitability_hint(description, self.truth)

        # ── Print rich cheat sheet to terminal ──
        logger.info("🖐 ASSIST: %s @ %s — %s (tab left open)",
                     title, company, reason)
        print(f"\n{'='*70}")
        print(f"🖐 ASSIST: {title} @ {company}")
        print(f"   Reason: {reason}")
        print(f"   URL: {url}")
        if location:
            print(f"   Location: {location}")
        if key_reqs:
            print("   ┌─ What they're looking for:")
            for req in key_reqs[:8]:
                print(f"   │  • {req}")
            print("   └─")
        if why_suitable:
            print("   ┌─ Why you're suitable (copy-paste):")
            print(f"   │  {why_suitable}")
            print("   └─")
        print("   Tab left open — complete manually when ready")
        print("   Agent continues with next job immediately")
        print(f"{'='*70}\n")

        # ── Append to assist_cheatsheet.csv ──
        self._append_assist_csv(job, reason, key_reqs, why_suitable)

        # ── Save full job description to artifact dir for easy reading ──
        try:
            desc_path = Path(ad) / "job_description.txt"
            with open(desc_path, "w", encoding="utf-8") as f:
                f.write(f"Company: {company}\n")
                f.write(f"Title: {title}\n")
                f.write(f"Location: {location}\n")
                f.write(f"URL: {url}\n")
                f.write(f"Assist Reason: {reason}\n")
                f.write(f"\n{'─'*50}\n")
                f.write("KEY REQUIREMENTS:\n")
                for req in key_reqs[:10]:
                    f.write(f"  • {req}\n")
                f.write(f"\n{'─'*50}\n")
                f.write("WHY YOU'RE SUITABLE (copy-paste):\n")
                f.write(f"{why_suitable}\n")
                f.write(f"\n{'─'*50}\n")
                f.write("FULL JOB DESCRIPTION:\n\n")
                f.write(description or "(no description available)")
            logger.debug("  Saved job_description.txt → %s", desc_path)
        except Exception:
            pass

        return States.ASSIST

    def _append_assist_csv(self, job, reason, key_reqs, why_suitable):
        """Append job info to a running CSV for quick reference."""
        import csv
        csv_path = Path(self.cfg.get("output_dir", "outputs")) / "assist_cheatsheet.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not csv_path.exists()

        try:
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow([
                        "timestamp", "company", "title", "location", "url",
                        "reason", "key_requirements", "why_suitable",
                        "description_snippet",
                    ])
                desc = (job.get("description", "") or "")[:1000].replace("\n", " ")
                w.writerow([
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                    job.get("company", ""),
                    job.get("title", ""),
                    job.get("location", ""),
                    job.get("url", ""),
                    reason,
                    " | ".join(key_reqs[:6]),
                    why_suitable,
                    desc,
                ])
            logger.debug("Assist CSV updated: %s", csv_path)
        except Exception as e:
            logger.debug("Assist CSV write error: %s", e)

    # ════════════════════════════════════════════════════════
    #  CHECK ASSIST TABS (called by main loop)
    # ════════════════════════════════════════════════════════

    def check_assist_tabs(self) -> Dict[str, str]:
        """Check if any assist tabs have been completed by human.
        Returns dict of {job_id: new_state} for completed tabs."""
        completed = {}
        closed_ids = []

        for jid, page in self.assist_tabs.items():
            try:
                if page.is_closed():
                    completed[jid] = States.UNCERTAIN
                    closed_ids.append(jid)
                    continue

                body = page.inner_text("body")[:5000].lower()
                for signal in CONFIRM_SIGNALS:
                    if signal in body:
                        completed[jid] = States.CONFIRMED
                        closed_ids.append(jid)
                        logger.info("✅ ASSIST confirmed: %s", jid)
                        try:
                            page.close()
                        except Exception:
                            pass
                        break
            except Exception:
                completed[jid] = States.UNCERTAIN
                closed_ids.append(jid)

        for jid in closed_ids:
            self.assist_tabs.pop(jid, None)
            state = completed.get(jid, States.UNCERTAIN)
            self.db.update_job_status(jid, state)
            if state in States.SUCCESS:
                self.db.increment_daily("applied")

        return completed

    def close_all_assist_tabs(self):
        """Close all remaining assist tabs on exit."""
        for jid, page in self.assist_tabs.items():
            try:
                if not page.is_closed():
                    page.close()
            except Exception:
                pass
        self.assist_tabs.clear()

    # ════════════════════════════════════════════════════════
    #  FAILURE / SCREENSHOTS
    # ════════════════════════════════════════════════════════

    def _fail(self, jid, ad, page, ss, msg):
        self._ss(page, ad, ss)
        self.db.update_job_status(jid, States.FAILED_RETRYABLE)
        self.db.log_event(jid, States.FAILED_RETRYABLE, msg)
        self.db.increment_daily("failed")
        try:
            with open(Path(ad) / "runlog.txt", "a") as f:
                f.write(f"[{datetime.utcnow().isoformat()}] {msg}\n"
                        f"  URL: {page.url}\n")
        except Exception:
            pass
        logger.error("FAILED: %s – %s", jid, msg)

    @staticmethod
    def _ss(page, d, ctr):
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(Path(d) / f"ss_{ctr[0]+1}.png"),
                full_page=False)
        except Exception:
            pass
        ctr[0] += 1


# ════════════════════════════════════════════════════════
#  STATIC HELPERS
# ════════════════════════════════════════════════════════

def _delay(lo=1.0, hi=3.0):
    time.sleep(random.uniform(lo, hi))


def _scroll(page):
    try:
        for _ in range(random.randint(2, 4)):
            page.mouse.wheel(0, random.randint(200, 400))
            time.sleep(random.uniform(0.4, 1.0))
    except Exception:
        pass


def _type(el, val: str):
    try:
        el.fill("")
        _delay(0.1, 0.3)
        el.type(val, delay=random.randint(20, 55))
        _delay(0.2, 0.5)
    except Exception as e:
        logger.debug("Type err: %s", e)


def _fill_long(el, val: str):
    """Fill a textarea with long text using fill() instead of type().
    
    el.type() types character-by-character at 20-55ms each.
    A 2000-char cover letter would take 40-110 seconds and often
    gets truncated. el.fill() sets the value instantly.
    """
    try:
        el.fill("")
        _delay(0.1, 0.3)
        el.fill(val)
        _delay(0.3, 0.6)
        # Trigger change/input events that some forms listen for
        try:
            el.dispatch_event("input")
            el.dispatch_event("change")
        except Exception:
            pass
        _delay(0.2, 0.5)
    except Exception as e:
        # Fallback: try type() with minimal delay for shorter texts
        logger.debug("fill_long fill() failed, trying type(): %s", e)
        try:
            el.fill("")
            el.type(val[:1500], delay=5)  # fast typing, truncate if needed
        except Exception:
            logger.debug("fill_long type() also failed: %s", e)


def _m(text: str, keywords: list) -> bool:
    return any(k in text for k in keywords)


def _sel_opt(sel_el, value: str):
    """Select an option from a <select> by value, label, or partial match."""
    for method in [
        lambda: sel_el.select_option(value=value),
        lambda: sel_el.select_option(label=value),
    ]:
        try:
            method()
            return
        except Exception:
            pass
    try:
        for opt in sel_el.locator("option").all():
            txt = (opt.inner_text() or "").strip()
            if value.lower() in txt.lower():
                v = opt.get_attribute("value")
                if v:
                    sel_el.select_option(value=v)
                    return
    except Exception:
        pass


def _has_country_code_sibling(el) -> bool:
    for xp in ["xpath=..", "xpath=../.."]:
        try:
            par = el.locator(xp)
            if par.locator("select").count() > 0:
                return True
        except Exception:
            pass
    return False


def _label(page: Page, el) -> str:
    """Get label for an input using multiple strategies."""
    # 1) <label for="id">
    try:
        eid = el.get_attribute("id")
        if eid:
            lbl = page.locator(f'label[for="{eid}"]')
            if lbl.count() > 0:
                t = lbl.first.inner_text().strip()
                if t:
                    return t
    except Exception:
        pass
    # 2) Wrapping <label>
    try:
        anc = el.locator("xpath=ancestor::label")
        if anc.count() > 0:
            return anc.first.inner_text().strip()
    except Exception:
        pass
    # 3) Preceding sibling
    try:
        prev = el.locator(
            "xpath=preceding-sibling::*[self::label or self::strong "
            "or self::span or self::div or self::p "
            "or self::h3 or self::h4][1]")
        if prev.count() > 0:
            t = prev.first.inner_text().strip()
            if t and len(t) < 100:
                return t
    except Exception:
        pass
    # 4) Parent text (first line only)
    try:
        ptxt = el.locator("xpath=..").inner_text()
        if ptxt:
            line = ptxt.strip().split("\n")[0].strip()
            if line and len(line) < 100:
                return line
    except Exception:
        pass
    # 5) Grandparent (first line only)
    try:
        gptxt = el.locator("xpath=../..").inner_text()
        if gptxt:
            line = gptxt.strip().split("\n")[0].strip()
            if line and len(line) < 100:
                return line
    except Exception:
        pass
    # 6) aria-label / placeholder
    try:
        a = el.get_attribute("aria-label")
        if a:
            return a
    except Exception:
        pass
    try:
        p = el.get_attribute("placeholder")
        if p:
            return p
    except Exception:
        pass
    return ""


def _section_label(page: Page, file_input) -> str:
    """Get the CLOSEST section heading for a file input."""
    kws = ["cv", "dokument", "brev", "letter", "övriga",
           "other", "resume", "bilaga", "personligt",
           "upload", "ladda", "överför"]

    # Preceding sibling
    try:
        sib = file_input.locator(
            "xpath=preceding-sibling::*[self::label or self::h2 "
            "or self::h3 or self::h4 or self::strong "
            "or self::span or self::p][1]")
        if sib.count() > 0:
            txt = sib.first.inner_text().strip()
            if txt and len(txt) < 80 and \
                    any(k in txt.lower() for k in kws):
                return txt
    except Exception:
        pass

    # Parent/grandparent first lines
    for xp in ["xpath=..", "xpath=../.."]:
        try:
            par = file_input.locator(xp)
            if par.count() > 0:
                txt = par.first.inner_text().strip()
                for line in txt.split("\n")[:2]:
                    line = line.strip()
                    if line and len(line) < 60 and \
                            any(k in line.lower() for k in kws):
                        return line
        except Exception:
            pass

    # aria-label
    try:
        aria = file_input.get_attribute("aria-label")
        if aria and len(aria) < 80:
            return aria
    except Exception:
        pass

    return ""


# ════════════════════════════════════════════════════════
#  ASSIST CHEAT SHEET HELPERS
# ════════════════════════════════════════════════════════

def _extract_requirements(description: str) -> List[str]:
    """Extract key requirements/skills from a job description.
    Returns a list of short requirement strings."""
    if not description:
        return []

    desc_lower = description.lower()
    reqs = []

    # ── Experience level ──
    exp_match = re.search(
        r"(\d+)\+?\s*(?:years?|års?)\s*(?:of\s*)?(?:experience|erfarenhet)",
        desc_lower)
    if exp_match:
        reqs.append(f"{exp_match.group(1)}+ years experience required")

    # ── Education ──
    if re.search(r"\b(master|msc|magister|civilingenjör)\b", desc_lower):
        reqs.append("Master's degree preferred")
    elif re.search(r"\b(bachelor|bsc|kandidat|högskole)\b", desc_lower):
        reqs.append("Bachelor's degree required")

    # ── Swedish language ──
    if re.search(r"\b(svenska|swedish)\b.*\b(flytande|fluent|krav|required|obehövs|native)\b",
                 desc_lower):
        reqs.insert(0, "⚠️ Fluent Swedish required")
    elif re.search(r"\b(svenska|swedish)\b.*\b(merit|plus|bonus|fördel)\b", desc_lower):
        reqs.append("Swedish is a plus (not required)")

    # ── Programming languages & frameworks ──
    tech_patterns = {
        "Python": r"\bpython\b",
        "Java": r"\bjava\b(?!\s*script)",
        "JavaScript / TypeScript": r"\b(javascript|typescript|js|ts)\b",
        "C/C++": r"\b(c\+\+|c/c\+\+)\b",
        "C#/.NET": r"\b(c#|\.net|dotnet)\b",
        "React": r"\breact\b",
        "Node.js": r"\bnode\.?js\b",
        "Angular": r"\bangular\b",
        "Docker / Kubernetes": r"\b(docker|kubernetes|k8s|container)\b",
        "AWS / Azure / GCP": r"\b(aws|amazon web|azure|gcp|google cloud)\b",
        "SQL / Databases": r"\b(sql|postgres|mysql|mongodb|database)\b",
        "CI/CD": r"\b(ci/?cd|jenkins|github actions|gitlab ci|pipeline)\b",
        "Machine Learning / AI": r"\b(machine learning|ml|deep learning|ai|artificial intelligence|tensorflow|pytorch)\b",
        "Cybersecurity": r"\b(security|cyber|penetration|malware|vulnerability|soc|threat)\b",
        "Linux": r"\b(linux|unix|bash|shell)\b",
        "Git": r"\bgit\b",
        "Agile / Scrum": r"\b(agile|scrum|kanban|sprint)\b",
        "REST / API": r"\b(rest\b|api|microservice)",
        "Embedded / IoT": r"\b(embedded|firmware|iot|rtos|fpga)\b",
    }
    for name, pattern in tech_patterns.items():
        if re.search(pattern, desc_lower):
            reqs.append(name)

    # ── Driver's license ──
    if re.search(r"\b(körkort|driver.?s?\s*licen)\b", desc_lower):
        reqs.append("Driver's license mentioned")

    # ── Security clearance ──
    if re.search(r"\b(säkerhetsprövning|security clearance|sekretess)\b", desc_lower):
        reqs.insert(0, "⚠️ Security clearance required")

    return reqs


def _generate_suitability_hint(description: str, truth: dict) -> str:
    """Generate a one-paragraph 'why you're suitable' hint based on job desc."""
    if not description:
        return ""

    desc_lower = description.lower()

    domain_scores = {
        "ai_ml": len(re.findall(
            r"\b(ai|machine learning|deep learning|tensorflow|pytorch|neural|nlp|ml)\b",
            desc_lower)),
        "security": len(re.findall(
            r"\b(security|cyber|penetration|malware|vulnerability|threat|soc)\b",
            desc_lower)),
        "fullstack": len(re.findall(
            r"\b(fullstack|full.?stack|frontend|backend|react|node|javascript|web|angular|vue)\b",
            desc_lower)),
        "cloud_devops": len(re.findall(
            r"\b(cloud|devops|aws|azure|docker|kubernetes|ci/?cd|terraform|infrastructure)\b",
            desc_lower)),
        "data": len(re.findall(
            r"\b(data engineer|data scientist|analytics|etl|sql|spark|big data|warehouse)\b",
            desc_lower)),
        "embedded": len(re.findall(
            r"\b(embedded|firmware|rtos|c\+\+|fpga|iot|hardware|microcontroller)\b",
            desc_lower)),
    }

    best = max(domain_scores, key=domain_scores.get)
    if domain_scores[best] == 0:
        best = "general"

    # CUSTOMIZE: Replace these with YOUR experience summaries.
    # Each domain maps to a short paragraph explaining why you're a good fit.
    hints = {
        "ai_ml": (
            "My master's thesis focused on AI-driven optimization using Python "
            "and TensorFlow. I implemented knowledge distillation for neural "
            "network compression and have hands-on experience with applied "
            "machine learning from multiple coursework projects."
        ),
        "security": (
            "I completed extensive coursework in malware analysis, software "
            "security, and penetration testing. Projects include YARA rule "
            "development, vulnerability assessment, threat modeling, and "
            "building automated detection pipelines."
        ),
        "fullstack": (
            "I built full-stack web applications using the MERN stack "
            "(MongoDB, Express, React, Node.js) with Docker and Kubernetes "
            "deployment, plus REST API design and CI/CD automation."
        ),
        "cloud_devops": (
            "I have hands-on experience with Docker, Kubernetes, AWS, and "
            "CI/CD pipelines from coursework and personal projects. I've "
            "deployed containerized applications and built scalable "
            "infrastructure in cloud environments."
        ),
        "data": (
            "I built data pipelines and decision support systems using Python, "
            "SQL, and analytics frameworks. Experience with ETL processes, "
            "predictive modeling, and data visualization."
        ),
        "embedded": (
            "My CS background includes system-level programming with C/C++, "
            "Linux, and networking. I have experience with embedded workflows, "
            "IoT sensor integration, and hardware-software co-design."
        ),
        "general": (
            "I recently completed my MSc in Computer Science with practical "
            "experience across AI/ML, cybersecurity, full-stack development, "
            "and cloud computing from multiple academic and personal projects."
        ),
    }

    return hints.get(best, hints["general"])