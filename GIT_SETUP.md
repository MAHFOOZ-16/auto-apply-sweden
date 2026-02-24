# 🚀 How to Push This to GitHub

## Step 1: Create the Repository

1. Go to [github.com/new](https://github.com/new)
2. **Repository name:** `auto-apply-sweden`
3. **Description:** `🤖 AI-powered job application agent for the Swedish job market. Auto-fills and submits applications on Arbetsförmedlingen using browser automation.`
4. Set to **Public**
5. Do NOT initialize with README (we already have one)
6. Click **Create repository**

## Step 2: Push Your Code

```bash
# Navigate to the project
cd auto-apply-sweden

# Initialize git
git init
git branch -M main

# Add all files
git add .

# Verify no personal data is staged
git diff --cached --name-only | grep -E "truth\.json|master_cv\.json|secrets/" && echo "⚠️  STOP! Personal files detected!" || echo "✅ Safe to commit"

# Commit
git commit -m "feat: initial release — AI job application agent for Sweden

- Auto-fetch jobs from Arbetsförmedlingen API
- Platform-aware form filling (Teamtailor, Varbi, Workday, etc.)
- LaTeX resume + cover letter generation
- Multi-step form handling with Nästa/Submit detection
- Assist mode for complex forms (leaves tab open)
- Anti-detection stealth (LinkedIn blocked)
- Progressive daily caps (60 → 80 → 100)
- SQLite tracking + CSV reports"

# Add your GitHub remote
git remote add origin https://github.com/YOUR_USERNAME/auto-apply-sweden.git

# Push
git push -u origin main
```

## Step 3: Add Repository Topics (for discoverability)

Go to your repo page → click the ⚙️ gear icon next to "About" → add these **Topics**:

```
job-automation, sweden, arbetsformedlingen, playwright, browser-automation,
job-application, ats, resume-builder, cover-letter, python, ai-agent,
job-search, career, automation, open-source, teamtailor, workday
```

## Step 4: Create a Release

```bash
git tag -a v1.0.0 -m "v1.0.0 — Initial public release"
git push origin v1.0.0
```

Then on GitHub → Releases → **Draft a new release** → Select tag `v1.0.0` → Add release notes.

## Step 5: Boost Discoverability

### SEO-optimized description for GitHub:
> 🤖 AI-powered job application agent for Sweden. Fetches jobs from Arbetsförmedlingen, generates tailored LaTeX resumes, auto-fills ATS forms (Teamtailor, Varbi, Workday), and submits applications via Playwright. Handles multi-step forms, Swedish/English fields, file uploads. Built-in anti-detection. Open source, MIT licensed.

### Share on:
- **Reddit:** r/sweden, r/TillSverige, r/cscareerquestions, r/automation, r/Python
- **Hacker News:** Show HN post
- **LinkedIn:** (manually — don't automate this!)
- **Swedish tech communities:** Slack/Discord groups for developers in Sweden

### Suggested post title:
> "I built an open-source AI agent that auto-applies to 60 jobs/day on Arbetsförmedlingen (Swedish job market)"

## 🛡️ Security Checklist Before Pushing

Run this to make sure no personal data is in the repo:

```bash
# Check for sensitive files
git ls-files | grep -E "truth\.json|master_cv\.json|\.pdf|secrets/"

# Check for emails/phones in code
grep -rn "@gmail\|personnummer\|password" agent/ data/ --include="*.py" --include="*.json" | grep -v "example\|YOUR_\|PASSWORD_HERE"

# Check for addresses
grep -rn "gatan\|vägen" agent/ data/ --include="*.py" --include="*.json" | grep -v "Storgatan\|Kungsgatan\|example"
```

All three commands should return empty (no results). If they find something, fix it before pushing!
