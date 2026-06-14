# Stage 5.6 中文测试需求文档

## 测试目的

验证 Stage 5.6 高速模式在保持 Stage 5 稳定行为的前提下，提升 Vivado bitstream 下载性能，并确认没有破坏 JTAG 引脚、位序、TAP 行为、USB CDC COM 口兼容性和 XVC 协议。

## 测试对象

- 固件：`exlink_jtag_bridge`
- 默认模式：Stage 5.6 performance mode
- 最大 Shift：131072 bit
- DMA chunk：8192 bit
- TinyUSB CDC RX/TX buffer：8192/8192 bytes
- 固定连接：

```text
CHAN0 / GPIO2 -> TMS
CHAN1 / GPIO3 -> TCK
CHAN2 / GPIO4 <- TDO
CHAN3 / GPIO5 -> TDI
GND           -> GND
```

## 测试前置条件

- 已烧录本轮生成的 `build\exlink-jtag\exlink_jtag_bridge.uf2`。
- Windows 设备管理器中确认 CDC 串口号，以下命令默认使用 `COM10`。
- Python 环境已安装 `pyserial`。
- Vivado 可连接目标 Zynq，bitstream 路径存在：

```text
F:/FPGA/ZYNQ7020/Flow_LED/prj/Flow_LED.runs/impl_1/flow_led.bit
```

## 软件静态测试

在不连接硬件的情况下执行：

```powershell
python .\sigrok-pico\tools\exlink_pack_equivalence_test.py
cmake --build build\exlink-jtag --target exlink_jtag_bridge
```

通过要求：

- TX fast pack 与 reference pack 完全一致。
- RX fast pack 与 reference pack 完全一致。
- 构建成功，无新增 warning。

## 短接回环测试

短接：

```text
CHAN3 / TDI ↔ CHAN2 / TDO
```

执行：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 info
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 capabilities

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

通过要求：

- `info` 返回正确固件身份。
- `capabilities` 显示最大 Shift 为 131072 bit，若固件实际回退则后续只测试 capabilities 支持的长度。
- bitbang `128-bit loopback` PASS。
- PIO `128/32768/65536/131072-bit loopback` PASS。
- boundary-test 全部 PASS。
- benchmark 完成且无串口超时、协议错误。

## Stress 测试

执行：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 32768 --count 1000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 131072 --count 500
```

如果最大 Shift 回退为 65536 bit，执行：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 65536 --count 1000
```

通过要求：

- 所有 loopback payload 完全匹配。
- Serial timeout = 0。
- Protocol error = 0。
- DMA timeout = 0。
- PIO recovery = 0。

## Firmware Profile 测试

执行：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile clear
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile on
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 benchmark --count 100
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile show
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile off
```

记录要求：

- `logical_shifts`
- `total_bits`
- `effective_rate_bit_s`
- `dma_chunks`
- `max_chunks_per_shift`
- `dma_timeouts`
- `pio_recoveries`
- `usb_rx_incomplete_waits`
- `usb_tx_space_waits`
- `usb_rx_read_calls`
- `usb_rx_bytes`
- `usb_rx_max_batch`
- `usb_tx_write_calls`
- `usb_tx_bytes`
- `usb_tx_max_batch`
- `usb_tx_flush_calls`
- `request_parse_us`
- `tx_prepare_us`
- `dma_pio_us`
- `tdo_pack_us`
- `response_queue_us`
- `shift_total_us`

## Zynq 扫描测试

移除回环短接，连接 Zynq。

执行 10 次：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 scan --bits 128
```

通过要求：

- 10 次扫描结果一致。
- 识别到稳定的非全 0、非全 1 IDCODE 候选值。
- 无 DMA timeout。
- 无 USB disconnect。

## Vivado 下载测试

开启 profile：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile clear
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile on
```

启动 XVC Server：

```powershell
python sigrok-pico\tools\exlink_xvc_server.py --port COM10 --default-tck-khz 5000
```

Vivado Tcl：

```tcl
set dev [current_hw_device]
set bit_file {F:/FPGA/ZYNQ7020/Flow_LED/prj/Flow_LED.runs/impl_1/flow_led.bit}

for {set i 1} {$i <= 5} {incr i} {
    puts "===== Stage 5.6 High-Speed Program $i / 5 ====="
    set start_ms [clock milliseconds]

    set_property PROGRAM.FILE $bit_file $dev
    program_hw_devices $dev

    set elapsed_ms [expr {[clock milliseconds] - $start_ms}]
    puts "===== elapsed: $elapsed_ms ms ====="
}
```

关闭 Hardware Target，让 XVC Server 输出 session summary。

停止 XVC 后执行：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile show
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile off
```

通过要求：

- Vivado 连续 5 次 `program_hw_devices` 成功。
- 每次 startup status 为 HIGH。
- XVC session summary 中 serial timeout = 0，protocol error = 0，errors = 0。
- firmware profile 中 DMA timeout = 0，PIO recovery = 0。

性能判定：

- 最低保留要求：Vivado 平均时间 ≤ 35 s。
- 正式通过目标：Vivado 平均时间 ≤ 30 s。
- 理想目标：Vivado 平均时间 ≤ 25 s。
- 有效传输率目标：≥ 1.1 Mbit/s。
- 理想有效传输率：≥ 1.5 Mbit/s。

## 验收结论填写规则

用户未实际执行并提供结果前，不得填写硬件 PASS。

测试记录至少包含：

- 固件 capabilities 输出。
- loopback、boundary、benchmark、stress 原始输出。
- firmware profile show 输出。
- XVC Server session summary。
- Vivado 5 次 elapsed ms。
- 是否有 USB disconnect、serial timeout、protocol error、DMA timeout、PIO recovery。
