# Exlink RP2040 JTAG Bridge Stage 5.6 高速下载记录

## Stage 5.5 基线

- Vivado 连续 5 次下载：43, 42, 42, 42, 43 s
- 平均下载时间：42.4 s
- 真实固件有效速率：约 792051 bit/s
- 32768-bit loopback stress：约 729395 bit/s
- DMA timeouts：0
- PIO recoveries：0
- Firmware 最大 Shift：32768 bit
- DMA chunk：2048 bit
- TinyUSB CDC RX/TX：Pico SDK 默认 256/256 bytes

## 修改前分析

- 当前 firmware 最大 Shift RAM：`tms_buffer`、`tdi_buffer`、`tdo_buffer` 各 4096 bytes，共 12288 bytes。
- 最大 Shift 改为 65536 bit：三个 packed buffer 共 24576 bytes。
- 最大 Shift 改为 131072 bit：三个 packed buffer 共 49152 bytes。
- DMA chunk 2048 bit staging：TX 2052 bytes，RX 260 bytes，共 2312 bytes。
- DMA chunk 4096 bit staging：TX 4100 bytes，RX 516 bytes，共 4616 bytes。
- DMA chunk 8192 bit staging：TX 8196 bytes，RX 1028 bytes，共 9224 bytes。
- 131019-bit XVC Shift 在 Stage 5.5 会拆成 4 个 firmware 请求；Stage 5.6 默认 131072 bit 后为 1 个 firmware 请求。
- Stage 5.5 每个 DMA chunk 重复执行 abort-if-busy、disable SM、clear FIFO、restart SM、clear IRQ、drive idle、创建 RX/TX DMA config、配置 DMA、enable SM、等待、disable SM、drive idle。
- Stage 5.5 TX 展开逐 bit 调用 `get_packed_bit()` 两次并逐 bit OR 到 TX word；RX 打包逐 bit 调用 `set_packed_bit()`。
- Stage 5.5 兼容构建链接结果：`.text` 35372 bytes，`.rodata` 2508 bytes，`.data` 4096 bytes，`.bss` 23280 bytes，主 RAM 已用 27568 bytes，主 RAM 余量 234576 bytes，heap 2048 bytes，stack 2048 bytes。

## 实施的五项优化

1. TX/RX 快速打包
   - TX 使用 Flash/rodata 中的 16-entry TMS/TDI LUT，每次处理 4 个 JTAG bit。
   - 非字节对齐 source offset 自动回退旧逐 bit reference 路径。
   - RX 对完整 32-bit PIO RX word 直接 little-endian store；partial word 保留安全逐 bit 路径。

2. DMA chunk 增大到 8192 bit
   - 性能模式 `EXLINK_JTAG_DMA_CHUNK_BITS=8192`。
   - 兼容模式回退到 2048 bit。

3. firmware 最大 Shift 增大到 131072 bit
   - 性能模式 `EXLINK_JTAG_MAX_SHIFT_BITS=131072`。
   - 兼容模式回退到 32768 bit。
   - 资源余量充足，本轮未回退到 65536 bit。

4. TinyUSB CDC 批量收发
   - 项目内新增 `tusb_config.h`，性能模式 CDC RX/TX 为 8192/8192 bytes。
   - 写入路径改为尽量写满 TX FIFO，TX FIFO 满时 flush 并调用 `tud_task()`，完整响应入队后最终 flush 一次。
   - profile 增加 `usb_rx_read_calls`、`usb_rx_bytes`、`usb_rx_max_batch`、`usb_tx_write_calls`、`usb_tx_bytes`、`usb_tx_max_batch`、`usb_tx_flush_calls`。

5. 减少每个 DMA chunk 的重复初始化
   - RX/TX DMA 固定 config 缓存在初始化/重配置阶段。
   - 性能模式下 logical shift 开始统一清理 PIO/DMA 状态，每个 chunk 只更新地址/count 并启动 DMA。
   - 错误恢复仍执行完整 abort、disable、clear FIFO、restart、clear IRQ、drive idle，并重新缓存 DMA config。

## 默认配置

- `EXLINK_JTAG_PERFORMANCE_MODE=1`
- `EXLINK_JTAG_USE_FAST_TX_PACK=1`
- `EXLINK_JTAG_USE_FAST_RX_PACK=1`
- `EXLINK_JTAG_USE_LARGE_DMA_CHUNK=1`
- `EXLINK_JTAG_USE_LARGE_SHIFT=1`
- `EXLINK_JTAG_USE_BATCHED_CDC=1`
- `EXLINK_JTAG_USE_REDUCED_CHUNK_RESET=1`
- 最大 Shift：131072 bit
- DMA chunk：8192 bit
- TinyUSB CDC RX/TX：8192/8192 bytes

## 构建与资源

构建命令：

```powershell
cmake --build build\exlink-jtag --target exlink_jtag_bridge
```

兼容模式构建命令：

```powershell
cmake -S .\sigrok-pico\pico_sdk_sigrok -B .\build\exlink-jtag-compat -G Ninja -DSIGROK_BOARD_EXLINK=ON -DEXLINK_BUILD_JTAG_BRIDGE=ON -DEXLINK_JTAG_PERFORMANCE_MODE=0
cmake --build .\build\exlink-jtag-compat --target exlink_jtag_bridge
```

| 构建 | .text | .rodata | .data | .bss | 主 RAM 已用 | 主 RAM 余量 | ELF | UF2 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Stage 5.5 compatibility | 35372 | 2508 | 4096 | 23280 | 27568 | 234576 | 769316 | 84992 |
| Stage 5.6 performance | 35932 | 2636 | 4096 | 82936 | 87224 | 174920 | 778460 | 86016 |

- heap section：2048 bytes
- stack dummy：2048 bytes，位于 scratch Y
- 性能模式主 RAM 余量约 170.8 KiB，大于 32 KiB 要求。
- 构建输出未见新增 warning。

## 软件等价性测试

命令：

```powershell
python .\sigrok-pico\tools\exlink_pack_equivalence_test.py
```

结果：

```text
PASS: 12442 fast TX/RX pack equivalence cases, seed=0xE56A
```

覆盖：

- 长度：1, 2, 3, 4, 5, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65, 127, 128, 129, 255, 256, 257, 511, 512, 513, 1023, 1024, 1025, 2047, 2048, 2049, 4095, 4096, 4097, 8191, 8192
- offset：0, 1, 2, 3, 7, 8, 9, 15, 16, 31, 32
- 数据模式：全 0、全 1、0x55、0xAA、递增、随机
- 随机测试：10000 组，固定 seed

## 硬件测试状态

本轮未执行真实硬件测试，因此以下项目不得标记 PASS：

- loopback benchmark
- stress
- firmware profile 前后对比
- XVC profile 前后对比
- Zynq scan
- Vivado 五次下载时间

## 未修改内容确认

- 未修改 JTAG 引脚定义。
- 未修改 TMS/TDI/TDO LSB-first 位序。
- 未修改 TCK 更新边沿或 TDO 采样边沿。
- 未修改 TAP 状态转换行为。
- 未修改 XVC `getinfo:`、`settck:`、`shift:` 外部协议格式。
- 未修改 USB VID/PID 和设备描述符。
- 未实施 USB Vendor Bulk、WinUSB/libusb、双核流水、DMA ping-pong、DMA chain 或超过 5 MHz TCK。

## 待用户执行的硬件命令

详见 `docs/stage5_6_test_requirements.md`。
