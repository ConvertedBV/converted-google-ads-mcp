"""Tests for the optional Analytics scopes on the Google OAuth provider."""

import unittest
from urllib.parse import parse_qs, urlparse

from google.api_core import exceptions as gexc

from ads_mcp.analytics.client import format_api_error
from ads_mcp.auth_provider import ConvertedGoogleProvider, merge_scopes

BASE = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/adwords",
]
GA = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/analytics.edit",
]


def _provider(optional):
    return ConvertedGoogleProvider(
        client_id="cid.apps.googleusercontent.com",
        client_secret="secret",
        base_url="https://ads-mcp.example.test",
        required_scopes=BASE,
        optional_scopes=optional,
        jwt_signing_key="k" * 32,
    )


def _scopes_in(url):
    return parse_qs(urlparse(url).query)["scope"][0].split()


class AuthProviderTest(unittest.TestCase):

    def test_merge_scopes(self):
        self.assertEqual(merge_scopes(["a", "b"], ["b", "c"]), ["a", "b", "c"])
        self.assertEqual(merge_scopes(None, None), [])

    def test_optional_scopes_requested_not_required(self):
        provider = _provider(GA)
        url = provider._build_upstream_authorize_url(
            "txn", {"scopes": BASE, "proxy_code_verifier": None}
        )
        scopes = _scopes_in(url)
        for scope in BASE + GA:
            self.assertIn(scope, scopes)
        # Analytics is not part of what a token must carry.
        self.assertFalse(set(GA) & set(provider.required_scopes))

    def test_no_optional_scopes_leaves_request_unchanged(self):
        provider = _provider([])
        url = provider._build_upstream_authorize_url(
            "txn", {"scopes": BASE, "proxy_code_verifier": None}
        )
        self.assertEqual(_scopes_in(url), BASE)

    def test_transaction_without_scopes_uses_required(self):
        provider = _provider(GA)
        url = provider._build_upstream_authorize_url(
            "txn", {"proxy_code_verifier": None}
        )
        scopes = _scopes_in(url)
        self.assertIn("https://www.googleapis.com/auth/adwords", scopes)
        self.assertIn(GA[0], scopes)

    def test_insufficient_scope_error_asks_to_reconnect(self):
        exc = gexc.PermissionDenied(
            "Request had insufficient authentication scopes. "
            "ACCESS_TOKEN_SCOPE_INSUFFICIENT"
        )
        self.assertIn("reconnect", format_api_error(exc))
        other = gexc.PermissionDenied(
            "User does not have sufficient permissions"
        )
        self.assertIn("Permission denied", format_api_error(other))


if __name__ == "__main__":
    unittest.main()
