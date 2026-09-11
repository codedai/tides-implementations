# 实验 04c：独立校准输入契约

当前没有满足以下要求的真实独立标签，也没有已训练的运营先验。本文只说明输入；
算法和实验结果见 [04c 主说明](experiment04c-occurrence-plausibility.md)。

## 1. 候选排序概率校准的标签

`continuous/train_calibration.py` 接受 JSON 数组、JSONL 或 CSV。
每条记录表示一个独立 operating episode 的代表 case；同一 episode 的多个分割片段
不能作为多个独立校准样本。真实标签必须来自独立核验，不能用当前 matcher 的第一名、
旧工作台点击、AvgOTP 或上游已有 schedule assignment 自动生成。

严格模式要求：

- 身份：`feed_version`、`route_id`、`service_date`、`case_id`、`episode_id`；
  `(feed_version, route_id, service_date, case_id)` 不得重复。
- 证据：`label_id`、`evidence_id` 为非空字符串；`evidence_sha256` 为来源证据的
  64 位小写十六进制 SHA-256；`verified: true`、`independent: true`。
  `label_source` 只能为 `manual_verified`、`external_ground_truth` 或
  `independent_validated`。`schedule_derived` 和 `uses_upstream_schedule_assignment`
  不得为真，建议显式写 `false`。声明与哈希不能替代人工核验或证明来源真实性。
- 真值二选一：非空 `true_trip_id`，或者 `no_scheduled_trip: true`。
- 分割：`split` 为 `train`、`validation`、`test`，完整数据必须同时包含三者。
  **route_id、service_date、episode_id 三个维度必须分别跨 split 不重叠**，
  不只是组合键不重叠。因此只有 2023-11-06 / route 29 不能构造合格留出集。
- 模型输入：非空 `candidates` 列表，稳定且唯一的 `candidate_id`（可用 `trip_id`
  替代）、`trip_id` 和 `score`；应保留完整评分候选，不只保留已通过门槛的结果。
  校准器按 score 排序，需要有效的有限数值候选特征。
  每条标签的 `scorer_fingerprint` 必须与 CLI 参数及被评价评分版本一致。
  CLI **不会**自动将 shadow 候选补到标签的 `candidates` 字段。

以下仅为**一条合成格式示例，不是真实标签，不足以训练，也不得激活**；虚构的
`true` 声明、ID、分数和哈希均只展示格式：

```json
[
  {
    "feed_version": "SYNTHETIC-feed", "route_id": "SYNTHETIC-train-route",
    "service_date": "2000-01-01", "case_id": "SYNTHETIC-case",
    "episode_id": "SYNTHETIC-train-episode", "split": "train",
    "label_id": "SYNTHETIC-label", "evidence_id": "SYNTHETIC-evidence",
    "evidence_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "verified": true, "independent": true, "label_source": "manual_verified",
    "schedule_derived": false, "uses_upstream_schedule_assignment": false,
    "true_trip_id": "SYNTHETIC-trip-1", "scorer_fingerprint": "SYNTHETIC-ONLY",
    "candidates": [
      {"candidate_id": "SYNTHETIC-candidate-1", "trip_id": "SYNTHETIC-trip-1", "score": 0.2},
      {"candidate_id": "SYNTHETIC-candidate-2", "trip_id": "SYNTHETIC-trip-2", "score": 1.3}
    ]
  }
]
```

默认至少 60 个 train、40 个 validation episode；每个启用的特征 bin 在两者中
分别至少有 15 个样本，test 必须非空。满足数量不保证通过校准，还要通过留出可靠性
检查；test 不参与拟合和模型选择。模型的适用范围仍受评分 fingerprint、特征支持域
及经过独立验证的 feed / route / service_date 限制。

可选独立真值字段：`true_pattern_id`、`true_delay_s`、`adjacent_trip_ids`、
`boundary_times_s`、`operating_regime`，分别支持对应评价。边界时刻必须与输出使用同一
时间轴；没有独立 regime 标签时，不要把 `inferred_delay_regime` 当作真值。
CSV 中布尔字段使用字面量 `true` / `false`，列表字段使用 JSON 字符串；推荐 JSON。

## 2. 实际离线命令与输出

从 `agencies/ttc` 运行。`--old-results` / `--new-results` 都支持 episode 数组，
也支持已有 `{ "old": [...], "new": [...], ... }` shadow envelope，分别读取对应侧。
同一个文件可传给两个参数。输出目录须存在，输出文件必须是新文件。

```sh
CALIBRATION_SHADOW='artifacts/merge-debug-20231106/runs/run-20231106-20260910T063603-e4bf6bc60823/shadow/global-episodes.json'
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.gtfs_matching.continuous.train_calibration \
  --old-results "$CALIBRATION_SHADOW" --new-results "$CALIBRATION_SHADOW" \
  --output artifacts/merge-debug-20231106/experiment04c-validation/calibration-availability.json
```

这条命令没有提供标签，预期为 `UNCALIBRATED`，不是训练成功。将来取得合格真实标签后，
在同一命令增加 `--labels /path/to/verified-labels.json` 和
`--scorer-fingerprint '与标签及实际评分结果一致的fingerprint'`。
四种决策模式应分别使用对应 shadow 文件，并与标签中的候选/评分 fingerprint 对齐。

工具记录输入文件内容哈希；结果即使通过也仅为 `FITTED_NOT_ACTIVATED`，
`activated` 始终为 `false`，不会修改工作台配置或重跑匹配。
没有独立标签、所需真值字段、支持域或有效分母时，相应指标为 `null` 并附可用性原因；
`null` 不等于 0。排名变化、候选数量、耗时、abstention 描述可计算，但不是准确率提升。
缺少有效校准器时，候选概率保持未校准，不能据此宣称已确认 scheduled occurrence。

## 3. 运营延误先验是另一种 artifact

上述 CLI **不训练运营先验**；输出固定注明
`OPERATIONAL_PRIOR_NOT_TRAINED_BY_RANK_CALIBRATION_UTILITY`。
`operational_prior.py` 只消费外部训练、独立验证过的
`continuous_operational_prior_artifact` **JSON 对象**，不是文件路径。

artifact 必须包含 `schema_version: operational-delay-prior-v1`、
`independently_validated: true`、非空 `validation_artifact` 引用、完整
`training_provenance` 和非空 `records`。provenance 要求：

- `independent_verified: true`；`split_policy` 必须精确为
  `route_id AND service_date AND episode_id disjoint`。
- `labels_sha256` 是标签集合的规范化内容摘要；应使用
  `evaluation.validate_independent_labels(..., require_complete_splits=True,
  require_provenance=True)` 返回的 `training_provenance`，不能混用 CLI 的原始文件哈希。
- `split_counts` 对三种 split 均为正整数；`split_groups` 在各 split 下包含非空、
  无重复的 `route_id` / `service_date` / `episode_id` 字符串数组，各数组长度不能超过
  对应样本数，且三个维度分别跨 split 不重叠。

以下是**合成 artifact 格式示例，不是训练产物；禁止复制为真实历史**：

```json
{
  "schema_version": "operational-delay-prior-v1",
  "independently_validated": true,
  "validation_artifact": "SYNTHETIC-ONLY-NOT-A-VALIDATION-REPORT",
  "training_provenance": {
    "independent_verified": true,
    "labels_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "split_policy": "route_id AND service_date AND episode_id disjoint",
    "split_counts": {"train": 1, "validation": 1, "test": 1},
    "split_groups": {
      "train": {"route_id": ["SYNTHETIC-A"], "service_date": ["2000-01-01"], "episode_id": ["SYNTHETIC-A"]},
      "validation": {"route_id": ["SYNTHETIC-B"], "service_date": ["2000-01-02"], "episode_id": ["SYNTHETIC-B"]},
      "test": {"route_id": ["SYNTHETIC-C"], "service_date": ["2000-01-03"], "episode_id": ["SYNTHETIC-C"]}
    }
  },
  "records": [{
    "record_id": "SYNTHETIC-global", "context": {}, "sample_count": 1,
    "centre_s": 0, "routine_early_scale_s": 120, "routine_late_scale_s": 300,
    "disruption_early_scale_s": 600, "disruption_late_scale_s": 1800,
    "disruption_probability": 0.05
  }]
}
```

每个 record 的 `sample_count` 必须为正整数，`centre_s` 为有限秒数，四个尺度为正秒数，
disruption 同侧尺度不小于 routine，混合概率严格介于 0 和 1。每个 record 都要给齐参数。
`context` 只能是层级前缀：`{}` → `feed_version` → 加 `route_id` → 加 `direction`
→ 加 `observed_time_bucket`，同一 context 不得重复；选择最具体的匹配记录。
候选 trip ID 和候选计划时刻不得进入 context。

API 接受整数小时桶 0–47；当前 04c 工作台从共同观测的本地墙钟小时生成 **0–23**，
所以外部训练 artifact 必须采用同一约定，不能自行把凌晨 1 点写成 25。
缺少匹配历史时使用明确标注 `uncalibrated_operational_defaults` 的工程参数，
默认先验尾部不会被检索器称为已验证 empirical tail。
