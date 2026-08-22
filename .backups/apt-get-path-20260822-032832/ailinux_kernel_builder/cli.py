from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .core import (
    BuildOptions,
    BuilderError,
    REQUIRED_PACKAGES,
    VERIFY_CHECKSUM,
    VERIFY_FULL,
    VERIFY_LOCAL,
    VERIFICATION_MODES,
    config_commands,
    copy_debs,
    ensure_module_signing_key,
    extract_source,
    installable_kernel_debs,
    make_environment,
    missing_packages,
    remove_workspace_path,
    source_info,
    verify_kernel_org_source,
)


def cli_log(message: str) -> None:
    print(f"[*] {message}", flush=True)


def check_dependencies() -> int:
    missing = missing_packages()
    if not missing:
        print("[OK] Alle benötigten Build- und Kernel-Abhängigkeiten sind installiert:")
        for pkg in REQUIRED_PACKAGES:
            print(f"  + {pkg}")
        return 0
    print(f"[!] Es fehlen {len(missing)} Paket(e):")
    for pkg in missing:
        print(f"  - {pkg}")
    print("\nInstallationsbefehl:")
    print(f"sudo apt update && sudo apt install -y {' '.join(missing)}")
    return 1


def install_dependencies() -> int:
    missing = missing_packages()
    if not missing:
        print("[OK] Alle Abhängigkeiten sind bereits installiert.")
        return 0

    print(f"[*] Installiere fehlende Pakete: {' '.join(missing)}")
    # Use pkexec if available and not root, else sudo or direct apt
    cmd: list[str] = []
    if os.geteuid() == 0:
        cmd = ["apt-get", "install", "-y", *missing]
    elif shutil.which("pkexec") and "DISPLAY" in os.environ:
        cmd = ["pkexec", "apt-get", "install", "-y", *missing]
    elif shutil.which("sudo"):
        cmd = ["sudo", "apt-get", "install", "-y", *missing]
    else:
        cmd = ["apt-get", "install", "-y", *missing]

    res = subprocess.run(cmd)
    if res.returncode != 0:
        print(f"[FEHLER] Installation fehlgeschlagen mit Exit Code {res.returncode}")
        return res.returncode

    left = missing_packages()
    if left:
        print(f"[FEHLER] Folgende Pakete fehlen weiterhin: {', '.join(left)}")
        return 1

    print("[OK] Alle Abhängigkeiten wurden erfolgreich installiert.")
    return 0


def verify_archive(archive_path: Path, mode: str, app_dir: Path) -> int:
    workspace = app_dir / ".ailinux-kernel-work"
    cache = workspace / "verification"
    print(f"[*] Prüfe Kernel-Quellarchiv: {archive_path}")
    print(f"[*] Prüfmodus: {mode}")
    try:
        res = verify_kernel_org_source(
            archive_path,
            cache,
            log=cli_log,
            mode=mode,
        )
        print("\n[OK] Verifikation erfolgreich:")
        print(f"  - Version: {res.source.version}")
        print(f"  - SHA-256: {res.sha256}")
        if res.signer_fingerprint:
            print(f"  - Signatur-Fingerprint: {res.signer_fingerprint}")
        else:
            print(f"  - Signatur: Unsigniert / {mode}")
        return 0
    except Exception as exc:
        print(f"\n[FEHLER] Verifikation fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


def build_kernel_cli(archive_path: Path, options: BuildOptions, app_dir: Path) -> int:
    if os.geteuid() == 0:
        print("[FEHLER] Kernel-Builds dürfen nicht als root gestartet werden.", file=sys.stderr)
        return 1

    workspace = app_dir / ".ailinux-kernel-work"
    cache = workspace / "verification"
    output = app_dir / "output"

    try:
        cli_log(f"Schritt 1: Prüfe Quelldatei ({options.verification_mode}) …")
        verification = verify_kernel_org_source(
            archive_path,
            cache,
            log=cli_log,
            mode=options.verification_mode,
        )
        cli_log(f"SHA-256: {verification.sha256}")

        missing = missing_packages()
        if missing:
            print(f"[FEHLER] Fehlende Build-Abhängigkeiten:\n  {', '.join(missing)}", file=sys.stderr)
            print(f"Führe 'python3 ailinux-kernel-builder.py --install-deps' aus.", file=sys.stderr)
            return 1

        source_dir = workspace / f"linux-{verification.source.version}"
        build_dir = workspace / f"build-{verification.source.version}"
        if options.clean_workspace:
            cli_log("Bereinige bisherigen Arbeitsordner …")
            remove_workspace_path(build_dir, workspace)

        cli_log(f"Schritt 2: Entpacke Quellen sicher …")
        remove_workspace_path(source_dir, workspace)
        source_dir = extract_source(verification, workspace, cli_log)
        build_dir.mkdir(parents=True, exist_ok=True)

        cli_log("Schritt 3: Konfiguriere Kernel …")
        boot_config = Path(f"/boot/config-{os.uname().release}")
        proc_config = Path("/proc/config.gz")
        target_config = build_dir / ".config"
        if not target_config.exists():
            if boot_config.is_file():
                shutil.copy2(boot_config, target_config)
                cli_log(f"Verwende Ausgangsconfig: {boot_config}")
            elif proc_config.is_file():
                import gzip
                with gzip.open(proc_config, "rb") as s, target_config.open("wb") as o:
                    shutil.copyfileobj(s, o)
                cli_log("Verwende Ausgangsconfig: /proc/config.gz")
            else:
                cli_log("Verwende Standard-Kernel-defconfig.")
                subprocess.run(["make", "-C", str(source_dir), f"O={build_dir}", "defconfig"], check=True)

        make_base = ["make", "-C", str(source_dir), f"O={build_dir}"]
        subprocess.run(make_base + ["olddefconfig"], check=True)

        config_tool = source_dir / "scripts/config"
        for operation, value in config_commands(options.performance_governor):
            if operation in ("--set-val", "--set-str"):
                assert value is not None
                symbol, setting = value.split("=", 1) if "=" in value else (value, "")
                cmd = [str(config_tool), "--file", str(target_config), operation, symbol, setting]
            else:
                cmd = [str(config_tool), "--file", str(target_config), operation, str(value)]
            subprocess.run(cmd, check=False)

        mok_cert: Path | None = None
        if options.self_sign_modules:
            priv_key, mok_cert = ensure_module_signing_key(workspace / "signing", cli_log)
            sig_cmds = (
                ("--enable", "CONFIG_MODULE_SIG"),
                ("--enable", "CONFIG_MODULE_SIG_ALL"),
                ("--enable", "CONFIG_MODULE_SIG_SHA256"),
                ("--disable", "CONFIG_MODULE_SIG_SHA1"),
                ("--disable", "CONFIG_MODULE_SIG_SHA224"),
                ("--disable", "CONFIG_MODULE_SIG_SHA384"),
                ("--disable", "CONFIG_MODULE_SIG_SHA512"),
                ("--set-str", f"CONFIG_MODULE_SIG_KEY={priv_key}"),
                ("--set-str", "CONFIG_SYSTEM_TRUSTED_KEYS="),
                ("--set-str", "CONFIG_SYSTEM_REVOCATION_KEYS="),
            )
            for op, val in sig_cmds:
                sym, sett = val.split("=", 1) if "=" in val else (val, "")
                cmd = [str(config_tool), "--file", str(target_config), op, sym]
                if op == "--set-str":
                    cmd.append(sett)
                subprocess.run(cmd, check=True)

        subprocess.run(make_base + ["olddefconfig"], check=True)

        # Validate configuration
        cfg_text = target_config.read_text(encoding="utf-8", errors="replace")
        has_hz = "CONFIG_HZ_1000=y" in cfg_text or "CONFIG_HZ=1000" in cfg_text
        has_preempt = (
            ("CONFIG_PREEMPT=y" in cfg_text or "CONFIG_PREEMPT_DYNAMIC=y" in cfg_text or "CONFIG_PREEMPTION=y" in cfg_text)
            and "CONFIG_PREEMPT_NONE=y" not in cfg_text
        )
        if not has_hz or not has_preempt:
            raise BuilderError("Kernelprofil konnte HZ=1000 oder PREEMPT nicht aktivieren.")

        cli_log(f"Schritt 4: Baue Debian-Pakete mit {options.jobs} Threads …")
        package_parent = build_dir.parent
        search_dirs = [package_parent] + [d for d in package_parent.iterdir() if d.is_dir()]
        before: dict[Path, int] = {}
        for directory in search_dirs:
            for path in directory.glob("*.deb"):
                before[path.resolve()] = path.stat().st_mtime_ns

        env = make_environment(verification.source.version)
        build_args = make_base + [f"-j{options.jobs}", "LOCALVERSION=-ailinux"]
        if options.native_tuning:
            build_args.append("KCFLAGS=-mtune=native")
        build_args.append("bindeb-pkg")

        start_time = time.monotonic()
        res = subprocess.run(build_args, env=env)
        if res.returncode != 0:
            raise BuilderError(f"Kernel-Kompilierung fehlgeschlagen mit Exit Code {res.returncode}")
        duration = (time.monotonic() - start_time) / 60
        cli_log(f"Build abgeschlossen in {duration:.1f} Minuten.")

        cli_log("Schritt 5: Erfasse erzeugte Debian-Pakete …")
        copied = copy_debs(package_parent, output, before, cli_log)
        print("\n[OK] Folgende Pakete wurden erstellt:")
        for pkg in copied:
            print(f"  + {pkg}")

        if mok_cert:
            print(f"\n[INFO] MOK-Zertifikat für Secure Boot:")
            print(f"  sudo mokutil --import {mok_cert}")

        if options.install_after_build:
            install_pkgs = installable_kernel_debs(copied)
            if not install_pkgs:
                raise BuilderError("Keine installierbaren Kernel-Image-/Header-Pakete gefunden.")
            cli_log("Installiere Kernel und Header …")
            install_cmd = ["sudo", "apt-get", "install", "-y", *[str(p) for p in install_pkgs]]
            subprocess.run(install_cmd, check=True)
            print("[OK] Kernel und Header wurden installiert.")

        return 0
    except Exception as exc:
        print(f"\n[FEHLER] {exc}", file=sys.stderr)
        return 1


def run_cli(app_dir: Path, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AILinux Kernel Builder - Verifizierter Bau von Linux-Kernels als Debian-Pakete."
    )
    parser.add_argument(
        "--check-deps", "-c",
        action="store_true",
        help="Prüfe, ob alle benötigten Build- und Kernel-Abhängigkeiten installiert sind.",
    )
    parser.add_argument(
        "--install-deps", "-i",
        action="store_true",
        help="Installiere fehlende Build- und Kernel-Abhängigkeiten über apt.",
    )
    parser.add_argument(
        "--verify", "-v",
        type=Path,
        metavar="ARCHIV",
        help="Prüfe ein Kernel-Quellarchiv (SHA-256 und ggf. GPG-Signatur) ohne Build.",
    )
    parser.add_argument(
        "--build", "-b",
        type=Path,
        metavar="ARCHIV",
        help="Baue den Kernel aus dem angegebenen Quellarchiv als Debian-Pakete (.deb).",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=VERIFICATION_MODES,
        default=VERIFY_FULL,
        help="Verifikationsmodus: 'full' (Standard für Releases), 'checksum' (für RCs/Snapshots), 'local' (ohne Online-Prüfung).",
    )
    parser.add_argument(
        "--jobs", "-j",
        type=int,
        default=max(1, os.cpu_count() or 1),
        help="Anzahl paralleler Build-Jobs (Standard: CPU-Kerne).",
    )
    parser.add_argument(
        "--native-tuning",
        action="store_true",
        help="Aktiviere CPU-spezifisches Scheduling (-mtune=native).",
    )
    parser.add_argument(
        "--schedutil",
        action="store_true",
        help="Nutze Schedutil statt Performance als CPU-Governor.",
    )
    parser.add_argument(
        "--self-sign-modules",
        action="store_true",
        help="Signiere Kernel-Module mit lokalem RSA-4096 Schlüssel.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Arbeitsordner vor dem Build vollständig bereinigen.",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Kernel und Header nach erfolgreichem Build installieren.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Erzwinge das Starten der grafischen Benutzeroberfläche.",
    )

    args = parser.parse_args(argv)

    if args.check_deps:
        return check_dependencies()

    if args.install_deps:
        return install_dependencies()

    if args.verify:
        mode = args.mode
        if "-rc" in args.verify.name and mode == VERIFY_FULL:
            mode = VERIFY_CHECKSUM
        return verify_archive(args.verify, mode, app_dir)

    if args.build:
        mode = args.mode
        if "-rc" in args.build.name and mode == VERIFY_FULL:
            mode = VERIFY_CHECKSUM
        options = BuildOptions(
            jobs=args.jobs,
            performance_governor=not args.schedutil,
            native_tuning=args.native_tuning,
            clean_workspace=args.clean,
            verification_mode=mode,
            self_sign_modules=args.self_sign_modules,
            install_after_build=args.install,
        )
        return build_kernel_cli(args.build, options, app_dir)

    # If no action arguments were provided or --gui was specified:
    from .gui import run
    return run(app_dir)
