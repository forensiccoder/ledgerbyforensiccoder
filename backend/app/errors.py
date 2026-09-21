"""Errors that are safe to show to the person uploading a statement."""
from __future__ import annotations


class StatementError(Exception):
    """A problem with the uploaded statement that the user can act on."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def password_required() -> StatementError:
    return StatementError(
        "PASSWORD_REQUIRED",
        "This PDF is password-protected. Enter the password (many Indian banks use "
        "your date of birth, customer ID or the last digits of your phone number).",
        status=401,
    )


def wrong_password() -> StatementError:
    return StatementError("WRONG_PASSWORD", "That password did not open the PDF.", status=401)
