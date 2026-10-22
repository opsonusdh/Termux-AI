"""Wrapper for retrieving system hostname information.

Uses socket.gethostname() and platform.node() to provide
hostname and FQDN (fully qualified domain name) for the device.
Cross-platform: works on Termux/Android, Linux, macOS, and Windows.
"""
import socket
import platform

# Default timeout for hostname resolution operations (seconds)
HOSTNAME_TIMEOUT = 10


def get_hostname() -> dict:
    """Retrieve the system hostname and fully qualified domain name (FQDN).

    Returns:
        dict with keys:
            - "hostname": The short hostname (via socket.gethostname()).
            - "fqdn": The fully qualified domain name (via socket.getfqdn()).
        On error, returns a dict with an "error" key describing the failure.
    """
    result: dict = {"hostname": "", "fqdn": ""}
    try:
        # socket.gethostname() may raise if the hostname cannot be determined,
        # so we fall back to platform.node() if it fails.
        try:
            hostname = socket.gethostname()
            if not hostname:
                hostname = platform.node() or ""
        except Exception:
            hostname = platform.node() or ""
        result["hostname"] = hostname

        try:
            fqdn = socket.getfqdn(hostname) if hostname else socket.getfqdn()
            if not fqdn:
                fqdn = hostname or ""
        except Exception:
            fqdn = hostname or ""
        result["fqdn"] = fqdn

    except Exception as e:
        return {"error": str(e)}

    return result
