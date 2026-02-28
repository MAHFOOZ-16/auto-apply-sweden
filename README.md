<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/playwright-automation-2EAD33?logo=playwright&logoColor=white" alt="Playwright">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome">
  <img src="https://img.shields.io/badge/platform-Sweden%20🇸🇪-blue" alt="Sweden">
</p>

<h1 align="center">🤖 Auto Apply Sweden</h1>

<p align="center">
  <strong>An AI-powered job application agent for the Swedish job market.</strong><br>
  Fetches jobs from Arbetsförmedlingen → ranks by fit → tailors CV + cover letter → auto-fills and submits applications via browser automation.
</p>

<p align="center">
  <a href="#-quickstart">Quickstart</a> •
  <a href="#-how-it-works">How It Works</a> •
  <a href="#%EF%B8%8F-architecture">Architecture</a> •
  <a href="#-configuration">Configuration</a> •
  <a href="#-contributing">Contributing</a>
</p>

---

## 🎯 What This Does

Applying for jobs in Sweden is repetitive. Most postings live on [Arbetsförmedlingen](https://arbetsformedlingen.se/) and use a handful of ATS platforms (Teamtailor, Varbi, Workday, etc). This agent automates the entire pipeline:

1. **Fetches** new job listings from the Arbetsförmedlingen API
2. **Ranks** them by fit score using your skills taxonomy
3. **Tailors** your CV and cover letter per job using keyword extraction
4. **Generates** LaTeX PDFs (ATS-optimized resume + personalized cover letter)
5. **Auto-fills** the application form via Playwright browser automation
6. **Submits** or falls back to **Assist mode** (leaves tab open for you to finish)
7. **Tracks** everything in SQLite with daily reports

**Day 1 results:** ~45-50 applications submitted out of 60 attempted, with the rest left as pre-filled assist tabs for manual completion.

---

## 🚀 Quickstart

### Prerequisites

- Python 3.10+
- A TeX distribution (`texlive-full` on Linux, `mactex` on macOS)
- Chrome or Chromium browser

### Installation

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/auto-apply-sweden.git
cd auto-apply-sweden

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium

# Install LaTeX (Ubuntu/Debian)
sudo apt install texlive-full    # Full TeX distribution
# macOS: brew install --cask mactex
```

### Setup Your Profile

```bash
# Copy the example files and fill in YOUR details
cp data/truth.example.json data/truth.json
cp data/master_cv.example.json data/master_cv.json

# Edit with your real information
nano data/truth.json        # Personal info, addresses, login credentials
nano data/master_cv.json    # Full CV data (education, experience, projects)
```

**⚠️ IMPORTANT:** Never commit `data/truth.json` or `data/master_cv.json` -- they contain your personal info. They're in `.gitignore` by default.

### (Optional) Add Extra Documents

Place your supporting documents in `data/`:
```
data/degree_certificate.pdf
data/transcript.pdf
data/sample_work.pdf
```

Update the paths in `data/truth.json` under `extra_documents`.

### Run

```bash
# First run -- resets database, opens browser for initial login
python -m agent.main --reset --no-exit

# Normal daily run
python -m agent.main

# Dry run (fetch + rank, no applications)
python -m agent.main --dry-run
```

On first run, the agent opens Chrome and you may need to log in to job platforms manually. Your session is saved in `secrets/browser_profile/` for subsequent runs.

---

## 🔧 How It Works

### The Application Pipeline

```
┌─────────────┐     ┌──────────┐     ┌──────────┐     ┌───────────┐
│  Fetch Jobs  │────▶│  Rank &  │────▶│  Tailor  │────▶│  Generate │
│  (AF API)    │     │  Filter  │     │  CV/CL   │     │  LaTeX    │
└─────────────┘     └──────────┘     └──────────┘     └───────────┘
                                                            │
                         ┌──────────────────────────────────┘
                         ▼
                  ┌──────────────┐     ┌────────────┐     ┌───────────┐
                  │  Open Page   │────▶│  Fill Form │────▶│  Submit   │
                  │  (Playwright)│     │  (Auto)    │     │  or Assist│
                  └──────────────┘     └────────────┘     └───────────┘
```

### Platform Detection

The agent detects which ATS platform a job uses and adapts its strategy:

| Platform | Mode | Multi-step | Notes |
|----------|------|------------|-------|
| Teamtailor | AUTO | ✅ (3 steps) | Most common in Sweden |
| Varbi | AUTO | ❌ | Single-page forms |
| ReachMee | AUTO | ❌ | Single-page |
| Jobylon | AUTO | ❌ | Single-page |
| Framtiden/Zoho | AUTO | ✅ (2 steps) | Municipal jobs |
| Workday | TRY_AUTO | ✅ (5 steps) | Complex - may need assist |
| SuccessFactors | TRY_AUTO | ✅ (4 steps) | SAP-based |
| SmartRecruiters | TRY_AUTO | ✅ (3 steps) | |
| Lever | TRY_AUTO | ❌ | |
| Greenhouse | TRY_AUTO | ✅ (3 steps) | |
| LinkedIn | **SKIP** | - | ⛔ Never automated (ban risk) |

### Assist Mode

When the agent can't fully complete a form (unknown mandatory fields, CAPTCHA, complex multi-step), it:

1. Pre-fills everything it can
2. Takes a screenshot
3. Leaves the tab open
4. Prints a **cheat sheet** to terminal with job requirements + suggested answers
5. Saves to `outputs/assist_cheatsheet.csv`
6. Continues to the next job immediately

When the daily cap is reached, the browser **stays open** so you can finish assist tabs. Press `Ctrl+C` and Chrome keeps running with your tabs intact.

### Anti-Detection (Stealth)

The agent includes stealth measures to avoid bot detection:
- `navigator.webdriver` hidden
- `window.chrome` runtime spoofed
- `navigator.plugins` faked (PDF viewer, etc.)
- Human-like typing delays and scroll behavior
- LinkedIn is **completely blocked** -- never visits, never logs in

---

## 🏗️ Architecture

```
auto-apply-sweden/
├── agent/                     # Core Python package
│   ├── __init__.py            # States enum (CONFIRMED, ASSIST, etc.)
│   ├── __main__.py            # Entry point
│   ├── main.py                # Orchestrator -- daily loop, scheduling
│   ├── apply_runner.py        # Browser automation -- form filling, submit
│   ├── platform_classifier.py # Detects ATS platform from URL/HTML
│   ├── answer_library.py      # Pattern-matched answers for form fields
│   ├── pdf_export.py          # LaTeX → PDF generation (emoji-safe)
│   ├── job_fetcher.py         # Arbetsförmedlingen API client
│   ├── ranker.py              # Fit scoring against skill taxonomy
│   ├── tailor.py              # CV/cover letter personalization
│   ├── scheduler.py           # Daily caps, ramp-up logic
│   ├── db.py                  # SQLite persistence layer
│   └── notifier.py            # Desktop/sound notifications
├── data/
│   ├── truth.example.json     # Your personal info template
│   ├── master_cv.example.json # Your CV data template
│   └── skill_taxonomy.json    # Skill matching rules
├── templates/
│   ├── resume_ats.tex.j2      # ATS-optimized LaTeX resume template
│   └── cover_letter.tex.j2    # Cover letter LaTeX template
├── config.yaml                # Runtime configuration
├── requirements.txt           # Python dependencies
└── secrets/                   # Browser profile (gitignored)
```

### Key Design Decisions

- **LaTeX for PDFs** - ATS systems parse LaTeX-generated PDFs better than Word. Emoji stripped, Swedish characters (ä å ö é) preserved.
- **Multi-step form loop** - Teamtailor uses 3-step forms. The agent handles "Nästa" (Next) vs "Skicka ansökan" (Submit) correctly.
- **Validation error detection** - After clicking submit, checks for Swedish/English error messages, `aria-invalid` attributes, and empty required fields. Falls back to Assist instead of losing the application.
- **Progressive daily caps** - Starts at 60/day, ramps to 80 after 3 stable days, 100 after 6 days.

---

## ⚙️ Configuration

### `config.yaml`

```yaml
# Job search
search_keywords: ["software", "developer", "engineer", "IT"]
location: "Sweden"
max_jobs_per_fetch: 100

# Daily limits
daily_cap_initial: 60
daily_cap_ramp_levels: [80, 100]
ramp_after_stable_days: 3

# Browser
headless_browser: false          # Set true for server/CI
browser_user_data_dir: "./secrets/browser_profile"

# Paths
truth_path: "data/truth.json"
cv_path: "data/master_cv.json"
output_dir: "outputs"
```

### `data/truth.json`

This is the most important file. It contains:

| Section | What to fill |
|---------|-------------|
| `personal` | Name, email, phone, personnummer |
| `addresses` | Street addresses for Stockholm, Malmö, etc. |
| `form_defaults` | Salary (SEK/month), notice period, work permit status |
| `login` | Email/password for Varbi and other platforms |
| `links` | LinkedIn URL, GitHub URL, portfolio |
| `extra_documents` | Paths to degree, transcript, sample work PDFs |

### `data/master_cv.json`

Your complete CV in structured JSON. Includes:
- Contact info, summary
- Skills (languages, frameworks, tools)
- Education (with courses)
- Work experience (with bullet points)
- Projects (with tech stack)
- Certifications, achievements, extracurriculars

---

## 📊 Output Files

After each run, you'll find:

| File | Contents |
|------|----------|
| `outputs/YYYY-MM-DD/all_jobs.csv` | Every job attempted with result |
| `outputs/assist_cheatsheet.csv` | Assist jobs with requirements + suggested answers |
| `outputs/YYYY-MM-DD/Company_Title/` | Per-job artifacts (resume, cover letter, screenshots) |
| `outputs/YYYY-MM-DD/Company_Title/job_description.txt` | Full job description + key requirements |
| `db/agent.db` | SQLite database with full history |
| `logs/agent.log` | Detailed runtime logs |

---

## 🤝 Contributing

We welcome contributions! This project is built for the Swedish job market but the architecture is adaptable to other countries.

### Ways to contribute:

- 🌍 **Add a new country** - Write a new job fetcher for your country's employment API
- 🏢 **Add ATS platform support** - Add detection rules in `platform_classifier.py`
- 🧠 **Improve the answer library** - Add patterns for common form questions
- 🐛 **Fix bugs** - Check the [Issues](../../issues) tab
- 📖 **Improve docs** - Better setup guides, translations, examples
- 🧪 **Add tests** - We need them!

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed guidelines.

### Quick PR workflow:

```bash
git fork → git clone → git checkout -b feature/my-feature
# Make changes
python -m agent.main --dry-run   # Test without submitting
git commit -m "feat: add X support"
git push origin feature/my-feature
# Open PR on GitHub
```

---

## ⚠️ Disclaimer

This tool automates job applications on your behalf. Please:

- **Never use on LinkedIn** - Automating LinkedIn will get your account banned. This tool blocks LinkedIn by default.
- **Review assist tabs** - Always review pre-filled applications before submitting manually.
- **Respect rate limits** - The default cap of 60/day is reasonable. Don't crank it to 1000.
- **Keep it legal** - This tool fills forms with YOUR real information. Don't use it to spam or mislead employers.
- **No guarantees** - This is an open-source tool. Use at your own risk.

---

## 📜 License

[MIT License](LICENSE) - use it, modify it, share it. Just don't blame us if something goes sideways.

---

## ⭐ Star History

If this project helped you land a job in Sweden, give it a ⭐! It helps others find it.

---

<p align="center">
  Built with ☕ and frustration at filling the same form 60 times a day.<br>
  <strong>Automate the boring stuff. Focus on the interviews.</strong>
</p>