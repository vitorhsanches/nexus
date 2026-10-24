"""Disposable calculator fixture with one intentional bug for Nexus Dev E2E."""


def add(left: int, right: int) -> int:
    """Return the sum of two integers (intentionally incorrect at baseline)."""
    return left - right
