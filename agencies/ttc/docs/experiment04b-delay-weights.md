# 实验 04b：加强绝对延误水平、降低延误动态成本

范围仍为 **2023-11-06、Route 29**。来源是完整实验 04
`run-20231106-20260910T030622-57e42718b0c1`，新实验单独落盘，不覆盖 04。
原始 AVL/APC、canonical references、序列投影、30 分钟联合空白切分，以及
global / early / middle / late 的证据选择范围全部保留。

## 本次结果

新运行：`run-20231106-20260910T041452-de4609d0825b`。
31 辆车、306 个分段，四模式共 1,224 个结果；重算及保存耗时 **770.294 秒
（约 12.8 分钟）**。主工作台默认选中 04b，旧 04 保留。

| 模式 | 首选候选相对 04 改变 | PATTERN_ONLY | UNMATCHED | BOUNDARY_UNCERTAIN |
|---|---:|---:|---:|---:|
| 全局 | 16 | 299 | 7 | 0 |
| 前期 | 9 | 299 | 7 | 0 |
| 中期 | 11 | 300 | 6 | 0 |
| 后期 | 4 | 293 | 7 | 6 |

上述为描述统计，不能解读成准确率提升。各模式状态未变，但首选排序以及数值质量
门槛的通过集合可以改变；无独立校准仍不产生正式计划候选或伪造概率。
各模式总览保留 300 条诊断首选计划线，另外 6 个分段没有可用候选。

## 参数与门槛政策

| 配置 | 04 | 04b |
|---|---:|---:|
| `continuous_absolute_delay_weight` | 1.0 | 10.0 |
| `continuous_delay_dynamics_weight` | 0.5 | 0.1 |
| `continuous_match_cost_max` | 3.0 | 7.3 |
| `continuous_calibration_max_score` | 3.0 | 7.3 |
| `continuous_local_support_cost_max` | 3.0 | 16.5 |
| `continuous_boundary_penalty` | 0.5 | 2.75 |
| `continuous_boundary_gain_min` | 0.5 | 2.75 |
| `continuous_local_missing_cost` | 隐式 4.0 | 显式 22.0 |
| `continuous_apc_support_cost_max` | 借用 local 门槛 3.0 | 独立保持 3.0 |
| `sequence_local_score_weight` | 1.0 | 30/73 ≈ 0.4109589041 |

其余最终评分权重不变。特别是 **delay fitter 内部的 prior / smooth 权重仍为
1.0 / 0.3**；新 10 / 0.1 作用于最终候选成本，不改变共享延误曲线的拟合目标。
同一候选因此应保留完全相同的拟合延误、原始分项与三层覆盖率。

选择门槛使用可解释的“权重预算归一化”，不是用某辆车、某个赢家或目标通过率调参：

- Whole score 权重和由 6.0 变为 14.6，比例为 73/30；保持基线的
  `score / sum(weights) <= 0.5` 成本预算，得到 `3 × 73/30 = 7.3`。
- Local AVL cost 只有 conditioned fit 和 absolute delay 两项，权重和由 2 变为
  11，比例为 5.5；其支持门槛、boundary penalty/gain 和 missing cost 采用此比例。
- APC 支持门槛衡量的是**未加权 APC 残差**，必须与加权 AVL local 门槛拆开，继续为 3。
- 缺失窗口使用独立 local missing cost 22，不能沿用 whole gate + 1 = 8.3，
  否则缺失证据可能比正常窗口更便宜。配置要求 missing cost 严格大于 local 支持门槛。
- 车辆全天 decoder 仍是 annotation-only shadow。其输入总分乘以 30/73，
  保持与未变的 transition / unknown 成本之间的数值尺度，不据此提升最终匹配。

这只是实验性的成本尺度政策，**不是统计校准，也不保证与 04 相同的通过集合**。
不同成本项具有不同经验分布，单凭权重和不能推导准确率或通过概率。
例如理想情况下固定一 headway 延误仅水平项贡献 5；两个 headway 贡献 18.75，
仍超过新门槛 7.3。绝对延误相对于选择门槛的约束确实比 04 更强。

以下设置不变：100 m 投影门控、最少点数/时长/行进距离、projection / schedule /
end-to-end coverage 门槛、4σ schedule 支持、APC 时间尺度、headway、检索 top-K /
best+delta / core/rescue buffers、90 s 连续性界限、概率与 credible-set 门槛。
检索粗成本不是新的七项加权总分，不随它缩放。

当前没有独立校准标签或历史 prior；旧模型的指纹不能沿用。校准 score/margin bins
保持未训练配置，不声称已完成校准。正式候选和概率继续遵守 abstention，诊断前三名
及全天总览的诊断首选保留，不能把质量门槛通过等同于已确认 scheduled occurrence。

## 运行与复现

在 `agencies/ttc` 目录执行：

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.gtfs_matching.multimode \
  --source run-20231106-20260910T030622-57e42718b0c1 --workers 8 \
  --continuous-absolute-delay-weight 10 \
  --continuous-delay-dynamics-weight 0.1 \
  --continuous-match-cost-max 7.3 \
  --continuous-calibration-max-score 7.3 \
  --continuous-local-support-cost-max 16.5 \
  --continuous-local-missing-cost 22 \
  --continuous-apc-support-cost-max 3 \
  --continuous-boundary-penalty 2.75 \
  --continuous-boundary-gain-min 2.75 \
  --sequence-local-score-weight 0.410958904109589 \
  --label '实验 04b · Route 29 · 绝对延误 ×10／动态 ×0.1'
```

从已经启用连续评分的 04 来源运行，不需要再次传 `--continuous`。
明确的 override 会重新计算全部四模式的局部证据、整段排名、门槛、boundary 和
shadow，而不是仅改界面显示的总分。来源配置值、override、代码与输入文件摘要均保存。

工作台地址：<http://127.0.0.1:8767>；新完成的实验可在版本选择器中选择，旧 04 不删除。

## 验证方法

已通过 **1,064 项 Python 回归、5 项连续界面单元测试、7 项双语单元测试**。
04b 在主工作台的 133 项浏览器检查通过，旧 04 在新版代码下的 133 项浏览器检查也通过；
均没有 POST、外网请求或浏览器错误。报告在
`artifacts/merge-debug-20231106/experiment04b-validation/`。
主页面默认运行和配置已经 HTTP 核对，候选卡显示原始成本、实际 10 / 0.1 权重及贡献。
主笔记库 SHA-256 前后仍为
`00bbe13be62749be4909b043c5438e0afff567e8fb8212a295e0984e94535e00`。

新增通用合成测试覆盖：独立 APC/local 门槛、missing-cost 尺度、边界模型、最终权重
重配不改变 delay fit、原始成本和 coverage、无校准继续退避、非法配置提前拒绝、
单/多 worker 的四模式 append-only replay、来源配置与代码 provenance。

全量审计使用 `tools/merge_review/tests/audit_continuous_run.py --reweight-only`，
逐候选核对原始分项、拟合过程、APC 支持、coverage、计划线、投影拒绝记录、候选召回集合
和四模式证据选择不变，并报告首选变化和质量门槛描述统计。
没有任何有效 delay fit 的候选，其 conditioned-fit 缺失 sentinel 允许随总分门槛改变；
这不是有效观测的残差改变，也不能作为成功匹配。

**全量审计 PASS**：306 个分段、1,224 个模式结果、36,170 个候选分数/拟合，
421 个来源文件 pins 和 421 个新运行 pins 全部通过。92,143 条 AVL 与 13,862 条 APC
以及上游字段不变，候选集合、决策分段、拟合过程和覆盖率均保持不变。
报告：`artifacts/merge-debug-20231106/experiment04b-validation/full-run-audit.json`。

| 模式 | 04 首选通过数值总分门槛 | 04b 首选通过数值总分门槛 |
|---|---:|---:|
| 全局 | 294 / 300 | 299 / 300 |
| 前期 | 299 / 300 | 300 / 300 |
| 中期 | 300 / 300 | 300 / 300 |
| 后期 | 284 / 300 | 299 / 300 |

分母是各模式具有数值候选的 300 个分段，不是全部 306 个分段。
该表只检查总成本，不代表通过其他 coverage/可信集门槛，更不代表真实匹配精度。
