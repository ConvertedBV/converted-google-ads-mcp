"""Google OAuth provider with optional extra scopes.

The Ads tools need the ``adwords`` scope; the Google Analytics tools need the
Analytics scopes. Making the Analytics scopes *required* would mean:

* every existing session is rejected until the user reconnects, and
* a user who unticks Analytics on Google's consent screen (or a pending
  Google verification of those scopes) blocks the Ads tools as well.

So the Analytics scopes are *requested* from Google on every login but not
*required* on the token. Sessions without them keep working for Ads; the
Analytics tools then answer with a clear "reconnect" message.
"""

from __future__ import annotations

from typing import Any

from fastmcp.server.auth.providers.google import GoogleProvider


def merge_scopes(base: list[str] | None, extra: list[str] | None) -> list[str]:
    """Base scopes followed by the extra ones, without duplicates."""
    merged: list[str] = []
    for scope in [*(base or []), *(extra or [])]:
        if scope and scope not in merged:
            merged.append(scope)
    return merged


class ConvertedGoogleProvider(GoogleProvider):
    """GoogleProvider that also asks Google for ``optional_scopes``."""

    def __init__(
        self, *, optional_scopes: list[str] | None = None, **kwargs: Any
    ):
        super().__init__(**kwargs)
        self._optional_scopes = list(optional_scopes or [])

    @property
    def optional_scopes(self) -> list[str]:
        return list(self._optional_scopes)

    def _build_upstream_authorize_url(
        self, txn_id: str, transaction: dict[str, Any]
    ) -> str:
        if self._optional_scopes:
            base = transaction.get("scopes") or self.required_scopes or []
            transaction = {
                **transaction,
                "scopes": merge_scopes(list(base), self._optional_scopes),
            }
        return super()._build_upstream_authorize_url(txn_id, transaction)
