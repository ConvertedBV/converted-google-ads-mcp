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
import http.client
import ipaddress
import socket
import ssl
import struct
import time
from typing import Any
import urllib.parse

from fastmcp.exceptions import ToolError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_FETCHED_BYTES_PER_CALL = 20 * 1024 * 1024
MAX_URLS_PER_CALL = 30
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


def _resolve_public_ip(url: str) -> tuple[str, str, int, str]:
    """Validates the URL and resolves it once to a public IP address.

    Returns (hostname, ip, port, path). Every resolved address must be public;
    the connection is then made to exactly this IP, so a DNS answer that
    changes after the check (DNS rebinding) cannot redirect the request.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ToolError(f"Error: Image URL must use https, got '{url}'.")
    port = parsed.port or 443
    try:
        infos = socket.getaddrinfo(
            parsed.hostname, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise ToolError(f"Error: Cannot resolve '{parsed.hostname}'.") from exc
    addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or any(not a.is_global for a in addresses):
        raise ToolError(
            f"Error: '{parsed.hostname}' resolves to a non-public address; "
            "only public image URLs are allowed."
        )
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    return parsed.hostname, str(addresses[0]), port, path


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection to a fixed IP with SNI and certificate checks for the
    original hostname. No proxies are used."""

    def __init__(self, hostname: str, ip: str, port: int, timeout: float):
        super().__init__(
            hostname,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _download(hostname: str, ip: str, port: int, path: str, url: str) -> bytes:
    """Downloads with one overall deadline and a size cap; no redirects."""
    deadline = time.monotonic() + FETCH_TIMEOUT_SECONDS
    connection = _PinnedHTTPSConnection(
        hostname, ip, port, FETCH_TIMEOUT_SECONDS
    )
    try:
        connection.request(
            "GET",
            path,
            headers={"User-Agent": _USER_AGENT, "Accept": "image/*"},
        )
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ToolError(
                f"Error: '{url}' redirects to '{response.getheader('Location')}'."
                " Pass the final image URL instead."
            )
        if response.status != 200:
            raise ToolError(f"Error: '{url}' returned HTTP {response.status}.")
        chunks: list[bytes] = []
        size = 0
        while True:
            if time.monotonic() > deadline:
                raise ToolError(
                    f"Error: Downloading '{url}' took longer than "
                    f"{FETCH_TIMEOUT_SECONDS} seconds."
                )
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise ToolError(f"Error: '{url}' is larger than 5 MB.")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        connection.close()


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
    hostname, ip, port, path = _resolve_public_ip(url)
    try:
        data = _download(hostname, ip, port, path, url)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"Error: Could not download '{url}': {exc}") from exc
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
            if len(self.image_digests) >= MAX_URLS_PER_CALL:
                raise ToolError(
                    f"Error: More than {MAX_URLS_PER_CALL} image URLs in one "
                    "call. Upload them first with create_image_assets."
                )
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
