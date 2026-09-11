# Experiment 05：APC 停站与乘客事件的版本化映射约定

## 适用范围与假设

本版本按用户确认的研究假设，将一条通过关联校验的 APC 记录解释为
同一车辆在一个站点的一次停站；该记录的 boarding/alighting 是这次停站的
累计上下车人数。这个假设不是已获得供应商认证的源数据定义，也不是 TIDES
要求所有 APC 厂商采用的粒度。原始 `grain_uncertain` 等诊断不被改成已验证。

生产配置 `config/experiment05-v1.toml` 明确选择：

```toml
[export]
passenger_event_policy = "apc_stop_interval_departure_proxy_v1"
```

旧的 `omit_without_event_semantics` 仍可显式选择；standalone 导出 API 在没有
提供 policy 时保持旧的保守行为。策略变化改变 config hash、代码内容版本和
cache key，不修改已经发布的旧运行。原始数据、匹配前证据、trip 分割、候选
评分/排序与三轮 assignment 不受这项**导出策略**影响。

## 时间、数量和事件类型

只对有可追溯 APC source row、有效到离站区间、唯一停站/实际行程归属的 visit
应用此策略。保留真实 source row identity，不将 APC 伪装成 AVL 坐标点。

| TIDES event_type | event_timestamp | event_count | 时间含义 |
|---|---|---|---|
| `Vehicle arrived at stop` | APC 原始到站时间 | 1 | 源记录的车辆到站时间 |
| `Vehicle departed stop` | APC 原始离站时间 | 1 | 源记录的车辆离站时间 |
| `Passenger boarded` | APC 原始离站时间 | 本次停站累计上车人数 | 以离站时刻记账的代表时间，不是逐乘客实测时刻 |
| `Passenger alighted` | APC 原始离站时间 | 本次停站累计下车人数 | 同上，不声称下车全部发生在离站瞬间 |

例如 APC 记录表示到站 10:00、离站 10:01、上车 5 人、下车 2 人，则生成到站、
离站和两条累计客流事件；两条客流事件均标记在 10:01。不会制造七条逐乘客事件，
也不会假定上车与下车的先后顺序。时间经既有时区归一化后按 UTC 导出。

人数为零不生成该类型的客流事件；缺失不填零，负数、非整数、不完整或倒置时间
等问题仍要拒绝/记录诊断，不能用本假设绕过数据完整性检查。停站累计人数不按
裁剪后的业务时长分摊。GTFS 匹配继续使用 APC 时间**区间**，而不是导出的代理时间。

每条导出事件的诊断保留 source row IDs、stop visit、原始到离站区间、策略 ID、
时间来源及是否为代理时间/未经独立确认的语义假设。这些信息写入 package 的
`diagnostics.json`，不添加到 TIDES core CSV 字段。没有真实事件级位置关联时，
`location_ping_id` 保持 null；不随意选择最近 AVL ping 或车门。

TIDES v1.0 允许 `event_count` 表示多次同类事件，同时要求事件时间戳；但并没有
规定停站累计客流必须用离站时间。本文件定义的是本项目的显式研究映射约定。
结构 validator 通过不等于源字段语义或时间精度已被独立证实。

## 当前 stop_visits 到底如何确定

生成发生在 pipeline 第 6 阶段，早于 GTFS 候选生成和最终 assignment：

```text
独立归一化的 APC 原始行
  → 映射站点到 canonical reference 的停站位置/区间
  → 在同车 performed trips 中检查时间、reference、站点、方向和进度
  → 唯一最优 trip + 唯一 stop_id
  → 一条 source APC 行生成一条 stop_visit
  → trip 内按原始到站时间、离站时间、source row ID 排序并编号
```

具体规则与当前默认值均以 `[apc]` 及 matcher/projection 配置为准：

1. 到站/离站时间均有效，离站不早于到站；区间长度不超过
   `maximum_service_interval_s = 1800` 秒。
2. 只在相同 vehicle ID 的 performed segments 中寻找归属。APC 区间与 segment
   有效业务区间可以重叠；不重叠时，两区间之间的距离不能超过
   `attach_time_buffer_s = 120` 秒。已确认删失的尾段不得因关联而被重新引入匹配。
3. 必须存在与该 trip 有效 AVL reference 一致的、非歧义 canonical stop 映射。
   没有真实 APC 车辆坐标并不导致删除；它仍是站点加时间区间证据，不是 GPS ping。
4. 若区间附近有可比较的有效 AVL 点，站点与 AVL 的最小 canonical progress
   差不得超过 `attach_progress_tolerance_m = 200` 米。这不是“每个点都必须在
   200 米内”，也不是用最近的 AVL 时间覆盖 APC 到离站时间。
5. 按 `(方向冲突, 时间间隔, -重叠时长, 进度误差)` 从小到大选择。方向冲突是
   软惩罚；有并列最优 trip 时保留歧义，不凭文件行序硬选。没有附近 AVL 时，
   仍可能依靠其他证据关联，不把缺少局部进度比较说成已观测一致。
6. trip 归属选定后，只有 stop 映射能收敛到唯一 stop_id 才生成 visit。
   `stop_visit_id` 由稳定 performed trip ID 与 source row ID 构造；
   `trip_stop_sequence` 从 1 开始连续编号。重复访问同一站不会仅因 stop_id
   相同而合并。
7. 实际到离站时间直接来自该 APC 行，dwell 是两者差；不从 GTFS schedule
   倒推实际停站。导出为整数秒 dwell 时不任意舍入无法表示的值。

## 当前边界与已知限制

- 这是一套 **APC 驱动的 observed stop-visit hypothesis**，不是从 AVL 轨迹独立
  识别出的完整停站清单。缺 APC 的实际停站不会自动补造；没有匹配 GTFS scheduled
  trip 并不妨碍一个有证据的 performed trip 保留 stop_visits。
- 目前一条 APC 源行对应一条 visit，不把邻近行合并或对 APC 做不可逆去重。
  配置中的 `visit_merge_gap_s` 目前没有用于合并逻辑，不能理解成正在执行 30 秒
  停站合并。重复/重叠 APC 区间需要独立审查，不能自动理解成不同乘客活动。
- 实际停站编号是**观察顺序**，不是 GTFS `stop_sequence`。当前 stage 6 不自动
  将 visit 映射到最终候选的某一次 scheduled stop occurrence；没有明确映射时，
  scheduled_stop_sequence 和计划到离站时间保持 null。事件通过 actual trip 和
  stop visit 关联，并采用所属 mode 的 trip_id_scheduled，不跨 mode 借用 assignment。
- `boarding_1/2`、`alighting_1/2` 有车门语义；没有车门分配证据时不把整车人数
  随意填入某个车门字段。人数在 passenger_events 的 event_count 和共享 APC 证据中保留。
- 后续如获得厂商字典或设备时间语义，应以新 policy ID 修订，而不是静默改变本版本。

实现入口：`tools/experiment05/apc.py:attach_apc`、
`tools/experiment05/tides.py:build_tides_rows`。标准固定版本及验证方式见
[TIDES 导出契约](tides05-contract.md)，各 stage 的数据结构见
[输出 schema](experiment05-output-schema.md)。
