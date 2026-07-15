from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from ailinux_kernel_builder.core import (
    BuilderError,
    SourceInfo,
    VerificationResult,
    config_commands,
    extract_source,
    source_info,
)


class CoreTests(unittest.TestCase):
    def test_source_url_for_stable_kernel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "linux-7.1.3.tar.xz"
            archive.touch()
            info = source_info(archive)
            self.assertEqual(info.version, "7.1.3")
            self.assertEqual(
                info.signature_url,
                "https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.1.3.tar.sign",
            )

    def test_repacked_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "my-kernel.zip"
            archive.touch()
            with self.assertRaises(BuilderError):
                source_info(archive)

    def test_profile_uses_madvise_and_bbr(self) -> None:
        flattened = " ".join(value or "" for _, value in config_commands(True))
        self.assertIn("CONFIG_TRANSPARENT_HUGEPAGE_MADVISE", flattened)
        self.assertIn("CONFIG_TCP_CONG_BBR", flattened)
        self.assertIn("CONFIG_CPU_FREQ_DEFAULT_GOV_PERFORMANCE", flattened)

    def test_extract_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "linux-7.1.3.tar"
            with tarfile.open(archive, "w") as handle:
                entry = tarfile.TarInfo("../escape")
                data = b"bad"
                entry.size = len(data)
                handle.addfile(entry, io.BytesIO(data))
            source = SourceInfo(
                archive=archive,
                version="7.1.3",
                suffix=".tar",
                base_url="",
                archive_url="",
                signature_url="",
                checksums_url="",
            )
            verification = VerificationResult(source, "0" * 64, "0" * 40)
            with self.assertRaises(BuilderError):
                extract_source(verification, root / "work", lambda _line: None)
            self.assertFalse((root / "escape").exists())

    def test_extract_rejects_absolute_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "linux-7.1.3.tar"
            with tarfile.open(archive, "w") as handle:
                entry = tarfile.TarInfo("linux-7.1.3/escape")
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/etc/passwd"
                handle.addfile(entry)
            source = SourceInfo(archive, "7.1.3", ".tar", "", "", "", "")
            verification = VerificationResult(source, "0" * 64, "0" * 40)
            with self.assertRaises(BuilderError):
                extract_source(verification, root / "work", lambda _line: None)


if __name__ == "__main__":
    unittest.main()
