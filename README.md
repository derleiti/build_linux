# AILinux Kernel Builder

[![CI](https://github.com/derleiti/build_linux/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/derleiti/build_linux/actions/workflows/ci.yml)
[![Security](https://github.com/derleiti/build_linux/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/derleiti/build_linux/actions/workflows/security.yml)

**Current release tag: v1.0.3**. PyQt6 builder for verified AI/gaming/low-latency Linux kernel Debian packages.

## Verification modes

1. **Full verification** — SHA-256 against kernel.org plus official release signature.
2. **Checksum/snapshot verification** — stable releases use official SHA-256 lists; release candidates can be byte-compared with the official git.kernel.org snapshot.
3. **Local archive mode** — intentionally unverified origin; the application records the local hash and requires explicit user acknowledgement.

The expected archive layout remains `linux-X.Y.Z[-rcN]`. Module self-signing is optional and does not replace upstream source verification.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./build_ailinux_kernel.sh
```

## Build standalone Linux binary

```bash
./build_linux_binary.sh
```

Built Debian packages and workspace data are kept under the application's output/work directories. Kernel source archives are intentionally not stored in Git; download them from kernel.org.

## Security notes

- installation of the built kernel remains an explicit operation
- persistent module-signing private keys use restricted file permissions
- source archive extraction and verification are fail-closed
- release-candidate handling documents the weaker/different upstream verification path rather than pretending a signature exists

## License

AILinux-authored Kernel Builder code is covered by the AILinux Proprietary Source License. Linux kernel sources and all third-party dependencies remain under their respective upstream licenses.
