"""
Main entry point for the Arbetsförmedlingen Job Application Agent.

Usage:
    python -m agent.main
    python -m agent.main --config path/to/config.yaml
    python -m agent.main --reset
    python -m agent.main --reset --no-exit
"""

import argparse
import json
import logging
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import yaml

from agent import States
from agent.db import AgentDB
from agent.job_fetcher import JobFetcher
from agent.ranker import JobRanker
from agent.tailor import Tailor
from agent.pdf_export import PDFExporter
from agent.apply_runner import ApplyRunner
from agent.scheduler import Scheduler
from agent.notifier import Notifier


# ──────────────────────────────────────────────
#  Logging
# ──────────────────────────────────────────────

def setup_logging(log_dir: str):
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    dfmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    if root.handlers:
        root.handlers.clear()

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(fmt, dfmt))
    root.addHandler(ch)

    fh = logging.FileHandler(log_path / "agent.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt, dfmt))
    root.addHandler(fh)

    eh = logging.FileHandler(log_path / "errors.log", encoding="utf-8")
    eh.setLevel(logging.ERROR)
    eh.setFormatter(logging.Formatter(fmt, dfmt))
    root.addHandler(eh)


logger = logging.getLogger("agent.main")


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def make_artifact_dir(output_base: str, job: Dict) -> Path:
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    company = (job.get("company") or "Unknown").replace(" ", "_")[:30]
    title = (job.get("title") or "Job").replace(" ", "_")[:30]
    job_id = job.get("job_id", "x")[:20]
    folder = f"{company}_{title}_{job_id}"
    folder = "".join(c for c in folder if c.isalnum() or c in "_-")
    path = Path(output_base) / date_str / folder
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_artifacts(d: Path, job_json: Dict, kw: list, cover_text: str = ""):
    with open(d / "job.json", "w", encoding="utf-8") as f:
        json.dump(job_json, f, indent=2, ensure_ascii=False, default=str)
    with open(d / "extracted_keywords.json", "w", encoding="utf-8") as f:
        json.dump(kw, f, indent=2, ensure_ascii=False)
    if cover_text:
        with open(d / "cover_letter.txt", "w", encoding="utf-8") as f:
            f.write(cover_text)


# ──────────────────────────────────────────────
#  Reset helpers
# ──────────────────────────────────────────────

def _unlink_if_exists(p: Path) -> bool:
    try:
        if p.exists():
            p.unlink()
            return True
    except Exception:
        return False
    return False


def _clear_dir_files(dir_path: Path) -> int:
    deleted = 0
    try:
        if not dir_path.exists() or not dir_path.is_dir():
            return 0
        for f in dir_path.glob("*"):
            try:
                if f.is_file():
                    f.unlink()
                    deleted += 1
            except Exception:
                continue
    except Exception:
        return deleted
    return deleted


def reset_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    db_path = Path(cfg.get("db_path", "db/agent.sqlite"))
    logs_dir = Path(cfg.get("log_dir", "logs"))

    db_deleted = _unlink_if_exists(db_path)
    # Also remove WAL and SHM files
    _unlink_if_exists(db_path.with_suffix(".sqlite-wal"))
    _unlink_if_exists(db_path.with_suffix(".sqlite-shm"))

    logs_deleted = _clear_dir_files(logs_dir)

    return {
        "db_path": str(db_path),
        "db_deleted": db_deleted,
        "logs_dir": str(logs_dir),
        "logs_files_deleted": logs_deleted,
    }


# ──────────────────────────────────────────────
#  Agent
# ──────────────────────────────────────────────

class Agent:
    """Top-level orchestrator - V2 with assist mode + summary report."""

    def __init__(self, config: Dict[str, Any]):
        self.cfg = config
        self.running = True

        self.db = AgentDB(config.get("db_path", "db/agent.sqlite"))
        self.notifier = Notifier(config)
        self.fetcher = JobFetcher(self.db, config)
        self.ranker = JobRanker(self.db, config)
        self.tailor = Tailor(self.db, config)
        self.pdf = PDFExporter(config)
        self.scheduler = Scheduler(self.db, config)
        self.runner = ApplyRunner(self.db, config, self.notifier)

        self._last_fetch: float = 0
        self._fetch_iv = config.get("job_fetch_interval_minutes", 15) * 60
        self._max_retries = config.get("max_retries_per_job", 3)

        # V2 tracking
        self._results = {
            "confirmed": [], "uncertain": [], "assist": [],
            "failed": [], "skipped": [],
        }

    def run(self):
        logger.info("═" * 60)
        logger.info("  Arbetsförmedlingen Job Agent v2.0 – Starting")
        logger.info("═" * 60)

        signal.signal(signal.SIGINT, self._signal)
        signal.signal(signal.SIGTERM, self._signal)

        self._resume_interrupted()

        try:
            self.runner.start_browser()
        except Exception as exc:
            logger.error("Browser start failed: %s", exc)
            logger.info("Run: playwright install chromium")
            sys.exit(1)

        try:
            while self.running and self.scheduler.should_continue():
                self._tick()

                # Check if daily cap reached → stop NEW applications
                if not self.scheduler.can_apply_now():
                    logger.info("Daily cap reached. Generating summary...")
                    break
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt – stopping new applications")

        # ─ Summary of the auto-apply phase ─
        self._generate_summary()

        # ─ If assist tabs are open, keep browser alive ─
        assist_count = len(self.runner.assist_tabs)
        if assist_count > 0:
            self._wait_for_assist_tabs()
            # After wait: check if any tabs still open
            remaining = len(self.runner.assist_tabs)
            if remaining > 0:
                # User pressed Ctrl+C -- DETACH from browser, leave it running
                self._generate_summary()
                print(f"\n  🟢 Browser left running with {remaining} tab(s).")
                print("     Finish your applications, then close Chrome manually.")
                print("     Agent process exiting - your tabs are SAFE.\n")
                logger.info("Detaching - %d assist tabs left in browser",
                            remaining)
                # Do NOT call stop_browser() -- leave Chrome alive
                try:
                    self._pw_detach()
                except Exception:
                    pass
                sys.exit(0)

        # No assist tabs → clean shutdown
        self.runner.stop_browser()
        logger.info("Agent stopped. Goodbye!")
        sys.exit(0)

    def _pw_detach(self):
        """Disconnect from Playwright without killing the browser.
        This leaves Chrome running with all open tabs intact.
        
        We null out all Playwright references so that when Python exits,
        the garbage collector doesn't try to clean up (which would kill Chrome).
        The Chrome process continues as a standalone OS process.
        """
        try:
            # Clear references so nothing tries to close them on exit
            self.runner.assist_tabs.clear()
            self.runner._ctx = None
            self.runner._pw = None
        except Exception:
            pass

    def _wait_for_assist_tabs(self):
        """Keep browser alive while assist tabs are open.
        Polls every 30s. Returns when all tabs completed OR user presses Ctrl+C.
        NEVER closes assist tabs - that's the user's job."""
        assist_count = len(self.runner.assist_tabs)
        if assist_count == 0:
            return

        print(f"\n{'='*70}")
        print(f"  ⏳ DAILY CAP REACHED - {assist_count} ASSIST tab(s) still open!")
        print("")
        print("  The browser will stay open so you can finish these applications.")
        print("  The agent checks every 30s for completed tabs.")
        print("")
        for jid, page in self.runner.assist_tabs.items():
            try:
                title = page.title()[:50] if not page.is_closed() else "(closed)"
            except Exception:
                title = "(unknown)"
            print(f"    🖐 {jid[:20]} -- {title}")
        print("")
        print("  When you finish all tabs, the agent exits automatically.")
        print("  Press Ctrl+C to exit - browser stays open with your tabs.")
        print(f"{'='*70}\n")

        try:
            while self.runner.assist_tabs:
                # Check if any assist tabs were completed by the user
                completed = self.runner.check_assist_tabs()
                for jid, state in completed.items():
                    entry = f"{jid} (assist→{state})"
                    if state == States.CONFIRMED:
                        self._results["confirmed"].append(entry)
                        print(f"  ✅ Assist tab confirmed: {jid}")
                    else:
                        self._results["uncertain"].append(entry)
                        print(f"  ❓ Assist tab closed: {jid}")

                remaining = len(self.runner.assist_tabs)
                if remaining == 0:
                    print("\n  ✅ All assist tabs completed! Exiting cleanly.")
                    break

                # Sleep in small chunks so Ctrl+C is responsive
                for _ in range(6):  # 6 × 5s = 30s check interval
                    if not self.runner.assist_tabs:
                        break
                    time.sleep(5)

        except KeyboardInterrupt:
            remaining = len(self.runner.assist_tabs)
            logger.info("Ctrl+C during assist wait -- %d tabs still open",
                        remaining)
            # Do NOT close tabs -- just return. Caller will detach browser.

    def _tick(self):
        now = time.time()

        # Check assist tabs periodically
        completed = self.runner.check_assist_tabs()
        for jid, state in completed.items():
            entry = f"{jid} (assist→{state})"
            if state == States.CONFIRMED:
                self._results["confirmed"].append(entry)
            else:
                self._results["uncertain"].append(entry)

        if now - self._last_fetch >= self._fetch_iv:
            try:
                self.fetcher.fetch_round()
                self.ranker.rank_discovered()
                self._last_fetch = now
                logger.info("Queue: %d", self.db.queue_size())
            except Exception as exc:
                logger.error("Fetch/rank error: %s", exc)

        self._requeue_retryable()

        if not self.scheduler.can_apply_now():
            return  # Will trigger exit in run()

        job = self.db.dequeue_next()
        if not job:
            time.sleep(30)
            return

        logger.info("─" * 50)
        logger.info(
            "Processing: %s at %s (fit=%.0f)",
            job.get("title"), job.get("company"), job.get("fit_score", 0),
        )
        logger.info("%s", self.scheduler.status_summary())

        result = self._process(job)

        # Buffer timing based on result
        if result in (States.CONFIRMED, States.SUBMITTED, States.UNCERTAIN):
            self.scheduler.wait_buffer()
        elif result == States.ASSIST:
            # Non-blocking -- immediate next job with minimal delay
            time.sleep(random.randint(3, 8))
        else:
            # Failed -- short delay
            time.sleep(random.randint(5, 15))

    def _process(self, job: Dict) -> str:
        job_id = job["job_id"]
        output_base = self.cfg.get("output_dir", "outputs")

        try:
            ad = make_artifact_dir(output_base, job)
            tailored = self.tailor.tailor_for_job(job)

            cover_text = tailored.get("cover_letter_text", "")
            job_keywords = tailored.get("extracted_keywords", [])
            save_artifacts(
                ad, tailored["job_json"],
                job_keywords, cover_text,
            )

            self.db.update_job_status(job_id, States.READY_TO_APPLY)
            resume_path, cover_path = self.pdf.export(
                tailored["resume_data"],
                tailored["cover_letter_data"],
                ad,
            )

            app_id = self.db.insert_application(
                job_id=job_id,
                status=States.READY_TO_APPLY,
                artifact_dir=str(ad),
                resume_path=str(resume_path),
                cover_path=str(cover_path),
            )

            # V2: pass job_keywords for answer library matching
            result = self.runner.apply_to_job(
                job, resume_path, cover_path, ad,
                cover_text=cover_text,
                job_keywords=job_keywords,
            )

            entry = f"{job.get('title', '?')} at {job.get('company', '?')}"

            # Log ALL jobs to master CSV for reference
            self._log_job_csv(job, result, str(ad))

            if result == States.CONFIRMED:
                self.db.update_application(app_id, status=States.CONFIRMED)
                self.scheduler.report_success()
                self._results["confirmed"].append(entry)
                return States.CONFIRMED

            elif result == States.UNCERTAIN:
                self.db.update_application(app_id, status=States.UNCERTAIN)
                self.scheduler.report_success()
                self._results["uncertain"].append(entry)
                return States.UNCERTAIN

            elif result == States.SUBMITTED:
                self.db.update_application(app_id, status=States.SUBMITTED)
                self.scheduler.report_success()
                self._results["confirmed"].append(entry)
                return States.SUBMITTED

            elif result == States.ASSIST:
                self.db.update_application(
                    app_id, status=States.ASSIST)
                self.scheduler.report_success()  # counts toward daily cap
                self._results["assist"].append(entry)
                return States.ASSIST

            elif result == States.FAILED_RETRYABLE:
                self.db.update_application(
                    app_id, status=States.FAILED_RETRYABLE)
                self.scheduler.report_failure()
                self._results["failed"].append(entry)
                return States.FAILED_RETRYABLE

            elif result == States.FAILED_PERMANENT:
                self.db.update_application(
                    app_id, status=States.FAILED_PERMANENT)
                self.db.update_job_status(job_id, States.FAILED_PERMANENT)
                self.scheduler.report_failure()
                self._results["failed"].append(entry)
                return States.FAILED_PERMANENT

            else:
                self.db.update_application(
                    app_id, status=result or States.FAILED_RETRYABLE)
                self.scheduler.report_failure()
                self._results["failed"].append(entry)
                return States.FAILED_RETRYABLE

        except Exception as exc:
            logger.error("Pipeline error %s: %s", job_id, exc, exc_info=True)
            self.db.update_job_status(job_id, States.FAILED_RETRYABLE)
            self.db.log_event(job_id, States.FAILED_RETRYABLE, str(exc))
            self.db.increment_daily("failed")
            self.scheduler.report_failure()
            entry = f"{job.get('title', '?')} at {job.get('company', '?')}"
            self._results["failed"].append(entry)
            return States.FAILED_RETRYABLE

    def _log_job_csv(self, job: Dict, result: str, artifact_dir: str):
        """Log every processed job to all_jobs.csv for complete reference."""
        import csv
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        csv_path = Path(self.cfg.get("output_dir", "outputs")) / date_str / "all_jobs.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not csv_path.exists()
        try:
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow([
                        "Time", "Company", "Title", "Location", "Result",
                        "URL", "Fit_Score", "Description",
                    ])
                desc = (job.get("description", "") or "")[:1500].replace("\n", " ")
                w.writerow([
                    datetime.utcnow().strftime("%H:%M"),
                    job.get("company", ""),
                    job.get("title", ""),
                    job.get("location", ""),
                    result,
                    job.get("url", ""),
                    job.get("fit_score", 0),
                    desc,
                ])
        except Exception:
            pass

    def _resume_interrupted(self):
        resumable = self.db.get_resumable_jobs()
        if not resumable:
            return
        logger.info("Resuming %d interrupted jobs", len(resumable))
        for job in resumable:
            s = job["status"]
            jid = job["job_id"]
            if s in (States.READY_TO_APPLY, States.APPLYING):
                self.db.update_job_status(jid, States.READY_TO_APPLY)
                self.db.enqueue(jid, priority=90)
            elif s in (States.WAITING_FOR_HUMAN, States.ASSIST):
                self.db.update_job_status(jid, States.READY_TO_APPLY)
                self.db.enqueue(jid, priority=20)

    def _requeue_retryable(self):
        retryable = self.db.get_retryable_jobs(self._max_retries)
        for job in retryable:
            self.db.update_job_status(job["job_id"], States.QUEUED)
            self.db.enqueue(job["job_id"], priority=30)
        if retryable:
            logger.info("Re-queued %d retryable", len(retryable))

    def _generate_summary(self):
        """Generate and print a day-end summary report."""
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        r = self._results

        n_confirmed = len(r["confirmed"])
        n_uncertain = len(r["uncertain"])
        n_assist = len(r["assist"])
        n_failed = len(r["failed"])
        total = n_confirmed + n_uncertain + n_assist + n_failed
        assist_open = len(self.runner.assist_tabs)

        report_lines = [
            "",
            "═" * 60,
            f"   JOB AGENT -- DAY SUMMARY ({date_str})",
            "═" * 60,
            "",
            f"   ✅ CONFIRMED:    {n_confirmed}  "
            "(confirmation page detected)",
            f"   ❓ UNCERTAIN:    {n_uncertain}  "
            "(submitted, no confirmation)",
            f"   🖐 ASSIST:       {n_assist}  "
            "(tabs left open for you)",
            f"   ❌ FAILED:       {n_failed}  "
            "(could not submit)",
            f"   📊 TOTAL:        {total}",
            "",
        ]

        if r["confirmed"]:
            report_lines.append("   CONFIRMED applications:")
            for i, entry in enumerate(r["confirmed"], 1):
                report_lines.append(f"   {i:2d}. {entry}  ✅")

        if r["uncertain"]:
            report_lines.append("")
            report_lines.append("   UNCERTAIN (check emails):")
            for i, entry in enumerate(r["uncertain"], 1):
                report_lines.append(f"   {i:2d}. {entry}  ❓")

        if r["assist"]:
            report_lines.append("")
            report_lines.append(f"   ASSIST tabs ({assist_open} still open):")
            for i, entry in enumerate(r["assist"], 1):
                report_lines.append(f"   {i:2d}. {entry}  🖐")

        if r["failed"]:
            report_lines.append("")
            report_lines.append("   FAILED (will retry tomorrow):")
            for i, entry in enumerate(r["failed"], 1):
                report_lines.append(f"   {i:2d}. {entry}  ❌")

        cap = self.scheduler._current_cap()
        report_lines.extend([
            "",
            f"   Daily cap: {cap} "
            f"(increases after {self.scheduler.ramp_after_days} stable days)",
            "═" * 60,
            "",
        ])

        report = "\n".join(report_lines)

        # Print to terminal
        print(report)
        logger.info(report)

        # Save to file
        try:
            out_dir = Path(self.cfg.get("output_dir", "outputs")) / date_str
            out_dir.mkdir(parents=True, exist_ok=True)

            with open(out_dir / "summary.txt", "w", encoding="utf-8") as f:
                f.write(report)

            with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
                json.dump({
                    "date": date_str,
                    "confirmed": n_confirmed,
                    "uncertain": n_uncertain,
                    "assist": n_assist,
                    "failed": n_failed,
                    "total": total,
                    "details": r,
                }, f, indent=2, ensure_ascii=False)

            logger.info("Summary saved: %s", out_dir / "summary.txt")
        except Exception as e:
            logger.error("Summary save error: %s", e)

    def _signal(self, signum, frame):
        logger.info("Signal %d – stopping after current job", signum)
        self.running = False


def main():
    parser = argparse.ArgumentParser(description="AF Job Agent")
    parser.add_argument("--config", default="config.yaml", help="Config path")
    parser.add_argument(
        "--reset", action="store_true",
        help="Delete DB and logs, then exit (or continue with --no-exit).",
    )
    parser.add_argument(
        "--no-exit", action="store_true",
        help="With --reset: reset then continue running.",
    )

    args = parser.parse_args()
    config = load_config(args.config)

    if args.reset:
        summary = reset_state(config)
        print("✅ Reset complete")
        print(f"  DB: {summary['db_path']} -> deleted={summary['db_deleted']}")
        print(f"  Logs: {summary['logs_dir']} -> files_deleted={summary['logs_files_deleted']}")
        if not args.no_exit:
            return

    setup_logging(config.get("log_dir", "logs"))
    Agent(config).run()


if __name__ == "__main__":
    main()