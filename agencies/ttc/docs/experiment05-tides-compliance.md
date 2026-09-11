# Experiment 05：TIDES 合规性与证据边界审计

审计日期：2026-09-10。范围：route 29、2023-11-06，已完成运行
`run-2023-11-06-a39c5491eae9f48b`，四种 decision modes；业务版本
`experiment-05-v1+663770f8d53984d6`。本次只读检查原始记录、代码、已发布结果，
没有修改算法、APC 解释策略或历史输出，也没有重新判定真实行程标签。

## 结论

当前结果通过本项目针对固定 TIDES v1.0 的结构、引用、分配和来源一致性检查，
可描述为“按 TIDES v1.0 组织、带有明确推断假设的研究输出”。
**不能描述为官方 TIDES 认证、已独立确认的实际运营事实，或法律合规证明。**
未发现本次真实输出的 schema/主键/已检查外键失败；但有下列重要语义限制。

优先级是后续完善顺序，不表示已经证明源数据错误：

| 优先级 | 发现 | 影响与边界 |
|---|---|---|
| P1 | 民用日不等于完整运营日 | 当前不检索前一运营日的 GTFS 跨午夜尾部；凌晨归属尚未解决。 |
| P1 | APC 一行一停站、离站代理客流时间 | 用户已批准的实验假设，不是厂家确认定义；同终端区间可能对应方向交接的两行。 |
| P1 | 一对一、无评分门槛的 scheduled assignment | 05 的实验约束，不是 TIDES 的一对一要求，也不是校准后的身份确认。 |
| P2 | 停站覆盖、计划站序及真实端点不完整 | 可选字段留空有效，但不能据此计算完整停站、准点率或实际全程时长。 |
| 发布前置 | 原始数据使用与再分发授权未在本审计证据中建立 | 源码/spec 许可不能替代数据许可；不能据此反向断言数据非法。 |

## 已验证的结构与引用

采用官方 v1.0 固定提交 `b2cb23178e00cb58dbdb1b9a5a8626818278ecbb`。
本地六张表 schema、package profile、table-schema 和许可证共九个文件的 SHA256
均与同提交的干净 upstream checkout 一致；每个包携带这些文件及
[vendor 记录](../schemas/experiment05/tides-v1/vendor.json)。在线对照采用
[官方 v1.0 表定义](https://tides-transit.org/v1.0/tables/) 和
[官方架构](https://tides-transit.org/v1.0/architecture/)，不混用 main 的后续版本。

已完成的可追溯复验：

- 原运行 stage 10：1,483 项检查通过，含四模式各六项客流代理策略完整性检查。
- 本次独立复验：**50/50 PASS**，重新校验实际 CSV、2,232 个发布文件哈希、原始输入
  与行级身份、GTFS 版本/日期/路线及四模式 assignment；三种随机打乱输入顺序后，
  assignment、审计与汇总均保持一致。见[独立报告](../outputs/experiment_05/acceptance/visualization-review/tides-compliance-independent.json)。
- 另外直接重放 `validate_proxy_event_mode`：四模式共 **24/24 PASS**；从 APC visit、
  source lineage 和 assignment 独立构造期望，核对实际事件 CSV 及 provenance，
  不调用 exporter 生成“期望结果”。见 [validation.py:16](../tools/experiment05/validation.py#L16)。

Frictionless 检查必填字段、类型、枚举、非负计数和唯一键；本项目另查复合 trip/visit
引用、vehicle/device/ping 引用、GTFS active occurrence 和 route/direction/shape/block，
并检查实际站序从 1 连续编号。不能把 schema 中的 `foreignReferences` 当作通用
Frictionless 已自动完成所有业务关联。见 [tides.py:616](../tools/experiment05/tides.py#L616)。

未知值写为空 CSV 单元格（schema 的 missing value），不写字符串 `None`/`null`，
不以 0 冒充缺失。UTC datetime 与 `service_date` 分开；无来源证据的 ping、车门和
真实端点不制造。`devices` 未输出，所有 core device ID 留空，避免悬空引用。

本次 GTFS：feed `ttc-d092742e6a5b`，有效期 2023-10-30 至 2023-11-18，
public route `29` 对应 GTFS `route_id=70291`。四个包各含 31 vehicles、352
trips_performed、112,002 vehicle_locations、13,599 stop_visits、38,211 passenger_events。
全局/前期/中期/后期分别分配 244/246/244/227 个 scheduled occurrences；
它们是四个替代实验结果，不能拼接后当作同一天额外发生的 trips。
见[真实运行摘要](../outputs/experiment_05/runs/run-2023-11-06-a39c5491eae9f48b/summary.json)。

## 语义核查：通过校验不等于实际事件已确认

### 1. APC 事件和 stop_visits

TIDES 的 passenger_events 包含车辆到/离站及上下客等事件；`event_timestamp`
必填、描述为记录的事件时间，`event_count` 明确可以表示一次记录中的多个上客。
它不要求每名乘客单独一行，但也没有规定把累计上下客时间设为离站时刻。
见[固定 passenger_events schema](../schemas/experiment05/tides-v1/passenger_events.schema.json)。

当前策略 `apc_stop_interval_departure_proxy_v1` 将一条有效、唯一附着的 APC 行
视为一次停站：到/离站事件采用原始 APC 两端时间，累计 boarding/alighting
均以离站时刻记账；它们**不是在该秒实测的逐乘客事件**。source IDs、原始区间、
代理时间和 `semantics_assumed` 保存在每个包的 diagnostics 中；只拿走 CSV 会丢失
这些精度限制。因此用于交换时必须同时交付[映射约定](experiment05-apc-events.md)
和 diagnostics，而不能仅凭 CSV 表面字段声称严格的实测事件语义。

本次每模式事件分解：到站 13,599、离站 13,599、上客记录 5,462、下客记录 5,551；
后两者 `event_count` 分别合计 21,122 和 19,347。**38,211 是相关事件行数，不是人数。**
计数为零不生成客流事件；缺失或无效计数不填零。未制造 APC vehicle-location ping。

stop_visits 在候选/assignment 之前生成：同车、时间区间、canonical stop/reference、
方向软约束和可用 AVL progress 共同决定唯一归属，然后按原始到站、离站、source ID
排序编号。不是把 GTFS 每个站补齐，也不是仅从 AVL 停车推断全部实际停站。
见 [apc.py:32](../tools/experiment05/apc.py#L32)、[apc.py:172](../tools/experiment05/apc.py#L172)。

独立检查 `stop_visits_evidence.parquet` 与源行发现：

- 13,862 条 route 29 APC 中附着 13,599 条；352 段中 67 段没有 APC visit。
- 已附着行无完全相同 raw-payload SHA，但 1,026 行仍带 `grain_uncertain=True`。
  所有附着行 `NoTrips=1`，仍不足以证明每行都是独立物理停站。
- 按 `(vehicle_id, stop_id, arrival_t, departure_t)` 分组有 **9 组二行同区间**，
  共 18 行；源 APC 南/北方向不同，均在终端 GTFS stop 10415。
  其中五组落入同一 performed segment，四组跨 segment。这可能是终端方向交接的
  分账，不是可直接删除的完全重复源行；现策略会保留两套进/离站事件。
  不应因此把物理停车次数翻倍，也不能在未确认粒度前直接合并或认定旅客被重复计数。

### 2. scheduled identity 与日期

TIDES 明确允许**一个 scheduled trip 对应多个 operated trips**。
05 的三轮、单调、一对一占用及无 score threshold 分配属于实验模型；它可能让被切分的
同一 scheduled occurrence 的另一片段失配，不能以“符合 TIDES”解释该限制。
见 [trips_performed schema:41](../schemas/experiment05/tides-v1/trips_performed.schema.json#L41)、
[assignment.py:1](../tools/experiment05/assignment.py#L1)。
已分配 ID 在 core 中表示模型推断的 Scheduled；未分配保持 null，不自动等价为真实
Unscheduled。没有独立标签，匹配准确率、概率校准及实际延误误差仍未知。
`AvgOTP` 仅保留为 audit 字段，不是本次生产匹配特征。

GTFS service day 可以跨民用日，且时间采用当地中午减 12 小时的基准、允许超过 24:00。
见 [GTFS 官方时间定义](https://gtfs.org/documentation/schedule/reference/#field-types)。
本实现按该基准转 UTC，本次 300 个 active trips 中 28 个含超过 24:00 的时刻。
但原始截取仍是本地民用日，所有段先赋请求日期；前日假设仅作诊断，
`feed_include_previous_day_tail=False`。本次 route 29 的 7,951 AVL、1,132 APC 行
具有前日假设，不代表这些行已确认属于前日，也不代表已覆盖前日尾班候选。
请求日之后凌晨的 AVL/APC 也未被完整补入。因此不能声称完整运营日 coverage。
见 [sources.py:87](../tools/experiment05/sources.py#L87)、
[sources.py:233](../tools/experiment05/sources.py#L233)、
[sources.py:287](../tools/experiment05/sources.py#L287)。

无 offset 的 DST 歧义/不存在时间被标记且不猜 fold；UTC 转换有合成测试，
但本次 11-06 不是切换日，不能拿本次实数结果宣称真实 DST 场景已全面验收。
见 [sources.py:123](../tools/experiment05/sources.py#L123)。

### 3. 可选字段与诊断记录

本次所有 stop_visits 的 scheduled_stop_sequence 和计划到离站时间为空：尚未将
每个实际 visit 映射到 assigned GTFS trip 的具体 stop occurrence。可选字段留空不导致
schema 失败，但目前不能直接由该 CSV 计算逐站 OTP。门别客流和 departure_load
也全部为空，不把总 boarding/alighting 强填到未知车门；总量可由 passenger_events
按类型及 visit key 汇总。见 [tides.py:424](../tools/experiment05/tides.py#L424)。

全部 352 段的 actual_trip_start/end 为空：raw/effective envelope 不是已确认的
起点发车与终点到达；真实行程端点没有被投影窗口边界冒充。
112,002 AVL location 行保留了 1,295 OFF_REFERENCE 和 19,859 路线内重复诊断行，
都仍可带 trip link；这种 link 是源分段归属，不表示该点通过投影、正在载客或属于
连续有效轨迹。trip_type 留空，不把 OFF_REFERENCE 自动叫 deadhead。
本次没有 confirmed censored tail，不能用本次结果验证真实删失尾部的处理准确性。
见 [tides.py:310](../tools/experiment05/tides.py#L310)。

device_id 的 schema 非 required，但描述对 passenger event 空 device ID 给出单设备等
条件；源数据没有设备清单，本次不能独立确认该描述条件。当前保留未知、不伪造设备，
后续应向 agency 确认记录是否已按多设备聚合。

## 许可、数据权限和正式发布边界

源码仓库有 [Apache 2.0 LICENSE](../../../LICENSE)；TIDES 规范的官方页面声明 CC BY 4.0，
本地包保留原 schema、固定提交、SHA 和许可证。CC BY 要求适当署名、许可证链接及
适用时标明修改，也不能暗示官方背书；它并不自动赋予其他数据或隐私相关权利。
见 [CC BY 4.0 官方说明](https://creativecommons.org/licenses/by/4.0/)。

这些许可不等于 TTC 原始 AVL/APC 或所用 GTFS archive 的数据再分发许可。
本审计未获得对应授权合同、数据共享条款或足以覆盖拟议发布用途的许可证据；
datapackage 也没有冒填数据 `licenses`。这表示**权限结论未建立**，不是判定持有或使用非法。
已有[源数据审计](source-data-audit.md#data-handling-note)也要求发布前确认权限。
正式对外提供数据前，应由数据负责人确认来源与用途授权、再分发范围、署名要求、
保留期限，以及车辆轨迹/人员关联的隐私处理；必要时请合格法律人员审核具体条款。

最终推荐表述：**结构与项目一致性校验通过；业务事实、运营日归属及部分事件语义仍为
明确标注的实验推断；不是官方认证，也不是数据使用法律意见。**
