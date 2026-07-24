from __future__ import annotations

import bz2
import gzip
import hashlib
import lzma
import os
import re
import shutil
import stat
import subprocess
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterable
from urllib.parse import urlparse


LogFn = Callable[[str], None]

VERIFY_FULL = "full"
VERIFY_CHECKSUM = "checksum"
VERIFY_LOCAL = "local"
VERIFICATION_MODES = (VERIFY_FULL, VERIFY_CHECKSUM, VERIFY_LOCAL)

ARCHIVE_RE = re.compile(
    r"^linux-(?P<version>\d+\.\d+(?:\.\d+)?(?:-rc\d+)?)"
    r"(?P<suffix>\.tar(?:\.(?:xz|gz|bz2|zst))?|\.zip)$"
)

# Published at https://www.kernel.org/signature.html.  A signature is accepted
# only when it resolves to one of these release-maintainer fingerprints.
OFFICIAL_SIGNER_FINGERPRINTS = {
    "ABAF11C65A2970B130ABE3C479BE3E4300411886",  # Linus Torvalds
    "647F28654894E3BD457199BE38DBBDC86092693E",  # Greg Kroah-Hartman
    "E27E5D8A3403A2EF66873BBCDEA66FF797772CDC",  # Sasha Levin
    "AC2B29BD34A6AFDDB3F68F35E7BFC8EC95861109",  # Ben Hutchings
}

KERNEL_KEY_EMAILS = (
    "torvalds@kernel.org",
    "gregkh@kernel.org",
    "sashal@kernel.org",
    "bwh@kernel.org",
)

KERNEL_ORG_DOWNLOAD_HOSTS = {"cdn.kernel.org", "git.kernel.org"}

REQUIRED_PACKAGES = (
    "build-essential",
    "bc",
    "bison",
    "flex",
    "libssl-dev",
    "libelf-dev",
    "libncurses-dev",
    "dwarves",
    "rsync",
    "fakeroot",
    "dpkg-dev",
    "debhelper",
    "cpio",
    "kmod",
    "openssl",
)


class BuilderError(RuntimeError):
    """User-facing build error."""


@dataclass(frozen=True)
class SourceInfo:
    archive: Path
    version: str
    suffix: str
    base_url: str
    archive_url: str
    signature_url: str
    checksums_url: str


@dataclass(frozen=True)
class VerificationResult:
    source: SourceInfo
    sha256: str
    signer_fingerprint: str | None


@dataclass(frozen=True)
class BuildOptions:
    jobs: int
    performance_governor: bool = True
    native_tuning: bool = False
    clean_workspace: bool = False
    verification_mode: str = VERIFY_FULL
    self_sign_modules: bool = False
    install_after_build: bool = False


def source_info(archive: Path) -> SourceInfo:
    archive = archive.expanduser().resolve()
    match = ARCHIVE_RE.fullmatch(archive.name)
    if not match:
        raise BuilderError(
            "Der Dateiname muss dem kernel.org-Versionsschema entsprechen, "
            "z. B. linux-7.1.3.tar.xz."
        )
    if not archive.is_file():
        raise BuilderError(f"Quelldatei nicht gefunden: {archive}")

    version = match.group("version")
    major = int(version.split(".", 1)[0])
    if major < 3:
        raise BuilderError("Diese App unterstützt kernel.org-Releases ab Linux 3.x.")
    stem = f"linux-{version}"
    if "-rc" in version:
        # kernel.org links mainline release candidates to a generated snapshot
        # of Linus Torvalds' Git tree. They are not published in the CDN
        # release directory with sha256sums.asc and a detached TAR signature.
        base_url = "https://git.kernel.org/torvalds/t/"
        archive_url = base_url + archive.name
        signature_url = ""
        checksums_url = ""
    else:
        base_url = f"https://cdn.kernel.org/pub/linux/kernel/v{major}.x/"
        archive_url = base_url + archive.name
        signature_url = base_url + stem + ".tar.sign"
        checksums_url = base_url + "sha256sums.asc"
    return SourceInfo(
        archive=archive,
        version=version,
        suffix=match.group("suffix"),
        base_url=base_url,
        archive_url=archive_url,
        signature_url=signature_url,
        checksums_url=checksums_url,
    )


def sha256_file(path: Path, callback: Callable[[int], None] | None = None) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
            done += len(chunk)
            if callback and total:
                callback(int(done * 100 / total))
    return digest.hexdigest()


def _validate_kernel_org_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in KERNEL_ORG_DOWNLOAD_HOSTS:
        raise BuilderError(f"Unsichere Download-Adresse abgelehnt: {url}")


def _open_kernel_org_url(url: str):
    _validate_kernel_org_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "AILinux-Kernel-Builder/1.0"})
    response = urllib.request.urlopen(request, timeout=60)
    try:
        _validate_kernel_org_url(response.geturl())
    except Exception:
        response.close()
        raise
    return response


def _download(url: str, destination: Path, log: LogFn) -> None:
    log(f"Lade Prüfdaten von {url}")
    try:
        with _open_kernel_org_url(url) as response, destination.open("wb") as out:
            shutil.copyfileobj(response, out)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        if isinstance(exc, BuilderError):
            raise
        raise BuilderError(f"Download von kernel.org fehlgeschlagen: {exc}") from exc


def _sha256_url(url: str, expected_size: int, log: LogFn) -> str:
    log(f"Vergleiche mit offiziellem Mainline-Snapshot von {url}")
    digest = hashlib.sha256()
    received = 0
    try:
        with _open_kernel_org_url(url) as response:
            while chunk := response.read(4 * 1024 * 1024):
                received += len(chunk)
                if received > expected_size:
                    raise BuilderError(
                        "Der kernel.org-Snapshot ist größer als das lokale Archiv; "
                        "der Online-Vergleich wurde abgebrochen."
                    )
                digest.update(chunk)
    except Exception as exc:
        if isinstance(exc, BuilderError):
            raise
        raise BuilderError(f"Download von kernel.org fehlgeschlagen: {exc}") from exc
    if received != expected_size:
        raise BuilderError(
            "Der kernel.org-Snapshot hat eine andere Größe als das lokale Archiv."
        )
    return digest.hexdigest()


def _manifest_hash(manifest: Path, filename: str) -> str:
    pattern = re.compile(r"^([0-9a-fA-F]{64})\s+[* ]?(.+?)\s*$")
    for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if match and Path(match.group(2)).name == filename:
            return match.group(1).lower()
    raise BuilderError(
        f"{filename} ist nicht im offiziellen sha256sums.asc gelistet. "
        "Neu gepackte ZIPs oder umbenannte Archive werden bewusst abgelehnt."
    )


def _open_uncompressed_tar(source: SourceInfo) -> BinaryIO:
    suffix = source.suffix
    if suffix == ".tar":
        return source.archive.open("rb")
    if suffix == ".tar.xz":
        return lzma.open(source.archive, "rb")
    if suffix == ".tar.gz":
        return gzip.open(source.archive, "rb")
    if suffix == ".tar.bz2":
        return bz2.open(source.archive, "rb")
    if suffix == ".tar.zst":
        try:
            from compression import zstd  # Python 3.14+

            return zstd.open(source.archive, "rb")
        except (ImportError, AttributeError) as exc:
            raise BuilderError("Für .tar.zst wird Python 3.14 oder neuer benötigt.") from exc
    raise BuilderError(
        "Dieses Format besitzt keine kernel.org-TAR-Signatur. Verwende das originale .tar.xz."
    )


def _run(args: list[str], *, input_data: bytes | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_data,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        check=False,
    )


def _prepare_keyring(keyring: Path, log: LogFn) -> None:
    gpg = shutil.which("gpg2") or shutil.which("gpg")
    if not gpg:
        raise BuilderError("GnuPG fehlt. Installiere das Debian-Paket 'gnupg'.")
    keyring.mkdir(parents=True, exist_ok=True)
    keyring.chmod(0o700)

    listing = _run([gpg, "--batch", "--homedir", str(keyring), "--with-colons", "--fingerprint"])
    known = {value for value in OFFICIAL_SIGNER_FINGERPRINTS if value in listing.stdout.replace(" ", "").upper()}
    if known:
        return

    log("Importiere Kernel-Maintainer-Schlüssel über kernel.org WKD …")
    for email in KERNEL_KEY_EMAILS:
        result = _run(
            [
                gpg,
                "--batch",
                "--homedir",
                str(keyring),
                "--auto-key-locate",
                "clear,wkd",
                "--locate-keys",
                email,
            ]
        )
        if result.returncode:
            log(f"Hinweis: Schlüssel {email} konnte nicht geladen werden.")


def _verify_tar_signature(source: SourceInfo, signature: Path, keyring: Path, log: LogFn) -> str:
    gpg = shutil.which("gpg2") or shutil.which("gpg")
    if not gpg:
        raise BuilderError("GnuPG fehlt.")
    _prepare_keyring(keyring, log)
    args = [
        gpg,
        "--batch",
        "--homedir",
        str(keyring),
        "--status-fd=1",
        "--verify",
        str(signature),
        "-",
    ]
    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None
        with _open_uncompressed_tar(source) as stream:
            while chunk := stream.read(4 * 1024 * 1024):
                process.stdin.write(chunk)
        process.stdin.close()
        stdout = process.stdout.read() if process.stdout else b""
        stderr = process.stderr.read() if process.stderr else b""
        returncode = process.wait()
    except Exception:
        process.kill()
        process.wait()
        raise

    output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    valid = re.search(r"\[GNUPG:\] VALIDSIG ([0-9A-Fa-f]{40,64})\b", output)
    if returncode or not valid:
        short = "\n".join(line for line in output.splitlines() if "GNUPG" not in line)[-1500:]
        raise BuilderError(f"OpenPGP-Signatur des Kernel-TARs ist ungültig.\n{short}")
    fingerprint = valid.group(1).upper()
    if fingerprint not in OFFICIAL_SIGNER_FINGERPRINTS:
        raise BuilderError(f"Signatur gültig, aber unbekannter Release-Schlüssel: {fingerprint}")
    return fingerprint


def verify_kernel_org_source(
    archive: Path,
    cache_dir: Path,
    log: LogFn = lambda _message: None,
    progress: Callable[[int], None] | None = None,
    mode: str = VERIFY_FULL,
) -> VerificationResult:
    if mode not in VERIFICATION_MODES:
        raise BuilderError(f"Unbekannter Prüfmodus: {mode}")
    source = source_info(archive)
    cache_dir.mkdir(parents=True, exist_ok=True)
    checksums = cache_dir / f"sha256sums-v{source.version}.asc"
    signature = cache_dir / f"linux-{source.version}.tar.sign"
    log("Berechne SHA-256 der Quelldatei …")
    actual = sha256_file(source.archive, progress)

    if mode == VERIFY_LOCAL:
        log(
            "WARNUNG: Online-Gegenprüfung deaktiviert. "
            "Der lokale SHA-256 wird nur dokumentiert; Herkunft und Echtheit sind nicht bestätigt."
        )
        return VerificationResult(source=source, sha256=actual, signer_fingerprint=None)

    if "-rc" in source.version:
        if mode == VERIFY_FULL:
            raise BuilderError(
                "kernel.org veröffentlicht Mainline-Release-Candidates als Git-Snapshot "
                "ohne separates sha256sums.asc und ohne TAR-Signatur. "
                "Wähle „Ohne Signatur“ für den bytegenauen Online-Vergleich mit "
                "git.kernel.org oder bewusst „Lokales Archiv“."
            )
        expected = _sha256_url(source.archive_url, source.archive.stat().st_size, log)
        if actual != expected:
            raise BuilderError(
                "SHA-256 stimmt nicht mit dem aktuellen offiziellen Mainline-Snapshot "
                "auf git.kernel.org überein. Das Archiv ist beschädigt, verändert "
                "oder kein Original."
            )
        log(f"SHA-256 stimmt mit dem offiziellen kernel.org-Snapshot überein: {actual}")
        log(
            "WARNUNG: Für diesen Mainline-RC ist keine separate TAR-Signatur verfügbar. "
            "Die Quelle wurde bytegenau mit dem offiziellen HTTPS-Snapshot verglichen."
        )
        return VerificationResult(source=source, sha256=actual, signer_fingerprint=None)

    _download(source.checksums_url, checksums, log)
    expected = _manifest_hash(checksums, source.archive.name)
    if actual != expected:
        raise BuilderError(
            "SHA-256 stimmt nicht mit kernel.org überein. Das Archiv ist beschädigt, "
            "verändert oder kein Original."
        )
    log(f"SHA-256 stimmt mit kernel.org überein: {actual}")

    fingerprint: str | None = None
    if mode == VERIFY_FULL:
        _download(source.signature_url, signature, log)
        log("Prüfe die Entwickler-Signatur des unkomprimierten TARs …")
        fingerprint = _verify_tar_signature(source, signature, cache_dir / "gnupg", log)
        log(f"Kernel-Signatur gültig: {fingerprint}")
    else:
        log(
            "WARNUNG: OpenPGP-Signaturprüfung deaktiviert. "
            "Die Quelle wurde nur gegen die von kernel.org gelistete SHA-256-Prüfsumme geprüft."
        )
    return VerificationResult(source=source, sha256=actual, signer_fingerprint=fingerprint)


def ensure_module_signing_key(key_dir: Path, log: LogFn) -> tuple[Path, Path]:
    """Create one persistent local module-signing key and its MOK certificate."""
    openssl = shutil.which("openssl")
    if not openssl:
        raise BuilderError("OpenSSL fehlt. Installiere das Debian-Paket 'openssl'.")
    key_dir.mkdir(parents=True, exist_ok=True)
    key_dir.chmod(0o700)
    private_key = key_dir / "ailinux-module-signing-key.pem"
    certificate = key_dir / "ailinux-module-signing-cert.x509"
    mok_certificate = key_dir / "ailinux-module-signing-cert.der"

    if private_key.is_file() and certificate.is_file() and mok_certificate.is_file():
        _ensure_kernel_signing_pem(private_key, certificate, log)
        private_key.chmod(0o600)
        log(f"Verwende vorhandenen lokalen Signaturschlüssel: {private_key}")
        return private_key, mok_certificate

    for path in (private_key, certificate, mok_certificate):
        path.unlink(missing_ok=True)
    log("Erzeuge einmaligen, lokal persistenten RSA-4096-Schlüssel für Kernelmodule …")
    result = _run(
        [
            openssl,
            "req",
            "-new",
            "-x509",
            "-newkey",
            "rsa:4096",
            "-sha256",
            "-nodes",
            "-days",
            "3650",
            "-subj",
            "/CN=AILinux Local Kernel Module Signing/",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ]
    )
    if result.returncode:
        raise BuilderError(f"Signaturschlüssel konnte nicht erzeugt werden:\n{result.stdout[-1500:]}")
    private_key.chmod(0o600)
    result = _run(
        [
            openssl,
            "x509",
            "-in",
            str(certificate),
            "-outform",
            "DER",
            "-out",
            str(mok_certificate),
        ]
    )
    if result.returncode:
        raise BuilderError(f"MOK-Zertifikat konnte nicht erzeugt werden:\n{result.stdout[-1500:]}")
    _ensure_kernel_signing_pem(private_key, certificate, log)
    certificate.chmod(0o644)
    mok_certificate.chmod(0o644)
    return private_key, mok_certificate


def _ensure_kernel_signing_pem(private_key: Path, certificate: Path, log: LogFn) -> None:
    """Keep the private key and its X.509 certificate in the PEM used by Kbuild."""
    key_data = private_key.read_bytes()
    certificate_data = certificate.read_bytes().strip()
    if not certificate_data:
        raise BuilderError(f"Leeres Modul-Signaturzertifikat: {certificate}")
    if certificate_data in key_data:
        return

    # certs/extract-cert reads CONFIG_MODULE_SIG_KEY and therefore needs the
    # certificate next to the private key in the same PEM file.
    combined = key_data.rstrip() + b"\n" + certificate_data + b"\n"
    temporary = private_key.with_name(private_key.name + ".tmp")
    try:
        temporary.write_bytes(combined)
        temporary.chmod(0o600)
        os.replace(temporary, private_key)
    finally:
        temporary.unlink(missing_ok=True)
    log("Kernel-Signatur-PEM um das zugehörige X.509-Zertifikat ergänzt.")


def installable_kernel_debs(debs: Iterable[Path]) -> list[Path]:
    """Select runtime image and matching headers, excluding debug/libc packages."""
    selected: list[Path] = []
    for path in debs:
        name = path.name
        if name.startswith("linux-headers-"):
            selected.append(path)
        elif name.startswith("linux-image-") and "-dbg_" not in name:
            selected.append(path)
    return sorted(selected)


def _safe_posix_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise BuilderError(f"Unsicherer Archivpfad abgelehnt: {name}")
    return path


def _validate_tar_member(member: tarfile.TarInfo) -> None:
    path = _safe_posix_path(member.name)
    if member.ischr() or member.isblk() or member.isfifo():
        raise BuilderError(f"Spezialdatei im Archiv abgelehnt: {member.name}")
    if member.issym() or member.islnk():
        target = PurePosixPath(member.linkname)
        if target.is_absolute():
            raise BuilderError(f"Absoluter Link im Archiv abgelehnt: {member.name}")
        resolved = path.parent.joinpath(target) if member.issym() else target
        depth = 0
        for part in resolved.parts:
            if part == "..":
                depth -= 1
            elif part not in ("", "."):
                depth += 1
            if depth < 0:
                raise BuilderError(f"Symlink verlässt das Zielverzeichnis: {member.name}")


def extract_source(verification: VerificationResult, workspace: Path, log: LogFn) -> Path:
    source = verification.source
    destination = workspace / f"linux-{source.version}"
    staging = workspace / f".extract-linux-{source.version}"
    if destination.exists() or staging.exists():
        raise BuilderError(
            f"Quellverzeichnis existiert bereits: {destination}. "
            "Aktiviere 'Arbeitsordner neu erstellen' oder entferne es bewusst."
        )
    workspace.mkdir(parents=True, exist_ok=True)
    staging.mkdir()
    log(f"Entpacke sicher nach {destination}")
    try:
        if source.suffix == ".zip":
            with zipfile.ZipFile(source.archive) as archive:
                for info in archive.infolist():
                    _safe_posix_path(info.filename)
                    mode = info.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise BuilderError(f"ZIP-Symlink abgelehnt: {info.filename}")
                archive.extractall(staging)
        else:
            with tarfile.open(source.archive, mode="r:*") as archive:
                members = archive.getmembers()
                if len(members) > 250_000:
                    raise BuilderError("Archiv enthält ungewöhnlich viele Dateien.")
                if sum(max(0, item.size) for item in members) > 12 * 1024**3:
                    raise BuilderError("Archiv ist entpackt ungewöhnlich groß.")
                for member in members:
                    _validate_tar_member(member)
                archive.extractall(staging, members=members, filter="data")

        roots = [item for item in staging.iterdir()]
        expected_root = staging / f"linux-{source.version}"
        if len(roots) != 1 or not expected_root.is_dir():
            raise BuilderError("Das Archiv besitzt nicht die erwartete kernel.org-Verzeichnisstruktur.")
        if not (expected_root / "Makefile").is_file() or not (expected_root / "Kconfig").is_file():
            raise BuilderError("Keine vollständigen Linux-Kernelquellen gefunden.")
        expected_root.rename(destination)
        staging.rmdir()
        return destination
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def config_commands(performance_governor: bool) -> tuple[tuple[str, str | None], ...]:
    commands: list[tuple[str, str | None]] = [
        ("--disable", "CONFIG_PREEMPT_NONE"),
        ("--disable", "CONFIG_PREEMPT_VOLUNTARY"),
        ("--disable", "CONFIG_PREEMPT_LAZY"),
        ("--disable", "CONFIG_PREEMPT_RT"),
        ("--enable", "CONFIG_PREEMPT"),
        ("--enable", "CONFIG_PREEMPT_DYNAMIC"),
        ("--enable", "CONFIG_HZ_1000"),
        ("--disable", "CONFIG_HZ_250"),
        ("--disable", "CONFIG_HZ_300"),
        ("--set-val", "CONFIG_HZ=1000"),
        ("--enable", "CONFIG_NO_HZ_IDLE"),
        ("--disable", "CONFIG_NO_HZ_FULL"),
        ("--enable", "CONFIG_SCHED_AUTOGROUP"),
        ("--enable", "CONFIG_UCLAMP_TASK"),
        ("--enable", "CONFIG_RCU_EXPERT"),
        ("--enable", "CONFIG_RCU_BOOST"),
        ("--enable", "CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE"),
        ("--disable", "CONFIG_CC_OPTIMIZE_FOR_SIZE"),
        ("--enable", "CONFIG_CPU_FREQ"),
        ("--enable", "CONFIG_CPU_FREQ_GOV_PERFORMANCE"),
        ("--enable", "CONFIG_CPU_FREQ_GOV_SCHEDUTIL"),
        ("--enable", "CONFIG_TRANSPARENT_HUGEPAGE"),
        ("--enable", "CONFIG_TRANSPARENT_HUGEPAGE_MADVISE"),
        ("--disable", "CONFIG_TRANSPARENT_HUGEPAGE_ALWAYS"),
        ("--enable", "CONFIG_HUGETLBFS"),
        ("--enable", "CONFIG_ZSWAP"),
        ("--enable", "CONFIG_ZSWAP_DEFAULT_ON"),
        ("--enable", "CONFIG_ZSMALLOC"),
        ("--disable", "CONFIG_ZSWAP_COMPRESSOR_DEFAULT_DEFLATE"),
        ("--disable", "CONFIG_ZSWAP_COMPRESSOR_DEFAULT_LZO"),
        ("--disable", "CONFIG_ZSWAP_COMPRESSOR_DEFAULT_842"),
        ("--disable", "CONFIG_ZSWAP_COMPRESSOR_DEFAULT_LZ4"),
        ("--disable", "CONFIG_ZSWAP_COMPRESSOR_DEFAULT_LZ4HC"),
        ("--enable", "CONFIG_ZSWAP_COMPRESSOR_DEFAULT_ZSTD"),
        ("--enable", "CONFIG_ZSTD_COMPRESS"),
        ("--enable", "CONFIG_KERNEL_ZSTD"),
        ("--enable", "CONFIG_MODULE_COMPRESS"),
        ("--disable", "CONFIG_MODULE_COMPRESS_GZIP"),
        ("--disable", "CONFIG_MODULE_COMPRESS_XZ"),
        ("--enable", "CONFIG_MODULE_COMPRESS_ZSTD"),
        ("--enable", "CONFIG_LRU_GEN"),
        ("--enable", "CONFIG_LRU_GEN_WALKS_MMU"),
        ("--enable", "CONFIG_IO_URING"),
        ("--enable", "CONFIG_IOSCHED_BFQ"),
        ("--enable", "CONFIG_BFQ_GROUP_IOSCHED"),
        ("--enable", "CONFIG_TCP_CONG_BBR"),
        ("--enable", "CONFIG_NET_SCH_FQ"),
        ("--enable", "CONFIG_NET_SCH_DEFAULT"),
        ("--disable", "CONFIG_DEFAULT_PFIFO_FAST"),
        ("--disable", "CONFIG_DEFAULT_CODEL"),
        ("--disable", "CONFIG_DEFAULT_FQ_CODEL"),
        ("--disable", "CONFIG_DEFAULT_FQ_PIE"),
        ("--disable", "CONFIG_DEFAULT_SFQ"),
        ("--set-str", "CONFIG_DEFAULT_TCP_CONG=bbr"),
        ("--enable", "CONFIG_DEFAULT_BBR"),
        ("--enable", "CONFIG_DEFAULT_FQ"),
        ("--enable", "CONFIG_DRM_ACCEL"),
        ("--enable", "CONFIG_IOMMU_SUPPORT"),
        ("--enable", "CONFIG_DEBUG_INFO_NONE"),
        ("--disable", "CONFIG_DEBUG_INFO"),
        ("--disable", "CONFIG_GDB_SCRIPTS"),
        ("--disable", "CONFIG_WERROR"),
    ]
    commands.extend(
        [
            ("--disable", "CONFIG_CPU_FREQ_DEFAULT_GOV_POWERSAVE"),
            ("--disable", "CONFIG_CPU_FREQ_DEFAULT_GOV_USERSPACE"),
            ("--disable", "CONFIG_CPU_FREQ_DEFAULT_GOV_ONDEMAND"),
            ("--disable", "CONFIG_CPU_FREQ_DEFAULT_GOV_CONSERVATIVE"),
            ("--disable", "CONFIG_CPU_FREQ_DEFAULT_GOV_SCHEDUTIL"),
            ("--enable", "CONFIG_CPU_FREQ_DEFAULT_GOV_PERFORMANCE"),
        ]
        if performance_governor
        else [
            ("--disable", "CONFIG_CPU_FREQ_DEFAULT_GOV_POWERSAVE"),
            ("--disable", "CONFIG_CPU_FREQ_DEFAULT_GOV_USERSPACE"),
            ("--disable", "CONFIG_CPU_FREQ_DEFAULT_GOV_ONDEMAND"),
            ("--disable", "CONFIG_CPU_FREQ_DEFAULT_GOV_CONSERVATIVE"),
            ("--disable", "CONFIG_CPU_FREQ_DEFAULT_GOV_PERFORMANCE"),
            ("--enable", "CONFIG_CPU_FREQ_DEFAULT_GOV_SCHEDUTIL"),
        ]
    )
    return tuple(commands)


def missing_packages(packages: Iterable[str] = REQUIRED_PACKAGES) -> list[str]:
    missing: list[str] = []
    for package in packages:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", package],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode or result.stdout.strip() != "install ok installed":
            missing.append(package)
    return missing


def remove_workspace_path(path: Path, workspace: Path) -> None:
    path = path.resolve()
    workspace = workspace.resolve()
    if path.parent != workspace or not path.name.startswith(("linux-", "build-")):
        raise BuilderError(f"Löschen außerhalb des Build-Arbeitsordners abgelehnt: {path}")
    if path.exists():
        shutil.rmtree(path)


def copy_debs(build_parent: Path, output_dir: Path, before: dict[Path, int], log: LogFn) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = {path.resolve(): path.stat().st_mtime_ns for path in build_parent.glob("*.deb")}
    created = sorted(path for path, mtime in candidates.items() if before.get(path) != mtime)
    if not created:
        raise BuilderError("Build beendet, aber es wurden keine neuen .deb-Dateien gefunden.")
    copied: list[Path] = []
    checksum_lines: list[str] = []
    for source in created:
        target = output_dir / source.name
        shutil.copy2(source, target)
        digest = sha256_file(target)
        checksum_lines.append(f"{digest}  {target.name}")
        copied.append(target)
        log(f"DEB gespeichert: {target}")
    (output_dir / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    return copied


def make_environment(version: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "KBUILD_BUILD_USER": "ailinux",
            "KBUILD_BUILD_HOST": "kernel-builder",
            "DEBFULLNAME": "AILinux Kernel Builder",
            "DEBEMAIL": "admin@ailinux.me",
            "KDEB_PKGVERSION": f"{version}-1",
        }
    )
    return env
