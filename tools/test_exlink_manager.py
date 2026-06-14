#!/usr/bin/env python3

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.exlink_manager.device_manager import DeviceInfo, select_preferred_device
from tools.exlink_manager.mode_protocol import (
    ModeBusyError,
    ModeProtocolError,
    parse_info_response,
    parse_mode_response,
)
from tools.exlink_manager.pulseview_controller import PulseViewError, validate_pulseview_path
from tools.exlink_manager.settings import AppSettings
from tools.exlink_manager.vivado import vivado_xvc_connect_tcl
from tools.exlink_manager import xvc_controller
import exlink_xvc_server


class ExlinkManagerUnitTests(unittest.TestCase):
    def test_mode_response_parse(self) -> None:
        self.assertEqual(parse_mode_response("@EXLINK:OK:MODE:SCOPE\n"), "SCOPE")
        self.assertEqual(parse_mode_response("@EXLINK:OK:SWITCHING:JTAG\n"), "JTAG")
        with self.assertRaises(ModeBusyError):
            parse_mode_response("@EXLINK:ERR:BUSY\n")
        with self.assertRaises(ModeProtocolError):
            parse_mode_response("sigrok-pico")

    def test_info_response_parse(self) -> None:
        info = parse_info_response("@EXLINK:OK:INFO:EXLINK-RP2040-MULTI v0.1:SCOPE\n")
        self.assertEqual(info.version, "EXLINK-RP2040-MULTI v0.1")
        self.assertEqual(info.mode, "SCOPE")

    def test_settings_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            settings = AppSettings(manual_com_port="COM9", xvc_port=2543, pulseview_path="C:/pv/pulseview.exe")
            settings.save(path)
            loaded = AppSettings.load(path)
            self.assertEqual(loaded.manual_com_port, "COM9")
            self.assertEqual(loaded.xvc_port, 2543)
            self.assertEqual(loaded.pulseview_path, "C:/pv/pulseview.exe")

    def test_device_preference(self) -> None:
        devices = [
            DeviceInfo(port="COM3", serial_number="A"),
            DeviceInfo(port="COM4", serial_number="B"),
        ]
        self.assertEqual(select_preferred_device(devices, preferred_serial="B").port, "COM4")
        self.assertEqual(select_preferred_device(devices, manual_port="COM3").serial_number, "A")

    def test_pulseview_path_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "pulseview.exe"
            exe.write_text("", encoding="utf-8")
            self.assertEqual(validate_pulseview_path(str(exe)), exe)
            with self.assertRaises(PulseViewError):
                validate_pulseview_path(str(Path(tmp) / "notepad.exe"))

    def test_vivado_connect_command_cleans_stale_target_and_uses_current_port(self) -> None:
        command = vivado_xvc_connect_tcl("127.0.0.1", 2543)
        self.assertIn("catch {close_hw_target}", command)
        self.assertIn("catch {disconnect_hw_server}", command)
        self.assertIn("connect_hw_server -allow_non_jtag", command)
        self.assertIn("open_hw_target -xvc_url localhost:2543", command)
        self.assertNotIn("2542", command)

    def test_xvc_namespace_defaults(self) -> None:
        config = exlink_xvc_server.XvcServerConfig(serial_port="COM8")
        args = exlink_xvc_server.namespace_from_config(config)
        self.assertEqual(args.default_tck_khz, 12500)
        self.assertEqual(args.force_tck_khz, 12500)
        self.assertEqual(args.engine, "pio_safe")
        self.assertEqual(args.dma_chunk_bits, 8192)
        self.assertEqual(args.max_logical_shift_bits, exlink_xvc_server.DEFAULT_LOGICAL_SHIFT_LIMIT_BITS)

    def test_xvc_namespace_promotes_legacy_gui_shift_limit(self) -> None:
        config = exlink_xvc_server.XvcServerConfig(serial_port="COM8", max_shift_bits=131072)
        args = exlink_xvc_server.namespace_from_config(config)
        self.assertGreaterEqual(args.max_logical_shift_bits, 523476)

    def test_xvc_server_start_stop_restart_with_stubbed_serve(self) -> None:
        calls = []

        def fake_serve(args, stop_event=None):
            calls.append(args.port)
            while stop_event is not None and not stop_event.is_set():
                time.sleep(0.01)

        with mock.patch.object(exlink_xvc_server, "serve", fake_serve):
            server = exlink_xvc_server.ExlinkXvcServer(
                exlink_xvc_server.XvcServerConfig(serial_port="COM8")
            )
            server.start()
            time.sleep(0.05)
            self.assertTrue(server.get_status().running)
            server.stop()
            server.wait(1.0)
            self.assertFalse(server.get_status().running)
            server.start()
            time.sleep(0.05)
            server.stop()
            server.wait(1.0)
            self.assertEqual(calls, ["COM8", "COM8"])

    def test_xvc_server_log_callback_does_not_redirect_process_streams(self) -> None:
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        seen_streams = []

        def fake_serve(_args, stop_event=None):
            seen_streams.append((sys.stdout, sys.stderr))
            while stop_event is not None and not stop_event.is_set():
                time.sleep(0.01)

        with mock.patch.object(exlink_xvc_server, "serve", fake_serve):
            server = exlink_xvc_server.ExlinkXvcServer(
                exlink_xvc_server.XvcServerConfig(serial_port="COM8"),
                log_callback=lambda _line: None,
            )
            server.start()
            time.sleep(0.05)
            server.stop()
            server.wait(1.0)

        self.assertEqual(sys.stdout, original_stdout)
        self.assertEqual(sys.stderr, original_stderr)
        self.assertEqual(seen_streams, [(original_stdout, original_stderr)])

    def test_xvc_controller_waits_for_listening(self) -> None:
        class FakeServer:
            def __init__(self, _config, _log_callback=None):
                self.status = exlink_xvc_server.XvcStatus()

            def start(self):
                self.status.running = True
                self.status.listening = True

            def get_status(self):
                return self.status

        with mock.patch.object(xvc_controller, "ExlinkXvcServer", FakeServer):
            controller = xvc_controller.XvcController()
            controller.start(exlink_xvc_server.XvcServerConfig(serial_port="COM8"), ready_timeout=0.2)
            self.assertTrue(controller.status().listening)

    def test_xvc_controller_reports_startup_timeout(self) -> None:
        class FakeServer:
            def __init__(self, _config, _log_callback=None):
                self.status = exlink_xvc_server.XvcStatus(running=True)

            def start(self):
                self.status.running = True

            def get_status(self):
                return self.status

        with mock.patch.object(xvc_controller, "ExlinkXvcServer", FakeServer):
            controller = xvc_controller.XvcController()
            with self.assertRaises(TimeoutError):
                controller.start(exlink_xvc_server.XvcServerConfig(serial_port="COM8"), ready_timeout=0.01)

    def test_pyinstaller_hidden_imports_present(self) -> None:
        spec = Path(__file__).with_name("exlink_manager") / "ExlinkManager.spec"
        text = spec.read_text(encoding="utf-8")
        self.assertIn("serial.tools.list_ports", text)
        self.assertIn("PySide6.QtWidgets", text)
        self.assertIn("exlink_xvc_server", text)


if __name__ == "__main__":
    unittest.main()
