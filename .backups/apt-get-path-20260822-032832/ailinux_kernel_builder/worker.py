from __future__ import annotations

import gzip
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from .core import (
    BuildOptions,
    BuilderError,
    VERIFY_CHECKSUM,
    config_commands,
    copy_debs,
    ensure_module_signing_key,
    extract_source,
    installable_kernel_debs,
    make_environment,
    missing_packages,
    remove_workspace_path,
    verify_kernel_org_source,
)


class KernelBuildWorker(QThread):
    log_line = pyqtSignal(str)
    phase = pyqtSignal(str)
    progress = pyqtSignal(int)
    success = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, archive: Path, app_dir: Path, options: BuildOptions, verify_only: bool = False):
        super().__init__()
        self.archive = archive
        self.app_dir = app_dir
        self.options = options
        self.verify_only = verify_only
        self._cancelled = False
        self._process: subprocess.Popen[str] | None = None

    def cancel(self) -> None:
        self._cancelled = True
        process = self._process
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise BuilderError("Vorgang wurde abgebrochen.")

    def _log(self, text: str) -> None:
        self.log_line.emit(text.rstrip())

    def _run_command(
        self,
        args: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        optional: bool = False,
    ) -> int:
        self._check_cancelled()
        self._log("$ " + " ".join(args))
        self._process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            start_new_session=True,
        )
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._log(line)
            if self._cancelled and self._process.poll() is None:
                os.killpg(self._process.pid, signal.SIGTERM)
        code = self._process.wait()
        self._process = None
        self._check_cancelled()
        if code and not optional:
            raise BuilderError(f"Befehl fehlgeschlagen (Exit {code}): {' '.join(args)}")
        return code

    def _initial_config(self, source_dir: Path, build_dir: Path) -> None:
        boot_config = Path(f"/boot/config-{os.uname().release}")
        proc_config = Path("/proc/config.gz")
        target = build_dir / ".config"
        if boot_config.is_file():
            shutil.copy2(boot_config, target)
            self._log(f"Ausgangsconfig: {boot_config}")
        elif proc_config.is_file():
            with gzip.open(proc_config, "rb") as source, target.open("wb") as out:
                shutil.copyfileobj(source, out)
            self._log("Ausgangsconfig: /proc/config.gz")
        else:
            self._log("Keine laufende Kernel-Config gefunden; verwende defconfig.")
            self._run_command(["make", "-C", str(source_dir), f"O={build_dir}", "defconfig"], self.app_dir)

    def _apply_profile(self, source_dir: Path, build_dir: Path) -> None:
        config_tool = source_dir / "scripts/config"
        config_file = build_dir / ".config"
        for operation, value in config_commands(self.options.performance_governor):
            if operation in ("--set-val", "--set-str"):
                assert value is not None
                symbol, setting = value.split("=", 1)
                args = [str(config_tool), "--file", str(config_file), operation, symbol, setting]
            else:
                args = [str(config_tool), "--file", str(config_file), operation, str(value)]
            self._run_command(args, source_dir, optional=True)

    def _apply_module_signing(self, source_dir: Path, build_dir: Path, private_key: Path) -> None:
        config_tool = source_dir / "scripts/config"
        config_file = build_dir / ".config"
        commands = (
            ("--enable", "CONFIG_MODULE_SIG"),
            ("--enable", "CONFIG_MODULE_SIG_ALL"),
            ("--enable", "CONFIG_MODULE_SIG_SHA256"),
            ("--disable", "CONFIG_MODULE_SIG_SHA1"),
            ("--disable", "CONFIG_MODULE_SIG_SHA224"),
            ("--disable", "CONFIG_MODULE_SIG_SHA384"),
            ("--disable", "CONFIG_MODULE_SIG_SHA512"),
            ("--set-str", f"CONFIG_MODULE_SIG_KEY={private_key}"),
            ("--set-str", "CONFIG_SYSTEM_TRUSTED_KEYS="),
            ("--set-str", "CONFIG_SYSTEM_REVOCATION_KEYS="),
        )
        for operation, value in commands:
            symbol, setting = value.split("=", 1) if "=" in value else (value, "")
            args = [str(config_tool), "--file", str(config_file), operation, symbol]
            if operation == "--set-str":
                args.append(setting)
            self._run_command(args, source_dir)

    def run(self) -> None:
        try:
            if os.geteuid() == 0:
                raise BuilderError("Kernel-Builds dürfen nicht als root gestartet werden.")
            workspace = self.app_dir / ".ailinux-kernel-work"
            cache = workspace / "verification"
            output = self.app_dir / "output"

            self.phase.emit("kernel.org-Original prüfen")
            verification = verify_kernel_org_source(
                self.archive,
                cache,
                self._log,
                self.progress.emit,
                mode=self.options.verification_mode,
            )
            self.progress.emit(100)
            self._check_cancelled()
            if self.verify_only:
                self.success.emit(
                    [
                        f"SHA-256: {verification.sha256}",
                        (
                            f"Signatur: {verification.signer_fingerprint}"
                            if verification.signer_fingerprint
                            else (
                                "Prüfung: kernel.org-SHA-256, keine OpenPGP-Signatur"
                                if self.options.verification_mode == VERIFY_CHECKSUM
                                else "Prüfung: keine Online-Gegenprüfung; lokaler SHA-256 dokumentiert"
                            )
                        ),
                    ]
                )
                return

            missing = missing_packages()
            if missing:
                self.phase.emit("Build-Abhängigkeiten installieren")
                self._log("Fehlende Systempakete: " + ", ".join(missing))
                if not shutil.which("pkexec"):
                    raise BuilderError(
                        "Fehlende Build-Abhängigkeiten und pkexec ist nicht verfügbar: "
                        + ", ".join(missing)
                    )
                self._run_command(["pkexec", "apt-get", "update"], self.app_dir)
                self._run_command(
                    ["pkexec", "apt-get", "install", "-y", *missing],
                    self.app_dir,
                )
                left = missing_packages()
                if left:
                    raise BuilderError(
                        "Build-Abhängigkeiten fehlen nach der Installation weiterhin: "
                        + ", ".join(left)
                    )
                self._log("Build-Abhängigkeiten vollständig.")

            source_dir = workspace / f"linux-{verification.source.version}"
            build_dir = workspace / f"build-{verification.source.version}"
            if self.options.clean_workspace:
                remove_workspace_path(build_dir, workspace)

            self.phase.emit("Quellen sicher entpacken")
            # Never reuse an extracted tree: it could have been changed after a
            # previous verification.  Re-extracting keeps the build tied to the
            # just-verified kernel.org archive while still allowing incremental
            # object builds in the separate O= directory.
            remove_workspace_path(source_dir, workspace)
            source_dir = extract_source(verification, workspace, self._log)
            build_dir.mkdir(parents=True, exist_ok=True)

            self.phase.emit("Kernel konfigurieren")
            if not (build_dir / ".config").exists():
                self._initial_config(source_dir, build_dir)
            make_base = ["make", "-C", str(source_dir), f"O={build_dir}"]
            self._run_command(make_base + ["olddefconfig"], self.app_dir)
            self._apply_profile(source_dir, build_dir)
            mok_certificate: Path | None = None
            if self.options.self_sign_modules:
                private_key, mok_certificate = ensure_module_signing_key(
                    workspace / "signing",
                    self._log,
                )
                self._apply_module_signing(source_dir, build_dir, private_key)
            self._run_command(make_base + ["olddefconfig"], self.app_dir)

            config = (build_dir / ".config").read_text(encoding="utf-8", errors="replace")
            if "CONFIG_HZ_1000=y" not in config or "CONFIG_PREEMPT=y" not in config:
                raise BuilderError("Kernelprofil konnte HZ=1000 oder PREEMPT nicht aktivieren.")

            self.phase.emit("Debian-Pakete bauen")
            package_parent = build_dir.parent
            before = {path.resolve(): path.stat().st_mtime_ns for path in package_parent.glob("*.deb")}
            env = make_environment(verification.source.version)
            build_args = make_base + [f"-j{self.options.jobs}", "LOCALVERSION=-ailinux"]
            if self.options.native_tuning:
                build_args.append("KCFLAGS=-mtune=native")
            build_args.append("bindeb-pkg")
            started = time.monotonic()
            self._run_command(build_args, self.app_dir, env=env)
            self._log(f"Buildzeit: {(time.monotonic() - started) / 60:.1f} Minuten")

            self.phase.emit("DEB-Dateien sammeln")
            copied = copy_debs(package_parent, output, before, self._log)
            results = [str(path) for path in copied]
            if mok_certificate:
                results.extend(
                    [
                        f"MOK-Zertifikat: {mok_certificate}",
                        "Secure Boot: Zertifikat bei Bedarf mit "
                        f"'sudo mokutil --import {mok_certificate}' registrieren und beim Neustart bestätigen.",
                    ]
                )

            if self.options.install_after_build:
                packages = installable_kernel_debs(copied)
                if not packages:
                    raise BuilderError("Keine installierbaren Kernel-Image-/Header-Pakete gefunden.")
                self.phase.emit("Kernel und Header installieren")
                self._run_command(
                    ["pkexec", "apt-get", "install", "-y", *[str(path) for path in packages]],
                    output,
                )
                results.append("Kernel und Header wurden installiert; der bisherige Kernel bleibt erhalten.")

            self.success.emit(results)
        except Exception as exc:
            self.failed.emit(str(exc))


class DependencyInstallWorker(QThread):
    log_line = pyqtSignal(str)
    success = pyqtSignal(list)
    failed = pyqtSignal(str)

    def run(self) -> None:
        try:
            missing = missing_packages()
            if not missing:
                self.success.emit(["Alle Build-Abhängigkeiten sind installiert."])
                return
            command = ["pkexec", "apt-get", "install", "-y", *missing]
            process = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.log_line.emit(process.stdout)
            if process.returncode:
                raise BuilderError(f"Paketinstallation fehlgeschlagen (Exit {process.returncode}).")
            left = missing_packages()
            if left:
                raise BuilderError("Weiterhin fehlend: " + ", ".join(left))
            self.success.emit(["Build-Abhängigkeiten wurden installiert."])
        except Exception as exc:
            self.failed.emit(str(exc))
