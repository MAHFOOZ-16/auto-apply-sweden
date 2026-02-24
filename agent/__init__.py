"""Arbetsförmedlingen Job Application Agent."""

__version__ = "2.0.0"


class States:
    """State machine constants for job processing."""
    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    TAILORING = "TAILORING"
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLYING = "APPLYING"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    ASSIST = "ASSIST"              # Tab left open for human to finish
    SUBMITTED = "SUBMITTED"        # Submit clicked (legacy compat)
    CONFIRMED = "CONFIRMED"        # Confirmation page detected
    UNCERTAIN = "UNCERTAIN"        # Submit clicked but no confirmation
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    SKIPPED_COOLDOWN = "SKIPPED_COOLDOWN"
    SKIPPED_LOW_FIT = "SKIPPED_LOW_FIT"

    RESUMABLE = {READY_TO_APPLY, APPLYING, WAITING_FOR_HUMAN, ASSIST}
    TERMINAL = {SUBMITTED, CONFIRMED, UNCERTAIN,
                FAILED_PERMANENT, SKIPPED_DUPLICATE,
                SKIPPED_COOLDOWN, SKIPPED_LOW_FIT}
    # All states that count toward the daily applied cap
    SUCCESS = {SUBMITTED, CONFIRMED, UNCERTAIN, ASSIST}
