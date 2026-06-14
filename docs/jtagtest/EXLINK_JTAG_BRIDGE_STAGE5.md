# Exlink RP2040 JTAG Bridge Stage 5

Stage 5 在 Stage 4 已验证的 PIO + DMA JTAG 固件基础上，重点完善 PC 端
XVC server 与 Vivado 的完整兼容性和长期稳定性。本阶段不主动修改已经通过
回归的 PIO program、DMA 数据格式、JTAG 边沿时序、bitbang 实现和固定引脚。

当前固件稳定基线：

```text
Firmware: EXLINK-RP2040-JTAG-BRIDGE v0.3
Engine: pio
DMA: enabled
Maximum shift: 32768 bits
PIO DMA chunk: 2048 bits
```

## 引脚

固定引脚保持 Stage 4 不变：

| Exlink channel | RP2040 GPIO | JTAG signal | Direction |
| --- | ---: | --- | --- |
| CHAN0 | GPIO2 | TMS | RP2040 output |
| CHAN1 | GPIO3 | TCK | RP2040 output |
| CHAN2 | GPIO4 | TDO | RP2040 input |
| CHAN3 | GPIO5 | TDI | RP2040 output |
| GND | GND | GND | Ground |

目标 JTAG bank 必须兼容 3.3 V。连接真实目标前必须拆掉 TDI/TDO 回环短接线。

## 链路结构

```text
Vivado
  |
  v
XVC over TCP
  |
  v
Python XVC Server
  |
  v
USB CDC protocol
  |
  v
RP2040 PIO + DMA JTAG bridge
  |
  v
Target JTAG
```

Stage 5 的主要改动位于：

```text
sigrok-pico/tools/exlink_xvc_server.py
sigrok-pico/tools/exlink_jtag_test.py
```

固件 C 代码保持 Stage 4 协议和时序。

## XVC 协议

Server 支持 Vivado 使用的三个 XVC 命令：

```text
getinfo:
settck:
shift:
```

### getinfo

Server 启动时读取固件 `Q` capability，使用固件返回的 `max_shift_bits` 生成
XVC info 字符串。v0.3 固件应返回：

```text
xvcServer_v1.0:32768
```

如果后续固件调整最大 shift，XVC server 会跟随固件 capability，而不是在多个
文件重复硬编码。

### settck

Vivado 的 `settck:` 发送目标 TCK 周期，单位为 ns。Server 处理流程：

1. 读取 4 字节 little-endian 周期值；
2. 换算为目标频率；
3. 钳位到固件支持范围 `50 kHz .. 5000 kHz`；
4. 通过 USB CDC `P` 命令设置 RP2040 PIO TCK；
5. 读取固件返回的实际 PIO TCK；
6. 将实际 TCK 重新换算为周期并返回 Vivado。

这一步会真实修改硬件 PIO divider，不只是在 Python 端保存频率。

### shift

`shift:` 处理要求：

```text
uint32 bit_count
packed TMS bytes
packed TDI bytes
```

Server 使用 `(bit_count + 7) // 8` 计算 TMS/TDI/TDO 字节数。所有 bitstream
保持 LSB-first。非整字节 shift 的最后一个字节会清除未使用高位。

单个 XVC `shift:` 请求不会自动 TAP reset。

## 半包读取和串口健壮性

Stage 5 消除“一次 read 收到完整数据”的假设。

TCP 侧统一使用：

```python
recv_exact(sock, length)
```

串口侧统一使用：

```python
serial_read_exact(ser, length)
serial_write_all(ser, data)
```

行为要求：

* TCP 数据可分多次到达；
* USB CDC 响应可分多次到达；
* 空 TCP 数据代表客户端断开；
* timeout 抛出明确异常；
* 不使用不完整帧继续解析；
* 协议错误后关闭当前 client socket；
* Vivado 断开后 server 主进程继续监听。

## 大请求分块

正常情况下，`getinfo:` 宣告固件最大 shift，让 Vivado 自行限制请求长度。
如果收到超过固件最大 shift 的逻辑请求，PC 端会自动分块：

```text
XVC logical shift
  |
  v
<= firmware max shift 子请求
  |
  v
依次发送到 RP2040
  |
  v
拼接完整 TDO
```

分块保持连续 bit offset，不按字节边界错误截断。分块之间不插入额外 TCK，
不 reset TAP。

Server 默认逻辑请求上限为：

```text
16777216 bits
```

可通过 `--max-logical-shift-bits` 调整。

## 启动能力同步

Server 启动阶段执行：

1. 打开 USB CDC serial；
2. `I` 读取 firmware info；
3. `Q` 读取 active engine、supported flags、max shift；
4. 如当前不是 PIO，则执行 `M 1` 切换到 PIO；
5. 使用 `P` 设置默认 PIO TCK 并读取固件返回的实际值；
6. 启动 TCP listener。

典型启动输出：

```text
Exlink XVC Server
Firmware: EXLINK-RP2040-JTAG-BRIDGE v0.3
Serial port: COM10
Engine: pio
DMA: enabled
Maximum shift: 32768 bits
PIO TCK: 1000 kHz
Listening on 127.0.0.1:2542
Waiting for XVC client...
```

## Session 统计

默认在客户端断开时输出本次 session summary：

```text
Session summary:
  Client: 127.0.0.1:xxxxx
  getinfo: 1
  settck: 1
  Shift requests: 12584
  Total bits: 98765432
  Minimum shift: 1
  Maximum shift: 32768
  Average shift: 7848
  Duration: 48.2 s
  Effective rate: 2050000 bit/s
  Serial timeouts: 0
  Protocol errors: 0
  Client reconnects: 0
  Errors: 0
```

可用 `--no-stats` 关闭 session summary。可用 `--log-shifts` 输出每个 shift 的
bit 数和分块数。`--verbose-bits` 会输出 TMS/TDI/TDO hex，仅适合小请求调试。

## 异常恢复流程

```text
等待 XVC client
  |
  v
处理 getinfo / settck / shift
  |
  +-- client 正常断开
  |     |
  |     v
  |   输出 summary，关闭 client socket，继续监听
  |
  +-- malformed command / TCP timeout
  |     |
  |     v
  |   记录 protocol error，关闭 client socket，继续监听
  |
  +-- serial timeout / bridge error
        |
        v
      清空 serial input/output buffer
        |
        v
      capabilities 恢复检查
        |
        v
      关闭 client socket，继续监听
```

`Ctrl+C` 会关闭 listener 和 serial port 后退出。

## 命令行

推荐启动：

```powershell
python sigrok-pico\tools\exlink_xvc_server.py --port COM10 --host 127.0.0.1 --tcp-port 2542
```

兼容旧参数：

```powershell
python sigrok-pico\tools\exlink_xvc_server.py --port COM10 --xvc-host 127.0.0.1 --xvc-port 2542
```

常用参数：

```text
--port COM10
--baudrate 115200
--timeout 2.0
--host 127.0.0.1
--tcp-port 2542
--socket-timeout 10.0
--default-tck-khz 1000
--max-logical-shift-bits 16777216
--log-shifts
--verbose-bits
--no-stats
```

Vivado Tcl：

```tcl
open_hw_manager
connect_hw_server
open_hw_target -xvc_url localhost:2542
```

## 构建

Stage 5 没有修改固件 C 代码，但仍需确认 Stage 4 固件目标没有被破坏：

```powershell
cmake --build build\exlink-jtag --target exlink_jtag_bridge
cmake --build build\exlink-jtag --target pico_sdk_sigrok
```

UF2 输出：

```text
build\exlink-jtag\exlink_jtag_bridge.uf2
build\exlink-jtag\pico_sdk_sigrok.uf2
```

## 软件检查

已执行的软件侧检查：

```powershell
python -B sigrok-pico\tools\exlink_xvc_server.py --help
git -C sigrok-pico diff --check
cmake --build build\exlink-jtag --target exlink_jtag_bridge
cmake --build build\exlink-jtag --target pico_sdk_sigrok
```

另用 fake bridge 做过无硬件 XVC socket 测试，覆盖碎片 TCP 下的：

```text
getinfo:
settck:
25-bit 非整字节 shift
9 / 9 / 7 bit 分块
```

这些测试只能证明 PC 端解析、分块和构建通过，不能替代真实 Vivado 验收。

## 硬件验收步骤

### 1. 启动 Server

```powershell
python sigrok-pico\tools\exlink_xvc_server.py --port COM10 --host 127.0.0.1 --tcp-port 2542
```

确认启动输出包含：

```text
Firmware: EXLINK-RP2040-JTAG-BRIDGE v0.3
Engine: pio
DMA: enabled
Maximum shift: 32768 bits
PIO TCK: 1000 kHz
Listening on 127.0.0.1:2542
```

### 2. Vivado 连接

```tcl
open_hw_manager
connect_hw_server
open_hw_target -xvc_url localhost:2542
```

检查：

* Zynq 正常出现；
* IDCODE 正确；
* Server 没有半包或长度错误；
* `settck` 日志显示固件实际 TCK 已改变。

### 3. 连续下载

```tcl
set_property PROGRAM.FILE {你的bitstream.bit} [current_hw_device]

for {set i 1} {$i <= 20} {incr i} {
    puts "===== Program $i / 20 ====="
    program_hw_devices [current_hw_device]
}
```

通过标准：20 次连续 program 均成功，Server 没有 serial timeout 或 protocol error。

### 4. 重连测试

重复 10 轮：

1. 关闭 Hardware Manager；
2. 确认 Python Server 仍运行并输出 session summary；
3. 重新打开 Hardware Manager；
4. 重新连接 `localhost:2542`；
5. 再下载一次 bitstream。

通过标准：不重启 Python、不重插 Exlink，仍可连接和下载。

### 5. Vivado 异常退出测试

在 XVC 连接期间直接关闭 Vivado。

预期 Server 输出类似：

```text
Client disconnected
Session summary:
...
Waiting for XVC client...
```

重新打开 Vivado 后应能再次连接。

### 6. 长时间连接测试

保持 Hardware Manager 连接至少 30 分钟，期间定期扫描或下载。

通过标准：

* COM10 不消失；
* Python 不退出；
* 固件不重启；
* DMA/PIO 不锁死；
* Vivado 不出现随机 unknown device；
* 断开后能够重新连接。

## Stage 5 完成标准

全部满足后才能进入 Stage 5.5：

```text
XVC getinfo 正确
XVC settck 真正控制硬件
XVC shift 位序和长度正确
TCP 半包处理正确
串口半包处理正确
Vivado 连续下载 20 次通过
Hardware Manager 重连 10 轮通过
Vivado 异常退出后 Server 继续运行
长时间连接无死锁
获得真实 shift 长度分布和 session 性能数据
```

## 当前未验证内容

截至本文档创建时，仓库内已完成软件实现和构建检查，但尚未在真实 Vivado、
真实 Exlink 和真实 Zynq 目标板上记录 Stage 5 通过结果。不要把软件检查结果
写成真实硬件验收通过。
