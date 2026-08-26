"""Query-only transports.  No public generic write method exists."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .catalog import Profile
from .safety import validate_query


class QueryTransport(Protocol):
    backend_name: str

    def query(self, command: str) -> str: ...
    def close(self) -> None: ...


@dataclass
class SimulatedTransport:
    profile: Profile
    backend_name: str = "built-in deterministic simulator"

    def query(self, command: str) -> str:
        validate_query(self.profile, command)
        return self.profile.simulated.get(command, "0")

    def close(self) -> None:
        return None


@dataclass
class DryRunTransport:
    profile: Profile
    backend_name: str = "dry-run; no instrument opened"

    def query(self, command: str) -> str:
        validate_query(self.profile, command)
        return "<not sent>"

    def close(self) -> None:
        return None


class PyVisaQueryTransport:
    backend_name = "PyVISA"

    def __init__(
        self,
        profile: Profile,
        resource_name: str,
        visa_library: str | None = None,
        timeout_ms: int = 3000,
    ) -> None:
        try:
            import pyvisa
        except ImportError as exc:
            raise RuntimeError(
                "Real mode needs PyVISA. Install with: pip install -e '.[visa]'"
            ) from exc

        self.profile = profile
        self.resource_name = resource_name
        self._rm = pyvisa.ResourceManager(visa_library) if visa_library else pyvisa.ResourceManager()
        self._resource = self._rm.open_resource(resource_name)
        # These are host/session attributes. They do not send configuration
        # commands to the instrument.
        self._resource.timeout = timeout_ms
        self._resource.query_delay = 0.05
        if "SOCKET" in resource_name.upper() or "ASRL" in resource_name.upper():
            self._resource.read_termination = "\n"
            self._resource.write_termination = "\n"

    def query(self, command: str) -> str:
        validate_query(self.profile, command)
        # PyVISA query performs exactly one allowlisted request followed by one
        # read.  There is intentionally no write() method in this class.
        return str(self._resource.query(command)).strip()

    def close(self) -> None:
        try:
            self._resource.close()
        finally:
            self._rm.close()


def list_visa_resources(visa_library: str | None = None) -> tuple[str, ...]:
    """Enumerate VISA resources without opening or messaging an instrument."""
    try:
        import pyvisa
    except ImportError as exc:
        raise RuntimeError(
            "VISA discovery needs PyVISA. Install with: pip install -e '.[visa]'"
        ) from exc

    rm = pyvisa.ResourceManager(visa_library) if visa_library else pyvisa.ResourceManager()
    try:
        return tuple(str(item) for item in rm.list_resources())
    finally:
        rm.close()


def query_identity_only(
    resource_name: str,
    visa_library: str | None = None,
    timeout_ms: int = 3000,
) -> str:
    """Send exactly one hard-coded, non-configuring identity query."""
    try:
        import pyvisa
    except ImportError as exc:
        raise RuntimeError(
            "Real identity checks need PyVISA. Install with: pip install -e '.[visa]'"
        ) from exc

    rm = pyvisa.ResourceManager(visa_library) if visa_library else pyvisa.ResourceManager()
    resource = None
    try:
        resource = rm.open_resource(resource_name)
        resource.timeout = timeout_ms
        resource.query_delay = 0.05
        return str(resource.query("*IDN?")).strip()
    finally:
        try:
            if resource is not None:
                resource.close()
        finally:
            rm.close()
