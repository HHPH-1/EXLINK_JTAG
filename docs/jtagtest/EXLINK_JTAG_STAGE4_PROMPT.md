继续开发 Exlink RP2040 JTAG Bridge。

当前 Stage 3 已经完成并通过真实硬件测试：

- USB CDC 正常
- info/capabilities 正常
- bitbang loopback 正常
- PIO 短 pattern 全部通过
- PIO 128-bit loopback 通过
- PIO boundary-test 覆盖 1..4096 bits 全部通过
- PIO 4096-bit × 1000 stress 通过
- 当前平均有效速率约 374005 bit/s
- Zynq JTAG scan 正常
- Vivado 可以识别目标并下载 bitstream
- 当前默认引擎为 pio

当前固定引脚定义，不允许更改：

CHAN0 / GPIO2 -> TMS
CHAN1 / GPIO3 -> TCK
CHAN2 / GPIO4 <- TDO
CHAN3 / GPIO5 -> TDI
GND           -> GND

TDI/TDO 回环测试接线：

CHAN3/TDI <-> CHAN2/TDO

现在实现 Stage 4：PIO + DMA 高速批量 JTAG。

==================================================
一、总体约束
==================================================

1. 保留当前 bitbang 引擎，禁止删除或重写现有稳定实现。
2. 保留当前无 DMA PIO 实现作为参考，不得破坏其位序、采样边沿和协议行为。
3. 默认 active engine 仍为 pio。
4. DMA 是 PIO 引擎的内部实现细节，不需要把 DMA 做成第三种用户引擎。
5. 不修改 pico_sdk_sigrok 原有逻辑分析仪 target。
6. 不整合逻辑分析仪/JTAG 双模式。
7. 不修改 ESP32、CH549 或其他 MCU。
8. 不引入 RTOS。
9. 不使用 malloc/calloc/realloc。
10. 所有缓冲区必须静态分配。
11. 必须继续支持任意 bit 数，不得只支持 8/32 bit 对齐长度。
12. 保持当前 TMS、TDI、TDO 的 LSB-first 规则完全不变。
13. 保持现有 USB CDC 命令向后兼容。
14. Stage 3 的测试命令必须继续正常工作。

==================================================
二、PIO DMA 引擎
==================================================

为 PIO JTAG TX 和 RX 分别配置 DMA 通道。

要求：

1. 初始化阶段申请两个 DMA channel：
   - 一个负责 memory -> PIO TX FIFO
   - 一个负责 PIO RX FIFO -> memory

2. 使用对应 PIO State Machine 的 DREQ：
   - DREQ_PIOx_TXn
   - DREQ_PIOx_RXn

3. 每次开始传输时：
   - 清理上一轮 DMA 状态；
   - 清理 PIO TX/RX FIFO；
   - 清理可能残留的 PIO IRQ；
   - 先启动 RX DMA；
   - 再启动 TX DMA；
   - 最后启动或触发 State Machine。

4. 必须确保 RX DMA 在第一个 TDO sample 到来前已经工作。

5. 等待 DMA 完成时必须带超时，禁止永久死循环。

6. 发生超时时：
   - abort TX DMA；
   - abort RX DMA；
   - disable PIO state machine；
   - 清空 FIFO；
   - 把 TCK 恢复为空闲低电平；
   - 返回明确错误状态。

7. DMA channel 在初始化时申请一次，禁止每次 shift 重复 claim/unclaim。

8. DMA 完成后必须验证：
   - TX DMA 已完成；
   - RX DMA 已完成；
   - PIO 没有残留未读取数据；
   - 返回的 TDO bit 数与请求一致。

9. 注意 RP2040 DMA 对齐和传输宽度。
   根据当前 PIO FIFO 数据格式选择 8、16 或 32 bit DMA，
   但必须在代码注释里说明选择原因。

10. 不要为了 DMA 强行改动已经验证通过的 JTAG edge timing。
    TCK、TMS、TDI 更新和 TDO 采样边沿必须与 Stage 3 一致。

==================================================
三、固定分块缓冲
==================================================

将外部可见的最大 Shift 长度提高到：

EXLINK_JTAG_MAX_SHIFT_BITS = 32768

但禁止一次性创建多份 32768 × 32-bit 的展开缓冲区。

采用固定大小 chunk，例如：

EXLINK_JTAG_DMA_CHUNK_BITS = 1024

或者根据当前 PIO 数据格式选择 1024/2048 bits。

要求：

1. 一个逻辑 shift 可以包含 1..32768 bits。
2. 内部按 chunk 顺序处理。
3. 最后一个 chunk 可以不是整字节长度。
4. 每个 chunk 都必须正确计算有效 bit 数。
5. 返回 TDO 时必须拼接到正确的 bit offset。
6. 最后一个字节中未使用的高位必须清零。
7. 分块边界不能改变 TAP 状态。
8. chunk 之间不能额外产生 TCK。
9. chunk 之间不能插入额外 TMS/TDI bit。
10. 对调用方而言，分块必须完全透明。

建议提供独立内部接口：

bool jtag_pio_shift_dma_chunk(
    const uint8_t *tms,
    const uint8_t *tdi,
    uint8_t *tdo,
    uint32_t source_bit_offset,
    uint32_t bit_count);

以及外部逻辑接口：

bool jtag_pio_shift(
    const uint8_t *tms,
    const uint8_t *tdi,
    uint8_t *tdo,
    uint32_t bit_count);

外部接口负责循环分块，DMA chunk 接口负责实际硬件传输。

不得改变已有公共接口，除非确实必要。
若修改公共接口，必须同步更新所有调用点。

==================================================
四、PIO 数据打包
==================================================

检查当前 PIO TX FIFO 的数据格式。

优先保持 Stage 3 已验证的数据格式，然后通过 DMA 搬运。

如果当前实现是“每个 JTAG bit 使用一个 32-bit FIFO word”，可以先在固定
chunk staging buffer 中展开，然后使用 DMA 传输，但必须满足：

1. staging buffer 只覆盖一个 chunk；
2. 禁止覆盖整个 32768-bit shift；
3. TX/RX staging buffer 总内存占用必须在注释或文档中列出；
4. 不得越界；
5. 正确处理 bit_count = 1；
6. 正确处理非 8/32 对齐长度。

不要在本阶段贸然重写为复杂的 2-bit packed PIO 程序，除非现有结构确实无法
使用 DMA。正确性优先于极限性能。

==================================================
五、时钟控制
==================================================

现有 bitbang 的：

--half-period-us

必须保留。

为 PIO 增加按实际 JTAG TCK 频率设置的能力，例如：

python sigrok-pico/tools/exlink_jtag_test.py --port COM10 clock-pio --khz 1000

支持范围建议：

50 kHz ～ 5000 kHz

要求：

1. 根据当前 PIO 程序每个 JTAG bit 消耗的 PIO cycle 数计算 clock divider。
2. 禁止在多个文件中散落 magic number。
3. 定义并注释：
   - PIO 每个 TCK 周期消耗多少条指令/周期；
   - divider 的计算方式；
   - 实际可达到的 TCK。
4. 返回实际设置后的频率，而不是只返回请求值。
5. 无效频率必须返回明确错误。
6. engine=bitbang 时继续使用 half-period-us。
7. engine=pio 时使用 PIO divider。
8. 切换 engine 后不得丢失各自已设置的时钟参数。
9. 初始默认 PIO TCK 设置为安全值，例如 500 kHz 或当前已经验证的频率。

==================================================
六、USB CDC 协议
==================================================

把 capability 中的最大 shift 更新为：

max_shift_bits=32768

版本字符串更新为：

EXLINK-RP2040-JTAG-BRIDGE v0.3

capabilities 至少应反映：

- bitbang
- pio
- dma
- max shift 32768 bits

要求：

1. 保持旧的 4096-bit 请求兼容。
2. 不改变现有请求和响应的位序。
3. 一个 shift 请求必须对应一个完整响应。
4. 出错时不能只返回部分 TDO 数据而不标记错误。
5. 对以下情况做长度检查：
   - bit_count = 0
   - bit_count > 32768
   - TMS 数据不足
   - TDI 数据不足
   - USB frame 不完整
6. 计算：
   byte_count = (bit_count + 7) / 8
7. 所有长度计算使用足够宽的无符号类型。
8. 检查整数溢出和数组越界。
9. USB 接收可以分多次到达，不得假设一次 tud_cdc_read 就获得完整 frame。
10. USB 响应尽可能一次批量写出，不要逐字节发送。

==================================================
七、PC 端 Python 工具
==================================================

修改：

sigrok-pico/tools/exlink_jtag_test.py

以及现有 XVC server/bridge 脚本。

要求：

1. max shift 更新为 32768 bits。
2. serial.write() 尽量整帧发送。
3. 实现 read_exact()，处理串口分段返回。
4. 不允许假设一次 serial.read() 返回完整响应。
5. 使用 bytes/bytearray/memoryview，避免逐 bit Python 循环。
6. XVC 收到大于固件 max shift 的请求时，自动按 32768 bits 分块。
7. 分块时正确切割 TMS/TDI bitstream。
8. 拼接 TDO 时保持原始 bit 顺序。
9. 非字节对齐的 XVC shift 也必须正确。
10. socket 接收也必须使用 read_exact 类似逻辑。
11. 处理客户端断开、串口超时和协议错误。
12. 错误后关闭当前 XVC connection，但不要让整个 server 崩溃。

增加或扩展以下测试：

boundary-test

边界至少覆盖：

1
2
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
255
256
257
511
512
513
1023
1024
1025
2047
2048
2049
4095
4096
4097
8191
8192
8193
16383
16384
16385
32767
32768

增加 DMA stress，例如：

python sigrok-pico/tools/exlink_jtag_test.py --port COM10 stress --bits 32768 --count 1000

stress 输出：

- engine
- DMA 是否启用
- bit count
- count
- total bits
- total time
- effective bit/s
- average request latency
- PASS/FAIL

增加 benchmark，例如：

python sigrok-pico/tools/exlink_jtag_test.py --port COM10 benchmark

benchmark 分别测试：

128
512
4096
8192
16384
32768 bits

每种长度至少运行 100 次，输出有效 bit/s。

==================================================
八、XVC 兼容性
==================================================

保持现有 Vivado XVC 行为不变。

必须继续支持：

getinfo:
settck:
shift:

要求：

1. getinfo 返回的最大长度应与固件能力一致。
2. settck 必须真正设置 PIO TCK，而不是只在 PC 端保存数值。
3. 若请求频率超出硬件支持范围，返回最接近的实际周期。
4. shift 数据必须保持 XVC 的 LSB-first 规则。
5. XVC 大 shift 可以在 PC 端分块，但不得改变最终 TDO 内容。
6. 同一个 shift 内的分块之间不得 reset TAP。
7. 不要在每个 XVC shift 前后自动执行 TAP reset。
8. 不要因为 DMA 改变 Vivado 已验证可用的 TAP 操作序列。

==================================================
九、测试和验收
==================================================

完成代码后首先执行软件构建。

必须确认：

1. pico_sdk_sigrok 原 target 仍能编译。
2. exlink_jtag_bridge target 能编译。
3. 生成 exlink_jtag_bridge.uf2。
4. 无新增编译 warning。
5. 无未使用 DMA channel 或 PIO state machine。

硬件测试分四组。

A. TDI/TDO 短接测试：

- info
- capabilities
- bitbang 128-bit loopback
- PIO 128-bit loopback
- boundary-test 到 32768
- 32768-bit loopback
- 32768-bit × 1000 stress
- benchmark

B. 不同时钟测试：

分别在以下频率执行 4096-bit loopback：

100 kHz
500 kHz
1000 kHz
2000 kHz
5000 kHz

5 MHz 不稳定不应影响前面较低频率的通过结果。
记录每个频率是否通过。

C. Zynq 测试：

拔掉 TDI/TDO 短接线并连接 Zynq：

- reset
- 连续执行 10 次 scan --bits 128
- 比较 10 次结果必须一致
- 在 500 kHz、1 MHz、2 MHz 下分别测试
- 先不要把 5 MHz 作为必须通过条件

D. Vivado 测试：

- Hardware Manager 正常连接
- 正常识别 Zynq 器件
- 下载 bitstream
- 连续下载至少 10 次
- 每次均成功
- 测试 reconnect
- 测试关闭再打开 Hardware Manager
- 确认 Vivado 操作期间固件没有重启或卡死

==================================================
十、性能目标
==================================================

正确性是硬性要求。

性能目标：

1. 32768-bit stress 必须无数据错误。
2. 相比 Stage 3 的约 374005 bit/s 不得退步。
3. 在合适的 PIO TCK 下，目标有效速率至少达到 1 Mbit/s。
4. 目标是 Stage 3 的 2 倍以上。
5. 若没有达到目标，不要隐藏结果。
6. 分别报告：
   - PIO 设置的 TCK；
   - 纯固件 shift 时间；
   - USB 往返时间；
   - PC/XVC 总时间；
   - 有效 bit/s。
7. 根据数据判断瓶颈位于：
   - PIO
   - DMA
   - USB CDC
   - Python/pyserial
   - XVC TCP

==================================================
十一、文档
==================================================

更新 Exlink JTAG 文档，包含：

1. Stage 4 架构。
2. DMA TX/RX 数据流。
3. 固定 chunk 大小。
4. 静态 RAM 使用量。
5. PIO divider 与 TCK 的计算方式。
6. 最大 shift 32768 bits。
7. 新增命令示例。
8. benchmark 示例。
9. bitbang 回退方式。
10. 故障恢复流程。

给出简单数据流图：

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

==================================================
十二、最终输出
==================================================

完成后输出：

1. 修改和新增的文件列表。
2. DMA channel 和 PIO SM 使用情况。
3. 每个静态缓冲区的大小。
4. 总静态 RAM 增量估算。
5. 新旧协议兼容性说明。
6. PIO TCK 计算说明。
7. Windows PowerShell 构建命令。
8. 完整硬件测试命令。
9. 尚未在真实硬件验证的项目。
10. 不要声称硬件测试已经通过，只能说明代码和构建结果。