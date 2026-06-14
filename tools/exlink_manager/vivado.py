from __future__ import annotations


def vivado_xvc_address(host: str, port: int) -> str:
    clean_host = host.strip() or "127.0.0.1"
    if clean_host == "127.0.0.1":
        clean_host = "localhost"
    return f"{clean_host}:{port}"


def vivado_xvc_connect_tcl(host: str, port: int) -> str:
    address = vivado_xvc_address(host, port)
    return (
        "catch {close_hw_target}; "
        "catch {disconnect_hw_server}; "
        "connect_hw_server -allow_non_jtag; "
        f"open_hw_target -xvc_url {address}"
    )
