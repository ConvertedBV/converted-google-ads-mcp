# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Authentication patches and monkey-patches for python-mcp compatibility."""

import json
from starlette.requests import Request
from fastmcp.server.auth.providers.google import GoogleProvider

# Monkey-patch RegistrationHandler to ensure incoming registration requests are always
# sanitized to contain both authorization_code and refresh_token, preventing 400 Bad Request.
try:
    from mcp.server.auth.handlers.register import RegistrationHandler

    _original_register_handle = RegistrationHandler.handle

    async def _patched_register_handle(self, request: Request):
        try:
            body_json = await request.json()
            modified = False
            if "grant_types" in body_json and isinstance(
                body_json["grant_types"], list
            ):
                if (
                    "authorization_code" in body_json["grant_types"]
                    and "refresh_token" not in body_json["grant_types"]
                ):
                    body_json["grant_types"].append("refresh_token")
                    modified = True

            if modified:
                request._json = body_json
                body_bytes = json.dumps(body_json).encode("utf-8")
                request._body = body_bytes

                body_consumed = False

                async def mock_receive():
                    nonlocal body_consumed
                    if not body_consumed:
                        body_consumed = True
                        return {
                            "type": "http.request",
                            "body": body_bytes,
                            "more_body": False,
                        }
                    return {"type": "http.disconnect"}

                request._receive = mock_receive
        except Exception:
            pass
        return await _original_register_handle(self, request)

    RegistrationHandler.handle = _patched_register_handle
except Exception:
    pass


def apply_scope_patches(auth: GoogleProvider) -> None:
    """Configures registration scopes to ensure compatibility with various MCP clients."""
    if (
        auth.client_registration_options
        and auth.client_registration_options.valid_scopes
    ):
        for s in ["email", "profile"]:
            if s not in auth.client_registration_options.valid_scopes:
                auth.client_registration_options.valid_scopes.append(s)

        # Set default_scopes to all valid_scopes so that if a client does not request scopes
        # during Dynamic Client Registration, they are registered with all valid scopes.
        auth.client_registration_options.default_scopes = list(
            auth.client_registration_options.valid_scopes
        )
