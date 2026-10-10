"""Read-only account precheck using an existing Metaculus bot token.

One GET, no redirects/retries, no question data, no account changes, no forecasts.
Metaculus records the normal token last-used timestamp during authentication.
No requests or credential reads happen on import or without --check.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
from typing import Any, Callable

HOST = "www.metaculus.com"
IDENTITY_PATH = "/api/users/me/"
TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 256 * 1024


class ConnectionCheckError(RuntimeError):
    """An intentionally fixed, safe-to-log diagnostic."""


def validate_identity(payload: Any) -> int:
    """Return only the verified ID for internal readback; never log profile data."""
    if not isinstance(payload, dict):
        raise ConnectionCheckError("Metaculus returned an unrecognized account response.")
    user_id = payload.get("id")
    if type(user_id) is not int or user_id <= 0:
        raise ConnectionCheckError("Metaculus account identity could not be verified.")
    if payload.get("is_bot") is not True:
        raise ConnectionCheckError("The configured credential was not verified as a bot account.")
    if payload.get("is_active") is not True or payload.get("is_spam") is not False:
        raise ConnectionCheckError("The bot account did not pass the account-status precheck.")
    if payload.get("api_forecasting_access") != "enabled":
        raise ConnectionCheckError("Account-level API forecasting access is not confirmed enabled.")
    return user_id


def check_connection(
    token: str,
    connection_factory: Callable[..., Any] | None = None,
) -> int:
    """Call only the fixed official HTTPS endpoint; never return its raw response."""
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise ConnectionCheckError("METACULUS_TOKEN is missing or malformed.")
    factory = connection_factory or http.client.HTTPSConnection
    connection = None
    try:
        # Standard-library HTTPS verifies TLS certificates by default. It neither
        # follows redirects nor reads proxy settings or retries requests.
        connection = factory(HOST, timeout=TIMEOUT_SECONDS)
        connection.request("GET", IDENTITY_PATH, headers={
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        })
        response = connection.getresponse()
        if response.status != 200:
            # Do not read error/redirect bodies or print locations/response headers.
            raise ConnectionCheckError("Metaculus did not accept the read-only identity check.")
        content_type = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ConnectionCheckError("Metaculus returned an unexpected response format.")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ConnectionCheckError("Metaculus identity response exceeded the size limit.")
        return validate_identity(json.loads(body))
    except ConnectionCheckError:
        raise
    except Exception:
        # Transport and parsing errors can contain private response/header data.
        raise ConnectionCheckError("Metaculus connection check failed; no response details were logged.") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Perform the single authenticated GET")
    args = parser.parse_args()
    if not args.check:
        print("Offline only. The authenticated account precheck requires --check.")
        return
    try:
        check_connection(os.environ.get("METACULUS_TOKEN", ""))
    except ConnectionCheckError as error:
        raise SystemExit(str(error)) from None
    print("Metaculus bot authentication and account-level API forecasting checks passed. No forecasts were submitted.")


if __name__ == "__main__":
    main()
