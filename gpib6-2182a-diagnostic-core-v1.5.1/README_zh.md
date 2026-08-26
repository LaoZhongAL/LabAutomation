# GPIB0 多仪器只读诊断与 GPIB6 2182A Live v1.5.1

本目录包含 v1.5.1 动态 instrument inventory、按精确型号复用的只读诊断 profile，以及唯一获批的 GPIB6 Keithley 2182A 实时电压 GUI。v1.5.1 保留 v1.1 的记录语义：电压 CSV 只保存仪器样本，人工干预的类型、位置和开始/结束区间只保存到独立 `interventions.jsonl`。

v1.5.1 保留 v1.5 的 P1 行为，并为 2182A operation-condition B0 增加固定短观察窗：同一 VISA session 的开始/结束读数之外，在末端再按 13 个固定非均匀时点执行精确 `STAT:OPER:COND?`，覆盖约 3 秒。程序只报告 B0 在该窗口内始终清除、发生变化、始终置位或证据不足；不会据此推断 FULL ACAL、Autozero、校准持续时间或根因。任一样本 B0 置位或证据不完整仍阻断 Readiness。v1.5.1 同时正式纳入此前未发布的 `errors.jsonl`/证据校验、2182A B9 非归因描述、`SYST:POSETUP?` 的 `PRES` 合法返回和修正后的规则失败文案。

## v1.5.1 动态 inventory 边界

程序启动、切换模式或切换仪器时都不会自动访问 GPIB。只有用户明确点击 `Refresh Inventory` 才建立新的 inventory snapshot。

一次 real refresh 严格执行：

1. `ResourceManager.list_resources()` 恰好调用一次；
2. 只保留该调用实际返回、且完整匹配 `GPIB0::<primary>::INSTR` 的资源，其中 primary address 只能为 1–30；
3. 不补齐缺失地址，不枚举 1–30，不把 GPIB1、USB、TCPIP、ASRL、board-only 或 secondary-address 资源改写成候选地址；
4. 按 primary address 顺序逐台访问，同时最多打开一个资源；
5. 每个保留资源最多发送一次固定 `*IDN?`，超时或失败不重试，也不发送 clear、reset 或恢复命令；
6. 每台资源在处理后立即关闭；单台失败只记录错误，refresh 继续处理下一台。

Inventory 只确认资源与身份，不运行型号诊断，也不授予 Live。真实模式中的候选地址永远来自本次 `list_resources()` 返回值，程序不会根据已有资产、模拟 fixture、历史运行或相邻地址猜测仪器位置。

## 身份分类与共享诊断 profile

`*IDN?` 必须解析成恰好四个字段。只有受支持的 Keithley vendor 和精确规范化型号才可映射到共享 profile：

| 精确型号 | 命令集 | 共享只读 profile | Live |
|---|---|---|---|
| `2182A` | SCPI | 电压测量、通道、量程、NPLC、滤波、触发与格式诊断 | 仅下述 GPIB6 精确资产 |
| `6221` | SCPI | 输出、互锁、状态 condition、电流量程、compliance、滤波、response 与 guard/ground 拓扑诊断 | 不支持 |
| `2450` | TSP | Precision & Safety Settings、active compliance、sense、autozero、filter、source readback、OVP 与互锁诊断 | 不支持 |

同型号的新序列号可以复用同一个诊断 profile，不需要为每台仪器复制查询表。但型号匹配不等于资产授权，也不会自动获得 Live。

2450 的 `*IDN?` 本身不能证明当前命令语言。只有精确匹配已确认 TSP 的资产策略，或以后显式提供同等强度的 TSP policy，才可进入 2450 TSP profile；否则 inventory 将其记录为 command-set ambiguous，不发送任何 TSP 属性读取。

以下情况只保存资源、原始身份或错误，不运行任何 model-specific profile：

- `*IDN?` 无响应、超时或 I/O 失败；
- 身份不是恰好四字段或字段解析失败；
- vendor 不受支持或型号不是精确 `2182A`、`6221`、`2450`；
- 2450 的 TSP command set 未被独立确认。

## simulate inventory 只是离线 fixture

当前 simulate refresh 固定产生五台 handoff fixture：

- `GPIB0::6::INSTR`：2182A，S/N `1340129`；
- `GPIB0::7::INSTR`：2182A，S/N `4510267`；
- `GPIB0::9::INSTR`：6221，S/N `4533811`；
- `GPIB0::10::INSTR`：6221，S/N `4581062`；
- `GPIB0::25::INSTR`：2450，S/N `04584128`，已知 TSP fixture。

这五项只用于确定性离线测试和 UI 演示，不是 real inventory 的候选表，也不能证明现场地址、连接或身份。simulate 不导入 PyVISA，不访问 GPIB。

## 唯一 Live 资产

实时电压能力仍只属于以下完整 allowlist：

- VISA 资源：`GPIB0::6::INSTR`
- 厂商：`KEITHLEY INSTRUMENTS INC.`
- 型号：`2182A`
- 序列号：`1340129`
- 固件：规范化后 `C02 /A02`

地址、四字段身份、2182A Readiness、recorder 和状态机必须全部通过才会启用 `Start Live Plot` 与 `Single FETCh?`。GPIB7 的 2182A、任意 6221、任意 2450、未知仪器和未来发现的同型号新资产都只能诊断；profile 被识别并不扩大 Live allowlist。

## query-only 安全边界

Inventory 阶段每个资源只允许一次 `*IDN?`。选择已识别仪器后，诊断阶段只执行该型号 profile 中逐字批准的查询，并在真实 VISA 发送点再次检查完整事务。

- 2182A/6221 的 SCPI profile 只接受精确的单条 `?` 查询；拒绝大小写漂移、尾随字符、分号和换行拼接。
- 2450 的 TSP profile 只接受精确 allowlist 中的简单 `print(attribute)` 读取。不能只按 `print(` 前缀放行，因为 TSP 表达式仍可能执行有副作用的函数。
- 2450 不允许赋值、脚本、`smu.measure.read()`、event/status destructive reads、trigger initiate/abort 或任意用户表达式。
- 2450 仅对 `status.condition`、`status.operation.condition`、`status.questionable.condition` 三个完整只读属性作窄例外；其他 `status.*`、event register 和函数仍拒绝。
- 6221 不发送手册未批准的 `SOUR:CURR?`、`SOUR:CURR:AMPL?` 或推断 alias，因此 DC setpoint 保持 UNKNOWN。
- GPIB6 Live 仍只允许 `*IDN?` 身份复核和 `FETCh?`。

程序没有任意 SCPI/TSP 输入框、通用 write API 或仪器配置控件，不发送 `*RST`、`ABOR`、`INIT`、软件触发、清缓存、清事件或配置写入。故障注入只存在于 `simulate`；`real` 模式在 GUI 和函数入口都拒绝模拟上下文。

`FETCh?` 读取 GPIB6 2182A 当前可用读数，不重建触发模型。重复读数按原始结果保存；空值、非数值、NaN、Inf 和绝对值不小于 `1e37` 的过量程哨兵会锁存故障。

## 诊断表与 Readiness

选择 inventory 中已识别的仪器后，点击 `Run Read-Only Diagnostics`。GUI 把最关心的当前值放在顶部 `Precision & Safety Settings`，完整原始响应、精确查询和规则结果放在下方 `Complete Read-Only Evidence`。每种型号使用自己的 summary builder，不把 2182A 字段套到 6221 或 2450。

通用硬门槛包括 recorder、通信、精确身份、响应解析与仪器安全状态。型号设置只有在存在明确规则时才给出 PASS/BLOCKED；当前没有 sample/device profile，因此量程、NPLC、filter 等只显示实际读回值和解释，不伪造最佳值，也不推算实时 accuracy/noise。

一次 nominal 诊断的候选/执行计数按型号计算：2182A `51/51`（28 条基础查询、10 条末端复读和 13 条 B0 时序观察）；6221 `21/21`；2450 `41/43`，其中与当前 source function 不适用的两条 compliance 分支明确跳过。`51/51` 属于 2182A 共享 profile，不属于 GPIB6 地址；real 模式先读取 inventory 和身份，再按识别出的精确型号选择 profile。末端复读和 B0 观察仍使用当前型号的相同 exact allowlist、同一个 VISA session、无 retry。稳定字段按布尔、有限数值或规范化文本比较；实时 condition word 不要求逐字相等，但每个样本都检查合法域与 2182A 型号专属危险状态。

2182A 的 B0 汇总使用开始、末端和 13 个附加样本的主机单调时间。只有 15 个样本完整、时间严格递增且 B0 全程清除时，观察项才为 PASS；窗口内发生 B0 变化或始终置位均为 BLOCKED；缺样本、查询失败、非法 condition word 或时间证据无效为 UNKNOWN 并阻断 Live。该短窗用于区分本次诊断期间的可观察状态，不是校准超时阈值，也不证明前面板 FULL ACAL 是否完成。

2182A 的通道、量程、NPLC、滤波、工频和触发参数全部来自本次查询，并按型号合法域解释；程序不再把 GPIB6 绑定到 CH1、10 mV、NPLC=5 或其他实验配方。精确获批资产进入 Live 前只额外检查当前标量电压读取路径所需的兼容状态：`VOLT:DC`、sample count 1、continuous initiation ON、ASCII data 和单一 `READ` element。这些条件不改变仪器设置。

2182A 的 operation/measurement/questionable condition 使用 2182A 手册的独立 mask：calibrating、reading overflow、invalid calibration constant 和 ACAL questionable condition 阻断；thermocouple reference condition 只警告。B9 只说明 ACAL 条件需要处理，不由程序推断具体原因。`SYST:POSETUP?` 接受手册规定的 `RST`、`PRES/PRESET` 和 `SAV0`。不会把 6221 的 B10 Idle 规则套到 `INIT:CONT=1` 的 2182A。

对 2450：

- compliance 必须按实际 source function 只读取 active branch：电压源读取 current limit，电流源读取 voltage limit；
- configured 4W 不代表当前 4W 一定生效；output OFF 时 effective sense 为 2W；
- output state 必须与 output-off mode 一起解释，OFF 不自动等同于物理开路；
- `smu.interlock.tripped` 的合法 0/1 返回表示 interlock asserted OFF/ON；只有非法返回阻断诊断；
- calibration validity 无法由批准的 TSP snapshot 证明，`calibration_traceability` 必须显示 UNKNOWN，而不是绿色 PASS。

对 6221，现有 `STAT:OPER:COND?` 的 B10 必须置位才表示 Idle；B10 清除时 `safe_idle` 为 BLOCKED。questionable calibration bit 只形成 `calibration_condition`，不等于外部校准证书已知或有效。

未知、解析失败或 command-set ambiguous 的 inventory entry 没有 profile，因此只能查看身份/错误，不能点击型号诊断。

## 状态机

每个已选仪器的诊断运行使用：

`DISCONNECTED → VERIFYING_IDENTITY → CHECKING_CONFIG → OBSERVE_READY`

只有精确获批的 GPIB6 2182A 可以继续到 `LIVE`。运行中超出轮询时限进入 `DEGRADED`，恢复正常时回到 `LIVE`；Pause 正常收尾后回到 `OBSERVE_READY`。身份、采集、CSV、events/interventions JSONL、manifest 或会话故障进入 `FAULT_LATCHED`，必须重新运行诊断。

新的 inventory refresh、仪器选择、Mode、fault 场景或 Output Folder 会立即使旧 diagnostic context 与 Readiness 失效。inventory 不会自动重连或改变仪器。

inventory、diagnostic、Live、single-fetch 的主机队列事件都必须匹配当前完整 owner 后才可更新界面。owner 只用于进程内隔离，不写入 manifest；磁盘证据继续使用既有 `run_id`、`stream_id` 和 inventory `snapshot_id`。Live 的匹配 `stream_id` 仍是第二层终止闸门。当前仍为单 worker、单总线顺序执行，不实现多仪器并发。

## 文件必须整包部署

不可只复制主脚本。以下文件必须保持同目录结构：

- `gpib6_2182a_monitor.py`
- `instrument_inventory.py`
- `instrument_profiles.py`
- `diagnostic_core.py`
- `fault_injection.py`
- `run_evidence.py`
- `stream_quality.py`
- `evidence_verifier.py`
- `START_KEITHLEY_QUERY_ONLY_DIAGNOSTICS.bat`
- `START_KEITHLEY_QUERY_ONLY_DIAGNOSTICS.ps1`
- `START_2182A_GPIB6_MONITOR.bat`
- `START_2182A_GPIB6_MONITOR.ps1`
- `README_zh.md`
- `RELEASE_NOTES_v1.5.1.md`
- `tests/` 全目录

## Windows 部署与离线验证

1. 校验发布 ZIP：

   ```powershell
   Get-FileHash "C:\LabAutomation\gpib6-2182a-diagnostic-core-v1.5.1.zip" -Algorithm SHA256
   ```

   作用：只读计算发布包哈希，不解压、不访问仪器。预期：与同名 `.sha256.txt` 和 v1.5.1 交接值完全一致；不覆盖旧版本目录。

2. 解压到全新目录：

   ```powershell
   Expand-Archive `
     "C:\LabAutomation\gpib6-2182a-diagnostic-core-v1.5.1.zip" `
     "C:\LabAutomation\gpib6-2182a-diagnostic-core-v1.5.1"
   ```

   作用：建立独立版本目录。预期：能看到 inventory、profile、运行模块、两个通用启动器和完整 `tests`。旧 `START_2182A_GPIB6_MONITOR.*` 可作为兼容别名一同保留。

3. 核对统一 Python：

   ```powershell
   Test-Path "C:\LabAutomation\.venv\Scripts\python.exe"
   ```

   作用：只检查共享解释器是否存在。预期：`True`。

4. 核对运行依赖：

   ```powershell
   & "C:\LabAutomation\.venv\Scripts\python.exe" -c "import sys, tkinter, pyvisa; print(sys.version); print(tkinter.TkVersion); print(pyvisa.__version__)"
   ```

   作用：只导入 Python、Tk 和 PyVISA，不打开 ResourceManager、不访问 GPIB。预期：全部导入成功并打印版本。

5. 运行离线测试：

   ```powershell
   Set-Location "C:\LabAutomation\gpib6-2182a-diagnostic-core-v1.5.1"
   & "C:\LabAutomation\.venv\Scripts\python.exe" -m unittest discover -s tests -t . -v
   ```

   作用：验证动态 inventory、单次身份查询、三种共享 profile、异步 owner、首尾一致性、2182A B0 时序分类、Live 隔离、manifest/events/interventions、纯样本 CSV 和故障矩阵；不会实例化真实 VISA 会话。预期：`Ran 175 tests`、`OK (skipped=4)`，没有 failure/error。

6. 启动 GUI：

   ```powershell
   & "C:\LabAutomation\gpib6-2182a-diagnostic-core-v1.5.1\START_KEITHLEY_QUERY_ONLY_DIAGNOSTICS.ps1"
   ```

   作用：使用固定共享 Python 启动 GUI。预期：默认 `simulate`、fault=`nominal`、状态 `DISCONNECTED`；尚未 refresh inventory，也没有 VISA 通信。若 Python 非零退出，启动器会保留并返回原退出码。

如果 PowerShell 执行策略阻止脚本，可双击同目录的 `START_KEITHLEY_QUERY_ONLY_DIAGNOSTICS.bat`。

## simulate 验证

1. 保持 Mode=`simulate`、fault=`nominal`，点击 `Refresh Inventory`。
2. 预期出现上述五台 fixture；选择 2182A、6221、2450 时分别使用对应共享 profile，且没有 VISA 通信。
3. 依次选择至少一台 6221 和 2450，点击 `Run Read-Only Diagnostics`。预期顶部显示各自的 Precision & Safety Settings，下方保留完整证据，Live/Single 始终禁用。
4. 选择 GPIB7 2182A 并运行诊断。预期复用 2182A profile并显示 `51/51`，且 B0 observation 为 `clear_for_entire_window`，但 Live/Single 仍禁用。
5. 选择精确 GPIB6 资产。预期在 2182A 型号诊断之外，只有额外 Live 兼容检查通过后才启用 Start/Single；地址本身不选择诊断 profile 或参数模板。
6. 点击 `Start Live Plot`。首个已提交样本后 `Mark Intervention: Start` 启用。
7. 选择 Intervention type，填写 Location，点击 Start；完成物理扰动后点击 End。预期显示带两条红色边界的浅红区间，且只有 `interventions.jsonl` flush 成功后才更新图形。Start/End 不发送仪器消息。
8. 再开始一个区间并直接点击 Pause。预期先追加 end，再关闭 CSV/VISA 会话并回到 `OBSERVE_READY`。

更换 fault 场景、Output Folder、仪器或刷新 inventory 后，必须重新运行所选仪器的诊断。

## real 现场验证

只有离线测试和 simulate smoke 全部通过后才进入 real：

1. 停止访问同一 GPIB0 bus 的 LabVIEW VI，关闭全部 NI MAX test panel，确保没有第二控制器。
2. 切换 Mode=`real`。预期 fault 自动变为 `nominal` 且故障注入禁用；此时尚不访问 GPIB。
3. 点击 `Refresh Inventory` 并确认 exclusive query-only 访问。
4. 预期列表只包含 `list_resources()` 实际返回的 GPIB0 primary 1–30 `INSTR`；每项最多一次 `*IDN?`。不存在的地址不会出现，也不会被探测。
5. 对 unknown、malformed、I/O error 或 2450 command-set ambiguous 项，只保留 identity/error，不运行 profile。
6. 选择已识别仪器并点击 `Run Read-Only Diagnostics`，再单独确认该资源的精确型号和序列号。Inventory 总线确认不会自动授权这一阶段。如果 BLOCKED/UNKNOWN，不要改仪器；关闭 GUI 并保留整个运行目录复核。
7. 6221、2450、GPIB7 2182A 即使诊断通过也保持 Live 禁用。
8. 只有 GPIB6 2182A 完整身份与 Readiness 通过后，才进行 30–60 s Live、Mark Intervention、Pause 和关闭 GUI。
9. 上传整个最新运行目录，而不是只上传 CSV。

## 证据、readout CSV 与人工干预

每次显式 inventory refresh 先建立：

`monitor_runs/YYYYMMDD-HHMMSS-mmm-<mode>-inventory-v1.5.1/`

其中：

- `inventory-refresh-plan.json`：在第一条身份查询前原子写入，固结一次列举、资源过滤、每资源最多一次 `*IDN?`、无重试/无写入的计划；
- `inventory-snapshot.json`：保存 raw resources、过滤结果、原始 IDN、解析身份、profile resolution、每项错误和计数。

如果 plan 写入失败，真实 inventory 不会开始。如果身份查询已完成但 snapshot 无法落盘，GUI 会明确显示 persist 失败；内存条目只可查看，不能运行型号诊断。

每次所选仪器的诊断运行建立：

`monitor_runs/YYYYMMDD-HHMMSS-mmm-<mode>-<target>-diagnostic-v1.5.1/`

其中可能包含：

- `run-manifest.json`：冻结本次资源、身份、profile、命令集、精确白名单、Readiness、状态、stream 统计和最终关闭状态；
- `events.jsonl`：稀疏、序号单调、使用单调经过时间的生命周期/故障事件；
- `errors.jsonl`：逐条镜像本次运行中 `events.jsonl` 的 ERROR 事件；Readiness 阻断包含 check ID、期望值、实际值、消息和 configuration snapshot 引用，运行异常包含原始异常类型与消息；正常运行为空文件；
- `interventions.jsonl`：人工干预的唯一事实源，只用于 GPIB6 Live；
- `configuration-snapshot.json`：原子写入的 model-specific 开始/末端查询、原始响应、耗时、sentinel consistency 和完整规则结果；
- `inventory-snapshot-reference.json`：复制本次诊断所冻结的完整 inventory snapshot，并记录 snapshot payload SHA-256、原 inventory 文件路径和文件 SHA-256；第一条型号诊断 query 前会重新解析源 JSON，确认其 canonical payload 与内存冻结 snapshot 完全一致；
- `configuration-failure.json`：主证据记录器异常时的尽力失败说明；
- `voltage-*.csv`：每次 GPIB6 Live 的完整 2182A readout 样本，不包含人工干预行。
- `voltage-*.quality.json`：对已关闭四列 CSV 的主机侧时序、延迟、重复、离散和线性漂移描述；
- `evidence-verification.json`：GUI 关闭运行时生成的 manifest/JSONL/CSV/quality 一致性检查结果。

CSV 文件名 `voltage-YYYYMMDD-HHMMSS-mmm.csv` 只记录一次测量开始时的 Windows 本地日期时间。CSV 数据区没有逐行系统时间，字段固定为：

- `elapsed_seconds`
- `voltage_v`
- `raw_response`
- `query_elapsed_ms`

CSV 的每一行都是真实 `FETCh?` 样本；GUI 显示最近 10 分钟，CSV 保存本次 stream 全部样本。

`interventions.jsonl` 在每个 diagnostic run 创建一次，通过 `stream_id` 与 Live CSV 关联。每行只有：

- `schema_version`、`run_id`、`seq`、`stream_id`、`intervention_id`
- `phase`（`start` 或 `end`）
- `elapsed_seconds`
- `intervention_type`
- `location`

可选类型为 `cable_disturbance`、`connector_disturbance`、`interface_mechanical_stress`、`other`；Location 必填。同一区间的 start/end 共用同一 `intervention_id`，两个时间之差为持续时间。每行不写系统时间，不包含阈值、异常判断或自动流程字段。若记录异常中断，未配对的 start 原样保留，程序不猜测结束时间。

`errors.jsonl` 是主机侧运行证据，不会查询或消费仪器 error/event queue，也不会增加任何仪器命令。`evidence-verification.json` 会核对它与 `events.jsonl` 中全部 ERROR 事件及 manifest `error_count` 完全一致。若解释器在 RunJournal 建立前被操作系统直接终止，或发生无法执行 Python 清理代码的硬崩溃，本文件不能保证生成或完成写入；这类故障仍需保留操作系统 crash report。

## v1.5.1 能证明与不能证明的范围

v1.5.1 能证明：一次明确 refresh 中 VISA 实际报告的资源、每资源单次身份结果、精确型号对应的批准只读诊断、关键字段在同一 session 首尾的可观察一致性、2182A B0 在本次短观察窗内的 clear/changed/set/incomplete 分类、主机异步结果归属、记录器可用性，以及唯一 GPIB6 2182A Live 的解析、时序、人工干预和主机侧证据一致性。

v1.5.1 不能证明：B0 的原因、FULL ACAL/Autozero 类型或历史完成时间、整个顺序 snapshot 是原子状态、未被 `list_resources()` 返回的地址上是否存在仪器、6221 DC setpoint、2450 trigger-model state、2450 未确认的命令语言、外部校准有效期、样品接线、低温系统热平衡、接地/屏蔽质量、真实噪声谱性能，或量程/NPLC/filter 对具体 device 是否最优。程序不根据人工标签自动判断或控制后续流程，不自动修正配置，不触发、不 INIT、不复位，也不执行多仪器协同。
