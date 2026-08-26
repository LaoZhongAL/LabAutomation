# Diagnostic Core v1.5.2 Release Notes

发布日期：2026-08-26（Asia/Tokyo）

## Modified

- 2182A operation-condition B0 稳定置位时的 Readiness disposition。
- 对应的最小回归测试、应用/profile 版本、README、Release Notes、HANDOFF 与部署包身份。

## Cause

- v1.5.1 把 `set_for_entire_window` 无条件设为 `BLOCKED`。
- 两台不同固件的 2182A 现场证据都完成 51/51 查询：15 个 operation word 均为 `273`，B0、B4 和 operation B8 全程置位，`INIT:CONT=1`，measurement/questionable condition 首尾均为 0，B9 清除，证据校验 32/32 通过。两次运行的唯一 blocker 都是 B0 规则本身。
- 这些证据说明稳定 B0 不能单独识别未完成 ACAL、Autozero 或其他具体原因；它不证明任意状态下的 B0 都无害。

## Change

- 仅当 B0 观察完整且全程置位、同批 15 个样本的 B4 Measuring 也全程置位，且现有 `INIT:CONT?` 回读为 1 时，`acquisition.operation_observation` 才降为非阻断 `WARN`。
- B0 全程清除仍为 `PASS`；B0 发生变化、B4 不连续或 `INIT:CONT!=1` 仍 `BLOCKED`；观察缺失、失败、非法或时间证据不完整仍 `UNKNOWN` 并阻断。
- questionable B8/B9、reading overflow、通信、身份、记录器、sentinel consistency 和 Live compatibility 仍独立 fail-closed。
- `APP_RELEASE_TAG` 更新为 `v1.5.2`，`APP_VERSION` 更新为 `1.5.2-query-only-b0-readiness`，profile 后缀更新为 v1.5.2。JSON 证据字段结构未变，诊断 schema 保持 4。

## Verification

- macOS 源目录完整离线测试：`Ran 177 tests`，173 通过，4 项需要 Tk 显示的 smoke 测试跳过，0 failure/error。
- 直接回归覆盖 operation word `17` 和 `273` 的窄化 WARN 路径、无 B4、`INIT:CONT=0`、B0 变化、观察查询失败、questionable B9 和 reading overflow 阻断路径。`17` 是离线回归值；本次两份新现场证据的原始字均为 `273`。

## Result

v1.5.2 取消了两台现场 2182A 所复现的 B0 单项假阻断，同时保留非连续 Measuring、状态变化、证据不足和独立危险 condition 的硬阻断。这是 Readiness 语义修正，不是校准或性能验证。

## Unchanged

- 不重新推断或改变任何 VISA 地址、四字段身份映射或 GPIB6 Live allowlist；GPIB7 仍仅能诊断。
- 不增加仪器查询、写入、配置、触发、clear、reset、event/error queue 消费、retry 或任意 SCPI/TSP 入口。
- 6221/2450 的查询、Readiness 和 nominal 计数不变；2182A 仍为 51/51。
- ACAL 保持为实验预备流程，不自动执行、判定或操作仪器。
- CSV、`interventions.jsonl`、异步 owner、单 worker 和同一 GPIB 总线顺序访问保持不变。
- 未操作 Git；v1.5.1 及更早 ZIP、SHA 和 Release Notes 不删除、不覆盖。

## Remaining blocker

v1.5.2 尚未在 Windows Tk GUI、NI-VISA/PyVISA 和真实 2182A 上重跑。现场证据支持本次规则修正，但不能代替 v1.5.2 的 Live/FETCh 稳定性、测量精度、噪声或物理性能实测。
