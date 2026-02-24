# Contributing to Auto Apply Sweden

Thanks for your interest in contributing! This guide will help you get started.

## 🏗️ Development Setup

```bash
# Fork and clone
git clone https://github.com/YOUR_USERNAME/auto-apply-sweden.git
cd auto-apply-sweden

# Create a virtual environment
python -m venv venv
source venv/bin/activate   # Linux/Mac
# venv\Scripts\activate    # Windows

# Install dependencies
pip install -r requirements.txt
pip install ruff pytest     # Dev tools

# Install Playwright
playwright install chromium

# Copy example data (DO NOT commit real data)
cp data/truth.example.json data/truth.json
cp data/master_cv.example.json data/master_cv.json
```

## 📁 Project Structure

```
agent/
├── main.py              # Orchestrator (start here to understand the flow)
├── apply_runner.py      # Browser automation (the big one — ~2000 lines)
├── platform_classifier.py  # ATS detection rules
├── answer_library.py    # Form field → answer matching
├── job_fetcher.py       # Arbetsförmedlingen API
├── ranker.py            # Job fit scoring
├── tailor.py            # CV/cover letter customization
├── pdf_export.py        # LaTeX → PDF
├── scheduler.py         # Daily cap logic
├── db.py                # SQLite layer
└── notifier.py          # Notifications
```

## 🔀 Branching & Pull Requests

1. Create a feature branch: `git checkout -b feature/my-feature`
2. Make your changes
3. Test with `--dry-run`: `python -m agent.main --dry-run`
4. Lint: `ruff check agent/`
5. Commit with a clear message: `git commit -m "feat: add Greenhouse support"`
6. Push and open a PR

### Commit message format

```
feat: add new feature
fix: fix a bug
docs: documentation only
refactor: code restructure without behavior change
test: add or fix tests
```

## 🎯 Areas Where Help Is Needed

### 🌍 Country Support (High Impact)
The biggest opportunity. Currently this only works with Sweden's Arbetsförmedlingen API. To add a new country:

1. Create `agent/fetchers/your_country_fetcher.py`
2. Implement `fetch_round()` that returns jobs in the standard format:
   ```python
   {"job_id": "...", "title": "...", "company": "...", "url": "...",
    "description": "...", "location": "...", "source": "your_api"}
   ```
3. The rest of the pipeline (ranking, tailoring, form filling) works regardless of country
4. Add a config option to select the fetcher

### 🏢 ATS Platform Support
To add a new platform:

1. Add detection rules in `platform_classifier.py`:
   ```python
   "new_platform": {
       "url_patterns": ["newplatform.com"],
       "html_signals": ["NewPlatform"],
       "mode": "TRY_AUTO",  # or AUTO if confident
       "multi_step": True,
       "max_steps": 3,
   }
   ```
2. Test with real job listings from that platform
3. Add any platform-specific form handling in `apply_runner.py`

### 🧠 Answer Library
Common form fields that need answers:

- Add regex patterns to `STATIC_PATTERNS` in `answer_library.py`
- Test Swedish AND English variants
- Include dropdown handling in `match_dropdown()`

### 🐛 Known Issues
- Workday forms are extremely complex (5+ steps, dynamic fields)
- Some Teamtailor sites have custom layouts that break detection
- File upload on drag-and-drop-only platforms needs work
- No test suite yet (biggest technical debt)

## 🧪 Testing

Currently there are no automated tests (contributions very welcome!). To test manually:

```bash
# Dry run — fetches and ranks but doesn't apply
python -m agent.main --dry-run

# Single job test — apply to one specific URL
python -m agent.main --test-url "https://example.com/job/123"

# Reset database and start fresh
python -m agent.main --reset
```

## 📐 Code Style

- We use **ruff** for linting: `ruff check agent/`
- Max line length: 100 characters
- Type hints encouraged but not required
- Docstrings for public methods
- Log important actions with `logger.info()`
- Use emoji in logs for visual scanning (✅ ❌ 🖐 ⚠️ etc.)

## 🔒 Security Guidelines

- **NEVER** commit personal data (`truth.json`, `master_cv.json`, `secrets/`)
- **NEVER** add LinkedIn automation — it will get users banned
- Be careful with browser automation on any platform that forbids it
- All credentials should come from `truth.json`, never hardcoded

## 💬 Communication

- **Issues** — Bug reports and feature requests
- **Discussions** — Questions, ideas, architecture decisions
- **Pull Requests** — Code contributions

## 📜 License

By contributing, you agree that your contributions will be licensed under the MIT License.
