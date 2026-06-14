# Exlink RP2040 多功能固件与管理工具

这个仓库基于 `sigrok-pico`，将 Exlink RP2040 板同时适配为：

- 逻辑分析仪 / 简易示波器固件，可配合 PulseView 使用；
- JTAG 下载器固件，可通过 XVC Server 接入 Vivado Hardware Manager；
- Windows 图形管理软件 `ExlinkManager.exe`，用于自动检测设备、切换模式、启动 XVC Server 和打开 PulseView。

当前推荐使用多功能固件 `exlink_multifunction.uf2`。该固件每次启动只进入一个模式，默认进入 SCOPE 模式；需要 JTAG 时由 PC 端管理软件发送控制命令并重启切换，不把模式状态写入 Flash。

## 主要目录

```text
pico_sdk_sigrok/                 RP2040 固件源码
pico_sdk_sigrok/multifunction/   Exlink 多功能启动与模式切换代码
tools/exlink_manager/            Windows 图形管理软件源码与打包脚本
tools/exlink_xvc_server.py       Exlink XVC Server
tools/exlink_jtag_test.py        JTAG bridge 调试/测试客户端
docs/                            Exlink 固件、管理器和 JTAG 阶段文档
pulseview/                       PulseView 相关历史说明
```

## 固件功能

### SCOPE 模式

SCOPE 模式继承 `sigrok-pico` 的逻辑分析仪 / 示波器功能。Windows 上建议安装 sigrok Nightly 版本，因为旧版 PulseView 0.4.2 和 sigrok-cli 0.7.2 不支持 `sigrok-pico`。

PulseView 下载页：

```text
https://sigrok.org/wiki/Downloads
```

### JTAG 模式

JTAG 模式将 Exlink RP2040 作为 Xilinx Virtual Cable 设备使用。PC 端运行 XVC Server 后，Vivado 可以通过 `open_hw_target -xvc_url <host>:<port>` 连接目标板。

常用 XVC 预设：

```text
稳定 2.5 MHz:   TCK 2500 kHz,  engine pio_safe, DMA chunk 8192
高速 12.5 MHz:  TCK 12500 kHz, engine pio_safe, DMA chunk 8192
保守 1 MHz:     TCK 1000 kHz,  engine pio_safe, DMA chunk 4096
恢复 500 kHz:   TCK 500 kHz,   engine pio_safe, DMA chunk 2048
```

Vivado 连接前请确认 GUI 中显示的监听端口，不要固定写死 `localhost:2542`。

## Windows 管理软件

`ExlinkManager.exe` 用于日常操作：

1. 自动检测 Exlink 串口设备；
2. 查询当前固件模式；
3. 在 SCOPE / JTAG 模式之间切换；
4. 启动和停止内置 XVC Server；
5. 复制 Vivado 连接命令；
6. 打开 PulseView；
7. 保存设置和日志。

设置与日志位置：

```text
%APPDATA%\ExlinkManager\settings.json
%APPDATA%\ExlinkManager\logs
```

更多说明见：

```text
docs/ExlinkManager.md
docs/MultifunctionFirmware.md
```

## 构建多功能固件

在仓库外层工作区执行：

```powershell
cmake -S .\sigrok-pico\pico_sdk_sigrok -B .\sigrok-pico\build\exlink-multifunction -G Ninja -DSIGROK_BOARD_EXLINK=ON -DEXLINK_BUILD_JTAG_BRIDGE=ON
cmake --build .\sigrok-pico\build\exlink-multifunction --target pico_sdk_sigrok exlink_jtag_bridge exlink_jtag_maxspeed exlink_multifunction -j 8
```

生成文件：

```text
sigrok-pico/build/exlink-multifunction/exlink_multifunction.uf2
```

刷写方式：

1. 按住 BOOTSEL 连接 Exlink RP2040；
2. 将 `exlink_multifunction.uf2` 拖入 RP2040 的 U 盘；
3. 设备重启后默认进入 SCOPE 模式；
4. 后续用 `ExlinkManager.exe` 切换到 JTAG 或回到 SCOPE。

## 构建 Windows 管理软件

在 `sigrok-pico` 目录下执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\exlink_manager\build_exlink_manager.ps1
```

如果依赖已经安装好，可以跳过安装步骤：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\exlink_manager\build_exlink_manager.ps1 -SkipInstall
```

生成文件：

```text
dist/ExlinkManager.exe
```

## 测试

管理软件和 XVC 控制层的单元测试：

```powershell
python .\tools\test_exlink_manager.py
```

Python 语法检查：

```powershell
python -m py_compile .\tools\exlink_manager\app.py .\tools\exlink_xvc_server.py .\tools\test_exlink_manager.py
```

硬件相关功能仍需在真实 Exlink 和目标板上验证，包括 PulseView 采样、Vivado 下载、长时间 XVC 连接和反复模式切换。

## 接线注意

Exlink RP2040 的 JTAG 输出为固定 3.3 V。目标 JTAG Bank 必须兼容 3.3 V。默认只连接：

```text
TCK
TMS
TDI
TDO
GND
```

不要默认把 Exlink 的 3V3 接到目标板 VREF 或 3V3。只有在明确需要由 Exlink 给目标供电，并确认电源设计允许时，才连接 3V3。

## 项目来源

原始 `sigrok-pico` 项目用于把 Raspberry Pi Pico / RP2040 作为 sigrok 逻辑分析仪和示波器使用。上游 libsigrok PR 已合入主线：

```text
https://github.com/sigrokproject/libsigrok/pull/181
```

原始入门文档：

```text
GettingStarted.md
AnalyzerDetails.md
SerialProtocol.md
PICOBuildNotes.md
SigrokBuildNotes.md
```
