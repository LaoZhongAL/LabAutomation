# 标准终端输入——环境与标准电阻测试

本文档用于独立的特殊版 GPIB 终端。每次只输入一行，不要把整段命令一次性粘贴到提示符中。

终端语法为：

```text
QUERY <VISA资源> <SCPI查询或TSP-print>
WRITE <VISA资源> <SCPI或TSP状态修改指令>
```

主机先打开 VISA 资源，真正传到对应仪器的只有最后面的 SCPI/TSP 消息。例如：

```text
QUERY GPIB0::9::INSTR *IDN?
```

大致等价于：

```python
instrument = resource_manager.open_resource("GPIB0::9::INSTR")
response = instrument.query("*IDN?")
```

下面的 2182A 指令依据 Keithley 官方 [2182A User's Manual](https://download.tek.com/manual/2182A-900-01C_July_2022_User.pdf)，6221 和 Delta 指令依据官方 [6220/6221 User's Manual](https://download.tek.com/manual/622x-900-01%20%28C%20-%20Oct%202008%29%28User%29.pdf)。

## 1. 终端本地指令

这些指令不会向仪器发送消息：

```text
HELP
MAP
STATUS
LIST
TIMEOUT 3000
CALC-R 1.25E-3 2.5E-6
LOCK-WRITES
EXIT
```

`CALC-R` 只在电脑上计算 `R = V / I`，输入单位必须是伏特和安培。示例结果是 `500 ohm`，不会访问任何仪器。

## 2. 已确认的实验室地址表

| VISA 资源 | 型号 | 序列号 | 预定作用 |
|---|---:|---:|---|
| `GPIB0::6::INSTR` | 2182A | 1340129 | 纳伏表候选 A |
| `GPIB0::7::INSTR` | 2182A | 4510267 | 纳伏表候选 B |
| `GPIB0::9::INSTR` | 6221 | 4533811 | 电流源候选 A |
| `GPIB0::10::INSTR` | 6221 | 4581062 | 电流源候选 B |
| `GPIB0::25::INSTR` | 2450 | 04584128 | SMU，TSP 模式 |
| `GPIB0::26::INSTR` | 2450 | 04464720 | SMU，TSP 模式 |

## 3. 第一次演示：只确认身份

下面 6 行是推荐的第一次真实上机演示，不会修改仪器设置：

```text
QUERY GPIB0::6::INSTR *IDN?
QUERY GPIB0::7::INSTR *IDN?
QUERY GPIB0::9::INSTR *IDN?
QUERY GPIB0::10::INSTR *IDN?
QUERY GPIB0::25::INSTR *IDN?
QUERY GPIB0::26::INSTR *IDN?
```

只要返回的型号或序列号与地址表不一致，就立刻停止。

## 4. 只查询环境记录

### 4.1 2182A——使用地址 6 或 7

把 `<2182A_RESOURCE>` 替换成 `GPIB0::6::INSTR` 或 `GPIB0::7::INSTR`。带尖括号的模板本身不是有效指令。

```text
QUERY <2182A_RESOURCE> *IDN?
QUERY <2182A_RESOURCE> SYST:VERS?
QUERY <2182A_RESOURCE> SYST:LFREQUENCY?
QUERY <2182A_RESOURCE> SYST:POSETUP?
QUERY <2182A_RESOURCE> SENS:FUNC?
QUERY <2182A_RESOURCE> SENS:CHAN?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:NPLC?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:RANG?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:RANG:AUTO?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN2:RANG?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN2:RANG:AUTO?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:DFILTER?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:LPASS?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN2:DFILTER?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN2:LPASS?
QUERY <2182A_RESOURCE> TRIG:COUNT?
QUERY <2182A_RESOURCE> TRIG:DELAY?
QUERY <2182A_RESOURCE> TRIG:SOURCE?
```

以上都是配置查询。`SENS:DATA:LATEST?` 会读取最近一次结果；修改配置以后，它可能仍然是旧数据：

```text
QUERY <2182A_RESOURCE> SENS:DATA:LATEST?
```

`SENS:DATA:FRESH?` 会主动触发一次新测量，所以终端发送前会要求再输入 `SEND ACTIVE_QUERY`：

```text
QUERY <2182A_RESOURCE> SENS:DATA:FRESH?
```

### 4.2 6221——使用地址 9 或 10

把 `<6221_RESOURCE>` 替换成 `GPIB0::9::INSTR` 或 `GPIB0::10::INSTR`。

```text
QUERY <6221_RESOURCE> *IDN?
QUERY <6221_RESOURCE> SYST:VERS?
QUERY <6221_RESOURCE> SYST:POSETUP?
QUERY <6221_RESOURCE> OUTP?
QUERY <6221_RESOURCE> OUTP:LTEARTH?
QUERY <6221_RESOURCE> OUTP:ISHIELD?
QUERY <6221_RESOURCE> OUTP:RESPONSE?
QUERY <6221_RESOURCE> OUTP:INTERLOCK:TRIPPED?
QUERY <6221_RESOURCE> SOUR:CURR?
QUERY <6221_RESOURCE> SOUR:CURR:RANG:AUTO?
QUERY <6221_RESOURCE> SOUR:CURR:RANG?
QUERY <6221_RESOURCE> SOUR:CURR:COMP?
QUERY <6221_RESOURCE> SOUR:CURR:FILT?
QUERY <6221_RESOURCE> SOUR:DELT:NVPRESENT?
QUERY <6221_RESOURCE> SOUR:DELT:HIGH?
QUERY <6221_RESOURCE> SOUR:DELT:LOW?
QUERY <6221_RESOURCE> SOUR:DELT:DELAY?
QUERY <6221_RESOURCE> SOUR:DELT:COUNT?
QUERY <6221_RESOURCE> SOUR:DELT:CSWITCH?
QUERY <6221_RESOURCE> SOUR:DELT:ARM?
```

关键解释：

- `OUTP?` 返回 `0` 表示电流源输出关闭。
- 对 6221 而言，`OUTP:INTERLOCK:TRIPPED?` 返回 `1` 表示互锁闭合/可用，返回 `0` 表示互锁断开或跳闸。
- `SOUR:DELT:NVPRESENT?` 返回 `1` 表示 6221 已经通过串口检测到所需的 2182/2182A；返回 `0` 表示 Delta 配对环境尚未准备好。

### 4.3 TSP 模式的 2450——可选环境确认

```text
QUERY GPIB0::25::INSTR *IDN?
QUERY GPIB0::25::INSTR print(localnode.model)
QUERY GPIB0::25::INSTR print(localnode.serialno)
QUERY GPIB0::25::INSTR print(localnode.version)
QUERY GPIB0::25::INSTR print(localnode.linefreq)
QUERY GPIB0::25::INSTR print(smu.source.output)
QUERY GPIB0::25::INSTR print(smu.source.func)
QUERY GPIB0::25::INSTR print(smu.source.level)
QUERY GPIB0::25::INSTR print(smu.source.range)
QUERY GPIB0::25::INSTR print(smu.measure.func)
QUERY GPIB0::25::INSTR print(smu.measure.range)
QUERY GPIB0::25::INSTR print(smu.measure.nplc)
QUERY GPIB0::25::INSTR print(smu.measure.sense)
QUERY GPIB0::25::INSTR print(smu.measure.terminals)
QUERY GPIB0::25::INSTR print(smu.interlock.tripped)
```

第二台 2450 把地址换成 26 即可。此前真实基线表明，对 2450 而言 `smu.OFF` 表示正常/未跳闸，`smu.ON` 表示互锁状态已触发/跳闸。

## 5. 标准电阻测试：只查询/手动设置模式

在标准电阻阻值、额定功率、安全电流和合规电压尚未确认以前，使用本模式。程序不配置仪器，也不打开输出。

### 5.1 接线或通电以前必须记录

```text
标准电阻标识：____________________
标称阻值：____________________ ohm
公差：____________________ %
最大功率：____________________ W
最大电压：____________________ V
最大电流：____________________ A
批准的测试电流：____________________ A
批准的合规电压：____________________ V
2182A 地址：____________________
6221 地址：____________________
接线：2 线 / 4 线
接线前确认 6221 输出为 OFF：是 / 否
```

任何安全关键值未知时，只允许查询，不得解锁写入。

### 5.2 通过仪器前面板手动设置以后

1. 查询 6221，核对实际电流、合规电压和输出状态：

   ```text
   QUERY <6221_RESOURCE> OUTP?
   QUERY <6221_RESOURCE> SOUR:CURR?
   QUERY <6221_RESOURCE> SOUR:CURR:RANG?
   QUERY <6221_RESOURCE> SOUR:CURR:COMP?
   QUERY <6221_RESOURCE> OUTP:INTERLOCK:TRIPPED?
   ```

2. 查询 2182A 的测量配置：

   ```text
   QUERY <2182A_RESOURCE> SENS:FUNC?
   QUERY <2182A_RESOURCE> SENS:CHAN?
   QUERY <2182A_RESOURCE> SENS:VOLT:DC:NPLC?
   QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:RANG?
   QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:RANG:AUTO?
   ```

3. 获取一次新电压读数时，先输入查询，再按提示确认：

   ```text
   QUERY <2182A_RESOURCE> SENS:DATA:FRESH?
   SEND ACTIVE_QUERY
   ```

4. 使用实际返回电压和实际查询到的电流，并保留正负号：

   ```text
   CALC-R <MEASURED_VOLTAGE_V> <ACTUAL_CURRENT_A>
   ```

精密测量中，单次读数不等于完整的电阻测量。应通过正反向电流和重复测量平均来抑制热电势；或者在串口及 Trigger Link 均已确认后，使用经过验证的 Delta 配置。

## 6. 已审批的写入模式——未知标准电阻时禁止使用

以下是模板，不代表批准的数值。物理接线、阻值/额定值、电流、量程和合规电压没有经过负责人确认以前，不得发送。输入前必须替换每一个 `<...>` 字段。

解锁：

```text
UNLOCK-WRITES I_UNDERSTAND_WRITES_CAN_CHANGE_INSTRUMENTS
```

之后每条普通写入会要求 `SEND`，高风险写入会要求 `SEND HIGH_RISK`。

### 6.1 首先关闭所选 6221 的输出

```text
WRITE <6221_RESOURCE> OUTP OFF
SEND
QUERY <6221_RESOURCE> OUTP?
```

### 6.2 配置 2182A 电压测量

```text
WRITE <2182A_RESOURCE> SENS:FUNC "VOLT:DC"
WRITE <2182A_RESOURCE> SENS:CHAN 1
WRITE <2182A_RESOURCE> SENS:VOLT:DC:NPLC <APPROVED_NPLC_0.01_TO_50>
WRITE <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:RANG:AUTO ON
```

每次写入后都重新查询核对：

```text
QUERY <2182A_RESOURCE> SENS:FUNC?
QUERY <2182A_RESOURCE> SENS:CHAN?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:NPLC?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:RANG:AUTO?
```

### 6.3 保持输出关闭并配置所选 6221

```text
WRITE <6221_RESOURCE> SOUR:CURR:RANG:AUTO ON
WRITE <6221_RESOURCE> SOUR:CURR:COMP <APPROVED_COMPLIANCE_V>
WRITE <6221_RESOURCE> SOUR:CURR <APPROVED_CURRENT_A>
```

这些都属于高风险写入，需要 `SEND HIGH_RISK`。打开输出以前必须回读：

```text
QUERY <6221_RESOURCE> OUTP?
QUERY <6221_RESOURCE> SOUR:CURR?
QUERY <6221_RESOURCE> SOUR:CURR:RANG:AUTO?
QUERY <6221_RESOURCE> SOUR:CURR:RANG?
QUERY <6221_RESOURCE> SOUR:CURR:COMP?
QUERY <6221_RESOURCE> OUTP:INTERLOCK:TRIPPED?
```

### 6.4 打开输出与关闭输出

只有物理电路和所有回读值均被批准后才能执行：

```text
WRITE <6221_RESOURCE> OUTP ON
SEND HIGH_RISK
```

通过终端关闭输出：

```text
WRITE <6221_RESOURCE> OUTP OFF
SEND
QUERY <6221_RESOURCE> OUTP?
LOCK-WRITES
```

仪器实体 OUTPUT OFF 控制和实验室断电/互锁流程仍然是主要安全手段；软件不是紧急停止装置。

## 7. 6221 + 2182A True Delta——只限高级实验

不要为了演示终端而 ARM Delta。True Delta 除 GPIB 以外，还要求 6221 与 2182A 之间的 RS-232 线和 Trigger Link。6221 通过串口控制 2182A，所以计算结果从 6221 读取。

先只查询准备状态：

```text
QUERY <6221_RESOURCE> OUTP?
QUERY <6221_RESOURCE> SOUR:DELT:NVPRESENT?
QUERY <6221_RESOURCE> SOUR:DELT:ARM?
```

只有输出为 OFF、批准的仪器对已经完成物理连接，而且 `NVPRESENT?` 返回 `1` 时才能继续。经审批后使用以下模板：

```text
WRITE <6221_RESOURCE> SOUR:DELT:HIGH <APPROVED_POSITIVE_CURRENT_A>
WRITE <6221_RESOURCE> SOUR:DELT:LOW <APPROVED_NEGATIVE_CURRENT_A>
WRITE <6221_RESOURCE> SOUR:DELT:DELAY <APPROVED_DELAY_S>
WRITE <6221_RESOURCE> SOUR:DELT:COUNT <APPROVED_FINITE_COUNT>
WRITE <6221_RESOURCE> SOUR:DELT:CSWITCH <ON_OR_OFF>
WRITE <6221_RESOURCE> SOUR:DELT:ARM
WRITE <6221_RESOURCE> INIT:IMM
```

以上全部是高风险写入。运行产生数据后，再从 6221 读取最新结果：

```text
QUERY <6221_RESOURCE> SENS:DATA:LATEST?
```

解除 ARM/中止并强制关闭输出：

```text
WRITE <6221_RESOURCE> SOUR:SWE:ABOR
WRITE <6221_RESOURCE> OUTP OFF
QUERY <6221_RESOURCE> OUTP?
LOCK-WRITES
```

## 8. 会消耗状态信息的诊断查询

以下查询可能消耗或清除错误队列/状态信息，所以要进行主动查询确认。只在故障诊断时使用，并保存日志：

```text
QUERY <RESOURCE> SYST:ERR?
QUERY <RESOURCE> *ESR?
```

不要把 `*RST`、`*CLS`、`READ?`、`MEAS?`、`INIT` 或触发指令当作试探命令。它们可能复位状态、清除证据、开始采集或改变测试流程。

## 9. 结束检查表

1. 分别用 `OUTP?` 确认两台 6221；除非批准的实验仍在运行，结果应为 `0`。
2. 输入 `LOCK-WRITES`。
3. 输入 `EXIT`。
4. 把 `terminal_logs` 中新生成的文件与接线记录和 GUI 基线压缩包一起保存。
5. 记录所有超时、型号不符或序列号不符；不要无判断地重复发送命令。
