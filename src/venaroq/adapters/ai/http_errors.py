"""Content-free cause codes for ambiguous provider HTTP outcomes (shared by the AI adapters)."""

import httpx


def ambiguous_cause(error: BaseException | None = None, *, status_code: int | None = None) -> str:
    """Name why an outcome is ambiguous: a client-side timeout reads very differently from a 5xx."""
    if status_code is not None:
        return f"server_error_{status_code}"
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.ReadError):
        return "read_error"
    if isinstance(error, httpx.WriteError):
        return "write_error"
    if isinstance(error, httpx.RemoteProtocolError):
        return "protocol_error"
    return "unspecified"
