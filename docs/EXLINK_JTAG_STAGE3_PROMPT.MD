# Exlink JTAG 第三阶段 A：实现无 DMA 的 PIO JTAG 基础引擎

## 一、当前状态

当前 bitbang JTAG 已经完成真实硬件验证：

* USB CDC 正常；
* GPIO回环正常；
* 可以稳定读取 Zynq JTAG 链；
* 可以通过 Vivado XVC 枚举 XC7Z020；
* 可以正常下载 bitstream；
* 引脚已经改为新的最终映射。

当前最终引脚映射：

```text
CHAN0 / GPIO2  → TMS
CHAN1 / GPIO3  → TCK
CHAN2 / GPIO4  ← TDO
CHAN3 / GPIO5  → TDI
GND            ↔ GND
```

对应宏定义：

```c
#define EXLINK_JTAG_TMS_GPIO    2u
#define EXLINK_JTAG_TCK_GPIO    3u
#define EXLINK_JTAG_TDO_GPIO    4u
#define EXLINK_JTAG_TDI_GPIO    5u
```

GPIO4/TDO必须始终保持输入，不能被PIO或CPU配置成输出。

---

# 二、本次修改范围

本次只完成：

1. 保留现有bitbang引擎；
2. 新增PIO JTAG引擎；
3. 支持bitbang/PIO运行时切换；
4. PIO第一版使用CPU搬运FIFO，不使用DMA；
5. PIO固定先测试1 MHz；
6. 保持现有USB CDC最大Shift长度4096 bit；
7. 修改PC测试工具，支持选择引擎；
8. 完成回环、边界长度和Zynq IDCODE扫描测试。

本次不要修改：

* XVC Server；
* XVC `settck`实现；
* 最大Shift长度；
* USB协议的4096 bit限制；
* 逻辑分析仪固件；
* Vivado下载流程；
* DMA；
* USB Composite；
* 原bitbang时序。

不要删除或重写已经验证成功的bitbang实现。

---

# 三、代码结构

建议增加：

```text
exlink_jtag_bridge/
├─ jtag_engine.c
├─ jtag_engine.h
├─ jtag_bitbang.c
├─ jtag_bitbang.h
├─ jtag_pio.c
├─ jtag_pio.h
├─ jtag_pio.pio
├─ jtag_protocol.c
└─ main.c
```

如果当前bitbang代码仍然叫：

```text
jtag_gpio.c
jtag_gpio.h
```

可以保留原文件名，不需要为了重命名进行大规模修改。

增加统一引擎接口：

```c
typedef enum
{
    JTAG_ENGINE_BITBANG = 0,
    JTAG_ENGINE_PIO = 1
} JtagEngineType_t;

bool jtag_engine_init(void);

bool jtag_engine_select(JtagEngineType_t type);

JtagEngineType_t jtag_engine_get_active(void);

bool jtag_engine_shift_bits(uint32_t bit_count,
                            const uint8_t *tms_bits,
                            const uint8_t *tdi_bits,
                            uint8_t *tdo_bits);

void jtag_engine_tap_reset(void);
```

`jtag_protocol.c`只能调用统一引擎接口，不能直接调用PIO或GPIO底层函数。

上电默认引擎暂时保持：

```text
BITBANG
```

避免新固件上电后直接破坏现有稳定功能。

---

# 四、增加引擎切换命令

USB CDC增加命令：

```text
M uint8 engine
```

参数：

```text
0 = bitbang
1 = PIO
```

返回：

```text
m
uint8 status
uint8 active_engine
```

状态定义：

```text
0 = 成功
1 = 参数非法
2 = 引擎初始化失败
3 = 引擎切换失败
```

PIO初始化失败时保持bitbang引擎，不得让设备进入无可用JTAG引擎的状态。

增加查询命令：

```text
Q
```

返回：

```text
q
uint8 status
uint8 active_engine
uint8 supported_engine_flags
uint32 max_shift_bits
```

其中：

```text
supported_engine_flags bit0 = bitbang
supported_engine_flags bit1 = PIO
max_shift_bits = 4096
```

这一版本暂时不加入时钟范围和协议版本等其他字段，避免一次改动过大。

---

# 五、PIO引脚配置

PIO使用：

```text
GPIO2～GPIO5
```

作为连续4位引脚窗口。

配置概念：

```c
sm_config_set_out_pins(&config, EXLINK_JTAG_TMS_GPIO, 4);
sm_config_set_in_pins(&config, EXLINK_JTAG_TDO_GPIO);
```

引脚方向必须分别配置：

```c
pio_sm_set_consecutive_pindirs(
    pio,
    sm,
    EXLINK_JTAG_TMS_GPIO,
    2,
    true);
```

对应：

```text
GPIO2/TMS = 输出
GPIO3/TCK = 输出
```

然后：

```c
pio_sm_set_consecutive_pindirs(
    pio,
    sm,
    EXLINK_JTAG_TDO_GPIO,
    1,
    false);
```

对应：

```text
GPIO4/TDO = 输入
```

然后：

```c
pio_sm_set_consecutive_pindirs(
    pio,
    sm,
    EXLINK_JTAG_TDI_GPIO,
    1,
    true);
```

对应：

```text
GPIO5/TDI = 输出
```

GPIO4即使位于PIO的4位OUT窗口中，也必须保持输入方向。PIO可以改变其输出锁存值，但绝对不能实际驱动GPIO4。

PIO初始化后的物理状态：

```text
TCK = 0
TMS = 1
TDI = 0
TDO = 输入，无上下拉
```

---

# 六、PIO输出数据格式

新映射：

```text
GPIO2 = TMS
GPIO3 = TCK
GPIO4 = TDO
GPIO5 = TDI
```

PIO每次执行：

```pio
out pins, 4
```

对应4位数据：

```text
bit0 → GPIO2 / TMS
bit1 → GPIO3 / TCK
bit2 → GPIO4 / 保留，物理保持输入
bit3 → GPIO5 / TDI
```

每一个JTAG bit需要两个nibble：

## TCK低电平nibble

```c
low_nibble =
    ((uint32_t)tms << 0) |
    ((uint32_t)tdi << 3);
```

其中：

```text
bit1/TCK = 0
bit2/TDO保留 = 0
```

## TCK高电平nibble

```c
high_nibble =
    low_nibble |
    (1u << 1);
```

发送顺序：

```text
low_nibble
high_nibble
```

因此：

```text
1个JTAG bit = 8个PIO TX bit
1个32位TX word = 4个JTAG bit
```

增加静态TX打包缓冲区：

```c
#define EXLINK_JTAG_MAX_SHIFT_BITS    4096u

#define EXLINK_JTAG_PIO_TX_WORDS \
    ((EXLINK_JTAG_MAX_SHIFT_BITS + 3u) / 4u)

static uint32_t g_pio_tx_words[EXLINK_JTAG_PIO_TX_WORDS];
```

禁止动态内存。

---

# 七、TX打包要求

实现：

```c
static uint32_t jtag_pio_pack_tx(uint32_t bit_count,
                                 const uint8_t *tms_bits,
                                 const uint8_t *tdi_bits,
                                 uint32_t *tx_words,
                                 uint32_t tx_word_capacity);
```

外部TMS/TDI输入仍然使用：

```text
LSB-first
```

读取单bit：

```c
static uint8_t get_packed_bit(const uint8_t *buffer,
                              uint32_t bit_index)
{
    return (buffer[bit_index >> 3] >>
            (bit_index & 7u)) & 1u;
}
```

每个JTAG bit向PIO TX流依次加入：

```text
low nibble
high nibble
```

禁止直接假设bit_count一定是4、8、16或32的整数倍。

TX缓冲区最后不足32位的部分补0，但PIO实际执行次数必须严格等于bit_count，不能因为补0产生额外JTAG时钟。

---

# 八、PIO程序要求

PIO程序需要：

1. 从FIFO获取本次bit_count；
2. 用X寄存器保存循环次数；
3. 每次循环消费两个4位nibble；
4. 先输出低电平nibble；
5. 给TMS/TDI建立时间；
6. 输出高电平nibble；
7. 在TCK高电平期间采样GPIO4/TDO；
8. 循环bit_count次；
9. 结束后将不足32位的ISR内容push；
10. 最终确保TCK恢复低电平。

概念程序：

```pio
.program exlink_jtag

pull block
mov x, osr

.wrap_target
out pins, 4
nop
out pins, 4
in pins, 1
jmp x-- loop
.wrap
```

上面只是概念，实际实现需要正确处理：

```text
bit_count
X寄存器减计数
最后一位
RX autopush
尾部不足32 bit
程序结束
```

禁止未经验证直接照抄概念代码。

推荐传入：

```text
bit_count - 1
```

使循环正好执行bit_count次。

PIO程序结束时可以无条件执行一次：

```pio
push
```

用于刷新最后不足32 bit的TDO。

如果bit_count正好是32的整数倍，无条件push可能产生一个额外空word。C端可以读取并丢弃该额外word，但必须设计明确，不能留下RX FIFO残留。

更推荐设计成：

```text
C端明确计算PIO实际返回word数
每次shift完成后彻底清空RX FIFO
```

---

# 九、PIO移位函数

实现：

```c
bool jtag_pio_shift_bits(uint32_t bit_count,
                         const uint8_t *tms_bits,
                         const uint8_t *tdi_bits,
                         uint8_t *tdo_bits);
```

参数检查：

```text
bit_count必须在1～4096
指针不能为NULL
```

开始前：

```c
pio_sm_set_enabled(pio, sm, false);
pio_sm_clear_fifos(pio, sm);
pio_sm_restart(pio, sm);
```

然后：

1. 清空TDO输出缓冲区；
2. 打包TX words；
3. 配置并启动PIO；
4. 先发送bit_count计数；
5. 再发送TX words；
6. 同时读取RX FIFO；
7. 等待PIO完成；
8. 禁用SM；
9. 将TCK强制恢复低电平；
10. 解包TDO；
11. 清空多余RX数据。

第一版不使用DMA。

---

# 十、CPU FIFO搬运要求

PIO TX FIFO和RX FIFO都很小，不能先一次性写完全部TX数据再读取RX，否则可能死锁。

必须用一个pump循环同时处理：

```text
TX FIFO有空间 → 写入下一个TX word
RX FIFO有数据 → 读取下一个RX word
```

概念：

```c
while (!transfer_complete)
{
    if ((tx_index < tx_word_count) &&
        !pio_sm_is_tx_fifo_full(pio, sm))
    {
        pio_sm_put(pio, sm, tx_words[tx_index]);
        tx_index++;
    }

    if ((rx_index < expected_rx_words) &&
        !pio_sm_is_rx_fifo_empty(pio, sm))
    {
        rx_words[rx_index] = pio_sm_get(pio, sm);
        rx_index++;
    }

    if (absolute_time_diff_us(start, get_absolute_time()) >
        timeout_us)
    {
        abort transfer;
    }

    tight_loop_contents();
}
```

不能无限阻塞。

4096 bit在1 MHz下理论JTAG时间约4 ms，考虑软件和FIFO搬运，建议单次PIO shift超时先设置为：

```text
100 ms
```

发生超时后：

```text
禁用SM
清FIFO
TCK恢复低电平
返回false
```

---

# 十一、TDO打包要求

外部返回的TDO格式保持：

```text
LSB-first
```

必须保证：

```text
TDO bit 0 → tdo_bits[0] bit0
TDO bit 1 → tdo_bits[0] bit1
...
```

PIO ISR内部使用左移还是右移均可，但最终USB返回的数据必须和现有bitbang完全一致。

禁止只测试IDCODE看起来正确就认为位序正确。

必须进行：

```text
TDI → TDO回环逐bit完全比较
```

---

# 十二、TAP Reset

PIO模式的TAP Reset可以先复用统一的Shift接口：

TMS序列：

```text
1,1,1,1,1,1,0
```

TDI全部为0。

也可以暂时使用bitbang实现Reset，但需要在代码中明确说明。

推荐所有引擎分别实现Reset：

```c
void jtag_bitbang_tap_reset(void);
void jtag_pio_tap_reset(void);
```

PIO Reset完成后必须进入：

```text
Run-Test/Idle
```

---

# 十三、第一版时钟

PIO第一版固定标称：

```text
1 MHz
```

根据：

```text
RP2040系统时钟
PIO每个JTAG bit实际使用的指令周期数
```

计算PIO clock divider。

不要假设PIO的一条指令等于一个完整JTAG周期。

增加函数：

```c
bool jtag_pio_set_frequency_hz(uint32_t requested_hz,
                               uint32_t *actual_hz);
```

本次先只在内部调用：

```c
jtag_pio_set_frequency_hz(1000000u, &actual_hz);
```

不需要修改XVC settck。

初始化时保存实际频率，供PC查询或日志使用。

---

# 十四、PC测试工具修改

修改：

```text
tools/exlink_jtag_test.py
```

增加：

```powershell
python tools\exlink_jtag_test.py --port COM10 capabilities
```

输出：

```text
Active engine: bitbang
Supported engines: bitbang, pio
Maximum shift: 4096 bits
```

增加引擎切换：

```powershell
python tools\exlink_jtag_test.py --port COM10 engine bitbang
python tools\exlink_jtag_test.py --port COM10 engine pio
```

增加带长度的回环测试：

```powershell
python tools\exlink_jtag_test.py --port COM10 loopback --bits 33
```

`loopback`默认仍可使用原长度，但必须支持用户指定1～4096 bit。

提示文字使用新的接线：

```text
Temporarily connect CHAN3/TDI to CHAN2/TDO.
Remove the jumper before connecting a target.
```

---

# 十五、测试顺序

## 测试1：bitbang回归

烧录新固件后：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 info
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine bitbang
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 reset
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 4096
```

必须继续PASS。

## 测试2：PIO基础回环

临时连接：

```text
CHAN3/TDI → CHAN2/TDO
```

执行：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine pio
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 128
```

必须PASS。

## 测试3：边界长度

依次测试：

```text
1
2
3
4
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
4095
4096
```

可以给测试工具增加：

```powershell
python tools\exlink_jtag_test.py --port COM10 boundary-test
```

自动完成以上长度。

每个长度使用至少3组不同测试数据：

```text
全0
全1
固定伪随机序列
```

所有测试必须逐bit一致。

## 测试4：连续压力测试

PIO回环：

```text
4096 bit
连续1000次
```

统计：

```text
成功次数
失败次数
总耗时
平均有效bit rate
```

任何一次错误都算失败。

## 测试5：Zynq扫描

拆掉回环线，连接Zynq。

执行：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine pio
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 reset
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 scan --bits 128
```

连续扫描20次。

每次必须稳定得到：

```text
0x23727093
0x4BA00477
```

---

# 十六、验收标准

本次阶段完成必须满足：

```text
[1] 原bitbang代码没有被删除
[2] bitbang回归测试通过
[3] 可以通过USB命令切换bitbang和PIO
[4] GPIO2=TMS
[5] GPIO3=TCK
[6] GPIO4=TDO且始终为输入
[7] GPIO5=TDI
[8] PIO固定1 MHz运行
[9] PIO 128 bit回环通过
[10] 所有边界长度测试通过
[11] 4096 bit连续回环1000次无错误
[12] PIO连续扫描Zynq 20次结果一致
[13] 每次shift结束后TCK保持低电平
[14] 超时后PIO和FIFO可以恢复
[15] pico_sdk_sigrok原目标仍能编译
[16] exlink_jtag_bridge目标正常编译
```

---

# 十七、完成报告

最终报告列出：

1. 新增和修改的文件；
2. PIO程序结构；
3. GPIO方向配置；
4. TX nibble打包格式；
5. TDO解包方式；
6. PIO实际频率；
7. bitbang回归结果；
8. 边界长度测试结果；
9. 1000次压力测试结果；
10. Zynq连续20次扫描结果；
11. 两个构建目标的编译结果；
12. 尚未实现的DMA、XVC时钟控制和32768 bit Shift。

不得在未执行真实测试时声称测试通过。
