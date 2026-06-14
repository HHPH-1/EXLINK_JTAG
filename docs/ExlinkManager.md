# ExlinkManager

`ExlinkManager.exe` manages the Exlink RP2040 multifunction firmware on Windows.
It switches SCOPE/JTAG modes, starts the built-in XVC server, and launches
PulseView. It does not bundle Vivado or PulseView.

## XVC Presets

Use the preset selector instead of hand-editing timing fields during normal use:

```text
Stable 2.5 MHz:       TCK 2500 kHz,  engine pio_safe, DMA chunk 8192, maximum shift 131072
High speed 12.5 MHz:  TCK 12500 kHz, engine pio_safe, DMA chunk 8192, maximum shift 131072
Conservative 1 MHz:   TCK 1000 kHz,  engine pio_safe, DMA chunk 4096, maximum shift 131072
Recovery 500 kHz:     TCK 500 kHz,   engine pio_safe, DMA chunk 2048, maximum shift 131072
```

`Maximum shift` is the firmware request chunk size. Vivado may send larger XVC
logical shifts; the PC server splits them automatically.

## Vivado Connection

After clicking **Start XVC Server**, wait until the XVC status shows `监听中`.
Then use the command shown in the GUI. Do not keep using a hard-coded
`localhost:2542` if the GUI is listening on another port such as `2543`.

The copied command has cleanup steps:

```tcl
catch {close_hw_target}
catch {disconnect_hw_server}
connect_hw_server -allow_non_jtag
open_hw_target -xvc_url <host>:<port>
```

If Vivado reports that `jsn-XVC-...` may be locked by another `hw_server`, close
old Vivado Hardware Manager sessions, run the copied cleanup command again, and
make sure the `<host>:<port>` matches the GUI.

## Logs And Settings

Settings are saved to:

```text
%APPDATA%\ExlinkManager\settings.json
```

Logs are saved to:

```text
%APPDATA%\ExlinkManager\logs
```
