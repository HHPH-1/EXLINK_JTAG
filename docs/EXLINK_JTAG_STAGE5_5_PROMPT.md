# 任务：Exlink RP2040 JTAG Bridge Stage 5.5 性能剖析与低风险优化

继续开发 Exlink RP2040 JTAG Bridge。

本阶段目标是：在不破坏 Stage 5 稳定性的前提下，定位真实性能瓶颈，并实施可量化、可回退的低风险优化。

不要一开始就重写 PIO、DMA 数据格式或 USB 协议。必须先加入性能统计，得到真实数据后再选择优化方向。

---

# 一、当前稳定基线

当前已经完成并通过：

* Firmware：`EXLINK-RP2040-JTAG-BRIDGE v0.3`
* 当前引擎：PIO
* 支持：`bitbang, pio, dma`
* DMA chunk：2048 bits
* 固件最大 Shift：32768 bits
* PIO 最高已验证 TCK：5 MHz
* bitbang 回退正常
* boundary-test：1～32768 bits 全部通过
* 5 MHz、32768-bit loopback：PASS
* 32768 bits × 1000 stress：PASS
* 大块回环有效速率：约 770.5 kbit/s
* Zynq JTAG 扫描正常
* Vivado 可识别 `xc7z020`
* Vivado 连续下载 20 次：PASS
* Hardware Target 重连并重新下载 10 次：PASS
* Vivado 完全退出后，XVC Server 可继续监听并接受新客户端
* 串口超时：0
* 协议错误：0
* XVC 会话错误：0

真实 Vivado 会话统计：

```text
Shift requests: 72086
Total bits: 345690915
Minimum shift: 8 bits
Maximum shift: 131019 bits
Average shift: 4796 bits
Duration: 581.3 s
Effective rate: 594655 bit/s
Serial timeouts: 0
Protocol errors: 0
Errors: 0
```

当前单次 Vivado bitstream 下载时间约：

```text
41～42 秒
```

---

# 二、固定硬件定义

禁止修改以下引脚：

```text
CHAN0 / GPIO2 -> TMS
CHAN1 / GPIO3 -> TCK
CHAN2 / GPIO4 <- TDO
CHAN3 / GPIO5 -> TDI
GND           -> GND
```

禁止改变：

* TMS/TDI/TDO 的 LSB-first 规则
* TCK 更新边沿
* TDO 采样边沿
* TAP 操作顺序
* XVC 请求位序
* Vivado 已验证通过的 JTAG 时序

---

# 三、本阶段总体原则

1. 先测量，后优化。
2. 每次只修改一个主要变量。
3. 每项优化必须可以独立回退。
4. 不允许一次同时修改 PIO、DMA、USB 和 Python 多层架构。
5. 不允许为了性能删除 bitbang 回退。
6. 不允许破坏现有 XVC：

   * `getinfo:`
   * `settck:`
   * `shift:`
7. 不允许破坏客户端断开后重新监听。
8. 不允许改变固件最大 Shift 32768 bits。
9. 不允许在热路径中逐请求打印日志。
10. 不使用动态内存。
11. 不引入 RTOS。
12. 不修改原有 sigrok 逻辑分析仪 target。
13. 不声称任何真实硬件测试已经通过，除非用户实际执行并提供结果。

---

# 四、先检查现有实现

修改代码前，先扫描并理解：

* XVC Server 的 TCP 收发路径
* Python 串口请求和响应路径
* XVC 大请求分块逻辑
* 固件 USB CDC 请求解析
* 固件 Shift 请求处理
* 2048-bit DMA chunk 循环
* TMS/TDI staging buffer 的生成方式
* DMA TX/RX 启动和等待方式
* TDO 重新打包方式
* TinyUSB CDC RX/TX 缓冲设置
* 是否存在多余的：

  * `bytes()` 转换
  * `bytearray()` 复制
  * 切片复制
  * `memcpy`
  * 全缓冲区清零
  * 每 chunk PIO/DMA 重置
  * 每 64 字节 flush
  * 逐字节 USB 写入

先输出简短分析：

1. 一次 XVC shift 的完整调用链。
2. 一次 32768-bit 固件 shift 会执行多少个 DMA chunk。
3. 每个 chunk 是否重新配置或重启 PIO/DMA。
4. 当前 TX/RX staging buffer 的数据格式。
5. 当前一个 JTAG bit 在 DMA buffer 中占多少字节。
6. 当前最可能的三个性能瓶颈。

完成分析后再开始修改。

---

# 五、Phase A：增加 PC 端性能剖析

主要修改：

```text
sigrok-pico/tools/exlink_xvc_server.py
```

根据仓库实际结构，也可以修改共用串口协议模块，但不要复制相同统计代码到多个文件。

## 5.1 每个 XVC shift 的计时点

使用高精度单调时钟：

```python
time.perf_counter_ns()
```

分别累计以下阶段：

1. `tcp_receive_ns`

   * 接收 XVC shift 长度、TMS 和 TDI 的时间
   * 只统计完整读取该请求数据的时间

2. `pc_prepare_ns`

   * 位偏移计算
   * 分块计算
   * TMS/TDI 切片或打包
   * 构造串口请求

3. `serial_write_ns`

   * 完整串口请求从开始写到写入完成

4. `serial_first_byte_wait_ns`

   * 串口请求发送完成后，到收到响应第一个字节的时间

5. `serial_response_read_ns`

   * 从响应首字节到完整响应读取完成的时间

6. `tdo_assemble_ns`

   * 多个固件子请求的 TDO 拼接时间
   * 非字节对齐处理时间

7. `tcp_send_ns`

   * 将完整 TDO 通过 socket 返回给 Vivado 的时间

8. `shift_total_ns`

   * 从开始处理该 `shift:` 到完整 TDO 返回结束的总时间

## 5.2 统计要求

不要为每个 Shift 打印日志。

只累计：

* 调用次数
* 总时间
* 最小时间
* 最大时间
* 平均时间

在客户端断开或用户主动查询时输出汇总。

建议输出：

```text
Performance summary:
  Shift requests:
  Total shifted bits:
  Total shift processing time:

  TCP receive:
    total:
    average:
    percentage:

  PC prepare:
    total:
    average:
    percentage:

  Serial write:
    total:
    average:
    percentage:

  Serial first-byte wait:
    total:
    average:
    percentage:

  Serial response read:
    total:
    average:
    percentage:

  TDO assemble:
    total:
    average:
    percentage:

  TCP send:
    total:
    average:
    percentage:
```

百分比以 `shift_total_ns` 为基准。

注意：这些阶段可能存在嵌套，输出中要说明统计定义，避免百分比被错误解释。

## 5.3 Shift 长度直方图

按以下区间统计：

```text
1～32 bits
33～128 bits
129～512 bits
513～2048 bits
2049～4096 bits
4097～8192 bits
8193～32768 bits
32769 bits 以上
```

每个区间记录：

* 请求数量
* 请求数量占比
* 总 bit 数
* 总 bit 数占比
* 平均请求长度
* 该区间总处理时间
* 该区间有效 bit/s

输出示例：

```text
Shift size histogram:
  1-32:
    requests=...
    request_ratio=...
    bits=...
    bit_ratio=...
    average_bits=...
    processing_time=...
    effective_rate=...
```

## 5.4 子请求统计

由于 Vivado 单个请求可能大于固件 32768-bit 最大长度，还需要统计：

* XVC logical shift 数量
* 发送到固件的 serial shift 子请求数量
* 平均每个 XVC logical shift 被拆成多少块
* 最大子请求数量
* 由于分块产生的额外非字节对齐次数

---

# 六、Phase B：增加固件端性能剖析

固件统计必须：

* 默认关闭
* 可运行时开启
* 不改变现有 Shift 响应格式
* 不在每个请求或 chunk 中打印
* 不使用浮点数
* 不使用动态内存
* 统计关闭时几乎没有额外开销

建议增加命令：

```text
profile on
profile off
profile show
profile clear
```

若现有协议不适合文本命令，可以使用当前协议风格实现等价命令。

## 6.1 固件计时项目

累计以下时间：

1. `request_parse_us`

   * 请求头解析
   * 长度验证
   * 完整请求接收后的协议处理

2. `tx_prepare_us`

   * TMS/TDI 从 packed 输入转换成 PIO/DMA staging buffer
   * 包括逐 bit 展开或查表转换

3. `dma_pio_us`

   * 从准备启动 RX DMA/TX DMA，到当前 chunk 完成
   * 所有 chunk 累计

4. `tdo_pack_us`

   * RX staging buffer 重新压缩和写入最终 TDO buffer

5. `response_queue_us`

   * 将响应交给 TinyUSB CDC 发送队列的时间
   * 不需要把 PC 实际收到数据的时间归入固件内部

6. `shift_total_us`

   * 固件收到完整 Shift 请求后，到完整响应进入 USB 发送队列的总时间

## 6.2 其他固件统计

记录：

* logical shift 数
* 总 bit 数
* DMA chunk 总数
* 每个 logical shift 平均 chunk 数
* 最大 chunk 数
* DMA timeout 数
* PIO recovery 数
* USB RX 不完整帧等待次数
* USB TX 等待可用空间次数

## 6.3 性能统计实现要求

* 使用 RP2040 可用的微秒级单调计时接口
* 所有累计时间使用至少 64-bit 无符号整数
* 注意计时溢出
* 使用宏或极轻量条件判断
* profiling 关闭时不执行多个时间读取
* 不要在 DMA/PIO 时序关键区加入 `printf`
* 不改变中断优先级和 DMA 配置

---

# 七、Phase C：先验证统计本身没有造成退化

完成 profiling 后，先关闭 profiling，执行原始测试：

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 info
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 capabilities
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine pio
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 5000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 boundary-test
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 benchmark
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 32768 --count 1000
```

要求：

* boundary-test 全部通过
* 32768 × 1000 stress 通过
* 统计关闭时，32768-bit 性能相对 770.5 kbit/s 下降不超过 2%
* 没有新增编译 warning
* 没有新增串口超时
* 没有新增协议错误

然后开启 profiling，用 Vivado 下载 5 次：

```tcl
set dev [current_hw_device]
set bit_file {F:/FPGA/ZYNQ7020/Flow_LED/prj/Flow_LED.runs/impl_1/flow_led.bit}

for {set i 1} {$i <= 5} {incr i} {
    puts "===== Profile Program $i / 5 ====="
    set_property PROGRAM.FILE $bit_file $dev
    program_hw_devices $dev
}
```

关闭 Hardware Target，让 XVC Server 输出会话统计。

---

# 八、Phase D：根据剖析结果实施低风险优化

只有完成 Phase A～C 并获得数据后，才能开始优化。

每次只选择一个优化方向。

## 8.1 优先级一：减少 Python 复制和对象创建

检查并优化：

* 避免重复 `bytes()` 转换
* 避免对大 TMS/TDI buffer 反复切片复制
* 优先使用 `memoryview`
* 预分配可复用 `bytearray`
* 串口请求尽可能一次性构造
* `serial.write()` 使用完整 frame
* 不逐字节写
* `socket.sendall()` 一次发送完整 TDO
* 不对已是 bytes-like 的对象重复复制
* 非字节对齐时只处理首尾边界，不逐 bit 复制全部数据

必须保持：

* LSB-first
* 非字节对齐正确
* 大于 32768-bit XVC 请求分块正确
* chunk 之间不插入 TAP reset 或额外 TCK

## 8.2 优先级二：优化短 Shift 快速路径

真实平均 Shift 只有约 4796 bits，并有大量短请求。

针对：

```text
1～32 bits
33～128 bits
129～512 bits
```

检查是否存在固定的大缓冲区处理。

优化要求：

* 不为 8-bit 请求清空完整 2048-bit staging buffer
* 只处理 `(bit_count + 7) / 8` 个有效字节
* 小请求尽量避免通用大请求路径中的多余分配
* 保持统一协议，不另外创建不兼容命令
* 不改变 TAP 行为

## 8.3 优先级三：减少固件全缓冲清零和重复复制

检查：

* 是否每个 chunk 清空完整 TX/RX staging buffer
* 是否每次 shift 清空完整 32768-bit TDO buffer
* 是否先复制到中间 USB buffer，再复制到协议 buffer
* 是否存在可以安全改为 `memcpy`、按字节处理或只清有效范围的逐 bit 循环

要求：

* 只清理有效范围
* 最后一个字节未使用高位仍必须清零
* 不允许泄漏上一请求的数据
* 不允许数组越界

## 8.4 优先级四：测试 DMA chunk 大小

保持 PIO 程序和数据格式不变，只测试：

```text
2048 bits
4096 bits
8192 bits
```

要求：

* chunk 大小使用单一宏定义
* 输出每种配置的静态 RAM 增量
* 不允许因为更大 chunk 导致栈上大数组
* 所有 staging buffer 必须静态分配
* 检查 `.bss`、RAM 使用量和链接结果
* 每种配置都执行完整 benchmark、boundary-test 和 stress

记录：

```text
chunk size
32768-bit throughput
average request latency
total static RAM
Vivado single program time
```

如果 4096 或 8192 没有明显提升，保留 2048，不要为了理论性能增加 RAM。

## 8.5 优先级五：减少每个 chunk 的 PIO/DMA重复初始化

检查当前每个 chunk 是否执行：

* disable PIO SM
* abort DMA
* reconfigure DMA
* clear FIFO
* clear IRQ
* enable PIO SM

若是，分析哪些动作只需在一个 logical shift 开始或错误恢复时执行。

低风险优化要求：

* logical shift 开始前统一清理一次
* chunk 之间尽量只更新 DMA地址、计数和有效 bit 数
* logical shift 结束后统一收尾
* chunk 之间不能产生额外 TCK
* chunk 之间不能丢失 TMS/TDI/TDO bit
* 超时恢复流程保持完整
* 若无法证明安全，不要修改

暂时不要实现复杂的 DMA ping-pong 或链式 DMA，除非前面优化仍明显不足。

## 8.6 优先级六：TinyUSB CDC 缓冲与发送策略

检查：

* `CFG_TUD_CDC_RX_BUFSIZE`
* `CFG_TUD_CDC_TX_BUFSIZE`
* CDC endpoint buffer
* `tud_cdc_write_available()`
* `tud_cdc_write()`
* `tud_cdc_write_flush()`
* `tud_task()` 调用位置

优化要求：

* 不逐字节发送
* 不每 64 字节主动 flush
* 尽可能批量写入
* 响应完整入队后再进行必要的 flush
* 不阻塞 TinyUSB task 太久
* 不改变 USB 描述符和 COM 口兼容性
* 记录修改前后的速度和 RAM 增量

---

# 九、本轮暂时禁止的高风险优化

在低风险优化完成前，不要实施：

* 重写 packed PIO 协议
* 每个 FIFO word 同时承载多个全新编码格式的 JTAG bit
* USB Vendor Bulk
* WinUSB/libusb
* 双核流水线
* DMA ping-pong 大改
* DMA chain 多通道架构
* TMS 压缩协议
* 修改 XVC 对外协议
* 提升固件最大 Shift
* 超过 5 MHz 的实机强制运行

可以在最终报告中提出这些后续方案，但本轮不要直接实现。

---

# 十、每次优化后的强制回归测试

每完成一项优化，都必须执行：

## 10.1 固件基础测试

```powershell
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 info
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 capabilities
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine bitbang
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 loopback --bits 128
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 engine pio
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 clock-pio --khz 5000
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 boundary-test
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 benchmark
python sigrok-pico\tools\exlink_jtag_test.py --port COM10 stress --bits 32768 --count 1000
```

## 10.2 Zynq测试

* 连续扫描 10 次
* 500 kHz、1 MHz、2 MHz、5 MHz 分别检查
* 扫描数据必须一致
* 无 DMA timeout
* 无 USB disconnect

## 10.3 Vivado测试

至少完成：

* 正确识别 `xc7z020`
* 连续下载 bitstream 5 次
* Hardware Target 关闭再打开 3 次
* Vivado客户端断开后 Server 继续监听
* 串口超时为 0
* 协议错误为 0

Vivado bitstream：

```text
F:/FPGA/ZYNQ7020/Flow_LED/prj/Flow_LED.runs/impl_1/flow_led.bit
```

Tcl：

```tcl
set dev [current_hw_device]
set bit_file {F:/FPGA/ZYNQ7020/Flow_LED/prj/Flow_LED.runs/impl_1/flow_led.bit}

for {set i 1} {$i <= 5} {incr i} {
    puts "===== Program $i / 5 ====="
    set_property PROGRAM.FILE $bit_file $dev
    program_hw_devices $dev
}
```

---

# 十一、性能目标

第一轮低风险优化目标：

```text
32768-bit回环：
当前约770.5 kbit/s
目标至少850 kbit/s
```

```text
真实Vivado会话：
当前约594.7 kbit/s
目标至少650 kbit/s
```

```text
Vivado单次下载：
当前约41～42秒
目标低于38秒
```

这些是目标，不是正确性硬指标。

硬性要求是：

* 正确性不得退化
* 稳定性不得退化
* boundary-test 必须通过
* 32768 × 1000 stress 必须通过
* Vivado 仍能稳定识别和下载
* XVC 重连功能必须保留
* Serial timeout = 0
* Protocol error = 0

若某项优化速度提升小于 3%，但明显增加复杂度或 RAM，应回退该优化。

---

# 十二、建议提交方式

每个优化点单独提交，例如：

```text
perf: add XVC timing statistics
perf: reduce Python buffer copies
perf: add short-shift fast path
perf: clear only active DMA buffer range
perf: benchmark configurable DMA chunk size
perf: reduce per-chunk DMA reconfiguration
perf: tune TinyUSB CDC buffers
```

不要把所有改动压成一个无法比较的提交。

---

# 十三、文档

新建或更新：

```text
docs/stage5_5_performance.md
```

记录：

1. Stage 5 稳定基线
2. 测量方法
3. PC 端时间分布
4. 固件端时间分布
5. Shift长度直方图
6. 每项优化前后的结果
7. DMA chunk对比
8. RAM增量
9. Vivado下载时间
10. 被回退的优化及原因
11. 当前剩余瓶颈
12. 是否值得进入高风险优化

---

# 十四、最终输出要求

完成后输出：

1. 代码结构分析
2. 修改和新增文件列表
3. 性能计时点说明
4. profiling 开启、关闭和查询方法
5. profiling关闭时的额外开销
6. Python端各阶段时间统计
7. 固件端各阶段时间统计
8. Shift长度分布
9. 每项优化前后对比
10. DMA chunk性能与RAM对比
11. 完整构建命令
12. 完整硬件测试命令
13. Vivado测试命令
14. 已回退的方案
15. 尚未真实硬件验证的内容
16. 下一步建议

不要伪造 benchmark 数值。

没有真实硬件结果时，只能报告：

* 代码完成情况
* 构建结果
* 静态分析结果
* 需要用户执行的测试项目

不要声称 Stage 5.5 已经通过。
