"""Secrets in the OS keyring: Windows Credential Manager, macOS Keychain, Linux Secret Service.

Entries live under service "callmaibe", keyed by account ("sip:agent@host" or
"turn:host:port:user"). Environment variables still take precedence, for
headless setups without a keyring. Never log or print the values.
"""

import logging

log = logging.getLogger(__name__)
SERVICE = "callmaibe"


def sip_key(uri: str) -> str:
    return uri if uri.startswith("sip:") else f"sip:{uri}"


def turn_key(server: str, username: str) -> str:
    return f"turn:{server}:{username}"


def backend_name() -> str:
    """Name of the usable keyring backend, or "" when there is none (e.g. WSL)."""
    try:
        import keyring
        from keyring.backends import fail

    except ImportError:
        return ""
    kr = keyring.get_keyring()
    return "" if isinstance(kr, fail.Keyring) else type(kr).__name__


def get(key: str) -> str:
    if not backend_name():
        return ""
    import keyring
    from keyring.errors import KeyringError

    try:
        return keyring.get_password(SERVICE, key) or ""
    except KeyringError as e:
        log.warning("keyring read failed: %s", e)
        return ""


def set(key: str, value: str) -> None:
    import keyring

    keyring.set_password(SERVICE, key, value)


def delete(key: str) -> bool:
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(SERVICE, key)
        return True
    except PasswordDeleteError:
        return False
