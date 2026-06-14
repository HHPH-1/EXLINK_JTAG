在当前 `sigrok-pico` 本地工作区和当前分支上，直接完成 Exlink 多功能固件和 Windows 图形管理软件。

不要只给分析或伪代码。需要完成代码、构建、软件测试、可执行文件打包，并在硬件环境可用时执行模式切换、示波器和 JTAG 回归测试。

# 一、当前稳定状态

当前已有两个独立且可工作的功能：

## 示波器/逻辑分析仪模式

基于现有：

```text
pico_sdk_sigrok
```

保留现有：

* sigrok/PulseView 协议；
* 数字逻辑分析；
* 模拟 ADC 示波器；
* PIO + DMA 采样；
* 原有通道和采样行为。

## JTAG模式

当前稳定配置：

```text
Firmware: EXLINK-RP2040-JTAG-MAXSPEED v0.4
Engine: pio_safe
PIO: 5 cycles/bit
clk_sys: 125000000 Hz
Default TCK: 12500000 Hz
DMA chunk: 8192 bit
Maximum shift: 131072 bit
```

实际验证：

```text
Zynq IDCODE: 0x23727093
Vivado 10/10 下载 PASS
Serial timeouts = 0
Protocol errors = 0
DMA timeouts = 0
PIO recoveries = 0
USB disconnects = 0
```

不要继续优化下载速度。

固定 JTAG 引脚：

```text
CHAN0 / GPIO2 -> TMS
CHAN1 / GPIO3 -> TCK
CHAN2 / GPIO4 <- TDO
CHAN3 / GPIO5 -> TDI
GND           -> GND
```

禁止修改这些引脚。

# 二、最终交付目标

制作一个统一固件：

```text
exlink_multifunction.uf2
```

统一固件包含两个互斥工作模式：

```text
SCOPE 模式
JTAG 模式
```

同时制作 Windows 图形管理程序：

```text
ExlinkManager.exe
```

管理程序功能：

```text
自动检测 Exlink
显示当前工作模式
切换到示波器模式
切换到 JTAG 模式
启动/停止 XVC Server
显示 XVC 日志
配置 TCK、XVC 地址和端口
打开 PulseView
处理 RP2040 重启和 USB 重新枚举
```

最终用户不需要安装 Python，双击 EXE 即可使用。

# 三、模式行为

## 默认模式

RP2040 每次真正断电后重新上电，必须默认进入：

```text
SCOPE
```

包括：

* 第一次刷入 UF2；
* 拔掉 USB 后重新插入；
* 完全断电后重新供电；
* 普通上电复位。

## 软件切换到 JTAG

管理软件发送切换命令：

```text
SCOPE -> JTAG
```

固件执行：

```text
确认当前模式处于空闲状态
发送切换确认
等待 USB 数据发送完成
记录临时 JTAG 启动标记
watchdog reboot
重新枚举 USB
进入 JTAG 模式
```

## 软件切换回 SCOPE

管理软件必须先停止 XVC Server，然后发送：

```text
JTAG -> SCOPE
```

固件执行相同的临时模式记录和 watchdog reboot，重新进入示波器模式。

## 断电恢复

禁止把用户选择的 JTAG 模式永久写入 Flash。

使用 watchdog scratch register 或等效的掉电不保持机制：

* 仅 watchdog 软件重启时读取临时模式；
* 冷启动、掉电启动、brownout 或无有效 magic 时进入 SCOPE；
* 不允许反复擦写 Flash；
* 不允许因为上一次处于 JTAG 模式，下一次重新插 USB 仍自动进入 JTAG。

# 四、固件架构

新增统一入口：

```text
multifunction/
├── main.c
├── mode_boot.c
├── mode_boot.h
├── mode_control.c
├── mode_control.h
└── mode_workspace.c
```

推荐结构：

```c
int main(void)
{
    ExlinkMode mode = exlink_mode_select_on_boot();

    if (mode == EXLINK_MODE_JTAG) {
        return exlink_jtag_mode_main();
    }

    return exlink_scope_mode_main();
}
```

把原有两个 `main()` 改为：

```c
int exlink_scope_mode_main(void);
int exlink_jtag_mode_main(void);
```

要求：

* 一个启动周期只初始化一个模式；
* SCOPE 模式不得初始化 JTAG PIO/DMA；
* JTAG 模式不得初始化示波器 ADC/PIO/DMA；
* 不做运行时原地热切换；
* 模式切换统一通过 watchdog reboot；
* 原独立 JTAG 和示波器 target 继续保留并能构建；
* 新增独立 `exlink_multifunction` target。

# 五、RAM共享与资源隔离

当前示波器需要约 220000 字节采样缓冲区，JTAG 需要最大 Shift 对应的 TMS、TDI、TDO 和 DMA 缓冲区。

禁止将两套大缓冲区同时作为独立静态数组放入 BSS。

实现共享模式工作区，例如：

```c
#define EXLINK_MODE_WORKSPACE_BYTES 220000u

static uint8_t g_mode_workspace[EXLINK_MODE_WORKSPACE_BYTES]
    __attribute__((aligned(4)));
```

或者使用等效的 union/arena 方案。

要求：

* SCOPE 模式把工作区用作 capture buffer；
* JTAG 模式从同一工作区划分 TMS、TDI、TDO、PIO TX、PIO RX 和临时缓冲区；
* 所有分区具有边界检查和静态断言；
* 不使用重叠后仍被访问的指针；
* 不在热路径反复 malloc/free；
* 构建输出 Flash、RAM、BSS 和剩余堆栈空间；
* 示波器模式必须保留原有采样缓冲区大小；
* JTAG 最大 Shift 必须保持 131072 bit；
* 保留足够栈空间；
* 不得超过 RP2040 RAM。

如果使用动态内存方案，也必须保证未选中的模式不分配大缓冲区，并加入分配失败处理，不能静默死机。

# 六、统一模式控制协议

在两个模式中实现完全相同的管理命令。

使用不会与 sigrok 和 JTAG 二进制协议冲突的保留控制帧。建议在命令边界识别以下 ASCII 帧：

```text
@EXLINK:INFO\n
@EXLINK:MODE?\n
@EXLINK:MODE:JTAG\n
@EXLINK:MODE:SCOPE\n
@EXLINK:BOOTSEL\n
```

响应格式：

```text
@EXLINK:OK:INFO:<version>:<mode>\n
@EXLINK:OK:MODE:SCOPE\n
@EXLINK:OK:MODE:JTAG\n
@EXLINK:OK:SWITCHING:JTAG\n
@EXLINK:OK:SWITCHING:SCOPE\n
@EXLINK:ERR:BUSY\n
@EXLINK:ERR:BAD_COMMAND\n
```

要求：

1. 只有在协议命令边界识别 `@EXLINK:`。
2. 不得在 JTAG Shift payload 中误识别。
3. 不得把 sigrok 正常命令误判为管理命令。
4. SCOPE 正在采样时，切换命令返回 `BUSY`。
5. JTAG 正在处理 Shift 时，不执行切换。
6. 切换前先发送完整 ACK 并 flush。
7. ACK 后留出短暂发送时间再 watchdog reboot。
8. 查询模式不得影响当前协议状态。
9. `BOOTSEL` 需要单独确认参数，避免误进入刷机模式。
10. 增加协议版本字段。

推荐增加：

```text
@EXLINK:CAPS?\n
```

返回：

```text
@EXLINK:OK:CAPS:SCOPE,JTAG,XVC,PIO,DMA\n
```

# 七、USB设备行为

尽可能保持：

```text
VID:PID = 2E8A:000A
```

以及稳定的 USB serial number，减少 Windows 每次切换后生成新 COM 口的概率。

要求：

* 模式切换重启后允许 USB 暂时消失；
* 管理软件等待设备重新枚举；
* 不能假设重新枚举后 COM 号码一定不变；
* 通过 VID、PID、USB serial number 和协议查询重新定位设备；
* 保留手动选择 COM 的功能；
* 如果 COM 被 PulseView、其他串口软件或旧 XVC Server 占用，显示明确错误；
* 不强行终止不属于本程序的进程。

如果两个模式必须使用不同 USB product string，也要保持相同 USB serial number，并由管理程序重新匹配。

# 八、Windows 图形管理程序

新建：

```text
tools/exlink_manager/
├── main.py
├── app.py
├── device_manager.py
├── mode_protocol.py
├── xvc_controller.py
├── pulseview_controller.py
├── settings.py
├── logging_setup.py
├── resources/
├── requirements.txt
└── ExlinkManager.spec
```

使用：

```text
Python 3
PySide6
pyserial
PyInstaller
```

UI 不得使用 Tk 主线程执行阻塞串口或 XVC操作。

## 主界面

至少包含以下区域。

### 设备状态

显示：

```text
设备：Exlink RP2040
连接状态
COM 端口
VID/PID
固件版本
当前模式
USB serial number
```

按钮：

```text
刷新设备
重新连接
```

### 工作模式

两个明显按钮：

```text
切换到示波器模式
切换到 JTAG 下载器
```

当前模式用状态标签显示：

```text
示波器模式
JTAG 模式
切换中
设备离线
设备忙
```

### JTAG/XVC

配置项：

```text
监听地址：127.0.0.1
XVC 端口：2542
TCK：12500 kHz
Engine：pio_safe
DMA chunk：8192 bit
Maximum shift：131072 bit
```

按钮：

```text
启动 XVC Server
停止 XVC Server
复制 Vivado 连接地址
```

显示：

```text
XVC Server 状态
Vivado 客户端连接状态
Shift 请求数
Total shifted bits
Serial timeouts
Protocol errors

vivado客户端中连接xvc Jtag命令
```

默认连接地址：

```text
localhost:2542
```

### 示波器

按钮：

```text
切换到示波器并打开 PulseView
仅切换到示波器
选择 PulseView 路径
```

不要重新实现完整波形显示界面。

示波器实际显示继续使用 PulseView；本软件负责：

* 切换固件模式；
* 检查设备已经进入 SCOPE；
* 启动用户选择的 PulseView；
* 保存 PulseView 路径；
* PulseView 未安装时给出安装/路径提示。

### 日志区

实时显示：

```text
设备发现
COM 打开和关闭
模式查询
模式切换
USB 消失
USB 重新枚举
XVC 启动
Vivado 连接
XVC 错误
PulseView 启动
```

支持：

```text
清空日志
复制日志
打开日志目录
```

# 九、管理软件操作逻辑

## 启动 XVC

用户点击：

```text
启动 XVC Server
```

软件自动执行：

```text
检测设备
查询当前模式
如果是 SCOPE：
    确认当前不在采样
    发送 MODE:JTAG
    关闭旧串口
    等待 USB 消失
    等待重新枚举
    重新定位 COM
    验证 MODE=JTAG
启动内置 XVC Server
更新 UI 状态
```

模式切换等待时间默认 15 秒，过程中 UI 不能卡死。

## 切换示波器

用户点击：

```text
切换到示波器模式
```

软件自动执行：

```text
如果 XVC Server 正在运行：
    正常停止 XVC Server
等待工作线程退出
发送 MODE:SCOPE
等待 USB 重新枚举
确认 MODE=SCOPE
```

## 打开 PulseView

执行：

```text
确保处于 SCOPE
确保 XVC 已停止
启动 PulseView
```

如果 COM 被当前管理软件占用，启动 PulseView 前必须关闭管理串口。

# 十、XVC Server重构

现有：

```text
tools/exlink_xvc_server.py
```

必须保留命令行用法，同时重构为可被 GUI 导入的模块。

建议提供：

```python
@dataclass
class XvcServerConfig:
    serial_port: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 2542
    tck_khz: int = 12500
    engine: str = "pio_safe"
    dma_chunk_bits: int = 8192
    max_shift_bits: int = 131072

class ExlinkXvcServer:
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def wait(self) -> None: ...
    def get_status(self) -> XvcStatus: ...
```

要求：

* GUI 中直接调用模块，不启动另一个 Python 解释器；
* XVC Server 运行在工作线程；
* 支持干净停止；
* 关闭监听 socket；
* 关闭客户端 socket；
* 关闭串口；
* 停止后可以再次启动；
* 日志通过 callback/signal 发送到 UI；
* 保留原 CLI 参数；
* 默认使用已验证的 12.5 MHz 配置；
* 不把 maxspeed 自动扫描功能放入普通 UI。

# 十一、线程和稳定性

使用 Qt Signal/Slot 和工作线程。

禁止：

* 在 UI 主线程中等待 COM；
* 在 UI 主线程中运行 XVC accept/recv；
* 在 UI 主线程中 sleep 等待重新枚举；
* 从工作线程直接修改 Qt 控件；
* 强制 kill 正在写串口的线程。

必须支持：

* 用户在切换期间取消；
* 软件正常退出时停止 XVC；
* 软件异常退出后下次可以重新打开设备；
* XVC 客户端异常断开后 Server 继续监听；
* COM 消失后状态变为离线；
* 设备重新插入后自动重新检测；
* 多个匹配设备时要求用户选择；
* 所有串口打开均设置 timeout；
* 不吞掉异常。

# 十二、配置保存

设置保存在：

```text
%APPDATA%\ExlinkManager\settings.json
```

保存：

```text
最近使用的设备 serial number
手动 COM 选择
XVC host
XVC port
TCK
PulseView 路径
窗口位置和大小
日志级别
```

不要把运行模式永久写入设置后在设备上电时自动恢复 JTAG。

设备上电默认 SCOPE 是固件硬性规则。

# 十三、EXE打包

提供：

```text
tools/exlink_manager/build_exlink_manager.ps1
tools/exlink_manager/ExlinkManager.spec
```

构建输出：

```text
dist/ExlinkManager.exe
```

要求：

* 用户电脑不需要安装 Python；
* 使用 PyInstaller one-file；
* GUI 模式不显示控制台窗口；
* 保留文件日志；
* 包含 PySide6、pyserial 和程序资源；
* EXE 启动失败时能在日志目录留下错误；
* 显示应用版本；
* Windows 10 和 Windows 11 x64 可运行；
* 不依赖当前源码目录；
* 不写死开发者电脑绝对路径；
* 不把 Vivado、PulseView 本体打包进 EXE；
* PulseView 路径由用户选择；
* Vivado 只需要通过 localhost:2542 连接，不由 UI 自动启动。

同时可以额外生成便于排错的：

```text
dist/ExlinkManager/
```

one-folder 版本，但最终必须有单文件 EXE。

# 十四、构建目标

保留并验证：

```text
pico_sdk_sigrok
exlink_jtag_bridge
exlink_jtag_maxspeed
```

新增：

```text
exlink_multifunction
```

输出：

```text
exlink_multifunction.uf2
```

多功能固件必须使用当前已经验证的 JTAG代码，不得回退：

```text
pio_safe
12.5 MHz
DMA chunk 8192 bit
Maximum shift 131072 bit
```

# 十五、软件测试

新增测试至少覆盖：

```text
冷启动默认 SCOPE
无效 scratch 默认 SCOPE
watchdog scratch 请求 JTAG
watchdog scratch 请求 SCOPE
控制协议 INFO
控制协议 MODE?
控制协议 MODE:JTAG
控制协议 MODE:SCOPE
SCOPE busy 时拒绝切换
JTAG busy 时拒绝切换
管理命令不会污染 sigrok 命令
管理命令不会污染 JTAG Shift payload
USB 重新枚举设备匹配
COM 号码变化后重新发现
XVC Server start/stop/restart
XVC client disconnect 后重新监听
GUI 后台线程退出
配置保存和读取
PulseView 路径处理
PyInstaller hidden imports
```

执行：

```text
CMake configure
示波器 target 构建
JTAG target 构建
maxspeed target 构建
multifunction target 构建
Python py_compile
Python unittest
PyInstaller one-file 构建
```

# 十六、硬件验收

在硬件可用时依次执行。

## 冷启动

1. 完全拔掉 Exlink USB。
2. 重新插入。
3. 查询模式。
4. 必须返回 SCOPE。

## 示波器

1. PulseView 能识别 Exlink。
2. 数字通道能采样。
3. 模拟通道能采样。
4. 连续采样能停止。
5. 采样性能不能明显低于整合前。

## 切换到 JTAG

1. UI 点击切换。
2. USB 重新枚举。
3. UI 自动找回设备。
4. 当前模式返回 JTAG。
5. 启动 XVC。
6. Vivado 能识别 `0x23727093`。
7. 下载同一 bitstream PASS。
8. XVC 停止后可以重新启动。

## 切换回示波器

1. UI 停止 XVC。
2. 切换 SCOPE。
3. USB 重新枚举。
4. PulseView 再次识别并采样。

## 循环切换

执行至少：

```text
SCOPE -> JTAG -> SCOPE
```

20 次。

要求：

```text
无 USB 永久丢失
无 COM 永久占用
无 DMA/PIO 锁死
无必须重新刷 UF2 的情况
```

## 断电默认模式

设备处于 JTAG 时直接拔掉 USB，再重新插入。

必须进入：

```text
SCOPE
```

# 十七、文档

新增：

```text
docs/MultifunctionFirmware.md
docs/ExlinkManager.md
```

说明：

* 模式架构；
* 上电默认模式；
* 软件切换流程；
* USB 重新枚举；
* UI 使用方法；
* Vivado 连接方法；
* PulseView 路径配置；
* UF2 刷写方法；
* EXE 构建方法；
* 故障恢复方法；
* COM 被占用的处理；
* 如何手动回到 SCOPE；
* 如何进入 BOOTSEL。

# 十八、最终交付

必须交付：

```text
exlink_multifunction.uf2
ExlinkManager.exe
PyInstaller spec
PowerShell 构建脚本
统一模式控制协议
GUI 源代码
重构后的 XVC Server
软件测试
硬件测试记录
使用文档
```

最终回复列出：

1. 修改文件；
2. 多功能固件架构；
3. 模式记录和冷启动判断方式；
4. RAM共享方式；
5. 固件构建结果；
6. 固件 RAM/Flash 占用；
7. 软件测试结果；
8. EXE 完整路径；
9. UF2 完整路径；
10. 硬件测试结果；
11. 冷启动是否默认 SCOPE；
12. PulseView 是否正常；
13. Vivado 下载是否正常；
14. 20 次模式切换是否通过；
15. 已知限制。

不得把未执行的硬件测试标记为 PASS。
