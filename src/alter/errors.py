"""Custom exception hierarchy for Alter.

All exceptions are subclasses of ``AlterError``. Each carries a human-readable
message suitable for display to the user (no raw stack traces).

Usage::

    from alter.errors import ParseError
    raise ParseError("Could not parse models.py: unexpected token at line 42")
"""


class AlterError(Exception):
    """Base exception for all Alter errors."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class ParseError(AlterError):
    """Raised when the parser fails to read an ORM model file."""


class GeneratorError(AlterError):
    """Raised when the generator fails to produce ORM code."""


class SchemaValidationError(AlterError):
    """Raised when the proposed schema is invalid (e.g. duplicate table names,
    broken foreign key references, or unsupported type combinations)."""


class SyncConflictError(AlterError):
    """Raised when a bidirectional sync detects unresolvable conflicts between
    the .alter file and the ORM model files."""


class SchemaFileError(AlterError):
    """Raised when the .alter file cannot be read, parsed, or written."""
