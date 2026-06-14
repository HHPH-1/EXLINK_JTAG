from __future__ import annotations

from datetime import datetime
import logging
import os
from pathlib import Path
import subprocess
import sys
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, APP_VERSION
from .device_manager import DeviceInfo, discover_devices, select_preferred_device, wait_for_reenumeration
from .logging_setup import log_dir
from .mode_protocol import ExlinkModeClient
from .pulseview_controller import PulseViewError, find_pulseview, launch_pulseview, validate_pulseview_path
from .settings import AppSettings
from .vivado import vivado_xvc_connect_tcl
from .xvc_controller import XvcController, XvcServerConfig


XVC_PRESETS: tuple[tuple[str, int, str, int, int], ...] = (
    ("自定义", 0, "", 0, 0),
    ("稳定 2.5 MHz", 2500, "pio_safe", 8192, 131072),
    ("高速 12.5 MHz", 12500, "pio_safe", 8192, 131072),
    ("保守 1 MHz", 1000, "pio_safe", 4096, 131072),
    ("恢复 500 kHz", 500, "pio_safe", 2048, 131072),
)


class BackgroundWorker(QThread):
    log_line = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, func: Callable[[Callable[[str], None]], Any], parent: QObject | None = None):
        super().__init__(parent)
        self.func = func

    def run(self) -> None:
        try:
            self.succeeded.emit(self.func(self.log_line.emit))
        except Exception as exc:
            self.failed.emit(f"{exc}\n{traceback.format_exc()}")


class LogEmitter(QObject):
    line = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings):
        super().__init__()
        self.settings = settings
        self.devices: list[DeviceInfo] = []
        self.current_device: DeviceInfo | None = None
        self.worker: BackgroundWorker | None = None
        self._last_xvc_error_logged = ""
        self.xvc_log_emitter = LogEmitter()
        self.xvc_log_emitter.line.connect(self.append_log)
        self.xvc = XvcController(self.xvc_log_emitter.line.emit)

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(900, 720)
        self._build_ui()
        self._restore_geometry()
        self._connect_signals()

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh_xvc_status)
        self.status_timer.start(1000)

        self.append_log(f"{APP_NAME} {APP_VERSION} started")
        self.refresh_devices()

    def _build_ui(self) -> None:
        central = QWidget(self)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(10, 10, 10, 6)
        central_layout.setSpacing(6)
        self.setCentralWidget(central)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        scroll_area.setMinimumHeight(0)
        central_layout.addWidget(scroll_area, stretch=1)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        scroll_area.setWidget(content)

        device_box = QGroupBox("设备状态")
        device_layout = QGridLayout(device_box)
        self.device_combo = QComboBox()
        self.manual_port_edit = QLineEdit(self.settings.manual_com_port)
        self.manual_port_edit.setPlaceholderText("COM8")
        self.refresh_button = QPushButton("刷新设备")
        self.reconnect_button = QPushButton("重新连接")
        self.connection_label = QLabel("设备离线")
        self.port_label = QLabel("-")
        self.vidpid_label = QLabel("-")
        self.version_label = QLabel("-")
        self.mode_label = QLabel("未知")
        self.serial_label = QLabel("-")

        device_layout.addWidget(QLabel("设备"), 0, 0)
        device_layout.addWidget(self.device_combo, 0, 1, 1, 3)
        device_layout.addWidget(QLabel("手动 COM"), 1, 0)
        device_layout.addWidget(self.manual_port_edit, 1, 1)
        device_layout.addWidget(self.refresh_button, 1, 2)
        device_layout.addWidget(self.reconnect_button, 1, 3)
        labels = [
            ("连接状态", self.connection_label),
            ("COM 端口", self.port_label),
            ("VID/PID", self.vidpid_label),
            ("固件版本", self.version_label),
            ("当前模式", self.mode_label),
            ("USB serial number", self.serial_label),
        ]
        for row, (name, label) in enumerate(labels, start=2):
            device_layout.addWidget(QLabel(name), row, 0)
            device_layout.addWidget(label, row, 1, 1, 3)
        layout.addWidget(device_box)

        mode_box = QGroupBox("工作模式")
        mode_layout = QHBoxLayout(mode_box)
        self.to_scope_button = QPushButton("切换到示波器模式")
        self.to_jtag_button = QPushButton("切换到 JTAG 下载器")
        self.open_scope_button = QPushButton("切换到示波器并打开 PulseView")
        mode_layout.addWidget(self.to_scope_button)
        mode_layout.addWidget(self.to_jtag_button)
        mode_layout.addWidget(self.open_scope_button)
        layout.addWidget(mode_box)

        xvc_box = QGroupBox("JTAG / XVC")
        xvc_layout = QGridLayout(xvc_box)
        self.xvc_preset_combo = QComboBox()
        for preset in XVC_PRESETS:
            self.xvc_preset_combo.addItem(preset[0], preset)
        self.host_edit = QLineEdit(self.settings.xvc_host)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self.settings.xvc_port)
        self.tck_spin = QSpinBox()
        self.tck_spin.setRange(50, 100000)
        self.tck_spin.setSuffix(" kHz")
        self.tck_spin.setValue(self.settings.tck_khz)
        self.engine_combo = QComboBox()
        self.engine_combo.addItems(["pio_safe", "pio", "pio_fast"])
        self.engine_combo.setCurrentText(self.settings.engine)
        self.dma_spin = QSpinBox()
        self.dma_spin.setRange(2048, 32768)
        self.dma_spin.setSingleStep(2048)
        self.dma_spin.setValue(self.settings.dma_chunk_bits)
        self.max_shift_spin = QSpinBox()
        self.max_shift_spin.setRange(1, 16 * 1024 * 1024)
        self.max_shift_spin.setValue(self.settings.max_shift_bits)
        self.sync_xvc_preset_combo()
        self.start_xvc_button = QPushButton("启动 XVC Server")
        self.stop_xvc_button = QPushButton("停止 XVC Server")
        self.copy_vivado_button = QPushButton("复制 Vivado 连接命令")
        self.xvc_status_label = QLabel("已停止")
        self.client_status_label = QLabel("未连接")
        self.shift_count_label = QLabel("0")
        self.shift_bits_label = QLabel("0")
        self.serial_timeouts_label = QLabel("0")
        self.protocol_errors_label = QLabel("0")
        self.vivado_cmd_edit = QLineEdit()
        self.vivado_cmd_edit.setReadOnly(True)

        form_items = [
            ("稳定预设", self.xvc_preset_combo),
            ("监听地址", self.host_edit),
            ("XVC 端口", self.port_spin),
            ("TCK", self.tck_spin),
            ("Engine", self.engine_combo),
            ("DMA chunk bit", self.dma_spin),
            ("Maximum shift bit", self.max_shift_spin),
            ("XVC Server 状态", self.xvc_status_label),
            ("Vivado 客户端", self.client_status_label),
            ("Shift 请求数", self.shift_count_label),
            ("Total shifted bits", self.shift_bits_label),
            ("Serial timeouts", self.serial_timeouts_label),
            ("Protocol errors", self.protocol_errors_label),
            ("vivado客户端中连接xvc Jtag命令", self.vivado_cmd_edit),
        ]
        for row, (name, widget) in enumerate(form_items):
            xvc_layout.addWidget(QLabel(name), row, 0)
            xvc_layout.addWidget(widget, row, 1, 1, 3)
        xvc_layout.addWidget(self.start_xvc_button, len(form_items), 1)
        xvc_layout.addWidget(self.stop_xvc_button, len(form_items), 2)
        xvc_layout.addWidget(self.copy_vivado_button, len(form_items), 3)
        layout.addWidget(xvc_box)

        scope_box = QGroupBox("示波器")
        scope_layout = QGridLayout(scope_box)
        self.pulseview_path_edit = QLineEdit(self.settings.pulseview_path)
        self.pick_pulseview_button = QPushButton("选择 PulseView 路径")
        self.scope_only_button = QPushButton("仅切换到示波器")
        scope_layout.addWidget(QLabel("PulseView"), 0, 0)
        scope_layout.addWidget(self.pulseview_path_edit, 0, 1)
        scope_layout.addWidget(self.pick_pulseview_button, 0, 2)
        scope_layout.addWidget(self.scope_only_button, 1, 1)
        layout.addWidget(scope_box)

        log_box = QGroupBox("日志")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        log_buttons = QHBoxLayout()
        self.clear_log_button = QPushButton("清空日志")
        self.copy_log_button = QPushButton("复制日志")
        self.open_log_dir_button = QPushButton("打开日志目录")
        log_buttons.addWidget(self.clear_log_button)
        log_buttons.addWidget(self.copy_log_button)
        log_buttons.addWidget(self.open_log_dir_button)
        log_buttons.addStretch(1)
        log_layout.addWidget(self.log_view)
        log_layout.addLayout(log_buttons)
        layout.addWidget(log_box, stretch=1)

        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.addStretch(1)
        self.credit_label = QLabel("By HHPH")
        self.credit_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        footer_layout.addWidget(self.credit_label, 0, Qt.AlignmentFlag.AlignRight)
        central_layout.addLayout(footer_layout)

        self.update_vivado_command()

    def _connect_signals(self) -> None:
        self.refresh_button.clicked.connect(self.refresh_devices)
        self.reconnect_button.clicked.connect(self.refresh_devices)
        self.device_combo.currentIndexChanged.connect(self.device_selected)
        self.to_scope_button.clicked.connect(lambda: self.switch_mode("SCOPE"))
        self.to_jtag_button.clicked.connect(lambda: self.switch_mode("JTAG"))
        self.scope_only_button.clicked.connect(lambda: self.switch_mode("SCOPE"))
        self.open_scope_button.clicked.connect(lambda: self.switch_mode("SCOPE", open_pulseview=True))
        self.pick_pulseview_button.clicked.connect(self.pick_pulseview)
        self.start_xvc_button.clicked.connect(self.start_xvc)
        self.stop_xvc_button.clicked.connect(self.stop_xvc)
        self.copy_vivado_button.clicked.connect(self.copy_vivado_address)
        self.clear_log_button.clicked.connect(self.log_view.clear)
        self.copy_log_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.log_view.toPlainText()))
        self.open_log_dir_button.clicked.connect(lambda: os.startfile(log_dir()))
        self.xvc_preset_combo.currentIndexChanged.connect(self.apply_xvc_preset)
        self.tck_spin.valueChanged.connect(self.sync_xvc_preset_combo)
        self.engine_combo.currentTextChanged.connect(self.sync_xvc_preset_combo)
        self.dma_spin.valueChanged.connect(self.sync_xvc_preset_combo)
        self.max_shift_spin.valueChanged.connect(self.sync_xvc_preset_combo)
        self.host_edit.textChanged.connect(self.update_vivado_command)
        self.port_spin.valueChanged.connect(self.update_vivado_command)

    def _restore_geometry(self) -> None:
        if self.settings.window_geometry_hex:
            self.restoreGeometry(bytes.fromhex(self.settings.window_geometry_hex))

    def closeEvent(self, event) -> None:
        self.save_settings()
        self.xvc.stop(timeout=3.0)
        super().closeEvent(event)

    def append_log(self, line: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {line}")
        logging.getLogger(__name__).info(line)

    def run_task(self, title: str, func: Callable[[Callable[[str], None]], Any], on_success: Callable[[Any], None]) -> None:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, title, "已有后台任务正在运行")
            return
        self.set_controls_enabled(False)
        self.append_log(title)
        self.worker = BackgroundWorker(func, self)
        self.worker.log_line.connect(self.append_log)
        self.worker.succeeded.connect(lambda result: self._task_succeeded(result, on_success))
        self.worker.failed.connect(self._task_failed)
        self.worker.finished.connect(lambda: self.set_controls_enabled(True))
        self.worker.start()

    @Slot(object)
    def _task_succeeded(self, result: object, on_success: Callable[[Any], None]) -> None:
        try:
            on_success(result)
        except Exception as exc:
            self._task_failed(f"{exc}\n{traceback.format_exc()}")

    @Slot(str)
    def _task_failed(self, message: str) -> None:
        first_line = message.splitlines()[0] if message else "unknown error"
        self.append_log(f"ERROR: {first_line}")
        QMessageBox.critical(self, "ExlinkManager", first_line)

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in [
            self.refresh_button,
            self.reconnect_button,
            self.to_scope_button,
            self.to_jtag_button,
            self.open_scope_button,
            self.scope_only_button,
            self.start_xvc_button,
            self.stop_xvc_button,
            self.pick_pulseview_button,
        ]:
            widget.setEnabled(enabled)

    def refresh_devices(self) -> None:
        manual_port = self.manual_port_edit.text().strip()
        preferred = self.settings.recent_serial_number

        def work(log: Callable[[str], None]) -> list[DeviceInfo]:
            log("Scanning serial ports")
            return discover_devices(manual_port=manual_port, preferred_serial=preferred, query=True)

        self.run_task("刷新设备", work, self.update_devices)

    def update_devices(self, devices: list[DeviceInfo]) -> None:
        self.devices = devices
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for device in devices:
            self.device_combo.addItem(device.display_name, device)
        self.device_combo.blockSignals(False)
        self.current_device = select_preferred_device(devices, self.settings.recent_serial_number, self.manual_port_edit.text().strip())
        if self.current_device is not None:
            index = devices.index(self.current_device)
            self.device_combo.setCurrentIndex(index)
            self.apply_device(self.current_device)
        else:
            self.apply_device(None)
        self.append_log(f"Found {len(devices)} Exlink candidate device(s)")

    def device_selected(self, index: int) -> None:
        if 0 <= index < len(self.devices):
            self.current_device = self.devices[index]
            self.apply_device(self.current_device)

    def apply_device(self, device: DeviceInfo | None) -> None:
        if device is None:
            self.connection_label.setText("设备离线")
            self.port_label.setText("-")
            self.vidpid_label.setText("-")
            self.version_label.setText("-")
            self.mode_label.setText("未知")
            self.serial_label.setText("-")
            return
        self.connection_label.setText("已连接" if not device.protocol_error else f"协议不可用: {device.protocol_error}")
        self.port_label.setText(device.port)
        self.vidpid_label.setText(device.vid_pid or "-")
        self.version_label.setText(device.firmware_version or "-")
        self.mode_label.setText(self.mode_text(device.mode))
        self.serial_label.setText(device.serial_number or "-")
        if device.serial_number:
            self.settings.recent_serial_number = device.serial_number

    @staticmethod
    def mode_text(mode: str) -> str:
        return {"SCOPE": "示波器模式", "JTAG": "JTAG 模式"}.get(mode, mode or "未知")

    def apply_xvc_preset(self, index: int) -> None:
        preset = self.xvc_preset_combo.itemData(index)
        if not preset or index == 0:
            return
        _name, tck_khz, engine, dma_chunk_bits, max_shift_bits = preset
        self.tck_spin.setValue(tck_khz)
        self.engine_combo.setCurrentText(engine)
        self.dma_spin.setValue(dma_chunk_bits)
        self.max_shift_spin.setValue(max_shift_bits)

    def sync_xvc_preset_combo(self) -> None:
        current = (
            self.tck_spin.value(),
            self.engine_combo.currentText(),
            self.dma_spin.value(),
            self.max_shift_spin.value(),
        )
        matched_index = 0
        for index in range(1, self.xvc_preset_combo.count()):
            _name, tck_khz, engine, dma_chunk_bits, max_shift_bits = self.xvc_preset_combo.itemData(index)
            if current == (tck_khz, engine, dma_chunk_bits, max_shift_bits):
                matched_index = index
                break
        self.xvc_preset_combo.blockSignals(True)
        self.xvc_preset_combo.setCurrentIndex(matched_index)
        self.xvc_preset_combo.blockSignals(False)

    def switch_mode(self, target_mode: str, open_pulseview: bool = False) -> None:
        device = self.current_device
        if device is None:
            QMessageBox.warning(self, "ExlinkManager", "未检测到 Exlink 设备")
            return
        target = target_mode.upper()
        if target == "SCOPE" and self.xvc.running:
            self.append_log("Stopping XVC before switching to SCOPE")
            self.xvc.stop()

        def work(log: Callable[[str], None]) -> DeviceInfo:
            log(f"Switching {device.port} to {target}")
            with ExlinkModeClient(device.port, timeout=1.5) as client:
                current = client.mode()
                log(f"Current mode: {current}")
                if current == target:
                    return device
                client.switch_mode(target)
            return wait_for_reenumeration(
                preferred_serial=device.serial_number,
                previous_port=device.port,
                desired_mode=target,
                timeout_s=15.0,
                log=log,
            )

        def done(new_device: DeviceInfo) -> None:
            self.current_device = new_device
            self.update_devices([new_device])
            if open_pulseview:
                self.launch_pulseview_from_ui()

        self.run_task(f"切换到 {self.mode_text(target)}", work, done)

    def xvc_config(self) -> XvcServerConfig:
        device = self.current_device
        if device is None:
            raise RuntimeError("No Exlink device selected")
        return XvcServerConfig(
            serial_port=device.port,
            listen_host=self.host_edit.text().strip() or "127.0.0.1",
            listen_port=self.port_spin.value(),
            tck_khz=self.tck_spin.value(),
            engine=self.engine_combo.currentText(),
            dma_chunk_bits=self.dma_spin.value(),
            max_shift_bits=self.max_shift_spin.value(),
        )

    def start_xvc(self) -> None:
        device = self.current_device
        if device is None:
            QMessageBox.warning(self, "ExlinkManager", "未检测到 Exlink 设备")
            return
        listen_host = self.host_edit.text().strip() or "127.0.0.1"
        listen_port = self.port_spin.value()
        tck_khz = self.tck_spin.value()
        engine = self.engine_combo.currentText()
        dma_chunk_bits = self.dma_spin.value()
        max_shift_bits = self.max_shift_spin.value()

        def work(log: Callable[[str], None]) -> DeviceInfo:
            selected = device
            with ExlinkModeClient(selected.port, timeout=1.5) as client:
                current = client.mode()
                log(f"Current mode: {current}")
                if current != "JTAG":
                    client.switch_mode("JTAG")
                    selected = wait_for_reenumeration(
                        preferred_serial=selected.serial_number,
                        previous_port=selected.port,
                        desired_mode="JTAG",
                        timeout_s=15.0,
                        log=log,
                    )
            log("Starting XVC Server")
            self.xvc.start(
                XvcServerConfig(
                    serial_port=selected.port,
                    listen_host=listen_host,
                    listen_port=listen_port,
                    tck_khz=tck_khz,
                    engine=engine,
                    dma_chunk_bits=dma_chunk_bits,
                    max_shift_bits=max_shift_bits,
                )
            )
            log(f"XVC Server listening on {listen_host}:{listen_port}")
            return selected

        def done(jtag_device: DeviceInfo) -> None:
            self.current_device = jtag_device
            self.update_devices([jtag_device])
            self.save_settings()
            self.refresh_xvc_status()

        self.run_task("启动 XVC Server", work, done)

    def stop_xvc(self) -> None:
        self.append_log("Stopping XVC Server")
        try:
            self.xvc.stop()
        except Exception as exc:
            self._task_failed(f"{exc}\n{traceback.format_exc()}")
        finally:
            self.refresh_xvc_status()

    def refresh_xvc_status(self) -> None:
        status = self.xvc.status()
        if status.listening:
            self.xvc_status_label.setText("监听中")
        elif status.running:
            self.xvc_status_label.setText("启动中")
        else:
            self.xvc_status_label.setText("已停止")
        self.client_status_label.setText("已连接" if status.client_connected else "未连接")
        self.shift_count_label.setText(str(status.shift_request_count))
        self.shift_bits_label.setText(str(status.total_shifted_bits))
        self.serial_timeouts_label.setText(str(status.serial_timeout_count))
        self.protocol_errors_label.setText(str(status.protocol_error_count))
        if status.last_error and status.last_error != self._last_xvc_error_logged:
            self._last_xvc_error_logged = status.last_error
            self.append_log(f"XVC error: {status.last_error}")

    def update_vivado_command(self) -> None:
        self.vivado_cmd_edit.setText(vivado_xvc_connect_tcl(self.host_edit.text(), self.port_spin.value()))

    def copy_vivado_address(self) -> None:
        command = vivado_xvc_connect_tcl(self.host_edit.text(), self.port_spin.value())
        QGuiApplication.clipboard().setText(command)
        self.append_log(f"Copied Vivado XVC command: {command}")

    def pick_pulseview(self) -> None:
        start = str(Path(self.pulseview_path_edit.text()).parent) if self.pulseview_path_edit.text() else ""
        path, _ = QFileDialog.getOpenFileName(self, "选择 PulseView", start, "PulseView (pulseview.exe);;EXE (*.exe)")
        if path:
            try:
                self.pulseview_path_edit.setText(str(validate_pulseview_path(path)))
                self.save_settings()
            except PulseViewError as exc:
                QMessageBox.warning(self, "PulseView", str(exc))

    def launch_pulseview_from_ui(self) -> None:
        configured = self.pulseview_path_edit.text().strip()
        path = find_pulseview(configured)
        if path is None:
            QMessageBox.warning(self, "PulseView", "未找到 PulseView，请选择 pulseview.exe 路径")
            return
        self.pulseview_path_edit.setText(str(path))
        self.save_settings()
        launch_pulseview(str(path))
        self.append_log(f"PulseView launched: {path}")

    def save_settings(self) -> None:
        self.settings.manual_com_port = self.manual_port_edit.text().strip()
        self.settings.xvc_host = self.host_edit.text().strip() or "127.0.0.1"
        self.settings.xvc_port = self.port_spin.value()
        self.settings.tck_khz = self.tck_spin.value()
        self.settings.engine = self.engine_combo.currentText()
        self.settings.dma_chunk_bits = self.dma_spin.value()
        self.settings.max_shift_bits = self.max_shift_spin.value()
        self.settings.pulseview_path = self.pulseview_path_edit.text().strip()
        self.settings.window_geometry_hex = bytes(self.saveGeometry()).hex()
        path = self.settings.save()
        self.append_log(f"Settings saved: {path}")


def run() -> int:
    app = QApplication(sys.argv)
    settings = AppSettings.load()
    window = MainWindow(settings)
    window.show()
    return app.exec()
