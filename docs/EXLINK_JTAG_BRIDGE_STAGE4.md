# Exlink RP2040 JTAG Bridge Stage 4

Stage 4 在 Stage 3 已验证 PIO 时序上增加 DMA 批量传输和 32768-bit
最大 shift。GPIO bitbang 引擎保留，PIO 仍然是用户可选引擎；DMA 是 PIO
内部实现细节，不作为第三种用户 engine。

## 引脚

固定引脚不变：

| Exlink channel | RP2040 GPIO | JTAG signal | Direction |
| --- | ---: | --- | --- |
| CHAN0 | GPIO2 | TMS | RP2040 output |
| CHAN1 | GPIO3 | TCK | RP2040 output |
| CHAN2 | GPIO4 | TDO | RP2040 input |
| CHAN3 | GPIO5 | TDI | RP2040 output |
| GND | GND | GND | Ground |

TDI/TDO 回环测试临时连接：

```text
CHAN3 / GPIO5 / TDI -> CHAN2 / GPIO4 / TDO
```

连接真实目标前必须拆掉短接线。目标 JTAG bank 必须兼容 3.3 V。

## 架构

```text
USB CDC/XVC
    |
    v
packed TMS/TDI request
    |
    v
fixed-size chunk staging
    |
    v
TX DMA -> PIO -> JTAG pins
                  |
                  v
             TDO sampling
                  |
                  v
RX DMA <- PIO RX FIFO
    |
    v
packed TDO response
```

外部协议仍然发送 LSB-first packed TMS/TDI，固件返回 LSB-first packed TDO。
一个逻辑 shift 可为 1..32768 bits。PIO 内部按 2048-bit chunk 顺序执行，
chunk 之间不 reset TAP，不额外产生 TCK，也不插入 TMS/TDI bit。

## PIO + DMA 数据格式

Stage 4 保持 Stage 3 已验证 PIO 程序：

```text
pull block          ; bit_count - 1
mov x, osr
out null, 32
pull block          ; first packed TX word
loop:
    out pins, 4     ; TMS/TDI valid, TCK low
    nop
    out pins, 4     ; TCK high
    in pins, 1      ; sample TDO while TCK high
    jmp x-- loop
    set pins, 0     ; idle low
    push block
```

TX FIFO 仍使用 32-bit word。每个 JTAG bit 展开为两个 4-bit nibble：

```text
low_nibble  = TMS | (TDI << 3)
high_nibble = low_nibble | (1 << TCK)
```

一个 32-bit TX word 携带 4 个 JTAG bit。DMA 传输宽度选择 32-bit，因为 PIO
TX/RX FIFO 均按 32-bit word 访问，且现有数据格式已经以 32-bit word 对齐。

每个 chunk 的 DMA 顺序：

1. abort 上一轮仍 busy 的 DMA；
2. disable PIO SM；
3. clear PIO TX/RX FIFO；
4. restart SM；
5. clear PIO IRQ flags；
6. 启动 RX DMA，`PIO RX FIFO -> rx_dma_words`；
7. 启动 TX DMA，`tx_dma_words -> PIO TX FIFO`；
8. enable PIO SM；
9. 等待 TX/RX DMA 完成，带 100 ms chunk timeout；
10. timeout 时 abort TX/RX DMA、disable SM、清 FIFO、TCK 恢复低电平并返回错误。

## 静态 RAM

固件不使用 `malloc/calloc/realloc`。主要静态 buffer：

| Buffer | Size |
| --- | ---: |
| `tms_buffer[4096]` | 4096 bytes |
| `tdi_buffer[4096]` | 4096 bytes |
| `tdo_buffer[4096]` | 4096 bytes |
| `tx_dma_words[513]` | 2052 bytes |
| `rx_dma_words[65]` | 260 bytes |
| PIO instruction table | 22 bytes |

相对 Stage 3，协议 buffer 从 3 x 512 bytes 增加到 3 x 4096 bytes；PIO 原先
4096-bit TX/RX 全量 staging 被 2048-bit chunk staging 替代。净静态 RAM 增量
约 10.8 KiB，未计链接器对齐填充。

## PIO TCK

PIO 每个 JTAG bit 消耗 5 个 PIO cycle：

```text
TCK_Hz = clk_sys / (clock_divider * 5)
clock_divider = clk_sys / (requested_TCK_Hz * 5)
```

支持范围：

```text
50 kHz .. 5000 kHz
```

默认 PIO TCK 为 500 kHz。`clock-pio` 返回固件实际设置后的 Hz。bitbang
仍使用旧的 `clock --half-period-us`，两套时钟参数在 engine 切换后分别保留。

## USB CDC 协议

版本字符串：

```text
EXLINK-RP2040-JTAG-BRIDGE v0.3
```

`Q` capability 返回：

```text
bit0 = bitbang
bit1 = pio
bit2 = dma
max_shift_bits = 32768
```

旧命令保持兼容：

```text
I  info
T  TAP reset
S  shift
K  bitbang half-period-us
M  engine select, 0=bitbang, 1=pio
Q  capabilities
```

Stage 4 新增：

```text
P
uint32 requested_pio_tck_hz
```

响应：

```text
p
uint8 status
uint32 actual_pio_tck_hz
```

`S` shift 对 `bit_count=0`、`bit_count>32768`、TMS/TDI 数据不足和 USB frame
不完整均返回错误状态，不返回伪造的部分 TDO。

## PC 工具

基础命令：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 info
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 capabilities
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine pio
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 1000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 32768
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 boundary-test
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 32768 --count 1000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 benchmark
```

`boundary-test` 覆盖：

```text
1, 2, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65,
127, 128, 129, 255, 256, 257, 511, 512, 513,
1023, 1024, 1025, 2047, 2048, 2049,
4095, 4096, 4097, 8191, 8192, 8193,
16383, 16384, 16385, 32767, 32768
```

`benchmark` 默认分别测试 128、512、4096、8192、16384、32768 bits，每种长度
100 次并输出有效 bit/s。

## XVC

XVC server 保持：

```text
getinfo:
settck:
shift:
```

`getinfo:` 返回 firmware capability 对应的最大 shift。`settck:` 会换算成 PIO
TCK 并发送固件 `P` 命令；超出 50 kHz..5 MHz 时在 PC 端钳位到最接近的可支持
频率，再把实际周期返回给 Vivado。`shift:` 超过固件最大 shift 时按 32768-bit
分块，保持 LSB-first bit 顺序，不在分块之间 reset TAP。

启动：

```powershell
python sigrok-pico\tools\exlink_xvc_server.py --port COM10 --xvc-host 127.0.0.1 --xvc-port 2542
```

Vivado Tcl：

```tcl
open_hw
connect_hw_server
open_hw_target -xvc_url localhost:2542
```


## 构建

```powershell
cmake -S sigrok-pico\pico_sdk_sigrok `
  -B build\exlink-jtag `
  -G Ninja `
  -DPICO_BOARD=pico `
  -DSIGROK_BOARD_EXLINK=ON `
  -DEXLINK_BUILD_JTAG_BRIDGE=ON `
  -Dpicotool_DIR=C:\Users\HHPH\Desktop\exlink_JTAG\rp2040\tools\picotool-2.1.0\picotool

cmake --build build\exlink-jtag --target exlink_jtag_bridge
cmake --build build\exlink-jtag --target pico_sdk_sigrok
```

UF2 输出：

```text
build\exlink-jtag\exlink_jtag_bridge.uf2
build\exlink-jtag\pico_sdk_sigrok.uf2
```

## 硬件验收命令

A. TDI/TDO 短接测试：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 info
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 capabilities
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine bitbang
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 128
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine pio
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 128
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 boundary-test
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 32768
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 32768 --count 1000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 benchmark
```

B. PIO 时钟测试：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 100
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 4096
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 500
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 4096
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 1000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 4096
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 2000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 4096
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 5000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 4096
```

C. Zynq 测试：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 reset
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 500
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 scan --bits 128
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 1000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 scan --bits 128
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 2000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 scan --bits 128
```

每个频率下连续执行 10 次 `scan --bits 128`，比较结果必须一致。

D. Vivado：

1. 启动 XVC server；
2. Hardware Manager 连接 `localhost:2542`；
3. 识别 Zynq 器件；
4. 连续下载 bitstream 至少 10 次；
5. 测试 reconnect；
6. 关闭再打开 Hardware Manager；
7. 确认固件没有重启或卡死。

本仓库构建只能证明代码编译通过；以上硬件结果必须在真实 Exlink 和目标板上
实际执行后再记录。
