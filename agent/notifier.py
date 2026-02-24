"""Notify the user when human action is needed."""

import logging
import platform
import subprocess

logger = logging.getLogger("agent.notifier")


class Notifier:
    """Send terminal + optional desktop notifications."""

    def __init__(self, config: dict):
        self.cfg = config
        self._desktop = self._check_desktop()

    @staticmethod
    def _check_desktop() -> bool:
        system = platform.system()
        if system == "Linux":
            try:
                subprocess.run(
                    ["which", "notify-send"], capture_output=True, check=True
                )
                return True
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass
        elif system == "Darwin":
            return True
        elif system == "Windows":
            try:
                from plyer import notification  # noqa
                return True
            except ImportError:
                pass
        return False

    def notify_human_needed(self, job_title: str, company: str,
                            reason: str, artifact_dir: str):
        title = "🔐 Job Agent: Human Action Needed"
        body = (
            f"Job: {job_title} at {company}\n"
            f"Reason: {reason}\nArtifacts: {artifact_dir}"
        )
        logger.warning("HUMAN NEEDED: %s – %s (%s)", job_title, company, reason)
        if self._desktop:
            self._send(title, body)
        print("\a", end="", flush=True)

    def notify_info(self, title: str, body: str):
        logger.info("%s: %s", title, body)
        if self._desktop:
            self._send(title, body)

    def notify_error(self, title: str, body: str):
        logger.error("%s: %s", title, body)
        if self._desktop:
            self._send(f"⚠️ {title}", body)

    def _send(self, title: str, body: str):
        system = platform.system()
        try:
            if system == "Linux":
                subprocess.Popen(
                    ["notify-send", "--urgency=critical", title, body],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            elif system == "Darwin":
                script = (
                    f'display notification "{body}" with title "{title}" '
                    f'sound name "Ping"'
                )
                subprocess.Popen(
                    ["osascript", "-e", script],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            elif system == "Windows":
                from plyer import notification
                notification.notify(
                    title=title, message=body[:256],
                    app_name="JobAgent", timeout=10,
                )
        except Exception as exc:
            logger.debug("Desktop notification failed: %s", exc)
