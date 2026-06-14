# 新增硬性要求：更改 Exlink JTAG 引脚映射

原有 JTAG 引脚映射全部废弃。固件、测试工具、文档和接线说明统一改成以下定义：

```text
Exlink CHAN0 / GPIO2  → TMS
Exlink CHAN1 / GPIO3  → TCK
Exlink CHAN2 / GPIO4  ← TDO
Exlink CHAN3 / GPIO5  → TDI
Exlink GND            ↔ GND
```

对应方向：

```text
GPIO2 / TMS：输出
GPIO3 / TCK：输出
GPIO4 / TDO：输入
GPIO5 / TDI：输出
```

## 1. C 宏定义

统一使用：

```c
#define EXLINK_JTAG_TMS_GPIO    2u
#define EXLINK_JTAG_TCK_GPIO    3u
#define EXLINK_JTAG_TDO_GPIO    4u
#define EXLINK_JTAG_TDI_GPIO    5u
```

禁止继续保留旧定义：

```c
GPIO2 = TCK
GPIO3 = TMS
GPIO4 = TDI
GPIO5 = TDO
```

检查整个仓库，删除或更新所有旧映射，包括：

```text
boards/exlink_rp2040.h
jtag_bitbang.c
jtag_gpio.c
jtag_pio.c
jtag_pio.pio
测试脚本提示信息
README
docs
接线表
注释
错误排查说明
```

## 2. GPIO初始化状态

初始化后必须满足：

```text
TCK = 0
TMS = 1
TDI = 0
TDO = 输入、无内部上拉、无内部下拉
```

示例逻辑：

```c
gpio_init(EXLINK_JTAG_TMS_GPIO);
gpio_set_dir(EXLINK_JTAG_TMS_GPIO, GPIO_OUT);
gpio_put(EXLINK_JTAG_TMS_GPIO, 1);

gpio_init(EXLINK_JTAG_TCK_GPIO);
gpio_set_dir(EXLINK_JTAG_TCK_GPIO, GPIO_OUT);
gpio_put(EXLINK_JTAG_TCK_GPIO, 0);

gpio_init(EXLINK_JTAG_TDI_GPIO);
gpio_set_dir(EXLINK_JTAG_TDI_GPIO, GPIO_OUT);
gpio_put(EXLINK_JTAG_TDI_GPIO, 0);

gpio_init(EXLINK_JTAG_TDO_GPIO);
gpio_set_dir(EXLINK_JTAG_TDO_GPIO, GPIO_IN);
gpio_disable_pulls(EXLINK_JTAG_TDO_GPIO);
```

GPIO4/TDO 在任何情况下都不能被配置为输出。

## 3. Bitbang时序

`jtag_clock_bit()` 保持以下时序，只修改实际引脚：

```text
1. TCK保持低电平；
2. 设置GPIO2/TMS；
3. 设置GPIO5/TDI；
4. 等待低半周期；
5. GPIO3/TCK拉高；
6. 等待高半周期；
7. 读取GPIO4/TDO；
8. GPIO3/TCK拉低；
9. 返回TDO。
```

TAP Reset 仍然执行：

```text
TMS=1，至少6个TCK
TMS=0，输出1个TCK
最终进入Run-Test/Idle
```

## 4. 新的目标板接线

连接 Zynq 时使用：

```text
Exlink CHAN0 / GPIO2  → Zynq TMS
Exlink CHAN1 / GPIO3  → Zynq TCK
Exlink CHAN2 / GPIO4  ← Zynq TDO
Exlink CHAN3 / GPIO5  → Zynq TDI
Exlink GND            ↔ Zynq GND
```

默认不连接 Exlink 3V3。

继续保留电压警告：

```text
Exlink输出固定为3.3 V。
目标JTAG Bank必须兼容3.3 V。
1.8 V JTAG目标不能直接连接。
```

## 5. 新的回环测试接线

原来的：

```text
CHAN2 / TDI ↔ CHAN3 / TDO
```

已经失效。

新的回环接线必须改为：

```text
CHAN3 / GPIO5 / TDI
          ↘
CHAN2 / GPIO4 / TDO
```

即：

```text
CHAN3 ↔ CHAN2
```

数据方向为：

```text
CHAN3/TDI → CHAN2/TDO
```

测试工具提示改成：

```text
Temporarily connect CHAN3/TDI to CHAN2/TDO.
Remove the jumper before connecting a target.
```

## 6. PIO实现必须重新设计

新映射中：

```text
GPIO2 = TMS
GPIO3 = TCK
GPIO4 = TDO
GPIO5 = TDI
```

TMS 和 TDI 不连续，因此不能继续使用旧方案：

```text
side-set GPIO2作为TCK
OUT GPIO3和GPIO4作为TMS/TDI
```

旧 PIO 方案必须删除或重写。

推荐将 GPIO2～GPIO5 作为一个连续的4位 PIO 输出窗口：

```c
sm_config_set_out_pins(&config, 2, 4);
sm_config_set_in_pins(&config, 4);
```

但必须保持引脚方向：

```text
GPIO2：输出
GPIO3：输出
GPIO4：输入
GPIO5：输出
```

可以分别配置：

```c
pio_sm_set_consecutive_pindirs(pio, sm, 2, 2, true);
pio_sm_set_consecutive_pindirs(pio, sm, 4, 1, false);
pio_sm_set_consecutive_pindirs(pio, sm, 5, 1, true);
```

虽然 `out pins, 4` 会更新 GPIO2～GPIO5 的输出锁存器，但 GPIO4 必须始终保持输入方向，因此不能实际驱动 TDO。

### PIO四位输出格式

每个输出 nibble 定义为：

```text
bit0 = GPIO2 / TMS
bit1 = GPIO3 / TCK
bit2 = GPIO4 / 保留，物理引脚保持输入
bit3 = GPIO5 / TDI
```

每个 JTAG bit 生成两个 nibble。

TCK低电平阶段：

```c
low_nibble =
    ((uint32_t)tms << 0) |
    ((uint32_t)tdi << 3);
```

TCK高电平阶段：

```c
high_nibble =
    low_nibble |
    (1u << 1);
```

发送顺序必须为：

```text
low_nibble
high_nibble
```

PIO配置为 LSB-first，使低电平 nibble 先被输出。

每个 JTAG bit 对应：

```text
4 bit低电平输出
4 bit高电平输出
```

因此：

```text
1个JTAG bit = 8个PIO TX bit
1个32位TX word = 4个JTAG bit
```

修改所有缓冲区大小和 DMA 长度计算，禁止继续按照：

```text
1个JTAG bit = 2个PIO TX bit
```

计算。

### PIO概念时序

PIO程序至少完成：

```pio
out pins, 4       ; 输出TCK=0、TMS和TDI
nop               ; 建立时间
out pins, 4       ; 输出TCK=1
in pins, 1        ; 从GPIO4采样TDO
nop               ; 高电平保持
```

每个 shift 结束后必须确保：

```text
GPIO3/TCK = 0
```

不得停在高电平。

## 7. 版本标识

由于物理接线发生不兼容变化，更新固件版本字符串，例如：

```text
EXLINK-RP2040-JTAG-BRIDGE v0.2
```

文档必须明确说明：

```text
v0.1 pin map:
CHAN0=TCK, CHAN1=TMS, CHAN2=TDI, CHAN3=TDO

v0.2 pin map:
CHAN0=TMS, CHAN1=TCK, CHAN2=TDO, CHAN3=TDI
```

新版本文档和控制台提示默认只展示 v0.2 接线，避免用户误接。

## 8. 实施顺序

不能同时修改引脚和首次加入 PIO 后直接测试 Vivado。

必须按照以下顺序：

### 阶段A：只修改bitbang引脚

暂时保持现有 GPIO bitbang 引擎，不加入 PIO。

验证：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 info
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 reset
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback
```

回环线使用：

```text
CHAN3/TDI → CHAN2/TDO
```

### 阶段B：扫描目标板

按照新接线连接 Zynq，连续扫描三次：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 scan --bits 128
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 scan --bits 128
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 scan --bits 128
```

预期仍然稳定读取：

```text
0x23727093
0x4BA00477
```

### 阶段C：Vivado验证

启动 XVC Server，确认：

```text
Vivado能够枚举xc7z020
能够正常下载bitstream
```

只有新的 bitbang 映射全部通过后，才开始实现 PIO 引擎。

### 阶段D：实现PIO

PIO必须使用新的 GPIO2～GPIO5 nibble 打包方案，不得恢复或依赖旧引脚顺序。

## 9. 验收标准

```text
[1] 仓库内不存在生效的旧引脚定义
[2] GPIO2=TMS
[3] GPIO3=TCK
[4] GPIO4=TDO且始终为输入
[5] GPIO5=TDI
[6] 新回环接线CHAN3→CHAN2测试通过
[7] bitbang模式可以稳定读取两个IDCODE
[8] bitbang模式可以通过Vivado下载bitstream
[9] PIO模式使用新的4位nibble输出格式
[10] PIO模式不能驱动GPIO4/TDO
[11] PIO模式边界长度测试全部通过
[12] 文档、注释和测试提示全部使用新映射
```
