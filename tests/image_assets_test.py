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
    sof0 = (
        b"\xff\xc0" + struct.pack(">HBHH", 17, 8, height, width) + b"\x00" * 10
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def public_dns(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.0.1", 443))]


class TestSniffAndRoles(unittest.TestCase):

    def test_sniff_formats(self):
        self.assertEqual(ia._sniff(png(1200, 628)), ("IMAGE_PNG", 1200, 628))
        self.assertEqual(
            ia._sniff(jpeg(1080, 1080)), ("IMAGE_JPEG", 1080, 1080)
        )
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
        private = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))
        ]
        with patch("socket.getaddrinfo", return_value=private):
            with self.assertRaisesRegex(ToolError, "non-public address"):
                ia.fetch_image("https://intranet.example/a.png")
        metadata = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("169.254.169.254", 443),
            )
        ]
        with patch("socket.getaddrinfo", return_value=metadata):
            with self.assertRaisesRegex(ToolError, "non-public address"):
                ia.fetch_image("https://metadata.example/a.png")

    def test_mixed_public_and_private_answer_is_refused(self):
        mixed = public_dns() + [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]
        with patch("socket.getaddrinfo", return_value=mixed):
            with self.assertRaisesRegex(ToolError, "non-public address"):
                ia.fetch_image("https://rebind.example/a.png")

    def test_downloads_from_the_checked_ip(self):
        with (
            patch("socket.getaddrinfo", side_effect=public_dns),
            patch(
                "ads_mcp.image_assets._download", return_value=png(1200, 628)
            ) as download,
        ):
            image = ia.fetch_image("https://favorcool.be/img/hero.png?v=2")
        download.assert_called_once_with(
            "favorcool.be",
            "142.250.0.1",
            443,
            "/img/hero.png?v=2",
            "https://favorcool.be/img/hero.png?v=2",
        )
        self.assertEqual(
            (image.mime, image.width, image.height), ("IMAGE_PNG", 1200, 628)
        )
        self.assertEqual(len(image.sha256), 64)

    def test_connection_is_pinned_to_ip_with_sni(self):
        connection = ia._PinnedHTTPSConnection(
            "favorcool.be", "142.250.0.1", 443, 5
        )
        connection._context = MagicMock()
        with patch("socket.create_connection") as create:
            connection.connect()
        create.assert_called_once_with(("142.250.0.1", 443), 5)
        connection._context.wrap_socket.assert_called_once_with(
            create.return_value, server_hostname="favorcool.be"
        )


class TestDownload(unittest.TestCase):

    def _connection(self, status=200, chunks=(), location=None):
        response = MagicMock(status=status)
        response.read1.side_effect = list(chunks) + [b""]
        response.getheader.return_value = location
        connection = MagicMock()
        connection.getresponse.return_value = response
        return connection

    def _download(self, connection):
        with patch(
            "ads_mcp.image_assets._PinnedHTTPSConnection",
            return_value=connection,
        ):
            return ia._download(
                "a.be", "1.2.3.4", 443, "/x.png", "https://a.be/x.png"
            )

    def test_reads_in_chunks(self):
        data = self._download(self._connection(chunks=[b"ab", b"cd"]))
        self.assertEqual(data, b"abcd")

    def test_redirect_refused(self):
        with self.assertRaisesRegex(
            ToolError, "redirects to 'https://b.be/y.png'"
        ):
            self._download(
                self._connection(status=302, location="https://b.be/y.png")
            )

    def test_size_limit(self):
        chunk = b"x" * (1024 * 1024)
        with self.assertRaisesRegex(ToolError, "larger than 5 MB"):
            self._download(self._connection(chunks=[chunk] * 6))

    def test_overall_deadline(self):
        # Start, connect, request, response and first chunk are within time;
        # the second chunk is past the 15 second deadline.
        times = iter([0, 1, 2, 3, 4, ia.FETCH_TIMEOUT_SECONDS + 1])
        with patch("time.monotonic", side_effect=lambda: next(times, 999)):
            with self.assertRaisesRegex(ToolError, "took longer than"):
                self._download(self._connection(chunks=[b"a", b"b", b"c"]))

    def test_socket_timeout_tracks_remaining_time(self):
        connection = self._connection(chunks=[b"ab"])
        times = iter([0, 5, 10, 12, 14])
        with patch("time.monotonic", side_effect=lambda: next(times, 14)):
            self._download(connection)
        timeouts = [
            c.args[0] for c in connection.sock.settimeout.call_args_list
        ]
        self.assertEqual(timeouts[:3], [5, 3, 1])


if __name__ == "__main__":
    unittest.main()
