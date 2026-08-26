# Diagnostic Core v1.5 Release Notes

发布日期：2026-08-26（Asia/Tokyo）

## Modified

- inventory 切换后的故障场景残留。
- inventory、diagnostic、Live、single-fetch 的主机侧异步事件归属。
- 2182A 与 2450 的型号专属非消费状态读取和 Readiness。
- 三型号同一 VISA session 内的关键字段首尾复读。
- 2182A 型号诊断与 GPIB6 Live 资产授权的错误耦合。
- configuration snapshot、GUI、manifest 和部署文档的发布身份。

## Cause

- 新 inventory target 可能继承只适用于旧型号的模拟故障场景。
- 除 Live 终止事件外，后台结果没有冻结完整操作归属，迟到事件可能污染后续选择。
- 2182A 缺少 autozero、FAZERO、LSYNC 和 condition 状态；2450 缺少三个批准的 condition 属性。
- 顺序查询只能证明各字段曾被读取，不能证明关键配置在一次诊断的开始和结束保持一致。
- 旧 `gpib6-2182a-ch1-10mv-nplc5` Readiness 把资源地址误作实验配置模板，导致精确 GPIB6 资产被强制要求 50 Hz、CH1、NPLC=5、10 mV 和固定滤波/触发设置；同型号其他地址却按型号合法域判断。

## Change

- 新 target 不支持当前故障场景时，同时把显示值和内部选择复位为 `nominal`。
- 每次后台操作使用不可变 owner；事件消费前先完整匹配 operation、kind、mode、target、snapshot、run 和 stream。Live 继续保留 `stream_id` 第二层闸门。
- 2182A 基础诊断由 22 条扩为 28 条精确只读查询；condition 使用型号专属 mask，且不套用 6221 的 B10 Idle。
- 2450 只窄放行 `status.condition`、`status.operation.condition`、`status.questionable.condition` 三个只读属性，不推断可编程 bit 或 trigger state。
- 6221 的 DC setpoint 仍保持不可观察；未批准 `SOUR:CURR?` 或其他推断 alias。
- 三型号在同一 session 末端复读 profile 指定字段；稳定字段按布尔、有限数值或规范化文本比较，动态 condition 只在两端分别检查。无 retry。
- 整段删除旧 GPIB6/CH1/10 mV/NPLC5 固定配方。所有 2182A 先使用同一型号规则读取并解释当前通道、量程、NPLC、滤波、工频与触发设置；精确 GPIB6 资产只追加现有标量电压 Live 所需的 `VOLT:DC`、单样本、连续采集、ASCII 和单一 `READ` 兼容检查。
- `38/38` 明确归属于 2182A 共享诊断 profile，不归属于 GPIB6 地址。
- `APP_VERSION` 更新为 `1.5.0-query-only-snapshot-ownership`，诊断 schema 更新为 3。

## Verification

在 macOS 开发环境使用 `/usr/bin/python3` 运行完整离线测试：168 项，164 通过，4 项需要 Tk 显示的 smoke 测试跳过，0 failure/error。

## Result

P1 的历史残留、异步事件归属、型号关键状态和同会话首尾一致性均有直接行为测试；精确 GPIB6 在 CH2、60 Hz、NPLC=10 及不同合法量程、滤波和触发设置下仍通过型号诊断与 Live 兼容回归；精确 query allowlist、唯一 GPIB6 Live allowlist、manifest/JSONL/CSV 证据校验保持通过。

## Unchanged

- 不重新推断或改变任何 VISA 地址和四字段身份映射。
- 不增加仪器写入、配置、触发、clear、reset、retry 或任意 SCPI/TSP 输入。
- GPIB6 2182A 仍是唯一获批 Live 资产；仍为单 worker、单总线顺序执行，不实现多仪器协同。
- readout CSV 与独立 `interventions.jsonl` 语义不变。
- 未操作 Git，旧发布 ZIP 未删除或覆盖。

## Remaining blocker

Windows Tk GUI、NI-VISA/PyVISA 现场链路和真实仪器 query-only 验证尚未在本次开发环境执行。
