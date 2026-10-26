"""Use the OS certificate store for HTTPS (corporate root CAs on Windows)."""

from __future__ import annotations


def install_os_trust_store() -> None:
    try:
        import truststore
    except ImportError:
        return
    truststore.inject_into_ssl()
