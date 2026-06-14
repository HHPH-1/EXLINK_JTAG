Stage 4 可以正式封版，下一项进入：

# Stage 5：XVC 与 Vivado 完整兼容和长期稳定性

这一阶段不再验证 PIO/DMA 本身是否正确，而是把整个链路从“能下载”提升到“可以日常反复使用”：

```text
Vivado
  ↓ XVC/TCP
Python XVC Server
  ↓ USB CDC
RP2040 PIO + DMA
  ↓ JTAG
Zynq
```

大纲中 Stage 5 的核心验收包括连续扫描、连续下载、断开重连、目标重新上电，以及服务端在客户端异常退出后继续运行。

## 先封存 Stage 4

```powershell
git add .
git commit -m "stage4: validate PIO DMA JTAG bridge"
git tag exlink-jtag-stage4-v0.3-stable
```

同时保留已验证的 UF2：

```text
exlink_jtag_bridge_stage4_v0.3_20260614.uf2
```

Stage 5 修改主要集中在 PC 端 XVC Server 和通信容错。除非发现明确缺陷，不修改已经通过验证的 PIO、DMA、JTAG 边沿和引脚定义。

---

# Stage 5A：完善 XVC 协议

必须完整支持：

```text
getinfo:
settck:
shift:
```

## `getinfo:`

返回：

```text
xvcServer_v1.0:32768
```

其中 `32768` 必须和固件 `max shift` 一致。

## `settck:`

要求：

* Vivado 发送周期值；
* Python 将周期换算成目标频率；
* 通过 USB CDC 命令真正修改 RP2040 PIO divider；
* 固件返回实际设置频率；
* Python 将实际周期返回 Vivado；
* 超出范围时使用最接近的可用值；
* 不允许只在 Python 中保存频率而不修改硬件。

## `shift:`

要求：

* 正确读取 `bit_count`；
* 正确计算 `(bit_count + 7) // 8`；
* 完整读取 TMS；
* 完整读取 TDI；
* 调用固件 Shift；
* 完整返回 TDO；
* 保持 LSB-first；
* 非整字节长度正确；
* 最后一个字节未使用高位清零；
* 单个 XVC 请求不能自动 TAP reset。

---

# Stage 5B：TCP 和串口健壮性

当前最重要的是消除“一次 read 就能收到完整数据”的假设。

Python 中统一实现：

```python
def recv_exact(sock, length):
    ...
```

和：

```python
def serial_read_exact(ser, length):
    ...
```

要求：

* TCP 数据可以分多次到达；
* 串口响应可以分多次到达；
* 返回空数据代表连接关闭；
* 超时必须抛出明确异常；
* 不允许使用不完整数据继续解析；
* 不允许把上一帧残留数据当作下一帧；
* 客户端异常断开时只关闭当前连接；
* XVC Server 主进程继续监听下一个 Vivado 连接。

Server 外层结构应类似：

```text
初始化串口
    ↓
监听 TCP 2542
    ↓
接受一个 Vivado 客户端
    ↓
处理 getinfo / settck / shift
    ↓
客户端断开或发生协议错误
    ↓
关闭当前 socket
    ↓
重新等待下一客户端
```

不要因为一次 Vivado 断连就退出整个 Python 进程。

---

# Stage 5C：固件能力同步

XVC Server 连接 RP2040 后，启动阶段先读取：

```text
info
capabilities
active engine
max shift
DMA enabled
PIO TCK
```

不能在 Python 中硬编码固件能力。

启动输出建议为：

```text
Exlink XVC Server
Firmware: EXLINK-RP2040-JTAG-BRIDGE v0.3
Serial port: COM10
Engine: pio
DMA: enabled
Maximum shift: 32768 bits
PIO TCK: 1000 kHz
Listening on 0.0.0.0:2542
```

若固件最大 Shift 小于 Server 默认值，以固件返回值为准。

---

# Stage 5D：大请求分块

Vivado 请求超过固件最大 Shift 时，PC 端自动分块：

```text
XVC logical shift
      ↓
最多 32768-bit 子请求
      ↓
依次发送到 RP2040
      ↓
拼接完整 TDO
```

要求：

* 分块不得插入额外 TCK；
* 分块不得 TAP reset；
* TMS/TDI 位序保持连续；
* 非字节对齐分块正确；
* 下一块起始 bit 不能错误地按字节边界截断；
* 最终 TDO 长度必须与原始 XVC 请求完全一致。

不过正常情况下，应优先让 XVC `getinfo` 宣告 `32768`，让 Vivado自行控制请求长度。

---

# Stage 5E：日志和真实负载统计

在不影响传输的前提下，增加可关闭的统计模式。

至少记录：

```text
XVC client address
getinfo count
settck count
shift request count
total shifted bits
minimum shift bits
maximum shift bits
average shift bits
serial timeout count
protocol error count
client reconnect count
```

客户端断开时输出本次会话总结：

```text
Session summary:
  Shift requests: 12584
  Total bits: 98765432
  Maximum shift: 32768
  Average shift: 7848
  Duration: 48.2 s
  Effective rate: 2.05 Mbit/s
  Errors: 0
```

该统计将作为后续 Stage 5.5 性能优化的依据。

---

# 给 Codex 的实施约束

```text
开始 Stage 5：XVC 和 Vivado 完整兼容及长期稳定性。

当前 Stage 4 已完成，稳定基线为：

- Firmware v0.3
- PIO + DMA
- DMA chunk 2048 bits
- max shift 32768 bits
- 5 MHz loopback PASS
- boundary-test 1..32768 PASS
- 32768 × 1000 stress PASS
- 实测约 770.5 kbit/s
- Zynq 和 Vivado Stage 4 回归已通过

本阶段禁止主动重写：

- PIO program
- DMA数据格式
- JTAG边沿时序
- bitbang实现
- 固定引脚定义

固定引脚：

CHAN0 / GPIO2 -> TMS
CHAN1 / GPIO3 -> TCK
CHAN2 / GPIO4 <- TDO
CHAN3 / GPIO5 -> TDI

本阶段重点修改 PC 端 XVC Server 和必要的通信容错逻辑。

完成以下工作：

1. 完整实现并检查 XVC：
   - getinfo:
   - settck:
   - shift:

2. getinfo 返回：
   xvcServer_v1.0:32768

3. settck 必须：
   - 解析 Vivado 请求周期；
   - 调用固件 PIO 时钟命令；
   - 返回固件实际设置后的周期；
   - 不允许只在 PC 端保存。

4. shift 必须：
   - 使用 recv_exact 读取 socket；
   - 使用 serial_read_exact 读取串口；
   - 支持任意 bit 数；
   - 支持非整字节长度；
   - 保持 LSB-first；
   - 不自动 reset TAP；
   - 最后一个字节无效高位清零。

5. 增加健壮的：
   - socket recv_exact()
   - serial read_exact()
   - serial write_all()
   - client disconnect handling
   - serial timeout handling
   - malformed command handling

6. Vivado断开后：
   - 关闭当前client socket；
   - 保留串口；
   - XVC Server继续监听；
   - 不退出主程序。

7. 协议或串口错误后：
   - 输出明确日志；
   - 关闭当前XVC客户端；
   - 尝试恢复固件通信；
   - 返回监听状态。

8. Server启动时读取固件：
   - info
   - capabilities
   - engine
   - max shift
   - PIO clock

9. 不在多个文件重复硬编码32768。

10. 增加会话统计：
   - shift次数
   - total bits
   - min/max/average shift
   - 会话时间
   - effective bit/s
   - timeout和protocol error次数

11. 支持Ctrl+C优雅关闭：
   - 关闭client socket
   - 关闭listen socket
   - 关闭serial port

12. 保持现有命令行参数兼容。

13. 构建固件目标，确认Stage 4固件没有被破坏。

14. 输出：
   - 修改文件列表
   - XVC协议处理流程
   - 异常恢复流程
   - PowerShell启动命令
   - Vivado Tcl连接命令
   - 完整测试步骤
   - 尚未由真实硬件验证的内容

不要声称真实Vivado测试已经通过。
```

---

# Stage 5 第一轮测试

## 1. 启动 Server

脚本名按仓库现有名称替换：

```powershell
python sigrok-pico\tools\exlink_xvc_server.py --port COM10 --host 127.0.0.1 --tcp-port 2542
```

## 2. Vivado 连接

```tcl
open_hw_manager
connect_hw_server
open_hw_target -xvc_url localhost:2542
```

确认：

* Zynq 正常出现；
* IDCODE 正确；
* Server 不报半包或长度错误；
* `settck` 日志显示固件实际 TCK 已改变。

## 3. 连续下载

```tcl
set_property PROGRAM.FILE {你的bitstream.bit} [current_hw_device]

for {set i 1} {$i <= 20} {incr i} {
    puts "===== Program $i / 20 ====="
    program_hw_devices [current_hw_device]
}
```

Stage 5 建议把 Stage 4 的 10 次提高到 **20 次**。

## 4. 重连测试

依次执行：

1. 关闭 Hardware Manager；
2. 确认 Python Server 仍在运行；
3. 重新打开 Hardware Manager；
4. 再次连接同一个 `localhost:2542`；
5. 再下载一次 bitstream；
6. 完整重复 10 轮。

## 5. Vivado异常退出测试

在连接期间直接关闭 Vivado。

预期：

```text
Client disconnected
Session summary ...
Waiting for XVC client...
```

重新打开 Vivado 后应能再次连接，不重启 Python、不重插 Exlink。

## 6. 长时间测试

保持 Hardware Manager 连接至少 30 分钟，期间每隔一段时间执行扫描或下载。

通过标准：

* COM10 不消失；
* Python 不退出；
* 固件不重启；
* DMA/PIO 不锁死；
* Vivado 不出现随机 unknown device；
* 断开后能够重新连接。

---

# Stage 5 完成标准

全部满足后才能进入 Stage 5.5：

```text
XVC getinfo 正确
XVC settck 真正控制硬件
XVC shift 位序和长度正确
TCP半包处理正确
串口半包处理正确
Vivado连续下载20次通过
Hardware Manager重连10轮通过
Vivado异常退出后Server继续运行
长时间连接无死锁
获得真实Shift长度分布和会话性能数据
```

此阶段仍然以**稳定性和真实负载测量**为主，不继续追逐 1 Mbit/s。Stage 5 验收后，再根据统计结果进入 Stage 5.5 专项加速。
