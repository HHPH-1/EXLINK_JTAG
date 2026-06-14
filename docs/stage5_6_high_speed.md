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
- 2026-06-14 复测：`ninja: no work to do.`

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

测试日期：2026-06-14

测试环境：

- Windows 串口枚举确认 `COM10` 为 Raspberry Pi Pico CDC 设备，`VID:PID=2E8A:000A`。
- 短接方式：`CHAN3 / TDI ↔ CHAN2 / TDO`。
- 已完成短接回环、stress、firmware profile benchmark、Zynq scan、XVC profile 和 Vivado 五次下载测试。

### 固件信息与能力

命令：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 info
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 capabilities
```

结果：

```text
EXLINK-RP2040-JTAG-BRIDGE v0.3
Active engine: pio
Supported engines: bitbang, pio, dma
Maximum shift: 131072 bits
```

### 短接回环测试

命令：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine bitbang
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 128
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine pio
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 5000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 128
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 32768
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 65536
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 131072
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 boundary-test
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 benchmark --count 100
```

结果：

- bitbang engine 切换成功，128-bit loopback PASS。
- pio engine 切换成功，PIO TCK 设置为 5000000 Hz。
- PIO loopback：128、32768、65536、131072 bit 全部 PASS。
- boundary-test：1 bit 至 131072 bit 边界长度全部 PASS。

Benchmark：

```text
Engine: pio
DMA: yes
  128 bits:     390039 bit/s over 100 runs (0.033 s)
  512 bits:     806614 bit/s over 100 runs (0.063 s)
 4096 bits:    1146770 bit/s over 100 runs (0.357 s)
 8192 bits:    1161221 bit/s over 100 runs (0.705 s)
16384 bits:    1165812 bit/s over 100 runs (1.405 s)
32768 bits:    1167663 bit/s over 100 runs (2.806 s)
65536 bits:    1169855 bit/s over 100 runs (5.602 s)
131072 bits:    1176532 bit/s over 100 runs (11.141 s)
PASS: benchmark completed
```

### Stress 测试

命令：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 32768 --count 1000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 131072 --count 500
```

结果：

```text
PASS: stress completed
Engine: pio
DMA: yes
Bit count: 32768
Count: 1000
Total bits: 32768000
Total time: 28.099 s
Effective bit/s: 1166173
Average request latency: 28.081 ms

PASS: stress completed
Engine: pio
DMA: yes
Bit count: 131072
Count: 500
Total bits: 65536000
Total time: 55.762 s
Effective bit/s: 1175272
Average request latency: 111.480 ms
```

### Firmware Profile Benchmark

命令：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile clear
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile on
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 benchmark --count 100
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile show
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile off
```

Benchmark 结果：

```text
Engine: pio
DMA: yes
  128 bits:     397958 bit/s over 100 runs (0.032 s)
  512 bits:     821843 bit/s over 100 runs (0.062 s)
 4096 bits:    1081104 bit/s over 100 runs (0.379 s)
 8192 bits:    1121147 bit/s over 100 runs (0.731 s)
16384 bits:    1148361 bit/s over 100 runs (1.427 s)
32768 bits:    1166621 bit/s over 100 runs (2.809 s)
65536 bits:    1169113 bit/s over 100 runs (5.606 s)
131072 bits:    1175620 bit/s over 100 runs (11.149 s)
PASS: benchmark completed
```

Profile 结果：

```text
Firmware performance profile
  enabled=1
  logical_shifts=800
  successful_shifts=800
  total_bits=25868800
  effective_rate_bit_s=1501745
  dma_chunks=3400
  average_chunks_per_shift=4
  max_chunks_per_shift=16
  dma_timeouts=0
  pio_recoveries=0
  usb_rx_incomplete_waits=1963878
  usb_tx_space_waits=311833
  usb_rx_read_calls=103398
  usb_rx_bytes=6470401
  usb_rx_max_batch=64
  usb_tx_write_calls=14403
  usb_tx_bytes=3239324
  usb_tx_max_batch=8192
  usb_tx_flush_calls=313436
  request_parse_us: count=800 total_us=7877620 avg_us=9847 min_us=23 max_us=40427
  tx_prepare_us: count=800 total_us=2234548 avg_us=2793 min_us=17 max_us=11303
  dma_pio_us: count=800 total_us=5180306 avg_us=6475 min_us=27 max_us=26248
  tdo_pack_us: count=800 total_us=111861 avg_us=139 min_us=1 max_us=568
  response_queue_us: count=800 total_us=1780477 avg_us=2225 min_us=25 max_us=17056
  shift_total_us: count=800 total_us=17225825 avg_us=21532 min_us=107 max_us=95739
```

本轮已通过项目：

- 软件等价性测试：PASS。
- 构建检查：PASS，构建目录已是最新产物。
- 固件 capabilities：最大 Shift 为 131072 bit。
- bitbang 128-bit loopback：PASS。
- PIO 128/32768/65536/131072-bit loopback：PASS。
- boundary-test：PASS。
- benchmark：PASS。
- stress 32768-bit 1000 次：PASS。
- stress 131072-bit 500 次：PASS。
- firmware profile benchmark：`dma_timeouts=0`，`pio_recoveries=0`。
- Zynq scan 10 次：PASS，稳定识别 `0x23727093`、`0x4BA00477`。
- XVC/Vivado 五次下载：PASS，5 次 startup status 均为 HIGH，平均 27.245 s。

尚未执行项目：无。

### Zynq Scan 测试

连接状态：

- 已移除 `CHAN3 / TDI ↔ CHAN2 / TDO` 短接。
- 已正确连接 Zynq 并上电。
- PIO TCK：5000 kHz。

命令：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine pio
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 5000
for ($i = 1; $i -le 10; $i++) {
    python sigrok-pico\tools\exlink_jtag_test.py --port COM10 scan --bits 128
}
```

结果：

```text
10 次扫描结果完全一致：
raw TDO bytes: 93 70 72 23 77 04 a0 4b 00 00 00 00 00 00 00 00
32-bit LSB-first words:
  [0] 0x23727093 candidate IDCODE
  [1] 0x4BA00477 candidate IDCODE
  [2] 0x00000000
  [3] 0x00000000
```

结论：

- Zynq scan PASS。
- 已识别到稳定的非全 0、非全 1 IDCODE 候选值。
- 测试期间未观察到 USB disconnect。

### Vivado 下载与 XVC Profile 测试

连接状态：

- Zynq JTAG 正常连接并上电。
- XVC server 使用 `COM10`，默认 TCK 为 5000 kHz。
- Vivado 2020.2 路径：`F:\apps\Vidado2020_2\Vivado\2020.2\bin\vivado.bat`。
- Bitstream：`F:/FPGA/ZYNQ7020/Flow_LED/prj/Flow_LED.runs/impl_1/flow_led.bit`。

执行说明：

- Vivado 首次 `open_hw_target -xvc_url localhost:2542` 偶发返回 `No devices detected`；按 Stage 5.5 既有流程执行 `close_hw_target`、等待 5 s 后重试成功。
- Vivado 枚举到 `arm_dap_0 xc7z020_1`，下载脚本明确选择 `xc7z020_1`，避免误选不可编程的 `arm_dap_0`。

Vivado 结果：

```text
===== hw devices: arm_dap_0 xc7z020_1 =====
===== selected hw device: xc7z020_1 =====

===== Stage 5.6 High-Speed Program 1 / 5 =====
INFO: [Labtools 27-3164] End of startup status: HIGH
program_hw_devices: elapsed = 00:00:26
===== elapsed: 26483 ms =====

===== Stage 5.6 High-Speed Program 2 / 5 =====
INFO: [Labtools 27-3164] End of startup status: HIGH
program_hw_devices: elapsed = 00:00:26
===== elapsed: 26442 ms =====

===== Stage 5.6 High-Speed Program 3 / 5 =====
INFO: [Labtools 27-3164] End of startup status: HIGH
program_hw_devices: elapsed = 00:00:28
===== elapsed: 27793 ms =====

===== Stage 5.6 High-Speed Program 4 / 5 =====
INFO: [Labtools 27-3164] End of startup status: HIGH
program_hw_devices: elapsed = 00:00:28
===== elapsed: 27756 ms =====

===== Stage 5.6 High-Speed Program 5 / 5 =====
INFO: [Labtools 27-3164] End of startup status: HIGH
program_hw_devices: elapsed = 00:00:28
===== elapsed: 27753 ms =====
```

Vivado 性能汇总：

- 5 次下载时间：26.483 s、26.442 s、27.793 s、27.756 s、27.753 s。
- 平均下载时间：27.245 s。
- 最短下载时间：26.442 s。
- 最长下载时间：27.793 s。
- 5 次 `program_hw_devices` 均成功。
- 5 次 Vivado 日志均报告 `End of startup status: HIGH`。
- 满足最低保留要求 `<= 35 s`。
- 满足正式通过目标 `<= 30 s`。
- 未达到理想目标 `<= 25 s`。

XVC session summary：

```text
Session summary:
  Client: 127.0.0.1:55230
  Session index: 1
  Total accepted clients: 1
  getinfo: 1
  settck: 1
  Shift requests: 6200
  Total bits: 164742661
  Minimum shift: 5
  Maximum shift: 524235
  Average shift: 26571
  Duration: 148.1 s
  Effective rate: 1112144 bit/s
  Serial timeouts: 0
  Protocol errors: 0
  Client reconnects: 0
  Errors: 0
```

XVC serial subrequest summary：

```text
XVC logical shifts: 6200
Serial shift subrequests: 7125
Average subrequests per shift: 1.15
Maximum subrequests per shift: 4
Unaligned subrequests: 310
```

XVC 性能要点：

- XVC session effective rate：1112144 bit/s。
- 达到有效传输率目标 `>= 1.1 Mbit/s`。
- 未达到理想有效传输率 `>= 1.5 Mbit/s`。
- `32769+` shift bucket 承载 162351852 bit，占 98.5%。
- `32769+` bucket effective rate：1200604 bit/s。

Firmware profile：

```text
Firmware performance profile
  enabled=1
  logical_shifts=7125
  successful_shifts=7125
  total_bits=164742661
  effective_rate_bit_s=1401031
  dma_chunks=25719
  average_chunks_per_shift=3
  max_chunks_per_shift=16
  dma_timeouts=0
  pio_recoveries=0
  usb_rx_incomplete_waits=12512997
  usb_tx_space_waits=3542208
  usb_rx_read_calls=660710
  usb_rx_bytes=41219805
  usb_rx_max_batch=64
  usb_tx_write_calls=172582
  usb_tx_bytes=20639367
  usb_tx_max_batch=8192
  usb_tx_flush_calls=3556465
  request_parse_us: count=7125 total_us=50147655 avg_us=7038 min_us=22 max_us=40745
  tx_prepare_us: count=7125 total_us=14251170 avg_us=2000 min_us=6 max_us=11303
  dma_pio_us: count=7125 total_us=32997191 avg_us=4631 min_us=3 max_us=26248
  tdo_pack_us: count=7125 total_us=724776 avg_us=101 min_us=1 max_us=570
  response_queue_us: count=7125 total_us=19178332 avg_us=2691 min_us=24 max_us=16952
  shift_total_us: count=7125 total_us=117586657 avg_us=16503 min_us=73 max_us=95725
```

Vivado/XVC 结论：

- Vivado 五次下载 PASS。
- XVC session summary：`Serial timeouts=0`，`Protocol errors=0`，`Errors=0`。
- Firmware profile：`dma_timeouts=0`，`pio_recoveries=0`。
- 测试期间未观察到 USB disconnect。
- 相对 Stage 5.5 平均 42.4 s，本轮平均 27.245 s，缩短约 35.7%。

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
