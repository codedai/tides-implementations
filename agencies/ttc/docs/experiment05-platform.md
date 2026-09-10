# 实验 05：进度平台段与可信观测终点

`tools/experiment05/plateau.py` 位于 sequence projection 之后、APC trip
membership 和 GTFS 最终匹配之前。它不会读取最终 candidate/winner，也不会创建新的
performed trip ID。内部停靠后恢复行驶仍属于同一分段；direction、联合数据 gap、
progress reset 的分段逻辑由 `segmentation.py` 单独负责。

接口为 `assess_trip_extent(points, apc, canonical, config)`，返回
`{extent, points, diagnostics}`。所有原始 AVL 行、原始坐标、UTC epoch 时间、投影备选
和 fragment ID 均保留。APC 只作为真实 stop/time interval 证据，不制造 GPS 点。

## 先诊断，不把“平的 s 曲线”等同于停车

使用互不重叠的 atomic windows。每个窗口保存真实坐标的半径、累计移动距离、
相邻点速度、各 reference 的 progress 范围/顺序、投影状态和检查完整性。
点数不足或时间缺口不能形成独立有效窗口；末尾不完整窗口的真实共址点只可作为
已有连续平台段的补充，不获得一份独立证据权重。

有效的方向/reference 备选存在前进，或者原始坐标确实移动时，不能认定静止：

| 状态 | 含义及处理 |
|---|---|
| `NORMAL` | 正常进度，或尚未达到持续平台时长 |
| `STATIONARY_PLATEAU` | 窗口同时表现为原始空间静止与 canonical progress 停滞 |
| `TERMINAL_LAYOVER` | 持续静止且接近有效 terminal / short-turn endpoint |
| `LONG_MIDROUTE_STOP` | 持续中途停车；不自动截尾 |
| `FROZEN_TELEMETRY` | 足量重复坐标与报告速度矛盾；只是遥测故障判断，不确认结束运营 |
| `OFF_REFERENCE_MOVING` | 原始坐标移动，但没有有效 reference 支持 |
| `REFERENCE_MISMATCH` | 当前 progress 不动，原始位置或其他有效 reference 明显前进 |
| `RECOVERED_AFTER_PLATEAU` | 后续同向或反向的有效连续进度恢复，保留原段 |
| `RIGHT_CENSORED_TAIL` | 下述多项条件全部成立后的可信观测终点截尾 |

完全重复的 GPS 坐标本身不能证明设备冻结，也可能是真实停车或坐标量化；必须报告
这种不可区分性。替代 reference 的前进会阻止截尾并进入诊断，本模块不擅自替换
投影 reference 或方向。

## 截尾的必要条件

只有同时满足以下条件才会改变有效终点：

1. 连续平台证据支持真实静止，或支持遥测冻结故障，而非 raw-moving/flat-progress。
2. plateau 内每个 observation 均声明所有 references 已检查，且完整备选清单覆盖
   当前 canonical bundle；仅检查当前 direction 不足够。
3. 原始位置和 canonical progress 同时接近有效 terminal。terminal 集合不仅包括
   reference 几何端点，还包括所有实际 scheduled trips 的首末 stop，因而覆盖 short turn。
4. 没有观察时间缺口、模糊时间、缺失的右边界标记或后续有效恢复；内部平台不是新边界。
5. 没有未解释的 APC 服务：异地服务、未知 boarding/alighting 或模糊时间阻止截尾。
   同地点有效交换使 idle 起点后移到最后一个 APC departure；真实坐标若与 stop code
   冲突，不能用 stop code 覆盖该空间反证。无 APC 活动仅表示“未观测到”，不是证明
   没有实际乘客服务。
6. 持续 idle 达到自适应时长，并有足够连续观测覆盖 recovery lookahead。
7. 全部仍适合测试的高召回 GTFS hypotheses 都通过 counterfactual advance 检查。

局部 passage headway 使用同一位置附近的 scheduled passages，而非最终匹配 trip 的
pattern 私有 headway。样本不足时明确使用 TOML 中的 fallback。持续门槛为：

`min(censor_max_duration_s, max(censor_min_duration_s,`
`headway × censor_headway_multiplier + censor_terminal_dwell_allowance_s))`。

实际决策还要求达到 `recovery_lookahead_s`，并考虑最后一次共址 APC 服务后的 idle 时长。

## 不允许自由延误过程解释整个平台

Counterfactual 检索只用有效 reference、观察位置和高召回 passage-time rescue buffer，
不读取已有 candidate 排名。每个 hypothesis 单独用 **首次平台之前** 的真实、连续、
单调观测拟合 delay。要求足够点数和 progress，且 pre-plateau delay MAD 合格。

每个 prior observation 通过 unsampled continuous GTFS passage 求残差；dwell 使用
arrival/departure 残差区间，不任意选择 arrival 或 departure。平台内不重新拟合 delay。
最保守的预测时钟为：

`t_schedule = t_observed − (delay_before_plateau + uncertainty`
`                        + bounded_growth_rate × time_since_plateau)`。

用 GTFS 的连续 movement/dwell 段求该时钟下应到达的 s。只有所有 eligible hypotheses
的最保守 advance 均达到配置门槛，才通过。一个仍可解释停站的长 scheduled dwell
即可否决；没有 reliable prior 的 plausible hypothesis 也会使结果 abstain。

已到自身 pattern 终点的 hypothesis，或在保守时钟下已经结束的 schedule，不可能
用于检验后续前进，必须作为明确原因计入 excluded counts。不会把它外推到 shape
终点之外；若全部 hypotheses 因此不适合检查，则拒绝截尾，而不是声称“全部通过”。

## 有效终点与输出

`extent` 分别记录：

- `raw_segment_start/end`：原始 observation 边界。
- `effective_segment_start/end`：匹配使用的可信证据范围。
- `plateau_onset_time`、`censor_decision_time`：首次稳定时刻与达到全部门槛的时刻。
- `censored_tail_duration_s`、`censor_reason`、`platform_states`、`diagnostic_flags`。
- `retained_endpoint_rid`、`retained_apc_rids`：保留的单个 AVL endpoint 和 APC intervals。

有效 end 回溯到首次稳定 plateau；若期间有共址 APC 服务，则保留到最后 departure。
平台只保留一个真实有效 AVL endpoint，其余平台 observation 加上
`exclude_from_matching=true`、`censored_tail=true`、`trajectory_included=false`。
APC interval 保留完整时间跨度，取代这部分重复静止 AVL 的轨迹表达；不修改原始 APC。

没有通过截尾的 OFF_REFERENCE 点仍然保留，`exclude_from_matching=false`，继续进入
空间证据的 coverage denominator，不能因为投影失败被本模块免费删除。

`physical_trip_end_confirmed` 始终为 false：此处确认的是**证据适用范围**，不是运营
事实。尾段之后没有观测，不能宣称车辆实际已结束 service。

## 配置与验证

全部决策阈值直接来自 `config/experiment05-v1.toml` 的 `[plateau]`，不设隐含模块默认值。
schedule uncertainty、projection quality、headway fallback/minimum gap count 使用该配置
已有的 `[matcher]` 值。缺失或非法配置会立即失败。

`tests/test_experiment05_plateau.py` 使用合成物理轨迹覆盖红灯、terminal/short turn、
长中途停靠与恢复、不恢复的尾段、速度矛盾的冻结坐标、raw-moving/flat-s、其他 reference
前进、terminal 反向恢复、数据缺口/边界不明、已结束 hypotheses、长 scheduled dwell
反证、APC 时间/空间/交换冲突，以及 atomic windows 不重复计数。不包含任何指定
vehicle、route、日期、历史 trip ID 或 expected-winner 生产规则。
