from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QSettings, QUrl
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .core import BuildOptions, missing_packages, source_info
from .worker import DependencyInstallWorker, KernelBuildWorker


class MainWindow(QMainWindow):
    def __init__(self, app_dir: Path):
        super().__init__()
        self.app_dir = app_dir
        self.settings = QSettings("AILinux", "KernelBuilder")
        self.worker: KernelBuildWorker | DependencyInstallWorker | None = None
        self.setWindowTitle("AILinux Kernel Builder")
        self.resize(980, 720)
        self._build_ui()
        self._apply_style()
        default_archive = app_dir / "linux-7.1.3.tar.xz"
        remembered = self.settings.value("archive", "", str)
        if remembered and Path(remembered).is_file():
            self.archive_edit.setText(remembered)
        elif default_archive.is_file():
            self.archive_edit.setText(str(default_archive))
        self._refresh_dependencies()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        title = QLabel("AILinux Kernel Builder")
        title.setObjectName("title")
        subtitle = QLabel(
            "Originale kernel.org-Quellen prüfen, für AI/Gaming/Low-Latency konfigurieren "
            "und als installierbare Debian-Pakete bauen."
        )
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        source_box = QFrame()
        source_box.setObjectName("card")
        source_layout = QVBoxLayout(source_box)
        source_layout.addWidget(QLabel("Kernel-Quellarchiv"))
        source_row = QHBoxLayout()
        from PyQt6.QtWidgets import QLineEdit

        self.archive_edit = QLineEdit()
        self.archive_edit.setPlaceholderText("linux-X.Y.Z.tar.xz")
        browse = QPushButton("Datei wählen …")
        browse.clicked.connect(self._browse)
        source_row.addWidget(self.archive_edit, 1)
        source_row.addWidget(browse)
        source_layout.addLayout(source_row)
        note = QLabel(
            "Standard: offizieller SHA-256-Eintrag plus Entwickler-Signatur. "
            "Die Signaturprüfung kann bewusst abgeschaltet werden; die kernel.org-Prüfsumme bleibt Pflicht."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        source_layout.addWidget(note)
        layout.addWidget(source_box)

        options_box = QFrame()
        options_box.setObjectName("card")
        form = QFormLayout(options_box)
        self.jobs = QSpinBox()
        self.jobs.setRange(1, max(1, os.cpu_count() or 1))
        self.jobs.setValue(max(1, os.cpu_count() or 1))
        self.performance = QCheckBox("Performance als Standard-Governor")
        self.performance.setChecked(True)
        self.native = QCheckBox("CPU-Tuning für diesen Rechner (-mtune=native)")
        self.clean = QCheckBox("Arbeitsordner für diese Version neu erstellen")
        self.verify_signature = QCheckBox("OpenPGP-Release-Signatur von kernel.org prüfen")
        self.verify_signature.setChecked(True)
        self.self_sign = QCheckBox("Kernelmodule mit persistentem lokalem Self-Signed-Key signieren")
        self.install = QCheckBox("Kernel und Header nach dem Build installieren")
        form.addRow("Parallele Jobs", self.jobs)
        form.addRow("Gaming", self.performance)
        form.addRow("CPU", self.native)
        form.addRow("Neuaufbau", self.clean)
        form.addRow("Quellprüfung", self.verify_signature)
        form.addRow("Secure Boot", self.self_sign)
        form.addRow("Installation", self.install)
        layout.addWidget(options_box)

        dep_row = QHBoxLayout()
        self.dep_label = QLabel()
        self.dep_label.setWordWrap(True)
        self.dep_button = QPushButton("Fehlende Pakete installieren")
        self.dep_button.clicked.connect(self._install_dependencies)
        dep_row.addWidget(self.dep_label, 1)
        dep_row.addWidget(self.dep_button)
        layout.addLayout(dep_row)

        action_row = QHBoxLayout()
        self.verify_button = QPushButton("Nur Original prüfen")
        self.verify_button.clicked.connect(lambda: self._start(True))
        self.build_button = QPushButton("DEB-Kernel bauen")
        self.build_button.setObjectName("primary")
        self.build_button.clicked.connect(lambda: self._start(False))
        self.cancel_button = QPushButton("Abbrechen")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        self.output_button = QPushButton("Ausgabe öffnen")
        self.output_button.clicked.connect(self._open_output)
        action_row.addWidget(self.verify_button)
        action_row.addWidget(self.build_button)
        action_row.addWidget(self.cancel_button)
        action_row.addStretch(1)
        action_row.addWidget(self.output_button)
        layout.addLayout(action_row)

        self.phase_label = QLabel("Bereit")
        self.phase_label.setObjectName("phase")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.phase_label)
        layout.addWidget(self.progress)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("monospace", 9))
        self.log.setPlaceholderText("Prüf- und Build-Ausgabe …")
        layout.addWidget(self.log, 1)
        self.setCentralWidget(root)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #11151c; color: #e8edf4; }
            QLabel#title { font-size: 26px; font-weight: 700; color: #77d6c5; }
            QLabel#muted { color: #95a0af; }
            QLabel#phase { color: #77d6c5; font-weight: 600; }
            QFrame#card { background: #1a202a; border: 1px solid #2c3542; border-radius: 8px; }
            QLineEdit, QSpinBox, QPlainTextEdit {
                background: #0c1016; border: 1px solid #344152; border-radius: 5px; padding: 7px;
                selection-background-color: #287d70;
            }
            QPushButton { background: #293342; border: 1px solid #3b485a; border-radius: 5px; padding: 8px 13px; }
            QPushButton:hover { background: #344154; }
            QPushButton:disabled { color: #66707c; background: #202630; }
            QPushButton#primary { background: #197667; border-color: #36a893; font-weight: 700; }
            QProgressBar { border: 1px solid #344152; border-radius: 4px; text-align: center; background: #0c1016; }
            QProgressBar::chunk { background: #197667; }
            """
        )

    def _browse(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Kernel-Quellarchiv wählen",
            str(self.app_dir),
            "Kernel-Archive (linux-*.tar.xz linux-*.tar.gz linux-*.tar.bz2 linux-*.tar.zst linux-*.tar linux-*.zip);;Alle Dateien (*)",
        )
        if filename:
            self.archive_edit.setText(filename)
            self.settings.setValue("archive", filename)

    def _archive(self) -> Path | None:
        path = Path(self.archive_edit.text().strip()).expanduser()
        try:
            source_info(path)
        except Exception as exc:
            QMessageBox.critical(self, "Ungültige Kernelquelle", str(exc))
            return None
        return path

    def _set_running(self, running: bool) -> None:
        self.verify_button.setEnabled(not running)
        self.build_button.setEnabled(not running)
        self.dep_button.setEnabled(not running and bool(missing_packages()))
        self.cancel_button.setEnabled(running and isinstance(self.worker, KernelBuildWorker))

    def _start(self, verify_only: bool) -> None:
        archive = self._archive()
        if archive is None:
            return
        if not self.verify_signature.isChecked():
            answer = QMessageBox.warning(
                self,
                "Signaturprüfung deaktiviert",
                "Die OpenPGP-Release-Signatur wird nicht geprüft. Das Archiv muss weiterhin exakt "
                "in der offiziellen kernel.org-SHA-256-Liste stehen.\n\nFortfahren?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not verify_only and self.clean.isChecked():
            answer = QMessageBox.question(
                self,
                "Arbeitsordner neu erstellen",
                "Die entpackten Quellen und Builddaten dieser Kernelversion werden gelöscht. Fortfahren?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not verify_only and self.install.isChecked():
            answer = QMessageBox.warning(
                self,
                "Kernel nach Build installieren",
                "Nach erfolgreichem Build werden Kernel-Image und Header über die "
                "System-Authentifizierung installiert. Der bisherige Kernel bleibt als Rückfalloption erhalten.\n\n"
                "Fortfahren?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        options = BuildOptions(
            jobs=self.jobs.value(),
            performance_governor=self.performance.isChecked(),
            native_tuning=self.native.isChecked(),
            clean_workspace=self.clean.isChecked(),
            verify_signature=self.verify_signature.isChecked(),
            self_sign_modules=self.self_sign.isChecked(),
            install_after_build=(not verify_only and self.install.isChecked()),
        )
        self.log.clear()
        self.progress.setValue(0)
        self.worker = KernelBuildWorker(archive, self.app_dir, options, verify_only)
        self.worker.log_line.connect(self._append_log)
        self.worker.phase.connect(self.phase_label.setText)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.success.connect(self._success)
        self.worker.failed.connect(self._failure)
        self.worker.finished.connect(lambda: self._set_running(False))
        self._set_running(True)
        self.worker.start()

    def _install_dependencies(self) -> None:
        missing = missing_packages()
        if not missing:
            self._refresh_dependencies()
            return
        answer = QMessageBox.question(
            self,
            "Systempakete installieren",
            "Folgende Pakete werden über die System-Authentifizierung installiert:\n\n"
            + " ".join(missing)
            + "\n\nFortfahren?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.worker = DependencyInstallWorker()
        self.worker.log_line.connect(self._append_log)
        self.worker.success.connect(self._success)
        self.worker.failed.connect(self._failure)
        self.worker.finished.connect(lambda: (self._set_running(False), self._refresh_dependencies()))
        self._set_running(True)
        self.worker.start()

    def _cancel(self) -> None:
        if isinstance(self.worker, KernelBuildWorker):
            self.phase_label.setText("Abbruch wird ausgeführt …")
            self.worker.cancel()

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(text.rstrip())
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _success(self, results: list) -> None:
        self.progress.setValue(100)
        self.phase_label.setText("Fertig")
        self._append_log("\n[OK]\n" + "\n".join(str(item) for item in results))
        QMessageBox.information(self, "Erfolgreich", "\n".join(str(item) for item in results))

    def _failure(self, message: str) -> None:
        self.phase_label.setText("Fehler")
        self._append_log("\n[FEHLER] " + message)
        QMessageBox.critical(self, "Kernel-Build fehlgeschlagen", message)

    def _refresh_dependencies(self) -> None:
        missing = missing_packages()
        if missing:
            self.dep_label.setText("Fehlende Build-Pakete: " + ", ".join(missing))
            self.dep_button.setEnabled(True)
        else:
            self.dep_label.setText("Build-Abhängigkeiten vollständig")
            self.dep_button.setEnabled(False)

    def _open_output(self) -> None:
        output = self.app_dir / "output"
        output.mkdir(exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output)))

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "Build läuft", "Bitte den laufenden Vorgang zuerst abbrechen.")
            event.ignore()
            return
        event.accept()


def run(app_dir: Path) -> int:
    application = QApplication([])
    application.setApplicationName("AILinux Kernel Builder")
    application.setOrganizationName("AILinux")
    window = MainWindow(app_dir)
    window.show()
    return application.exec()
