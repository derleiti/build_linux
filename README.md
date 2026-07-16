# AILinux Kernel Builder

PyQt6-App zum verifizierten Bau eines AI-/Gaming-/Low-Latency-Kernels als
Debian-Pakete. Die Installation bleibt standardmäßig deaktiviert und kann nach
dem Build ausdrücklich zugeschaltet werden.

## Sicherheitsmodell und optionale Signaturmodi

Standardmäßig müssen vor jedem Build beide Prüfungen erfolgreich sein:

1. SHA-256 stimmt mit `sha256sums.asc` auf `cdn.kernel.org` überein.
2. Die Signatur `linux-X.Y.Z.tar.sign` ist gültig und stammt von einem der auf
   kernel.org veröffentlichten Release-Schlüssel.

Darum werden umbenannte, veränderte oder neu als ZIP gepackte Quellen bewusst
abgelehnt. Unterstützt werden originale `linux-X.Y.Z.tar`, `.tar.xz`,
`.tar.gz`, `.tar.bz2` und mit Python 3.14 auch `.tar.zst`.

Für offizielle kernel.org-Archive kann die OpenPGP-Prüfung in der GUI bewusst
deaktiviert werden. Die SHA-256-Prüfung gegen `sha256sums.asc` bleibt zwingend
aktiv. Umbenannte oder lokal veränderte Archive werden auch in diesem Modus
nicht akzeptiert.

Optional erzeugt die App unter `.ailinux-kernel-work/signing/` einen
persistenten lokalen RSA-4096-Schlüssel und signiert damit alle gebauten
Kernelmodule. Der private Schlüssel erhält Modus `0600` und wird bei späteren
Builds wiederverwendet. Für Secure Boot muss das ausgegebene DER-Zertifikat
einmalig als Machine Owner Key registriert und beim folgenden Neustart bestätigt
werden:

```bash
sudo mokutil --import .ailinux-kernel-work/signing/ailinux-module-signing-cert.der
```

Diese Option signiert Kernelmodule; sie ersetzt keine distributionsseitige
Microsoft-/Ubuntu-Signatur des Kernel-Images.

## Start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./build_ailinux_kernel.sh
```

## Linux-Binary bauen

Das Buildskript erstellt mit PyInstaller eine eigenständige GUI-Binary. Python-
Pakete und PyInstaller werden dabei isoliert unter `.build-venv/` installiert:

```bash
./build_linux_binary.sh
```

Das Ergebnis liegt unter `dist/ailinux-kernel-builder`. Die Arbeitsdaten und
gebauten Debian-Pakete werden beim Start neben der Binary in
`.ailinux-kernel-work/` beziehungsweise `output/` abgelegt.

Die Binary gilt für die Architektur, auf der sie gebaut wurde. Für möglichst
breite Kompatibilität sollte sie auf der ältesten unterstützten Linux-
Distribution gebaut werden.

Das vorhandene `linux-7.1.3.tar.xz` wird beim ersten Start automatisch
vorausgewählt. GnuPG lädt die offiziellen Maintainer-Schlüssel über das
kernel.org Web Key Directory in einen isolierten Schlüsselring unter
`.ailinux-kernel-work/verification/gnupg`.

Kernel-Quellarchive werden wegen ihrer Größe nicht im Git-Repository
gespeichert. Ein Release muss direkt von kernel.org geladen werden, zum
Beispiel:

```bash
wget https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.1.3.tar.xz
```

Danach prüft die App das lokale Archiv zwingend gegen die offiziellen
Prüfsummen und die Entwickler-Signatur.

## Ausgabe und Arbeitsdaten

- Quellen und Objektdateien: `.ailinux-kernel-work/`
- fertige Pakete: `output/*.deb`
- Prüfsummen der Pakete: `output/SHA256SUMS`

Der Build nutzt die Config des laufenden Kernels, migriert sie mit
`olddefconfig` und setzt anschließend das AILinux-Profil:

- vollständige Preemption, dynamische Preemption und 1000 Hz
- Scheduler-Autogrouping, Utilization Clamping und RCU Boost
- Performance- oder Schedutil-Governor
- THP `madvise`, HugeTLB, Zswap/Zstd
- BFQ, io_uring, TCP BBR/FQ
- Performance-Kompilierung, Zstd-Kernel/Module, keine Debug-Info
- IOMMU und DRM-Accelerator-Infrastruktur für AI-Workloads

Die Quellen werden nach jeder erfolgreichen Originalprüfung frisch entpackt.
Ein alter oder nachträglich veränderter Quellbaum wird nie wiederverwendet;
lediglich der getrennte Objektordner kann inkrementell weitergebaut werden.

`-mtune=native` ist optional. Es optimiert das Scheduling der erzeugten
Maschinencodes für den Build-Rechner, ohne per `-march=native` zusätzliche
CPU-Befehlssätze zu erzwingen.

Mit der GUI-Option „Kernel und Header nach dem Build installieren“ werden nur
das Runtime-Image und die passenden Header über `pkexec apt-get` installiert.
Debug- und `linux-libc-dev`-Pakete bleiben außen vor. Der bisherige Kernel wird
nicht entfernt und bleibt als Rückfalloption erhalten.

Alternativ erfolgt die Installation nach erfolgreichem Build manuell:

```bash
cd output
sudo apt install ./linux-image-*-ailinux_*.deb ./linux-headers-*-ailinux_*.deb
```

Vor der Installation sollte ausreichend freier Platz in `/boot` vorhanden
sein. Der bisherige Kernel bleibt als Rückfalloption installiert.
