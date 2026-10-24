"""Independent, default-off gate for human-approved Dev integrations."""

import os

ENV_VAR = "NEXUS_DEV_INTEGRATION_ENABLED"


def integration_enabled() -> bool:
    """Fail closed: only the exact opt-in value enables integration."""
    return os.environ.get(ENV_VAR) == "1"
