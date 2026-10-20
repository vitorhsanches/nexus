"""Shared pytest fixtures.

Many Review dispatcher tests pass fake worktree paths such as
``C:/fake/worktree`` together with stub launchers. The deterministic Git
prerequisite check correctly rejects those paths, so it is stubbed to pass for
every test. The real check runs against real Git repositories in
tests/test_review_fail_closed.py, and tests there override this stub.
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _stub_review_git_prerequisites():
    with patch(
        "nexus.dispatchers.review._verify_review_prerequisites",
        return_value=None,
    ):
        yield
