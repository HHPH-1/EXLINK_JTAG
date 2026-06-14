from __future__ import annotations

from dataclasses import dataclass
import time

try:
    import serial
except ImportError as exc:  # pragma: no cover - dependency hint
    serial = None
    _SERIAL_IMPORT_ERROR = exc
else:
    _SERIAL_IMPORT_ERROR = None


CONTROL_PREFIX = "@EXLINK:"
VALID_MODES = {"SCOPE", "JTAG"}


class ModeProtocolError(RuntimeError):
    pass


class ModeBusyError(ModeProtocolError):
    pass


@dataclass
class ModeInfo:
    version: str = ""
    mode: str = "UNKNOWN"
    raw_info: str = ""


def _require_serial() -> None:
    if serial is None:
        raise ModeProtocolError("pyserial is required") from _SERIAL_IMPORT_ERROR


def parse_mode_response(line: str) -> str:
    text = line.strip()
    if text in ("@EXLINK:OK:MODE:SCOPE", "@EXLINK:OK:SWITCHING:SCOPE"):
        return "SCOPE"
    if text in ("@EXLINK:OK:MODE:JTAG", "@EXLINK:OK:SWITCHING:JTAG"):
        return "JTAG"
    if text == "@EXLINK:ERR:BUSY":
        raise ModeBusyError("device is busy")
    if text.startswith("@EXLINK:ERR:"):
        raise ModeProtocolError(text)
    raise ModeProtocolError(f"unexpected response: {text!r}")


def parse_info_response(line: str) -> ModeInfo:
    text = line.strip()
    prefix = "@EXLINK:OK:INFO:"
    if not text.startswith(prefix):
        raise ModeProtocolError(f"unexpected INFO response: {text!r}")
    payload = text[len(prefix):]
    parts = payload.split(":")
    if len(parts) < 2:
        raise ModeProtocolError(f"incomplete INFO response: {text!r}")
    mode = parts[-1].upper()
    version = ":".join(parts[:-1])
    if mode not in VALID_MODES:
        mode = "UNKNOWN"
    return ModeInfo(version=version, mode=mode, raw_info=text)


class ExlinkModeClient:
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        _require_serial()
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial = None

    def __enter__(self) -> "ExlinkModeClient":
        self.open()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def open(self) -> None:
        if self._serial is None:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
            time.sleep(0.05)
            self._serial.reset_input_buffer()

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def command(self, command: str) -> str:
        if not command.startswith(CONTROL_PREFIX):
            raise ValueError("management command must start with @EXLINK:")
        if not command.endswith("\n"):
            command += "\n"
        self.open()
        assert self._serial is not None
        self._serial.write(command.encode("ascii"))
        self._serial.flush()
        raw = self._serial.readline()
        if not raw:
            raise ModeProtocolError("no response from device")
        return raw.decode("ascii", errors="replace").strip()

    def info(self) -> ModeInfo:
        return parse_info_response(self.command("@EXLINK:INFO\n"))

    def mode(self) -> str:
        return parse_mode_response(self.command("@EXLINK:MODE?\n"))

    def caps(self) -> list[str]:
        line = self.command("@EXLINK:CAPS?\n")
        prefix = "@EXLINK:OK:CAPS:"
        if not line.startswith(prefix):
            raise ModeProtocolError(f"unexpected CAPS response: {line!r}")
        return [value for value in line[len(prefix):].split(",") if value]

    def switch_mode(self, target_mode: str) -> str:
        target = target_mode.upper()
        if target not in VALID_MODES:
            raise ValueError(f"invalid target mode {target_mode!r}")
        return parse_mode_response(self.command(f"@EXLINK:MODE:{target}\n"))
