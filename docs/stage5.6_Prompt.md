# Stage 5.6：Exlink RP2040 JTAG 高速下载性能冲刺

继续开发：

```text
HHPH-1/sigrok-pico
```

本阶段不再只做单个小优化。

本阶段目标是用一次集中的性能改造，显著缩短 Vivado bitstream 下载时间。

当前稳定基线：

```text
Vivado 连续 5 次下载：
43, 42, 42, 42, 43 s

平均下载时间：
42.4 s

真实固件有效速率：
约 792051 bit/s

32768-bit loopback stress：
约 729395 bit/s

DMA timeouts：
0

PIO recoveries：
0
```

Stage 5.6 目标：

```text
最低目标：
Vivado 平均下载时间 ≤ 32 s

正式目标：
Vivado 平均下载时间 ≤ 30 s

理想目标：
Vivado 平均下载时间 ≤ 25 s

有效传输率目标：
≥ 1.1 Mbit/s

理想有效传输率：
≥ 1.5 Mbit/s
```

不要为预计低于 3% 的微小收益投入大量时间。

---

# 一、必须先阅读

开始修改前阅读：

```text
docs/stage5_5_performance.md

pico_sdk_sigrok/exlink_jtag_bridge/jtag_protocol.h
pico_sdk_sigrok/exlink_jtag_bridge/jtag_protocol.c
pico_sdk_sigrok/exlink_jtag_bridge/jtag_pio.c
pico_sdk_sigrok/exlink_jtag_bridge/jtag_pio.h
pico_sdk_sigrok/exlink_jtag_bridge/usb_cdc_transport.c
pico_sdk_sigrok/exlink_jtag_bridge/jtag_profile.c
pico_sdk_sigrok/exlink_jtag_bridge/jtag_profile.h

tools/exlink_jtag_test.py
tools/exlink_xvc_server.py

所有 TinyUSB 配置文件
所有包含 CFG_TUD_CDC_RX_BUFSIZE 或 CFG_TUD_CDC_TX_BUFSIZE 的文件
```

先输出一份简短分析，必须包含：

1. 当前 firmware 最大 Shift 的 RAM 占用。
2. 最大 Shift 改为 65536 和 131072 bit 后的 RAM 占用。
3. DMA chunk 改为 4096 和 8192 bit 后的 RAM 占用。
4. TinyUSB CDC RX/TX buffer 当前大小。
5. 当前一个 131019-bit XVC Shift 被拆成多少 firmware 请求。
6. 当前每个 DMA chunk 重复执行哪些 PIO/DMA 初始化操作。
7. 当前 TX 展开和 TDO 打包的逐 bit 操作成本。
8. 链接结果中的 Flash、`.data`、`.bss`、heap 和 stack 余量。

分析后直接实施优化，不要停下来等待确认。

---

# 二、本阶段采用集中优化策略

本阶段一次实现以下五项优化：

```text
A. TX/RX 快速打包
B. DMA chunk 增大到 8192 bit
C. firmware 最大 Shift 增大到 131072 bit
D. TinyUSB CDC 缓冲与 flush 优化
E. 减少每个 DMA chunk 的 PIO/DMA 重复初始化
```

所有优化默认启用。

同时必须保留编译期回退宏，出现问题时可以快速关闭单项，而不是回滚整份代码。

建议总开关：

```c
#ifndef EXLINK_JTAG_PERFORMANCE_MODE
#define EXLINK_JTAG_PERFORMANCE_MODE 1
#endif
```

建议单项开关：

```c
EXLINK_JTAG_USE_FAST_TX_PACK
EXLINK_JTAG_USE_FAST_RX_PACK
EXLINK_JTAG_USE_LARGE_DMA_CHUNK
EXLINK_JTAG_USE_LARGE_SHIFT
EXLINK_JTAG_USE_BATCHED_CDC
EXLINK_JTAG_USE_REDUCED_CHUNK_RESET
```

默认全部为 `1`。

关闭 `EXLINK_JTAG_PERFORMANCE_MODE` 时应恢复 Stage 5.5 的主要配置：

```text
Maximum shift = 32768 bits
DMA chunk = 2048 bits
旧 TX/RX 打包路径
旧 CDC 行为
保守的 chunk 重置流程
```

---

# 三、严格保持不变的内容

禁止修改：

```text
JTAG 引脚定义
TMS/TDI/TDO LSB-first
TCK 更新边沿
TDO 采样边沿
TAP 状态转换行为
XVC getinfo:
XVC settck:
XVC shift:
bitbang 回退
sigrok 逻辑分析仪 target
USB VID/PID 和设备描述符
COM 口兼容性
```

固定引脚：

```text
CHAN0 / GPIO2 -> TMS
CHAN1 / GPIO3 -> TCK
CHAN2 / GPIO4 <- TDO
CHAN3 / GPIO5 -> TDI
GND           -> GND
```

本阶段不要实施：

```text
USB Vendor Bulk
WinUSB/libusb
双核流水
DMA ping-pong
DMA chain 多通道架构
异步多个 XVC 请求并行
超过 5 MHz TCK
完全重写 XVC 协议
```

---

# 四、优化 A：TX/RX 快速打包

## 4.1 TX 打包

优化：

```text
packed TMS/TDI
    ↓
PIO TX DMA staging buffer
```

当前逐 bit 路径必须保留为 reference fallback。

快速路径应：

```text
每次至少处理 4 个 JTAG bit
优先每次处理 8 个或更多 bit
使用 Flash 中的 const LUT
避免逐 bit get_packed_bit()
避免逐 bit OR
只处理有效范围
```

推荐至少实现：

```c
static const uint32_t jtag_tx_encode_lut[256];
```

也允许采用更高效且容易证明正确的双表或字节级查表。

要求：

```text
LUT 不进入 .bss
不使用动态内存
不使用大型栈数组
尾部 1～3 bit 正确
非字节对齐 offset 有安全 fallback
未使用 bit 清零
```

## 4.2 RX 打包

优化：

```text
PIO RX DMA word
    ↓
packed TDO
```

完整 32-bit RX word 不得继续逐 bit 调用 `set_packed_bit()`。

必须先确认 RX word 的真实 bit 顺序，然后：

```text
直接 uint32_t store
或 memcpy
或固定 bit/byte reverse
```

最后不足 32 bit 的 partial word使用单独安全路径。

要求正确覆盖：

```text
1
2
3
7
8
9
15
16
17
31
32
33
63
64
65
127
128
129
2047
2048
2049
```

---

# 五、优化 B：DMA chunk 增大到 8192 bit

把：

```c
EXLINK_JTAG_DMA_CHUNK_BITS 2048
```

性能模式下改为：

```c
EXLINK_JTAG_DMA_CHUNK_BITS 8192
```

保留编译期回退到：

```text
2048 bit
```

8192-bit chunk 预计 staging RAM：

```text
TX buffer 约 8196 bytes
RX buffer 约 1028 bytes
合计约 9224 bytes
```

相对 2048-bit chunk 增加约：

```text
6912 bytes
```

必须使用静态分配，禁止放在栈上。

一个 32768-bit firmware Shift 应从：

```text
16 chunks
```

下降到：

```text
4 chunks
```

一个 131072-bit firmware Shift应为：

```text
16 chunks
```

---

# 六、优化 C：firmware 最大 Shift 增大到 131072 bit

性能模式下把：

```c
EXLINK_JTAG_MAX_SHIFT_BITS
```

改为：

```text
131072 bits
```

也就是：

```text
16384 bytes
```

以下三个 packed buffer：

```text
tms_buffer
tdi_buffer
tdo_buffer
```

总计约：

```text
49152 bytes
```

必须检查链接后的实际 RAM 使用量。

如果 131072-bit 配置导致：

```text
RAM 余量不足
链接失败
栈空间危险
TinyUSB 不稳定
```

则自动回退到：

```text
65536 bits
```

不要直接退回 32768，除非 65536 也不可行。

工具同步修改：

```text
tools/exlink_jtag_test.py
```

不要继续硬编码：

```text
MAX_SHIFT_BITS = 32768
```

测试工具应优先查询 firmware capabilities 获得最大 Shift。

如果命令解析阶段必须有本地上限，设为：

```text
131072
```

但实际测试长度不得超过 firmware capabilities 返回值。

XVC Server 继续使用 firmware capabilities 自动生成：

```text
xvcServer_v1.0:<firmware_max_shift>
```

不得在 XVC Server 中另行硬编码。

---

# 七、优化 D：TinyUSB CDC 批量收发

当前重点检查：

```text
CFG_TUD_CDC_RX_BUFSIZE
CFG_TUD_CDC_TX_BUFSIZE
tud_cdc_available()
tud_cdc_read()
tud_cdc_write_available()
tud_cdc_write()
tud_cdc_write_flush()
tud_task()
```

性能模式下建议：

```text
CFG_TUD_CDC_RX_BUFSIZE >= 4096
CFG_TUD_CDC_TX_BUFSIZE >= 4096
```

RAM 足够时优先：

```text
RX = 8192
TX = 8192
```

## 7.1 写入策略

当前代码不能在每次成功的部分写入后无条件 flush。

改为：

```text
尽可能写满 TX FIFO
TX FIFO 满时 flush
调用 tud_task() 推进 USB
继续写剩余数据
完整响应写入后最终 flush 一次
```

要求：

```text
不能因为只在最后 flush 而死锁
不能逐字节发送
不能每 64 bytes 手动 flush
不能改变 CDC 响应格式
```

## 7.2 读取策略

读取时：

```text
每次读取所有当前可用数据
尽量减少循环次数
不得只读取一个字节
不得清空完整无关 buffer
```

## 7.3 增加统计

在 profile 中增加：

```text
usb_rx_read_calls
usb_rx_bytes
usb_rx_max_batch
usb_tx_write_calls
usb_tx_bytes
usb_tx_max_batch
usb_tx_flush_calls
```

保留已有：

```text
usb_rx_incomplete_waits
usb_tx_space_waits
```

不得逐请求打印。

---

# 八、优化 E：减少每 chunk 的 PIO/DMA 重置

当前每个 chunk 可能重复：

```text
abort DMA
disable SM
clear FIFO
restart SM
clear IRQ
drive idle
创建 DMA config
配置 RX DMA
配置 TX DMA
enable SM
等待
disable SM
```

把安全操作分成三个阶段。

## 8.1 logical shift 开始

只执行一次：

```text
确认 DMA 不 busy
disable SM
clear FIFO
restart SM
clear IRQ
drive idle
```

## 8.2 每个 chunk

尽量只执行：

```text
准备当前 TX/RX buffer
设置 DMA read/write address
设置 transfer count
启动 RX DMA
启动 TX DMA
enable SM
等待完成
必要的 chunk 收尾
```

DMA 固定配置：

```text
data size
read increment
write increment
DREQ
固定 FIFO 地址
```

应在初始化时配置或缓存，不要每 chunk 重新构造。

## 8.3 logical shift 结束

执行一次：

```text
disable SM
确认 DMA 完成
确认 RX FIFO 状态
drive idle
```

## 8.4 错误恢复

发生：

```text
DMA timeout
FIFO 状态错误
DMA busy 异常
```

仍执行完整恢复：

```text
abort TX DMA
abort RX DMA
disable SM
clear FIFO
restart SM
clear IRQ
drive idle
重新配置必要状态
```

不得为了速度删除错误恢复。

如果无法证明某个动作可以安全移动，则保留该动作。

---

# 九、软件等价性测试

新增自动测试，至少验证：

```text
快速 TX pack == reference TX pack
快速 RX pack == reference RX pack
```

覆盖长度：

```text
1, 2, 3, 4, 5,
7, 8, 9,
15, 16, 17,
31, 32, 33,
63, 64, 65,
127, 128, 129,
255, 256, 257,
511, 512, 513,
1023, 1024, 1025,
2047, 2048, 2049,
4095, 4096, 4097,
8191, 8192
```

覆盖 offset：

```text
0, 1, 2, 3, 7, 8, 9, 15, 16, 31, 32
```

数据模式：

```text
全 0
全 1
0x55
0xAA
递增模式
随机模式
```

随机测试至少：

```text
10000 组
固定 seed
```

失败必须返回非零退出码。

不要把大量时间花在构建复杂测试框架上；简单可靠的 Python 等价性脚本即可。

---

# 十、构建和资源检查

构建：

```powershell
cmake --build build\exlink-jtag --target exlink_jtag_bridge
```

同时生成或检查 linker map。

记录两种构建：

```text
Stage 5.5 compatibility mode
Stage 5.6 performance mode
```

记录：

```text
.text
.rodata
.data
.bss
heap/stack 预留
总 RAM
剩余 RAM
UF2 size
ELF size
```

要求：

```text
无新增 warning
无动态内存
无大型栈 buffer
所有大 buffer 静态分配
LUT 位于 Flash/rodata
性能模式仍保留安全 RAM 余量
```

建议至少保留：

```text
32 KiB 未分配 RAM 余量
```

如果 131072 bit + 8192 CDC buffer 导致余量不足，优先：

```text
CDC buffer 8192 → 4096
```

其次：

```text
最大 Shift 131072 → 65536
```

不要先减小 DMA chunk。

---

# 十一、更新测试工具

更新：

```text
tools/exlink_jtag_test.py
```

要求：

```text
支持最大 131072-bit Shift
boundary-test 根据 firmware capabilities 选择长度
加入 32769、65535、65536、65537、131071、131072
stress 支持 131072 bit
不能向固件发送超过 capabilities 的请求
```

benchmark 至少测试：

```text
128
512
4096
8192
16384
32768
65536
131072
```

只测试不超过 firmware capabilities 的长度。

---

# 十二、硬件测试命令

Codex 完成代码和编译后，不要宣称硬件已经通过。

输出准确测试命令。

## 12.1 短接回环

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

仅运行 firmware capabilities 支持的长度。

## 12.2 Stress

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 32768 --count 1000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 131072 --count 500
```

如果最大 Shift 为 65536：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 65536 --count 1000
```

## 12.3 固件 Profile

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile clear
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile on
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 benchmark --count 100
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile show
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile off
```

---

# 十三、Zynq 与 Vivado 测试

移除回环短接，连接 Zynq。

先扫描 10 次：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 scan --bits 128
```

开启 profile：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile clear
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile on
```

启动 XVC：

```powershell
python sigrok-pico\tools\exlink_xvc_server.py --port COM10 --default-tck-khz 5000
```

Vivado 连续下载 5 次：

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

停止 XVC 后：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile show
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 profile off
```

---

# 十四、验收标准

必须保持：

```text
bitbang loopback PASS
PIO loopback PASS
完整 boundary-test PASS
32768 × 1000 stress PASS
最大 Shift stress PASS
Zynq scan 10/10 一致
Vivado 连续 5 次成功
startup status HIGH
DMA timeout = 0
PIO recovery = 0
serial timeout = 0
protocol error = 0
```

性能最低保留要求：

```text
Vivado 平均时间 ≤ 35 s
```

正式通过目标：

```text
Vivado 平均时间 ≤ 30 s
```

理想目标：

```text
Vivado 平均时间 ≤ 25 s
```

如果组合优化后仍高于 35 s：

```text
不要继续做小于 3% 的微调
用编译宏依次关闭单项
快速确认哪项没有收益或导致退化
保留有明确收益的部分
```

---

# 十五、文档

创建：

```text
docs/stage5_6_high_speed.md
```

记录：

```text
Stage 5.5 基线
实施的五项优化
RAM 计算
链接结果
兼容模式与性能模式资源对比
软件等价性测试
loopback benchmark
stress 结果
firmware profile 前后对比
XVC profile 前后对比
Vivado 五次下载时间
被关闭或回退的优化
最终默认配置
```

用户未执行前不得填写硬件 PASS。

---

# 十六、最终输出

完成后输出：

1. 修改文件列表。
2. 五项优化的实现摘要。
3. 最大 Shift 最终选择：131072 或 65536。
4. DMA chunk 最终值。
5. TinyUSB RX/TX buffer 最终值。
6. Flash 和 RAM 前后变化。
7. 软件等价性测试结果。
8. 编译结果。
9. 当前默认性能宏。
10. 用户应执行的完整硬件命令。
11. 尚未执行的硬件验证。
12. 明确说明没有修改 JTAG 引脚、位序和外部 XVC 协议。

完成 Stage 5.6 后停止，不自动开始 USB Vendor Bulk 或 WinUSB 改造。
