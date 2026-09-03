"""In-memory Capability Registry for the Nexus Agent Capability Router V1."""

from nexus.capabilities.models import Capability


class CapabilityNotFoundError(KeyError):
    pass


class CapabilityRegistry:
    """Stores capability definitions and answers lookup queries."""

    def __init__(self):
        self._capabilities = {}

    def register_capability(self, capability):
        """Register a capability definition, returning the registered one."""
        if not isinstance(capability, Capability):
            capability = Capability(**capability)
        self._capabilities[capability.name] = capability
        return capability

    def get_capability(self, name):
        try:
            return self._capabilities[name]
        except KeyError:
            raise CapabilityNotFoundError(name)

    def list_capabilities(self):
        return list(self._capabilities.values())


# Module-level default registry backing the convenience functions below.
_registry = CapabilityRegistry()


def register_capability(capability):
    """Register a capability on the default capability registry."""
    return _registry.register_capability(capability)


def get_capability(name):
    return _registry.get_capability(name)


def list_capabilities():
    return _registry.list_capabilities()
