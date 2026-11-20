# Copyright 2026 Converted BV.
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

"""Image handling for asset-based campaigns (PMax, Demand Gen).

Images are referenced either by an existing asset ID or by an https URL.
URLs are fetched server-side with SSRF protection (public addresses only, no
redirects, size limit) and checked against the format and aspect ratio that
Google Ads requires for the role the image is used in.
"""

from dataclasses import dataclass
from enum import Enum
import hashlib
import ipaddress
import socket
import struct
from typing import Any
import urllib.parse
import urllib.request

from fastmcp.exceptions import ToolError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_FETCHED_BYTES_PER_CALL = 20 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 15
_USER_AGENT = "Mozilla/5.0 (compatible; google-ads-mcp image fetcher)"


class ImageRole(str, Enum):
    """How an image is used; determines the required aspect ratio."""

    MARKETING = "MARKETING"  # landscape 1.91:1
    SQUARE = "SQUARE"  # 1:1
    PORTRAIT = "PORTRAIT"  # 4:5
    LOGO = "LOGO"  # 1:1
    LANDSCAPE_LOGO = "LANDSCAPE_LOGO"  # 4:1


# role -> (aspect ratio width/height, min width, min height, label)
_ROLE_SPECS = {
    "MARKETING": (1.91, 600, 314, "landscape 1.91:1, at least 600x314"),
    "SQUARE": (1.0, 300, 300, "square 1:1, at least 300x300"),
    "PORTRAIT": (0.8, 480, 600, "portrait 4:5, at least 480x600"),
    "LOGO": (1.0, 128, 128, "square 1:1, at least 128x128"),
    "LANDSCAPE_LOGO": (4.0, 512, 128, "landscape 4:1, at least 512x128"),
}
_RATIO_TOLERANCE = 0.02


@dataclass(frozen=True)
class FetchedImage:
    url: str
    data: bytes
    mime: str  # MimeTypeEnum name
    width: int
    height: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ToolError(
            f"Error: '{req.full_url}' redirects to '{newurl}'. Pass the final "
            "image URL instead."
        )


def _require_public_host(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ToolError(f"Error: Image URL must use https, got '{url}'.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as exc:
        raise ToolError(f"Error: Cannot resolve '{parsed.hostname}'.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ToolError(
                f"Error: '{parsed.hostname}' resolves to a non-public address; "
                "only public image URLs are allowed."
            )


def _sniff(data: bytes) -> tuple[str, int, int]:
    """Returns (MimeTypeEnum name, width, height) from the image bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "IMAGE_PNG", width, height
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return "IMAGE_GIF", width, height
    if data[:3] == b"\xff\xd8\xff":
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = struct.unpack(">H", data[index + 2 : index + 4])[0]
            if marker in (
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            ):
                height, width = struct.unpack(
                    ">HH", data[index + 5 : index + 9]
                )
                return "IMAGE_JPEG", width, height
            index += 2 + length
        raise ToolError("Error: Could not read the JPEG dimensions.")
    raise ToolError(
        "Error: Unsupported image format; Google Ads accepts JPEG, PNG and GIF."
    )


def check_role(width: int, height: int, role: str, source: str) -> None:
    """Raises when an image does not fit the aspect ratio of its role."""
    ratio, min_w, min_h, label = _ROLE_SPECS[role]
    actual = width / height if height else 0
    if abs(actual - ratio) / ratio > _RATIO_TOLERANCE or (
        width < min_w or height < min_h
    ):
        raise ToolError(
            f"Error: Image {source} is {width}x{height}; a "
            f"{role.lower().replace('_', ' ')} image must be {label}."
        )


def fetch_image(url: str) -> FetchedImage:
    """Downloads an image from a public https URL, with safety limits."""
    url = (url or "").strip()
    _require_public_host(url)
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"Error: Could not download '{url}': {exc}") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise ToolError(f"Error: '{url}' is larger than 5 MB.")
    mime, width, height = _sniff(data)
    return FetchedImage(url, data, mime, width, height)


class AssetPool:
    """Creates text and image assets once per request and hands out names.

    Text assets are deduplicated by text, image URLs by URL. Existing assets
    are referenced by ID. All create operations are collected in `operations`
    (as MutateOperations) so they can precede the operations that use them.
    """

    def __init__(self, ga_client: Any, customer_id: str, temp_ids: "TempIds"):
        self._client = ga_client
        self._cid = customer_id
        self._ids = temp_ids
        self._texts: dict[str, str] = {}
        self._images: dict[str, str] = {}
        self._sizes: dict[str, tuple[int, int]] = {}
        self._fetched_bytes = 0
        self.operations: list[Any] = []
        self.image_digests: dict[str, str] = {}

    def text(self, text: str) -> str:
        if text not in self._texts:
            op = self._client.get_type("MutateOperation")
            asset = op.asset_operation.create
            asset.resource_name = self._ids.next("assets")
            asset.text_asset.text = text
            self.operations.append(op)
            self._texts[text] = asset.resource_name
        return self._texts[text]

    def youtube(self, video_id: str) -> str:
        key = f"youtube:{video_id}"
        if key not in self._images:
            op = self._client.get_type("MutateOperation")
            asset = op.asset_operation.create
            asset.resource_name = self._ids.next("assets")
            asset.youtube_video_asset.youtube_video_id = video_id
            self.operations.append(op)
            self._images[key] = asset.resource_name
        return self._images[key]

    def image(self, ref: str, role: str, name: str) -> str:
        ref = str(ref).strip()
        if ref.isdigit():
            return f"customers/{self._cid}/assets/{ref}"
        if not ref.lower().startswith("https://"):
            raise ToolError(
                f"Error: Image '{ref}' must be an asset ID or an https URL."
            )
        if ref not in self._images:
            image = fetch_image(ref)
            self._fetched_bytes += len(image.data)
            if self._fetched_bytes > MAX_FETCHED_BYTES_PER_CALL:
                raise ToolError(
                    "Error: More than 20 MB of images in one call. Upload them "
                    "first with create_image_assets and pass the asset IDs."
                )
            check_role(image.width, image.height, role, ref)
            op = self._client.get_type("MutateOperation")
            asset = op.asset_operation.create
            asset.resource_name = self._ids.next("assets")
            asset.name = name[:100]
            asset.image_asset.data = image.data
            asset.image_asset.mime_type = getattr(
                self._client.enums.MimeTypeEnum, image.mime
            )
            self.operations.append(op)
            self._images[ref] = asset.resource_name
            self.image_digests[ref] = image.sha256
            self._sizes[ref] = (image.width, image.height)
        else:
            # Same URL reused in another role: check that role too.
            check_role(*self._sizes[ref], role, ref)
        return self._images[ref]


class TempIds:
    """Allocates unique temporary (negative) resource IDs for one request."""

    def __init__(self, customer_id: str):
        self._cid = customer_id
        self._next = 0

    def next(self, collection: str) -> str:
        self._next -= 1
        return f"customers/{self._cid}/{collection}/{self._next}"
