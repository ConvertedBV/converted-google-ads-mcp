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

"""Module declaring the singleton MCP instance.

The singleton allows other modules to register their tools with the same MCP
server using `@mcp.tool` annotations, thereby 'coordinating' the bootstrapping
of the server.
"""

import os
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider
from mcp.types import Icon

from ads_mcp.auth_storage import create_client_storage

_CLIENT_ID = os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID")
_CLIENT_SECRET = os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET")
_BASE_URL = os.environ.get("GOOGLE_ADS_MCP_BASE_URL", "http://localhost:8080")
_JWT_SIGNING_KEY = os.environ.get("GOOGLE_ADS_MCP_JWT_SIGNING_KEY")

_CONVERTED_ICON_BASE64 = (
    "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"
)

_SERVER_ICONS = [
    Icon(
        src=f"data:image/png;base64,{_CONVERTED_ICON_BASE64}",
        mimeType="image/png",
        sizes=["96x96"],
    )
]
_SERVER_KWARGS = {
    "name": "Converted Google Ads",
    "website_url": "https://converted.be",
    "icons": _SERVER_ICONS,
}

if _CLIENT_ID and _CLIENT_SECRET:
    client_storage = create_client_storage()
    provider_kwargs: dict[str, Any] = {
        "client_id": _CLIENT_ID,
        "client_secret": _CLIENT_SECRET,
        "base_url": _BASE_URL,
        "required_scopes": [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
            "https://www.googleapis.com/auth/adwords",
        ],
    }
    if _JWT_SIGNING_KEY:
        provider_kwargs["jwt_signing_key"] = _JWT_SIGNING_KEY
    if client_storage is not None:
        provider_kwargs["client_storage"] = client_storage

    auth = GoogleProvider(**provider_kwargs)
    mcp = FastMCP(auth=auth, **_SERVER_KWARGS)
else:
    mcp = FastMCP(**_SERVER_KWARGS)


def initialize_and_mount_tools(parent_mcp: FastMCP) -> None:
    """Loads the tools configuration and dynamically mounts the tools sub-servers."""
    from ads_mcp.config import ToolsConfig
    import importlib
    import pkgutil
    import ads_mcp.tools as tools_pkg

    # Map of category name -> FastMCP sub-server
    sub_servers = {}

    # Discover and dynamically load all tool modules
    for _, module_name, _ in pkgutil.iter_modules(tools_pkg.__path__):
        full_module_name = f"ads_mcp.tools.{module_name}"
        module = importlib.import_module(full_module_name)

        # Find any FastMCP instances defined in the module
        for attr_name in dir(module):
            attr_val = getattr(module, attr_name)
            if isinstance(attr_val, FastMCP):
                category = attr_val.name
                sub_servers[category] = attr_val

    config = ToolsConfig.load()

    for category, sub_mcp in sub_servers.items():
        if not config.is_namespace_enabled(category):
            continue

        # Filter disabled tools inside the sub-server before mounting
        tool_names = []
        for key, val in sub_mcp.local_provider._components.items():
            if key.startswith("tool:"):
                tool_names.append(val.name)

        for name in tool_names:
            if not config.is_tool_enabled(category, name):
                sub_mcp.local_provider.remove_tool(name)

        # Determine prefix/namespace
        namespace_prefix = config.get_namespace_prefix(category)

        # Mount the sub-server
        parent_mcp.mount(sub_mcp, namespace=namespace_prefix or None)


# Automatically initialize and mount tools upon import
initialize_and_mount_tools(mcp)
