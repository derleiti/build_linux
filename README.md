# AILinux Kernel Builder

PyQt6-App zum verifizierten Bau eines AI-/Gaming-/Low-Latency-Kernels als
Debian-Pakete. Die App installiert den Kernel **nicht** selbst und verändert
weder `/boot` noch GRUB.

## Sicherheitsmodell

Vor jedem Build müssen beide Prüfungen erfolgreich sein:

1. SHA-256 stimmt mit `sha256sums.asc` auf `cdn.kernel.org` überein.
2. Die Signatur `linux-X.Y.Z.tar.sign` ist gültig und stammt von einem der auf
   kernel.org veröffentlichten Release-Schlüssel.

Darum werden umbenannte, veränderte oder neu als ZIP gepackte Quellen bewusst
abgelehnt. Unterstützt werden originale `linux-X.Y.Z.tar`, `.tar.xz`,
`.tar.gz`, `.tar.bz2` und mit Python 3.14 auch `.tar.zst`.

## Start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./build_ailinux_kernel.sh
```

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

Installation erfolgt nach erfolgreichem Build bewusst manuell, zum Beispiel:

```bash
cd output
sudo apt install ./linux-image-*-ailinux_*.deb ./linux-headers-*-ailinux_*.deb
```

Vor der Installation sollte ausreichend freier Platz in `/boot` vorhanden
sein. Der bisherige Kernel bleibt als Rückfalloption installiert.
