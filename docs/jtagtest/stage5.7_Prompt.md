在当前 `sigrok-pico` 本地工作区和当前 JTAG 分支上，直接实现一个“Exlink RP2040 JTAG 最高下载速度一键测试版本”。

不要只分析或给建议，要完成代码修改、构建、软件测试，并在硬件环境允许时实际执行测试。

# 一、当前状态

当前硬件和软件状态：

```text
RP2040 外部晶振：12 MHz
实际 PIO 时钟计算必须读取 clock_get_hz(clk_sys)，不能把 12 MHz 当作 clk_sys
USB：RP2040 原生 USB CDC
JTAG 引擎：PIO + DMA
最大 Shift：131072 bit
当前稳定 TCK：5 MHz
当前同一 bitstream 平均下载时间：27.245 s
之前 Stage 5.5 平均下载时间：42.4 s
参考下载器平均下载时间：约 3.400 s
目标：测出当前 RP2040 架构下能够达到的最高稳定下载速度
```

现有 Vivado/XVC 测试结果：

```text
Vivado 连续五次下载 PASS
Serial timeouts = 0
Protocol errors = 0
Errors = 0
dma_timeouts = 0
pio_recoveries = 0
未观察到 USB disconnect
```

固定引脚：

```text
CHAN0 / GPIO2 -> TMS
CHAN1 / GPIO3 -> TCK
CHAN2 / GPIO4 <- TDO
CHAN3 / GPIO5 -> TDI
GND           -> GND
```

不得修改这些引脚。

# 二、总目标

新增一个独立实验固件：

```text
exlink_jtag_maxspeed.uf2
```

新增一键测试工具：

```text
tools/exlink_maxspeed_test.py
```

新增 Vivado 批处理程序：

```text
tools/exlink_vivado_program.tcl
```

一条命令完成：

```text
检查固件
→ 读取实际 clk_sys
→ 计算当前 PIO 理论最大 TCK
→ 从理论最大值开始向下降频
→ IDCODE 快速筛选
→ Vivado 下载测试
→ 边界二分搜索
→ 最佳配置连续下载确认
→ 输出报告
```

最终必须分别给出：

```text
理论最大 TCK
最高 IDCODE 稳定 TCK
最高可以完成 Vivado 下载的 TCK
连续下载稳定的最高 TCK
实际下载时间最短的 TCK
最快单次下载时间
最快下载中位数
相对 27.245 s 的提升
距离 3.400 s 还差多少
当前主要瓶颈
```

最高 TCK 和最快下载配置必须分别统计，不能默认它们相同。

# 三、修改原则

1. 只在当前本地工作区增量修改。
2. 不允许 reset、checkout、覆盖用户已有代码。
3. 不破坏现有普通 JTAG 固件。
4. 不破坏逻辑分析仪 target。
5. 保留 bitbang。
6. 保留当前安全 PIO 引擎作为回退。
7. 新建独立 `exlink_jtag_maxspeed` CMake target。
8. 不超频 RP2040 系统时钟。
9. 第一轮只测试 Pico SDK 当前正常 `clk_sys` 下的极限。
10. 不得伪造硬件测试结果。
11. 当前最大 Shift 已经是 131072 bit，禁止退回旧的 32768 bit。

# 四、解除 5 MHz 固定限制

检查并修改所有 5 MHz 限制，包括但不限于：

```text
jtag_engine.h
jtag_pio.c
jtag_protocol.*
tools/exlink_jtag_test.py
tools/exlink_xvc_server.py
```

不能只修改 Python 参数范围。

PIO TCK 必须根据运行时系统时钟计算：

```c
uint32_t sys_hz = clock_get_hz(clk_sys);
```

理论最大频率：

```text
maximum_tck = clk_sys_hz / pio_cycles_per_jtag_bit
```

固件不得写死：

```text
12 MHz
125 MHz
25 MHz
```

必须返回实际运行数据。

扩展 capabilities/info，至少输出：

```text
Firmware version
Protocol version
xosc_hz
clk_sys_hz
clk_usb_hz
Active engine
PIO cycles per bit
Minimum TCK
Maximum theoretical TCK
Maximum allowed TCK
Requested TCK
Actual TCK
DMA chunk bits
Maximum Shift bits
```

设置频率时要求：

* 检查 PIO divider 合法范围；
* divider 不得小于硬件允许值；
* 返回实际应用的 TCK；
* 设置失败时保持原来的稳定频率；
* 切换频率时避免产生异常窄 TCK 脉冲；
* 切换前关闭 PIO SM；
* 清空 FIFO；
* 重启 SM；
* 最终保持 TCK 低电平。

# 五、PIO 引擎

保留现有安全 PIO 引擎：

```text
pio_safe
```

当前安全引擎预计是 5 个 PIO cycle/JTAG bit。

另外尝试实现实验性高速引擎：

```text
pio_fast
```

目标为 4 个 PIO cycle/JTAG bit。

优先检查现有 PIO 程序中的额外 `nop` 是否可以删除，但必须确保：

```text
TMS/TDI 在 TCK 上升沿前稳定
TDO 在正确边沿采样
最后一个 bit 不丢失
非整字节长度正确
非 32 bit 对齐长度正确
Shift 结束后 TCK 为低
TAP reset 正常
RX FIFO 无残留
DMA 数量完全匹配
```

必须先做软件等价性和 loopback 等价性验证。

如果 `pio_fast` 失败：

```text
保持 pio_safe 为默认
pio_fast 标记为 unavailable
报告失败原因
继续使用 pio_safe 测试最高下载速度
```

不得因为 fast 引擎失败而破坏当前正常功能。

# 六、DMA 分块测试

当前 DMA 分块也可能限制性能。

实验固件支持运行时选择候选分块：

```text
2048 bit
4096 bit
8192 bit
16384 bit
32768 bit
```

根据 RAM 占用决定最终支持范围，但至少要支持：

```text
2048 bit
8192 bit
```

要求：

* 静态分配缓冲区；
* Shift 热路径禁止 malloc/free；
* 构建时输出 RAM/Flash 使用量；
* 检查缓冲区边界；
* 不覆盖 TMS、TDI、TDO；
* DMA timeout 根据 bit 数和实际 TCK 动态计算；
* timeout 留有安全余量；
* 低频大 Shift 不得被固定 100 ms timeout 误杀。

先通过 benchmark 筛选 engine/chunk 组合，再让最快的 1～2 组进入真实 Vivado 下载测试。

# 七、USB CDC 优化

在保持当前协议兼容的前提下优化：

```text
批量读取 USB RX
避免逐字节处理 payload
请求头和 payload 分阶段解析
使用固定接收缓冲区
一个完整响应最多执行一次必要 flush
禁止每个 DMA chunk 单独 flush
避免重复清零整个最大缓冲区
减少无意义 busy polling
保持 tud_task/TinyUSB 正常运行
PC 断开后可以重新连接
一次失败不能让固件永久卡死
```

不要先改成新的 USB Vendor 协议。本任务先测清当前 CDC 架构的极限。

# 八、Firmware Profile

保留并扩展 Profile。

至少输出：

```text
logical_shifts
successful_shifts
failed_shifts
total_bits
effective_rate_bit_s
requested_tck_hz
actual_tck_hz
pio_cycles_per_bit
dma_chunk_bits
dma_chunks
average_chunks_per_shift
max_chunks_per_shift
dma_timeouts
pio_recoveries
data_mismatches
usb_rx_incomplete_waits
usb_tx_space_waits
usb_rx_read_calls
usb_rx_bytes
usb_rx_max_batch
usb_tx_write_calls
usb_tx_bytes
usb_tx_max_batch
usb_tx_flush_calls
request_parse_us
tx_prepare_us
dma_pio_us
tdo_pack_us
response_queue_us
shift_total_us
maximum_dma_time_us
```

Profile 关闭时不能明显拖慢正常下载。

# 九、XVC Server

修改：

```text
tools/exlink_xvc_server.py
```

增加参数：

```text
--force-tck-khz
--engine
--dma-chunk-bits
--profile
--json-summary
--log-file
```

`--force-tck-khz` 启用时：

* Vivado 的 `settck:` 仍要正常回复；
* 但实际固件频率保持为测试程序指定值；
* Vivado 后续的 `settck:` 不得覆盖测试频率；
* 日志同时记录 Vivado requested TCK 和固件 actual TCK。

Session Summary 增加：

```text
Requested TCK
Actual TCK
Engine
PIO cycles per bit
DMA chunk
Shift requests
Total shifted bits
Effective rate
Serial timeouts
Protocol errors
DMA timeouts
PIO recoveries
USB disconnects
```

# 十、Vivado 自动下载 Tcl

新建：

```text
tools/exlink_vivado_program.tcl
```

通过 Vivado batch mode 完成：

```tcl
open_hw_manager
connect_hw_server
open_hw_target -xvc_url localhost:2542
识别硬件器件
设置 PROGRAM.FILE
program_hw_devices
刷新设备状态
关闭连接
```

要求：

1. Vivado 启动时间不计入下载时间。
2. 只测量 `program_hw_devices` 调用时间。
3. 同一个 Vivado session 内执行 warm-up 和正式下载。
4. Tcl 捕获错误并返回非零退出码。
5. 输出机器可解析结果。
6. 支持指定器件索引。
7. 不依赖中文输出内容判断 PASS。

输出示例：

```text
EXLINK_PROGRAM run=0 type=warmup status=PASS elapsed_ms=12345
EXLINK_PROGRAM run=1 type=test status=PASS elapsed_ms=12003
EXLINK_PROGRAM run=2 type=test status=FAIL elapsed_ms=0 reason=...
```

# 十一、一键测试程序

新建：

```text
tools/exlink_maxspeed_test.py
```

命令示例：

```powershell
python tools\exlink_maxspeed_test.py `
  --port COM10 `
  --bitstream "<BITSTREAM绝对路径>" `
  --vivado "<vivado.bat绝对路径>" `
  --xvc-port 2542 `
  --coarse-step-khz 2500 `
  --binary-resolution-khz 250 `
  --warmup 1 `
  --runs 3 `
  --confirm-runs 10 `
  --baseline-seconds 27.245 `
  --target-seconds 3.400
```

允许增加：

```text
--minimum-khz
--maximum-khz
--device-index
--engine
--dma-chunks
--skip-fast-engine
--skip-vivado
```

# 十二、最高频率搜索方式

默认必须采用 top-down 搜索，不是从低到高。

流程：

```text
读取 clk_sys
读取当前 PIO cycles/bit
计算理论最大 TCK
从理论最大 TCK 开始
以 2500 kHz 步长向下降频
```

例如安全引擎在 `clk_sys=125 MHz` 且 5 cycle/bit 时，可能从接近 25 MHz 开始，但必须由运行时计算，不能写死。

每个频率先执行快速筛选：

```text
设置 TCK
读取 actual TCK
TAP reset
连续读取 IDCODE 10 次
检查 10 次 IDCODE 完全一致
检查 serial/DMA/PIO 错误计数
```

快速筛选失败：

```text
记录失败
执行完整恢复
降低频率
继续测试
```

快速筛选通过后：

```text
启动 XVC Server
启动 Vivado batch
warm-up 下载 1 次
正式下载 1 次作为粗筛
```

找到第一个可以下载的频率后，在：

```text
最近失败频率
最近通过频率
```

之间进行二分搜索，直到频率差小于：

```text
250 kHz
```

然后对所有候选稳定频率执行：

```text
warm-up 1 次
正式下载 3 次
```

选择下载时间中位数最短的配置，不是频率数字最大的配置。

# 十三、失败恢复

任何高频失败后必须自动执行：

```text
关闭 Vivado batch 进程
关闭 XVC session
停止 TX DMA
停止 RX DMA
关闭 PIO SM
清空 TX/RX FIFO
清除 PIO IRQ
重启 PIO SM
TCK 拉低
重新打开串口
切换回已知安全频率
TAP reset
低频读取 IDCODE
```

如果低频 IDCODE 也无法恢复：

```text
立即停止自动测试
保存已有结果
提示需要重新插拔或检查目标板
```

不能继续盲目向目标板发送高速时钟。

# 十四、最佳配置确认

选择正式下载中位数最短的配置，然后执行：

```text
连续下载 10 次
```

最终稳定 PASS 条件：

```text
10/10 下载成功
Serial timeouts = 0
Protocol errors = 0
DMA timeouts = 0
PIO recoveries = 0
USB disconnects = 0
IDCODE 始终一致
```

如果最佳配置确认失败：

```text
自动选择次优配置
再次执行连续 10 次确认
```

最终推荐值必须是确认测试通过的配置。

# 十五、结果统计

每组配置记录：

```text
Engine
PIO cycles per bit
DMA chunk
Requested TCK
Actual TCK
IDCODE
IDCODE scan pass count
Warm-up time
每次下载时间
Minimum
Maximum
Mean
Median
P95
Standard deviation
总 Shift bits
XVC effective bit/s
Firmware effective bit/s
request_parse 时间
tx_prepare 时间
dma_pio 时间
tdo_pack 时间
response_queue 时间
错误计数
```

计算：

```text
相对 27.245 s 的时间缩短百分比
相对 27.245 s 的下载效率提升
相对 3.400 s 还慢多少倍
要达到 3.400 s 所需平均有效 JTAG bit/s
```

必须根据 Profile 判断当前主要瓶颈是：

```text
PIO/TCK
DMA 分块
USB RX
USB TX
请求解析
TMS/TDI 数据准备
XVC 请求往返
Vivado 自身固定操作
```

不要只根据猜测下结论。

# 十六、报告

输出到：

```text
reports/maxspeed/YYYYMMDD_HHMMSS/
```

包含：

```text
summary.md
results.csv
results.json
environment.json
vivado_*.log
xvc_*.log
firmware_profile_*.txt
build.log
software_tests.log
```

`environment.json` 至少包含：

```text
Git commit
Git dirty 状态
固件版本
COM 端口
VID/PID
clk_sys
Vivado 路径
Vivado 版本
bitstream 路径
bitstream SHA-256
测试开始时间
操作系统
Python 版本
```

`summary.md` 开头直接给出：

```text
最高理论 TCK
最高 IDCODE 稳定 TCK
最高可下载 TCK
10 次确认稳定 TCK
最快下载配置
最快单次时间
最快平均值
最快中位数
相对 27.245 s 提升
距离 3.400 s 的差距
最终推荐默认 TCK
主要瓶颈
下一步最值得做的唯一优化
```

# 十七、构建和软件测试

必须完成：

```text
CMake configure
原 exlink_jtag_bridge target 构建
新 exlink_jtag_maxspeed target 构建
逻辑分析仪 target 构建
Python py_compile
现有测试
新增测试
```

新增单元测试至少覆盖：

```text
运行时最大 TCK 计算
PIO divider 边界
top-down 频率序列
二分搜索
频率设置失败恢复
IDCODE 一致性判断
Vivado 日志解析
PASS/FAIL 判断
Mean/Median/P95/标准差
JSON/CSV/Markdown 输出
force-tck 行为
非整字节 Shift
131072-bit 最大 Shift
```

不得将硬件未执行的测试标记为 PASS。

# 十八、实际硬件测试

代码和构建完成后：

1. 如果需要重新刷 UF2，给出 UF2 的完整路径。
2. 暂停并明确提示用户刷入固件。
3. 用户确认固件已刷入后再执行硬件测试。
4. 如果当前环境已经可以直接访问 COM10、Zynq、Vivado 和 bitstream，则实际运行一键测试。
5. 不要在连接 Zynq 时执行 TDI/TDO 短接 loopback。
6. 不要修改或覆盖用户的 bitstream。
7. 不要编造 Vivado 下载时间。

# 十九、最终交付

必须交付：

```text
exlink_jtag_maxspeed.uf2
tools/exlink_maxspeed_test.py
tools/exlink_vivado_program.tcl
更新后的 tools/exlink_xvc_server.py
更新后的测试工具
新增软件测试
使用说明文档
测试报告
```

最终回复必须列出：

1. 修改的文件；
2. PIO safe/fast 的实际周期数；
3. DMA chunk 支持值；
4. 构建结果；
5. 软件测试结果；
6. UF2 路径；
7. 一键测试命令；
8. 硬件实测结果；
9. 最快稳定下载时间；
10. 是否达到 3.400 秒；
11. 若未达到，当前唯一最大的瓶颈。

不要只提供设计方案或伪代码，直接完成实现、构建、测试和报告。
