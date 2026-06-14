# 第二阶段：Exlink RP2040 JTAG Bridge + PC端 XVC Server

## 目标

在当前已经完成 Exlink 硬件适配的 sigrok-pico Fork 中，新增一个独立的 RP2040 JTAG Bridge 固件目标，并新增 PC 端 XVC Server，使 Vivado Hardware Manager 可以通过 Exlink 连接目标 Zynq JTAG。

本阶段必须实现和验证：

1. RP2040 USB CDC 能正常枚举；
2. GPIO2～GPIO5 能产生和读取 JTAG 信号；
3. PC 工具可以通过 USB CDC 下发原始 TMS/TDI 位流；
4. RP2040 返回 TDO；
5. PC 端 Python XVC Server 可以监听 `127.0.0.1:2542` 并桥接 USB CDC；
6. Vivado 可以通过 XVC 连接该 Server；
7. Vivado Hardware Manager 能枚举目标 Zynq 的 JTAG 链并读取有效 IDCODE。

本阶段第一版不实现：

* 逻辑分析仪/JTAG 双模式合并；
* Flash 模式保存；
* USB Composite；
* PIO JTAG；
* ESP32 控制；
* RP2040 端直接实现 TCP/XVC；
* 自动烧录 FPGA bitstream。

最低硬件验收是 Vivado `open_hw_target` 成功并读到非全 0、非全 1 的 JTAG IDCODE。Vivado `program_hw_devices` 下载 bitstream 作为扩展验证项，不作为第一轮必须通过项。

先使用 GPIO bitbang 跑通，XVC 协议由 PC 端 Python 程序转换为 RP2040 的 USB CDC 二进制 JTAG 协议。

---

# 一、Git 分支

从第一阶段已经验证和烧录成功的版本创建新分支。

先确认当前状态：

```bash
git status
git log --oneline --decorate -10
git tag
```

如果已有第一阶段稳定标签，例如：

```text
exlink-la-baseline-v0.1
```

则从该标签创建分支：

```bash
git checkout -b feature/exlink-jtag-bridge exlink-la-baseline-v0.1
```

如果没有标签，则从当前已经验证的 Exlink 分支创建：

```bash
git checkout -b feature/exlink-jtag-bridge
```

不要直接在 `main` 修改。

---

# 二、保持原逻辑分析仪目标不变

现有：

```text
pico_sdk_sigrok
```

必须继续保持可编译。

新增第二个目标：

```text
exlink_jtag_bridge
```

最终应同时生成：

```text
pico_sdk_sigrok.uf2
exlink_jtag_bridge.uf2
```

不要把 JTAG 代码直接塞进现有 `pico_sdk_sigrok.c` 主循环。

---

# 三、建议文件结构

在现有 `pico_sdk_sigrok` 目录内增加：

```text
pico_sdk_sigrok/
├─ exlink_jtag_bridge/
│  ├─ main.c
│  ├─ jtag_gpio.c
│  ├─ jtag_gpio.h
│  ├─ jtag_protocol.c
│  ├─ jtag_protocol.h
│  ├─ usb_cdc_transport.c
│  └─ usb_cdc_transport.h
├─ boards/
│  └─ exlink_rp2040.h
└─ CMakeLists.txt
```

PC 工具放到：

```text
tools/
├─ exlink_jtag_test.py
└─ exlink_xvc_server.py
```

`exlink_jtag_test.py` 用于底层 USB CDC、GPIO loopback 和 IDCODE 诊断。`exlink_xvc_server.py` 用于连接 Vivado。

如果当前工程已有更合适的目录结构，可以按现有风格调整，但不要把所有代码写进单个 `main.c`。

---

# 四、实际硬件引脚

必须使用 Exlink 原理图确认的映射：

```c
#define EXLINK_JTAG_TCK_GPIO    2u   /* CHAN0，RP2040输出 */
#define EXLINK_JTAG_TMS_GPIO    3u   /* CHAN1，RP2040输出 */
#define EXLINK_JTAG_TDI_GPIO    4u   /* CHAN2，RP2040输出 */
#define EXLINK_JTAG_TDO_GPIO    5u   /* CHAN3，RP2040输入 */
```

物理接线：

```text
Exlink CHAN0 / GPIO2 → 目标板 TCK
Exlink CHAN1 / GPIO3 → 目标板 TMS
Exlink CHAN2 / GPIO4 → 目标板 TDI
Exlink CHAN3 / GPIO5 ← 目标板 TDO
Exlink GND            ↔ 目标板 GND
```

不要使用 GPIO0、GPIO1。

不要把 RP2040 自身的 SWD/SWCLK 当成目标 JTAG。

## 电压安全

Exlink 的 RP2040 JTAG输出是固定 3.3 V，没有目标电压检测和自动电平转换。

必须在文档中警告：

* 目标 JTAG 电平必须兼容 3.3 V；
* 如果目标 JTAG Bank 是1.8 V，不能直接连接；
* 默认只连接 TCK、TMS、TDI、TDO、GND；
* 不要默认把 Exlink 3V3 和目标板 VREF/3V3直接相连；
* 只有明确需要由 Exlink 给目标供电并确认电源允许时，才可以连接3V3。

---

# 五、JTAG GPIO驱动

实现接口：

```c
void jtag_gpio_init(void);

void jtag_gpio_deinit(void);

void jtag_set_half_period_us(uint32_t half_period_us);

uint32_t jtag_get_half_period_us(void);

uint8_t jtag_clock_bit(uint8_t tms, uint8_t tdi);

void jtag_tap_reset(void);

bool jtag_shift_bits(uint32_t bit_count,
                     const uint8_t *tms_bits,
                     const uint8_t *tdi_bits,
                     uint8_t *tdo_bits);
```

初始化状态：

```text
TCK = 0
TMS = 1
TDI = 0
TDO = 输入
```

TDO 默认不启用内部上拉和下拉。

建议输出配置：

```c
gpio_set_drive_strength(pin, GPIO_DRIVE_STRENGTH_4MA);
gpio_set_slew_rate(pin, GPIO_SLEW_RATE_SLOW);
```

第一版默认：

```c
#define JTAG_DEFAULT_HALF_PERIOD_US  2u
```

大约是低速测试用途，不追求精确频率。

`jtag_clock_bit()` 时序：

```text
1. TCK保持低电平；
2. 设置TMS；
3. 设置TDI；
4. 等待半周期；
5. TCK拉高；
6. 等待半周期；
7. 读取TDO；
8. TCK拉低；
9. 返回TDO。
```

TDO数据按每字节 LSB first 保存：

```c
bit_value = (buffer[bit_index >> 3] >> (bit_index & 7u)) & 1u;
```

`jtag_tap_reset()`：

```text
TMS=1，输出至少6个TCK
然后TMS=0，输出1个TCK
最终进入Run-Test/Idle
```

---

# 六、USB CDC实现要求

可以沿用现有 Pico SDK/TinyUSB CDC 栈。

CMake保持：

```cmake
pico_enable_stdio_usb(exlink_jtag_bridge 1)
pico_enable_stdio_uart(exlink_jtag_bridge 0)
```

USB二进制协议中禁止使用：

```c
printf()
puts()
```

普通调试文字不能写入同一个 CDC 数据流，否则会破坏协议。

如需要调试日志，使用编译宏：

```c
#define EXLINK_JTAG_DEBUG 0
```

默认关闭。

使用：

```c
tud_ready()
tud_task()
tud_cdc_available()
tud_cdc_read()
tud_cdc_write()
tud_cdc_write_flush()
```

实现：

```c
bool usb_cdc_read_exact(uint8_t *buffer,
                        size_t length,
                        uint32_t timeout_ms);

bool usb_cdc_write_all(const uint8_t *buffer,
                       size_t length,
                       uint32_t timeout_ms);
```

要求：

* 不能无限阻塞；
* USB断开或数据超时后，协议状态机回到等待命令状态；
* 不使用动态内存；
* 不信任PC发送的长度；
* 所有多字节整数使用小端格式。

---

# 七、二进制协议

定义：

```c
#define EXLINK_JTAG_MAX_SHIFT_BITS   4096u
#define EXLINK_JTAG_MAX_SHIFT_BYTES  \
    ((EXLINK_JTAG_MAX_SHIFT_BITS + 7u) / 8u)
```

使用静态缓冲区：

```c
static uint8_t tms_buffer[EXLINK_JTAG_MAX_SHIFT_BYTES];
static uint8_t tdi_buffer[EXLINK_JTAG_MAX_SHIFT_BYTES];
static uint8_t tdo_buffer[EXLINK_JTAG_MAX_SHIFT_BYTES];
```

不要使用 `malloc()`。

## 7.1 设备信息命令

PC发送：

```text
'I'
```

RP2040返回：

```text
'i'
uint16_t text_length
text[text_length]
```

字符串：

```text
EXLINK-RP2040-JTAG-BRIDGE v0.1
```

## 7.2 TAP复位命令

PC发送：

```text
'T'
```

RP2040执行 `jtag_tap_reset()`。

返回：

```text
't'
uint8_t status
```

其中：

```text
status = 0：成功
status != 0：失败
```

## 7.3 Shift命令

PC发送：

```text
'S'
uint32_t bit_count
tms_bits[(bit_count + 7) / 8]
tdi_bits[(bit_count + 7) / 8]
```

要求：

```text
bit_count范围：1～4096
位顺序：LSB first
```

RP2040返回：

```text
's'
uint8_t status
uint32_t bit_count
tdo_bits[(bit_count + 7) / 8]
```

如果长度非法：

```text
status = 1
```

如果接收超时：

```text
status = 2
```

如果执行失败：

```text
status = 3
```

非法命令返回：

```text
'e'
uint8_t error_code
```

## 7.4 时钟延时命令

PC发送：

```text
'K'
uint32_t half_period_us
```

允许范围：

```text
1～100 us
```

返回：

```text
'k'
uint8_t status
uint32_t applied_half_period_us
```

超过范围时返回错误，不得直接使用非法值。

---

# 八、主循环

`main.c`负责：

```c
int main(void)
{
    stdio_init_all();
    jtag_gpio_init();
    jtag_protocol_init();

    while (true)
    {
        tud_task();
        jtag_protocol_task();
        tight_loop_contents();
    }
}
```

不要在主循环中加入长时间阻塞。

收到完整Shift命令后允许同步执行bitbang，但协议接收必须有超时。

---

# 九、CMake目标

增加选项：

```cmake
option(EXLINK_BUILD_JTAG_BRIDGE
       "Build standalone Exlink RP2040 JTAG bridge"
       OFF)
```

当：

```cmake
SIGROK_BOARD_EXLINK=ON
EXLINK_BUILD_JTAG_BRIDGE=ON
```

时创建目标：

```cmake
add_executable(exlink_jtag_bridge
    exlink_jtag_bridge/main.c
    exlink_jtag_bridge/jtag_gpio.c
    exlink_jtag_bridge/jtag_protocol.c
    exlink_jtag_bridge/usb_cdc_transport.c
)
```

加入头文件路径：

```cmake
target_include_directories(exlink_jtag_bridge PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/boards
    ${CMAKE_CURRENT_LIST_DIR}/exlink_jtag_bridge
)
```

链接：

```cmake
target_link_libraries(exlink_jtag_bridge
    pico_stdlib
    pico_stdio_usb
    hardware_gpio
    hardware_sync
)
```

如果当前 Pico SDK 中 GPIO函数不需要单独的 `hardware_gpio`，按实际可用库调整，不要保留不存在的目标。

设置：

```cmake
pico_enable_stdio_usb(exlink_jtag_bridge 1)
pico_enable_stdio_uart(exlink_jtag_bridge 0)
pico_add_extra_outputs(exlink_jtag_bridge)
```

保持现有 `pico_sdk_sigrok` 目标完全可编译。

---

# 十、构建命令

使用独立构建目录：

```powershell
cmake -S pico_sdk_sigrok `
  -B build/exlink-jtag `
  -G Ninja `
  -DPICO_BOARD=pico `
  -DSIGROK_BOARD_EXLINK=ON `
  -DEXLINK_BUILD_JTAG_BRIDGE=ON `
  -Dpicotool_DIR=C:\Users\HHPH\Desktop\exlink_JTAG\rp2040\tools\picotool-2.1.0\picotool

cmake --build build/exlink-jtag --target exlink_jtag_bridge
```

预期输出：

```text
build/exlink-jtag/exlink_jtag_bridge.elf
build/exlink-jtag/exlink_jtag_bridge.bin
build/exlink-jtag/exlink_jtag_bridge.hex
build/exlink-jtag/exlink_jtag_bridge.uf2
```

同时验证原逻辑分析仪仍能编译：

```powershell
cmake --build build/exlink-jtag --target pico_sdk_sigrok
```

---

# 十一、PC底层测试脚本

新增：

```text
tools/exlink_jtag_test.py
```

依赖：

```powershell
py -m pip install pyserial
```

命令行：

```text
python exlink_jtag_test.py --port COM8 info
python exlink_jtag_test.py --port COM8 loopback
python exlink_jtag_test.py --port COM8 reset
python exlink_jtag_test.py --port COM8 scan --bits 128
python exlink_jtag_test.py --port COM8 clock --half-period-us 5
```

## 11.1 info

发送 `I`，验证返回：

```text
EXLINK-RP2040-JTAG-BRIDGE v0.1
```

## 11.2 loopback

测试前用户临时短接：

```text
CHAN2 / TDI → CHAN3 / TDO
```

不接目标板。

脚本发送已知伪随机位流：

```text
TMS全部为0
TDI为测试数据
```

验证返回TDO和TDI完全一致。

测试完成后提醒用户拆掉短路线。

## 11.3 scan

扫描流程：

1. 发送T命令复位TAP并进入Run-Test/Idle；
2. 发送TMS序列进入Shift-DR：

```text
1, 0, 0
```

3. 移入指定数量的0，同时读取TDO；
4. 最后一个数据位使用TMS=1，进入Exit1-DR；
5. 再输出：

```text
TMS=1 → Update-DR
TMS=0 → Run-Test/Idle
```

打印：

* 原始TDO字节；
* LSB-first位流；
* 每32位拆分出的十六进制值；
* 值是否可能是JTAG IDCODE。

候选IDCODE基础检查：

```text
bit0必须为1
不能是0x00000000
不能是0xFFFFFFFF
```

不要硬编码只有一个固定Zynq IDCODE，因为不同Zynq型号和JTAG链配置可能不同。

---

# 十二、PC端 XVC Server

新增：

```text
tools/exlink_xvc_server.py
```

依赖：

```powershell
py -m pip install pyserial
```

默认架构：

```text
Vivado Hardware Manager
        │
        │ XVC TCP，默认 127.0.0.1:2542
        ▼
tools/exlink_xvc_server.py
        │
        │ USB CDC 二进制协议
        ▼
exlink_jtag_bridge.uf2
        │
        │ GPIO2～GPIO5 bitbang JTAG
        ▼
目标 Zynq JTAG
```

启动方式：

```powershell
python tools/exlink_xvc_server.py --port COM8 --xvc-host 127.0.0.1 --xvc-port 2542
```

Vivado Tcl 连接方式：

```tcl
open_hw
connect_hw_server
open_hw_target -xvc_url localhost:2542
```

## 12.1 XVC命令

第一版实现 XVC 基本命令：

```text
getinfo:
settck:
shift:
```

`getinfo:` 返回：

```text
xvcServer_v1.0:4096\n
```

其中 `4096` 表示 XVC Server 对外声明的单次 shift 位数上限。

`settck:` 后跟 `uint32_t period_ns`，小端。XVC Server 将周期换算为固件的 `half_period_us`：

```text
half_period_us = ceil(period_ns / 2000)
```

并钳位到：

```text
1 us <= half_period_us <= 100 us
```

如果 Vivado 请求更快或更慢的 TCK，不直接失败；XVC Server 使用钳位后的实际值发送 RP2040 `K` 命令，并把实际周期以 `uint32_t actual_period_ns` 小端返回给 Vivado。

`shift:` 后跟：

```text
uint32_t bit_count
tms_bits[(bit_count + 7) / 8]
tdi_bits[(bit_count + 7) / 8]
```

XVC Server 将其转换为一个或多个 RP2040 `S` 命令，并返回：

```text
tdo_bits[(bit_count + 7) / 8]
```

位顺序保持 LSB first。

## 12.2 Shift分片

RP2040 固件单次 `S` 命令上限保持：

```text
EXLINK_JTAG_MAX_SHIFT_BITS = 4096
```

如果 Vivado 发来的 XVC `shift` 超过 4096 bit，PC 端 XVC Server 必须分片：

```text
XVC shift N bits
    ├─ CDC S 4096 bits
    ├─ CDC S 4096 bits
    └─ CDC S remaining bits
```

分片时必须保持原始 bit 顺序，按顺序拼接每个分片返回的 TDO，再统一返回给 Vivado。

## 12.3 日志与错误处理

XVC Server 默认打印简洁日志：

```text
串口打开状态
XVC监听地址
Vivado客户端连接/断开
settck请求值和实际值
shift分片统计
CDC协议错误
```

不要把大量 TMS/TDI/TDO 原始位流默认打印到控制台。需要时通过命令行参数启用，例如：

```powershell
python tools/exlink_xvc_server.py --port COM8 --verbose-bits
```

如果 USB CDC 超时、断开或 RP2040 返回错误，XVC Server 应关闭当前 Vivado TCP 连接，让 Vivado 重新连接；不要静默返回伪造的 TDO 数据。

---

# 十三、硬件测试顺序

完成代码和编译后，在文档中给出以下顺序。

## 测试A：USB

1. 烧录 `exlink_jtag_bridge.uf2`；
2. Windows设备管理器确认出现CDC COM口；
3. 执行：

```powershell
python tools/exlink_jtag_test.py --port COMx info
```

## 测试B：GPIO回环

先不连接Zynq。

临时连接：

```text
CHAN2 → CHAN3
```

运行：

```powershell
python tools/exlink_jtag_test.py --port COMx loopback
```

预期：

```text
PASS
```

完成后拆掉短接线。

## 测试C：示波器/逻辑分析仪观察

观察：

```text
CHAN0 = TCK
CHAN1 = TMS
CHAN2 = TDI
```

发送TAP reset和shift命令，确认：

* TCK空闲低；
* TMS/TDI在TCK上升沿前稳定；
* TDO在上升沿采样。

## 测试D：连接Zynq

连接前确认目标JTAG电压兼容3.3 V。

仅连接：

```text
CHAN0 → TCK
CHAN1 → TMS
CHAN2 → TDI
CHAN3 ← TDO
GND    ↔ GND
```

执行：

```powershell
python tools/exlink_jtag_test.py --port COMx reset
python tools/exlink_jtag_test.py --port COMx scan --bits 128
```

如果结果全为：

```text
0xFFFFFFFF
```

优先检查：

* TDO悬空；
* 目标板未供电；
* 没有共地；
* TDO接错；
* 目标JTAG电压不兼容。

如果全为：

```text
0x00000000
```

优先检查：

* TDO被拉低；
* TCK没有输出；
* TAP状态跳转错误；
* 引脚短路；
* 目标器件处于复位或JTAG禁用状态。

## 测试E：Vivado XVC连接

先确认测试A～D已经通过，尤其是底层 `scan` 已经读到候选 IDCODE。

启动 XVC Server：

```powershell
python tools/exlink_xvc_server.py --port COMx --xvc-port 2542
```

在 Vivado Tcl Console 中执行：

```tcl
open_hw
connect_hw_server
open_hw_target -xvc_url localhost:2542
```

硬性验收：

```text
Vivado open_hw_target 成功
Hardware Manager 能枚举 JTAG 链
能读到有效 IDCODE
```

扩展验证：

```text
program_hw_devices 下载 bitstream
refresh_hw_device 后状态正常
```

如果 Vivado 连接失败，优先检查：

* XVC Server 是否仍在运行；
* `localhost:2542` 是否被其他程序占用；
* COM口是否被 `exlink_jtag_test.py` 或串口工具占用；
* `settck` 是否被钳位到固件可接受范围；
* 底层 `scan` 是否仍能读到 IDCODE。

---

# 十四、文档

新增：

```text
docs/EXLINK_JTAG_BRIDGE_STAGE2.md
```

内容包括：

* 本阶段目标；
* GPIO映射；
* USB协议；
* 构建命令；
* UF2位置；
* 回环测试；
* Zynq接线；
* 电压警告；
* IDCODE扫描步骤；
* PC端 XVC Server 启动方式；
* Vivado Hardware Manager 连接方式；
* 已实现和暂未作为硬性验收的 Vivado 功能。

---

# 十五、提交

建议拆分提交：

```text
feat: add standalone Exlink JTAG bridge target
feat: add RP2040 GPIO bitbang JTAG engine
feat: add binary USB CDC JTAG protocol
test: add Exlink JTAG Python test client
feat: add Python XVC server for Vivado
docs: add stage 2 JTAG bridge validation guide
```

完成后：

```bash
git status
git diff --check
git log --oneline --decorate -10
git push -u origin feature/exlink-jtag-bridge
```

---

# 十六、完成报告

最终报告必须列出：

1. 修改和新增的文件；
2. 两个UF2的路径；
3. 原逻辑分析仪目标是否仍编译通过；
4. JTAG Bridge是否编译通过；
5. PC测试脚本使用方式；
6. XVC Server使用方式；
7. Vivado连接命令；
8. USB协议定义；
9. XVC协议实现范围；
10. 尚未执行的真实硬件测试；
11. 不得在未连接真实Exlink和Zynq时声称IDCODE读取成功；
12. 不得在未实际执行 Vivado 连接时声称 Vivado 已经枚举成功。

实际执行代码修改和编译，不要只输出设计方案。
