"""Scheduler-specific exceptions."""


class SchedulerError(Exception):
    """Base scheduler error."""


class SchedulerValidationError(SchedulerError, ValueError):
    """Raised when scheduler input is invalid."""


class SchedulerNotFoundError(SchedulerError, LookupError):
    """Raised when a job or run does not exist."""


class SchedulerConflictError(SchedulerError):
    """Raised when durable scheduler state conflicts."""


class SchedulerOwnershipError(SchedulerConflictError):
    """Raised for an invalid lease owner or an expired lease."""


class SchedulerClosedError(SchedulerError):
    """Raised when a closed scheduler store is used."""
