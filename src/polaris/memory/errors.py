"""Curated memory exceptions."""


class MemoryStoreError(RuntimeError):
    """Base class for memory store failures."""


class MemoryClosedError(MemoryStoreError):
    """The memory store is closed."""


class MemoryConflictError(MemoryStoreError):
    """An optimistic write precondition did not match."""


class MemoryNotFoundError(MemoryStoreError):
    """The requested memory does not exist in the supplied scope."""


class MemoryValidationError(MemoryStoreError, ValueError):
    """A memory operation was invalid."""
