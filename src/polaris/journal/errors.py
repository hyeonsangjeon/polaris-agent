"""Errors raised by the durable execution journal."""


class JournalError(Exception):
    """Base class for journal failures."""


class JournalClosedError(JournalError):
    """The journal has already been closed."""


class JournalNotFoundError(JournalError):
    """A requested journal record does not exist."""


class InvalidTransitionError(JournalError):
    """A state transition is not allowed."""


class OwnershipError(JournalError):
    """A worker attempted to modify a step owned by another worker."""


class LeaseExpiredError(JournalError):
    """A worker attempted to use an expired lease."""


class BudgetExceededError(JournalError):
    """A budget reservation would exceed a configured limit."""


class JournalConflictError(JournalError):
    """A unique durable operation conflicts with an existing operation."""


class JournalValidationError(JournalError):
    """Invalid journal input was supplied."""
