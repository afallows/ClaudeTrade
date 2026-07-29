"""Credential resolution.

Secrets are **never** written to source, config files, the database, or logs.
They are resolved on demand, in priority order:

1. **Environment variable** -- ``CLAUDETRADE_SECRET_<NAME>`` (upper-cased).
   Useful for CI, containers and one-off runs.
2. **Windows Credential Manager** (or macOS Keychain / Secret Service on other
   platforms) through ``keyring``, under the service name ``ClaudeTrade``.
   This is the recommended store for a desktop install.
3. **Not found** -- the caller disables the dependent feature and continues.

Retrieved values are wrapped in ``SecretValue``, whose ``repr``/``str`` are
masked, so an accidental f-string or traceback does not spill the key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from claudetrade.logging_setup import audit_event, get_logger

log = get_logger(__name__)

SERVICE_NAME = "ClaudeTrade"
ENV_SECRET_PREFIX = "CLAUDETRADE_SECRET_"


class SecretNotFoundError(LookupError):
    """A required credential could not be resolved from any backend."""


@dataclass(frozen=True)
class SecretValue:
    """A credential that resists accidental disclosure.

    Call ``.reveal()`` at the exact point of use -- typically when building an
    HTTP header -- and never store the revealed string.
    """

    name: str
    _value: str
    source: str = "unknown"

    def reveal(self) -> str:
        return self._value

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"<secret {self.name} from {self.source}>"

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return self.__str__()

    def masked(self) -> str:
        """Last four characters only, for a 'which key is loaded?' display."""
        if len(self._value) <= 4:
            return "****"
        return f"****{self._value[-4:]}"


def _env_key(name: str) -> str:
    return f"{ENV_SECRET_PREFIX}{name.upper()}"


def _keyring_backend() -> Any | None:
    """Import keyring lazily; it is optional and can fail on headless systems."""
    try:
        import keyring

        backend = keyring.get_keyring()
        # The fail backend raises on every call; treat it as unavailable.
        if backend.__class__.__name__ == "Keyring" and "fail" in backend.__class__.__module__:
            return None
        return keyring
    except Exception as exc:  # pragma: no cover - environment dependent
        log.debug("keyring unavailable: %s", exc)
        return None


def get_secret(name: str, *, required: bool = False) -> SecretValue | None:
    """Resolve a named credential.

    Args:
        name: Logical credential name, e.g. ``anthropic_api_key``. This is the
            value stored in the config file -- it is a lookup key, not a secret.
        required: Raise instead of returning ``None`` when absent.

    Raises:
        SecretNotFoundError: when ``required`` and nothing resolves.
    """
    env_value = os.environ.get(_env_key(name))
    if env_value:
        audit_event("secret_read", credential=name, backend="environment")
        return SecretValue(name=name, _value=env_value, source="environment")

    keyring = _keyring_backend()
    if keyring is not None:
        try:
            stored = keyring.get_password(SERVICE_NAME, name)
        except Exception as exc:  # pragma: no cover - backend specific
            log.warning("credential store read failed for %s: %s", name, exc)
            stored = None
        if stored:
            audit_event("secret_read", credential=name, backend="keyring")
            return SecretValue(name=name, _value=stored, source="keyring")

    if required:
        raise SecretNotFoundError(
            f"credential '{name}' not found. Set the environment variable "
            f"{_env_key(name)}, or store it with:  claudetrade secrets set {name}"
        )
    log.debug("credential '%s' not configured; dependent feature disabled", name)
    return None


def set_secret(name: str, value: str) -> str:
    """Store a credential in the OS credential store.

    Returns:
        The backend used ("keyring").

    Raises:
        RuntimeError: when no credential store is available; the caller should
            fall back to instructing the user to set an environment variable.
    """
    keyring = _keyring_backend()
    if keyring is None:
        raise RuntimeError(
            "no OS credential store is available on this system. "
            f"Set the environment variable {_env_key(name)} instead."
        )
    keyring.set_password(SERVICE_NAME, name, value)
    audit_event("secret_written", credential=name, backend="keyring")
    log.info("credential '%s' stored in the OS credential store", name)
    return "keyring"


def delete_secret(name: str) -> bool:
    """Remove a credential from the OS store. Returns True if something was removed."""
    keyring = _keyring_backend()
    if keyring is None:
        return False
    try:
        keyring.delete_password(SERVICE_NAME, name)
    except Exception:
        return False
    audit_event("secret_deleted", credential=name, backend="keyring")
    return True


def has_secret(name: str) -> bool:
    """Whether a credential resolves, without revealing it."""
    return get_secret(name) is not None


def describe_secrets(names: list[str]) -> dict[str, dict[str, str]]:
    """Status of several credentials, for the Settings screen.

    Never returns the values -- only whether each resolves, from where, and a
    masked tail so the operator can confirm they loaded the intended key.
    """
    out: dict[str, dict[str, str]] = {}
    for name in names:
        secret = get_secret(name)
        out[name] = (
            {"configured": "no", "source": "-", "masked": "-"}
            if secret is None
            else {"configured": "yes", "source": secret.source, "masked": secret.masked()}
        )
    return out
