# 实验 04c：分离运营延误、班次混淆与检索

本实验直接基于 **04**，不是 04b。实验范围仍为 2023-11-06、route 29；
原始观测、方向/30 分钟空档分割、canonical references、100 米投影门控和四种决策
切片沿用 04 已固定的输入。原有 03d、04、04b 结果不覆盖。

## 三个不同的问题

1. **运营延误先验**：这种提前/晚点在运营上有多合理？使用物理秒尺度的非对称
   routine/disruption 混合分布。所有竞争候选共用从观测得到的时间段、线路和方向
   上下文；不再用每个候选自身的 headway 缩放先验。没有独立历史时，参数明确标为
   工程假设，不是校准后的运营概率。
2. **局部班次位移**：为解释观测，需要跨过多少个可混淆班次？在当前观测 s 范围内，
   按几何和有序停站兼容性构建共同候选类；记录前后计划通过间隔、其来源和可信度。
   单独计算跨过的竞争通过时刻数，以及超出相邻通过时刻中点所界定时间单元的距离。
   精确 pattern ID 不同本身不应产生有利的延误分母。
3. **高召回检索**：哪些候选值得计算？核心集合保留最近通过班次和直接前后邻居，
   rescue 保留额外相邻班次以及有延误尾部/车辆序列证据支持的假设。进入 rescue
   并不豁免前两项代价，也不代表与核心候选同等的先验概率。

缺少局部邻居时使用层级统计回退，并暴露 `headway_source`、
`headway_confidence`、`previous_headway_s`、`next_headway_s`。
不能仅因狭窄 exact-pattern 分组内没有相邻班次而替换成 3,600 秒。
跨 reference 的原始 s 坐标不能直接相减，必须先验证几何/进度映射。

局部集合以**观测**到的有序停站约束为共同条件，不以每个候选自身的完整 stop
pattern 建立私有邻域。可靠 APC 必须同时落在当前 AVL 的时空覆盖内才能作为
硬子序列约束；覆盖外或低可信度 APC 仍保留为 APC 证据，不因此丢弃。
没有实际停站证据区分的计划停站顺序冲突，应作为未消除的模式歧义报告。
active feed pool 中的前一 service day 跨午夜尾班按原始绝对时间参与竞争；
若调用者显式指定 service_date，则检索和整趟评估使用同一个限定集合。

## 评分和身份保护

`continuous_occurrence_model_enabled` 是新特性的开关，默认关闭，以保留旧实验语义。
04c 显式开启；保持 04 的外层绝对延误权重 **1.0**、动态权重 **0.5**，
另报告独立的 `occurrence_displacement_cost`（默认权重 1.0）。
旧七项成本仍逐项保存，新模型共八项，原始成本 × 权重 = 加权贡献。
未校准时不为了产生 MATCHED 而放宽质量门槛。

新运营先验的默认 routine 提前/晚点尺度为 120 / 300 秒，disruption 为
600 / 1,800 秒，disruption 混合权重为 0.05，分量为非对称双侧 Student-t3。
绝对延误成本为相对于分布众数的负对数密度差；这些是可配置的未校准假设，
不是从本日匹配结果反推的历史规律。局部位移成本为
`crossing_weight × crossed_occurrences + cell_weight × outside_cell_s / common_local_headway_s`；
两个内部权重默认均为 1。缺少可信的班次位置证据不会被当成零成本。

延误状态的默认 atomic 时长为 60 秒、最小事件间隔为 15 秒；超过 90 秒的
观测空档断开状态连接。投影距离、GTFS 端点 timepoint 属性及插值、停站、终点
和观测间隔进入时间不确定性，动态成本不按 GPS ping 数累计。

共享延误仍对每个 performed-fragment/candidate 一次拟合，局部窗口不能自行平移。
新动态项在不重叠的物理时间状态上计算，并纳入投影、计划插值、停站/终点和
观测缺口的不确定性；保留逐观测回写结果和 fragment 分隔。
APC 无真实坐标时仍仅为停站时间区间证据，AvgOTP 等上游计划派生字段不进入评分。

跨过至少一个局部可混淆班次的候选，不能仅因平移后拟合好获得 MATCHED。
身份保护要求明确、可验证且没有进入延误拟合的独立班次证据；普通 APC 时间拟合、
终点接近或未验证的车辆序列软分数不能自动豁免。诊断候选仍可展示，不能伪装为
最终已确认班次。保护诊断和位移统计与候选分项一起保存。

## 运行与复核

从 `agencies/ttc` 对指定 **04** 结果重新匹配，复用已校验投影，重算四种模式：

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.gtfs_matching.multimode \
  --source run-20231106-20260910T030622-57e42718b0c1 \
  --occurrence-model --workers 8 \
  --label '实验 04c · 运营延误先验与局部班次位移'
```

输出为新的 append-only run，包含配置、输入/代码哈希、逐点延误、候选分项、
检索来源、四模式全天总览和 shadow reports。所有阈值与权重来自
`continuous/config.py::defaults()` 及其加载模块，完整写入运行配置。
冻结包可以保存独立 04c profile；已有旧冻结包不修改。

### 本次已完成的重算

- 输入 04：`run-20231106-20260910T030622-57e42718b0c1`。
- 输出 04c：`run-20231106-20260910T063603-e4bf6bc60823`。
- 306 个片段 × global / early / middle / late，8 workers，批处理耗时
  605.634 秒。该时间是复用 04 投影后的匹配重算，不是从原始 CSV 开始的耗时。
- 完成记录及 421 个输出 SHA-256 pins 已通过工作台加载检查。
- 本次评分代码保存在
  `releases/workbench-04c-20260910T065019-6915fbec`。
  此后仅修复离线评价对旧版本 `occurrence_displacement: null` 的兼容性，
  并将本文加入冻结包；这些改动没有重写或重算本次候选分数。
  最终启动版本由 `config/workbench-release.json` 指定，旧快照保留。

逐模式 shadow 报告在输出 run 的 `shadow/` 下；进一步独立检查和浏览器验收
报告在 `artifacts/merge-debug-20231106/experiment04c-validation/` 下。
正式 scheduled-occurrence 结果没有被强制升级为 MATCHED：缺少校准时仍保留
PATTERN_ONLY 等状态，工作台展示明确标注的诊断候选供人工复核。

## 校准限制

当前工作区没有满足本次要求的独立 trip 标签。旧 labeling UI 中的少量选择没有
独立性、验证和 episode/split 绑定，不能自动转成 ground truth。
单一 route/day 也不能支持跨 route **且**跨 service day 的留出验证。

评价流程要求 route、service_date 和 operating episode 在 train / validation /
test 之间分别不重叠；训练运营先验、选择超参数与最终测试不得共用运行片段。
按 headway regime 分别报告 exact-trip accuracy、adjacent-occurrence confusion、
retrieval recall、top-3 coverage、non-abstained precision、abstention rate 和 ECE。
没有独立标签的指标为 null；无标签的排名变化、候选数和运行时间仅是描述性结果，
不能宣称算法准确率提高。概率仍保持未校准，不强迫输出 top-1。
