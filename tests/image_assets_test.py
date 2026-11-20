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

"""Tests for image fetching, sniffing and role checks."""

import socket
import struct
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

from ads_mcp import image_assets as ia


def png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof0 = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, height, width) + b"\x00" * 10
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def public_dns(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.0.1", 443))]


class TestSniffAndRoles(unittest.TestCase):

    def test_sniff_formats(self):
        self.assertEqual(ia._sniff(png(1200, 628)), ("IMAGE_PNG", 1200, 628))
        self.assertEqual(ia._sniff(jpeg(1080, 1080)), ("IMAGE_JPEG", 1080, 1080))
        gif = b"GIF89a" + struct.pack("<HH", 300, 300) + b"\x00" * 4
        self.assertEqual(ia._sniff(gif), ("IMAGE_GIF", 300, 300))
        with self.assertRaisesRegex(ToolError, "JPEG, PNG and GIF"):
            ia._sniff(b"RIFF....WEBPVP8 ")

    def test_roles(self):
        ia.check_role(1200, 628, "MARKETING", "x")
        ia.check_role(1200, 1200, "SQUARE", "x")
        ia.check_role(960, 1200, "PORTRAIT", "x")
        ia.check_role(1200, 300, "LANDSCAPE_LOGO", "x")
        with self.assertRaisesRegex(ToolError, "landscape 1.91:1"):
            ia.check_role(1200, 1200, "MARKETING", "x")
        with self.assertRaisesRegex(ToolError, "at least 128x128"):
            ia.check_role(100, 100, "LOGO", "x")


class TestFetchImage(unittest.TestCase):

    def test_rejects_http_and_private_hosts(self):
        with self.assertRaisesRegex(ToolError, "must use https"):
            ia.fetch_image("http://example.be/a.png")
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
        with patch("socket.getaddrinfo", return_value=private):
            with self.assertRaisesRegex(ToolError, "non-public address"):
                ia.fetch_image("https://intranet.example/a.png")
        metadata = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]
        with patch("socket.getaddrinfo", return_value=metadata):
            with self.assertRaisesRegex(ToolError, "non-public address"):
                ia.fetch_image("https://metadata.example/a.png")

    def _opener(self, data: bytes) -> MagicMock:
        response = MagicMock()
        response.read.return_value = data
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        return opener

    def test_downloads_and_sniffs(self):
        with patch("socket.getaddrinfo", side_effect=public_dns), patch(
            "urllib.request.build_opener", return_value=self._opener(png(1200, 628))
        ):
            image = ia.fetch_image("https://favorcool.be/hero.png")
        self.assertEqual((image.mime, image.width, image.height), ("IMAGE_PNG", 1200, 628))
        self.assertEqual(len(image.sha256), 64)

    def test_size_limit(self):
        big = png(1200, 628) + b"\x00" * ia.MAX_IMAGE_BYTES
        with patch("socket.getaddrinfo", side_effect=public_dns), patch(
            "urllib.request.build_opener", return_value=self._opener(big)
        ):
            with self.assertRaisesRegex(ToolError, "larger than 5 MB"):
                ia.fetch_image("https://favorcool.be/huge.png")

    def test_redirects_are_refused(self):
        handler = ia._NoRedirect()
        request = MagicMock(full_url="https://a.be/x.png")
        with self.assertRaisesRegex(ToolError, "redirects to"):
            handler.redirect_request(request, None, 302, "Found", {}, "https://b.be/y.png")


if __name__ == "__main__":
    unittest.main()
