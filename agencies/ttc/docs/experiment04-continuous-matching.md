# 实验 04：连续时空匹配

后续权重对照实验见 [实验 04b](experiment04b-delay-weights.md)：外层绝对延误权重 10、
动态权重 0.1，并配套调整成本尺度门槛；本页记录的原始 04 参数和结果不变。

本次运行范围仅为 **2023-11-06、Route 29**。新核心没有按车辆、线路 ID、日期、时段或预期赢家分支；工作台的数据选择器仍是这一天、这条线路的实验工具，不是已部署的全机构摄取服务。

04 以完整 03d `run-20231106-20260909T183705-d67ffa30723d` 为输入，原样复用 canonical references、序列投影、30 分钟联合空白切分和源记录，重算 global / early / middle / late。这是控制输入的 scorer 对照，不是重新清洗数据；不会改写 03d、源 CSV/parquet 或人工 review。

## 本次完成的运行

运行 ID：`run-20231106-20260910T030622-57e42718b0c1`。306 个分段、31 辆车、四模式共 1,224 个结果；8 个 worker 重算及保存用时 **693.743 s（约 11.6 分钟）**。工作台：<http://127.0.0.1:8767>，默认选中实验 04，03d 仍可选择。

| 模式 | PATTERN_ONLY | UNMATCHED | BOUNDARY_UNCERTAIN | 相对 03d 首选排序变化 |
|---|---:|---:|---:|---:|
| 全局 | 299 | 7 | 0 | 72 |
| 前期 | 299 | 7 | 0 | 48 |
| 中期 | 300 | 6 | 0 | 35 |
| 后期 | 293 | 7 | 6 | 70 |

以上是描述统计，不是精度改善。四模式每个总览均有 300 条诊断首选计划线，另外 6 个分段没有可用诊断候选；没有经校准的正式计划班次候选。PATTERN_ONLY 与“有数值候选但未通过校准”可以同时存在。

验收：950 项 Python 回归、5 项连续界面单元测试、7 项双语单元测试；新04的133项浏览器检查与旧03d的103项浏览器检查全部通过。浏览器报告及截图位于 `artifacts/merge-debug-20231106/experiment04-validation/`。人工 review 数据库 SHA-256 在启动新工作台后仍为 `00bbe13be62749be4909b043c5438e0afff567e8fb8212a295e0984e94535e00`。

全量落盘复核也已通过：1,224 个模式结果、36,170 个候选分数逐项满足成本加和、原始时钟和三层覆盖率恒等式；409 个 source pins、421 个新结果 pins 全部一致。92,143 条 AVL 和 13,862 条 APC 源观测及上游字段不变。报告为 `artifacts/merge-debug-20231106/experiment04-validation/full-run-audit.json`。完整候选及中间诊断约占 3.1 GiB 的压缩 case 文件；保留高召回中间结果有明确的存储成本。

## 架构和数据契约

| 阶段 | 实现 | 契约 |
|---|---|---|
| 多线路命名空间 | `continuous/references.py` | feed_version + route_id 分组，内部 direction + family + reference；不同 reference 的 s 不相减 |
| family / canonical 验证 | `canonical.py` | 几何覆盖、分支、ordered stops、低歧义单调嵌入；最长 shape 不合格则保留多 reference；新模式支持东西走向 |
| 序列投影 | `projection.py` | 所有允许的 references 上评估物理距离、heading、时间、进度、可达速度；方向是 prior，不是过滤器 |
| off-reference runs | `off_reference.py` | 100 m 默认门控；保留原始行、替代 reference 和拒绝原因；孤立异常可跳过，持续 runs / GPS gaps 分片 |
| APC 规范化 | `continuous/apc.py` | canonical stop ID、s、服务区间、上下客、confidence；无坐标不制造 GPS 点；忽略 AvgOTP、vendor assignment、delayed 等派生计划信息 |
| 连续计划线 | `continuous/schedule.py` | 原生 stop_times；departure → 下站 arrival 为连续运动段；dwell 为固定 s 的时间区间；二分查询通过时刻 |
| 候选检索 | `continuous/retrieval.py` | 先兼容 pattern/reference，再按观测位置的计划通过时刻检索 occurrence；每窗口每 pattern 保留 top-K 或 best + delta，并取并集 |
| 窗口 | `continuous/windows.py` | 不重叠 60 s atomic、300/600 s contexts、APC/terminal/GPS/方向/分片 event windows；跨尺度 observation 总权重为 1 |
| 共享延误 | `continuous/delay.py` | 候选整段一次 IRLS 稳健状态模型；时间支持加权、物理变化率约束；禁止每窗口自行重置时移 |
| 整段整合 | `continuous/integration.py` | 所有检索候选重评；七项成本、覆盖率、局部稳健统计、共享过程和一处 change-point 诊断 |
| 车辆全天 | `continuous/sequence.py` | 可选软约束 DP，含 unknown / extra、split、班次、block、时间连续性；本实验只作 shadow 注释，不据此提升最终结论 |
| 校准 | `continuous/calibration.py` | 独立 episode 标签、route/date/episode 分割、held-out 检查、显式 unknown mass、最多三个可信候选 |
| 序列化 / 对照 | `continuous/serialization.py`、`continuous/evaluation.py`、`continuous/shadow.py` | 全部组件与中间证据落盘；JSON round-trip、成本加和、三级覆盖率恒等式检查；可不重新摄取而重做评价 |

数学核心输入时间必须位于同一个、明确声明的时间轴。`clock.py` 提供 GTFS service-day 的 noon-minus-12h 时间定义、超过 24:00 的时间和 DST ambiguous / nonexistent observation 检查。本次 pinned replay **继续使用 03d 的 naive local wall-clock 轴**，不暗中把既有数字解释成 UTC。跨 DST 新摄取必须显式转换双方时间，处理 fold，不能直接复用此实验的 wall-clock 数字。相关规范：[GTFS Schedule Reference](https://gtfs.org/documentation/schedule/reference/)。

## 评分

在每个有效真实 AVL 点的同一 canonical s 上求计划区间，保存原始 residual 区间 `[observed_t - departure_t, observed_t - arrival_t]`。当 delay 状态落入该 residual 区间时，conditioned interval-distance loss 为 0；否则计算到最近端点的差。原始 residual 不被强制写成单个 0。GTFS 非 timepoint / 运动段插值使用更大的时间不确定性。

一对 performed segment / candidate 只拟合一个共享 delay process。状态拟合使用 temporal support × quality，不能用原始点数充当独立时长。孤立 rejected points 不另起 delay 模型；跨真实持续 off-reference / 长 gap 则不施加虚假相邻状态连接。

| 持久化组件 | 默认外层权重 | 含义 |
|---|---:|---|
| `absolute_delay_level_cost` | 1.0 | 有验证来源的历史 prior；无历史时用局部 headway 归一化延误的稳健成本。大延误允许，但不是免费平移 |
| `conditioned_fit_cost` | 1.0 | 扣除该共享 delay process 后，按时间不确定性归一化的稳健 residual |
| `delay_dynamics_cost` | 0.5 | 延误沿真实时间、进度变化的物理合理性 |
| `APC_stop_time_cost` | 0.5 | APC 服务区间与同一个共享 delay 修正后的实际服务站点区间的相容性 |
| `projection_and_coverage_cost` | 2.0 | cross-track residual + 丢失的投影时长比例 + 丢失的 schedule alignment 比例 |
| `pattern_progress_cost` | 1.0 | heading、显式方向 prior、倒退进度、未服务站点；单个有信心的关键站点矛盾另有不被大量干线观测稀释的项 |
| `vehicle_sequence_cost` | 0.0 | 预留给经验证的联合决策；此次为 0，实际 sequence contributions 单独保存在 shadow，不改写局部概率 |

总成本等于七项加权贡献之和，越低越好，**不是概率**。原始组件、权重、贡献、pattern 子项、每点 passage 区间、delay/uncertainty、局部结果均保存。`support_score = exp(-cost)` 仅为展示尺度，也不是校准概率。

窗口不投票，也不各自调平 schedule。整段 score 使用候选共享 delay fit；另外报告 atomic 的 weighted mean / median、trimmed mean、p90、连续支持窗口数、informative coverage、APC 支持和 delay median/MAD。上下文窗口用于检索和诊断，不重复加入整段成本。change-point 是统计假设，还需附近 terminal/gap/direction/reference 等事件佐证才标记 BOUNDARY_UNCERTAIN，不是已确认边界。

## 覆盖率与门槛

三层均报告 observation 数量，以及原始相邻 observation 的 interval 数量和真实时长：

- projection：valid projected / all spatial；
- schedule alignment：schedule-supported valid / valid projected；
- end-to-end：schedule-supported valid / all spatial。

每层都有显式分子、分母、ratio。对每种单位，`projection × schedule = end_to_end`。这里是 **AVL 轨迹的空间证据覆盖率**；有真实坐标的 APC 单独经过空间门控后仍作为 stop-time evidence，无坐标 APC 也继续保留，不混入 AVL 轨迹分母。长缺口仍留在 all-spatial interval 的时间分母里，但不产生可靠连接。单点失败只影响它及相邻边，不会清零整条片段。为向后兼容保留的旧 `schedule_aligned_*_ratio` alias 是 **end-to-end**；请优先读取新的 `coverage_metrics.schedule`。

主要默认阈值：

| 设置 | 默认 |
|---|---:|
| projection gate（上游） | 100 m |
| 最少有效点 / 有效时长 / 有效行进 | 3 / 20 s / 50 m |
| 有效投影质量下限 | 0.2 |
| 连续覆盖/状态最长普通 gap | 90 s |
| 最低 projection / schedule / end-to-end 时长覆盖率 | 0.35 / 0.35 / 0.25 |
| 总成本绝对上限 | 3.0 |
| 单点 schedule 支持 residual | 不超过 4 × temporal uncertainty |
| 核心检索 buffer | 2 × local headway |
| rescue buffer | max(core, 4 × headway, 3600 s) + clock uncertainty + gap rescue |
| gap rescue 增量 | 600 s |
| 检索每 pattern top-K / best + delta | 12 / 1.0 |
| 无邻近班次的 headway 默认 / 最小 / 最大 | 600 / 60 / 3600 s |
| APC 时间成本尺度 | 180 s |
| heading 成本尺度 | 45° |
| 方向 fallback 成本 | 1.0 |
| 倒退尺度 | 100 m |
| delay IRLS prior / smooth 系数 | 1.0 / 0.3 |
| delay 增长 / 恢复最大率 | 0.95 / 3.0 秒每秒 |
| 关键站点矛盾额外权重 | 1.0 |

全部默认值以 `continuous/config.py::defaults()` 及其加载的模块 `DEFAULTS` 为准，并完整写入每次运行的 `config.json`；秒、米、角度、无量纲成本在参数名中标明。Web 允许有界的数值/布尔配置，模型、先验字典和路径不能从请求注入。旧 DTW 的 `score_*` 权重只在 continuous flag 关闭时生效。参考数组索引、GTFS 临时重采样步长不参与新生产成本。

delay 的相邻状态还受 `0.95 × dt + 0.5 × abs(ds)` 的进度相关变化界限约束，3 s/s 不是无条件允许的恢复率。当前 terminal event detector 检查 reference 起点附近（s ≤ 200 m）；末端和复杂 terminal 集合尚未单独配置，不能把它当作完整 terminal detector。

NO_EVIDENCE 表示没有足够可靠数据；NO_CANDIDATE 表示已有足够可靠数据，但没有候选通过绝对质量检查。两者在 `evidence_status` 明确区分。它们不是强制选出最接近班次的理由。

## 校准边界

当前没有独立 route/date held-out 标签或经验证历史 delay prior，所以本实验不会伪造 candidate probability、accuracy 或校准误差。形状证据足够时可以是 PATTERN_ONLY；计划候选仍显示在工作台的诊断前三名，全天总览按既有规则采用诊断首选，且明确不是确认匹配。

校准模块包含 unknown/outside-top3 概率、可信质量门控、scorer feature/code/weight fingerprint、模型 digest 和 feed/route/date 验证域检查。未知新域不会自动沿用旧概率。允许大真实延误不等于已经验证了大延误下的 exact-trip accuracy。

车辆序列 shadow 使用软 block/时间/delay 连续性；没有坐标或独立车辆分配的 terminal/substitution 字段时，对应约束不编造证据。它目前是 annotation-only，尚未成为经校准的最终决策层。重复站点 / loop 的 canonical 进度必须已展开；多义 passage 显式拒绝，不以独立最近点或旧离散 DTW 偷选一个访问。这是保守退避，不是声称已经解决所有 loop 实际数据。

## 复现与 shadow 评价

在 `agencies/ttc` 目录：

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python -m tools.merge_review.gtfs_matching.multimode --source run-20231106-20260909T183705-d67ffa30723d --continuous --workers 8 --label '实验 04 · 连续时空评分'
```

旧模式不加 `--continuous`；已有 source03d 不重写。新 run 中保存 `shadow/{global,early,middle,late}-episodes.json`、`-report.json`、`-vehicle-sequence.json`。报告区分 descriptive 排序/状态变化与独立标签指标，按 route、headway、shape structure、data quality 分层。此次只有一个 route/date，不能声称验证跨线路推广效果。

有独立标签后可以重新评价保存的分数，不重新摄取：

```sh
uv run --offline --no-sync python -m tools.merge_review.gtfs_matching.continuous.shadow --episodes PATH_TO_RUN/shadow/global-episodes.json --labels verified-labels.json --output new-independent-report.json
```

标签需要 `case_id, feed_version, route_id, service_date, episode_id, split, verified=true, independent=true, label_source`，以及 `true_trip_id` 或 `no_scheduled_trip=true`。`label_source` 只能是 `manual_verified / external_ground_truth / independent_validated`；train、validation、test 不得共享 route、date 或 operating episode。只有 test 标签进入最终精度报告。可选 `true_pattern_id / adjacent_trip_ids / true_delay_s / boundary_times_s` 支持相应指标。

报告包括 retrieval recall、pattern accuracy、exact trip accuracy、adjacent-trip confusion、diagnostic/reported top3 coverage、非退避 precision、abstention、ECE、delay MAE、boundary precision/recall/F1 和 runtime；缺少标签或必要属性时为 null 并附原因。旧 03d 未保存每段独立运行时间时，不会虚构 old runtime；04 另外记录完整 replay 墙钟时间。

## 测试与验收

新数学、集成、校准、sequence、序列化和 UI 测试与原有 canonical/projection/off-reference 测试一起运行。涵盖相邻同 shape 班次、高频、长延误、提前运行、停站、稀疏 AVL、GPS gap、孤立/持续 off-reference、分支/short-turn、反方向近邻、重复站、loop/retrace 保守退避、feed/date 隔离、>24:00、DST、四模式隔离。

一般不变性测试更改 GTFS 临时 5/10/15/20 s 重采样设置、新增共线 shape 顶点，以及改变 AVL 采样密度。检查 candidate ranking、status、coverage 和容差内成本，而不是为某个历史赢家写规则。9042 只作为一项只读历史回归，不是标签或调参目标。

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python -m pytest tests tools --ignore=tools/labeling_ui -q --tb=short
node tools/merge_review/tests/test_continuous_ui.mjs
node tools/merge_review/tests/test_i18n_runtime.mjs
```

HTTP/浏览器测试需要本机 socket 权限；`tools/labeling_ui` 是旧的独立 app，因其依赖/模块冲突不属于本次回归集合。
