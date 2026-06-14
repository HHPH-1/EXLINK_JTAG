from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable

try:
    from serial.tools import list_ports
except ImportError as exc:  # pragma: no cover - dependency hint
    list_ports = None
    _LIST_PORTS_IMPORT_ERROR = exc
else:
    _LIST_PORTS_IMPORT_ERROR = None

from .mode_protocol import ExlinkModeClient, ModeInfo, ModeProtocolError


EXLINK_VID = 0x2E8A
EXLINK_PID = 0x000A


@dataclass
class DeviceInfo:
    port: str
    description: str = ""
    hwid: str = ""
    vid: int | None = None
    pid: int | None = None
    serial_number: str = ""
    manufacturer: str = ""
    product: str = ""
    mode: str = "UNKNOWN"
    firmware_version: str = ""
    protocol_error: str = ""

    @property
    def vid_pid(self) -> str:
        if self.vid is None or self.pid is None:
            return ""
        return f"{self.vid:04X}:{self.pid:04X}"

    @property
    def display_name(self) -> str:
        serial_text = f" SN={self.serial_number}" if self.serial_number else ""
        mode_text = f" {self.mode}" if self.mode != "UNKNOWN" else ""
        return f"{self.port} {self.vid_pid}{mode_text}{serial_text}".strip()


def _require_list_ports() -> None:
    if list_ports is None:
        raise RuntimeError("pyserial is required") from _LIST_PORTS_IMPORT_ERROR


def _from_port_info(port_info) -> DeviceInfo:
    return DeviceInfo(
        port=port_info.device,
        description=port_info.description or "",
        hwid=port_info.hwid or "",
        vid=port_info.vid,
        pid=port_info.pid,
        serial_number=port_info.serial_number or "",
        manufacturer=port_info.manufacturer or "",
        product=port_info.product or "",
    )


def list_candidate_devices(include_all: bool = False) -> list[DeviceInfo]:
    _require_list_ports()
    devices: list[DeviceInfo] = []
    for port_info in list_ports.comports():
        is_exlink = port_info.vid == EXLINK_VID and port_info.pid == EXLINK_PID
        if include_all or is_exlink:
            devices.append(_from_port_info(port_info))
    return devices


def query_device(device: DeviceInfo, timeout: float = 1.0) -> DeviceInfo:
    try:
        with ExlinkModeClient(device.port, timeout=timeout) as client:
            info: ModeInfo = client.info()
            device.mode = info.mode
            device.firmware_version = info.version
            device.protocol_error = ""
    except Exception as exc:
        device.protocol_error = str(exc)
    return device


def discover_devices(
    manual_port: str = "",
    preferred_serial: str = "",
    query: bool = True,
    include_all: bool = False,
) -> list[DeviceInfo]:
    devices = list_candidate_devices(include_all=include_all)
    if manual_port and all(d.port.upper() != manual_port.upper() for d in devices):
        devices.insert(0, DeviceInfo(port=manual_port, description="Manual COM port"))

    if preferred_serial:
        devices.sort(key=lambda d: d.serial_number != preferred_serial)

    if query:
        for device in devices:
            query_device(device)
    return devices


def select_preferred_device(devices: list[DeviceInfo], preferred_serial: str = "", manual_port: str = "") -> DeviceInfo | None:
    if not devices:
        return None
    if manual_port:
        for device in devices:
            if device.port.upper() == manual_port.upper():
                return device
    if preferred_serial:
        for device in devices:
            if device.serial_number == preferred_serial:
                return device
    return devices[0]


def wait_for_reenumeration(
    preferred_serial: str = "",
    previous_port: str = "",
    desired_mode: str = "",
    timeout_s: float = 15.0,
    log: Callable[[str], None] | None = None,
) -> DeviceInfo:
    deadline = time.monotonic() + timeout_s
    saw_disappear = False
    last_error = "device not found"
    desired = desired_mode.upper()

    while time.monotonic() < deadline:
        devices = discover_devices(preferred_serial=preferred_serial, query=True)
        if previous_port and all(d.port.upper() != previous_port.upper() for d in devices):
            if not saw_disappear and log:
                log("USB device disappeared during mode switch")
            saw_disappear = True

        candidate = select_preferred_device(devices, preferred_serial=preferred_serial)
        if candidate is not None:
            if not desired or candidate.mode == desired:
                if log:
                    log(f"Device re-enumerated on {candidate.port} mode={candidate.mode}")
                return candidate
            last_error = candidate.protocol_error or f"mode is {candidate.mode}, waiting for {desired}"
        time.sleep(0.3)

    raise TimeoutError(f"timed out waiting for Exlink device: {last_error}")
