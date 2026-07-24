from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ailinux_kernel_builder.core import (
    BuilderError,
    SourceInfo,
    VERIFY_LOCAL,
    VerificationResult,
    config_commands,
    ensure_module_signing_key,
    extract_source,
    installable_kernel_debs,
    source_info,
    verify_kernel_org_source,
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

    def test_source_url_for_mainline_release_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "linux-7.2-rc4.tar.gz"
            archive.touch()
            info = source_info(archive)
            self.assertEqual(
                info.archive_url,
                "https://git.kernel.org/torvalds/t/linux-7.2-rc4.tar.gz",
            )
            self.assertEqual(info.signature_url, "")
            self.assertEqual(info.checksums_url, "")

    def test_full_verification_explains_unsigned_release_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "linux-7.2-rc4.tar.gz"
            archive.write_bytes(b"release candidate")
            with self.assertRaisesRegex(BuilderError, "Mainline-Release-Candidates"):
                verify_kernel_org_source(archive, root / "cache")

    def test_repacked_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "my-kernel.zip"
            archive.touch()
            with self.assertRaises(BuilderError):
                source_info(archive)

    def test_local_mode_records_hash_without_online_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "linux-7.1.3.tar.xz"
            archive.write_bytes(b"local release candidate")
            result = verify_kernel_org_source(
                archive,
                root / "cache",
                mode=VERIFY_LOCAL,
            )
            self.assertIsNone(result.signer_fingerprint)
            self.assertEqual(
                result.sha256,
                "a6f731703b47bfff599a7bbfcd98b70de348bef5ade8c40c252beb310f0d395d",
            )

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

    def test_installable_debs_exclude_debug_and_libc_packages(self) -> None:
        packages = [
            Path("/tmp/linux-image-7.1.3-ailinux_7.1.3-1_amd64.deb"),
            Path("/tmp/linux-image-7.1.3-ailinux-dbg_7.1.3-1_amd64.deb"),
            Path("/tmp/linux-headers-7.1.3-ailinux_7.1.3-1_amd64.deb"),
            Path("/tmp/linux-libc-dev_7.1.3-1_amd64.deb"),
        ]
        self.assertEqual(
            [path.name for path in installable_kernel_debs(packages)],
            [
                "linux-headers-7.1.3-ailinux_7.1.3-1_amd64.deb",
                "linux-image-7.1.3-ailinux_7.1.3-1_amd64.deb",
            ],
        )

    @mock.patch("ailinux_kernel_builder.core.shutil.which", return_value="/usr/bin/openssl")
    def test_existing_module_signing_key_is_made_kernel_compatible(
        self, _which: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_dir = Path(directory)
            private_key = key_dir / "ailinux-module-signing-key.pem"
            certificate = key_dir / "ailinux-module-signing-cert.x509"
            mok_certificate = key_dir / "ailinux-module-signing-cert.der"
            private_key.write_bytes(b"PRIVATE KEY\n")
            certificate.write_bytes(b"CERTIFICATE\n")
            mok_certificate.write_bytes(b"DER")
            messages: list[str] = []

            returned_key, returned_mok = ensure_module_signing_key(key_dir, messages.append)
            ensure_module_signing_key(key_dir, messages.append)

            self.assertEqual(returned_key, private_key)
            self.assertEqual(returned_mok, mok_certificate)
            self.assertEqual(private_key.read_bytes(), b"PRIVATE KEY\nCERTIFICATE\n")
            self.assertEqual(private_key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                messages.count(
                    "Kernel-Signatur-PEM um das zugehörige X.509-Zertifikat ergänzt."
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
