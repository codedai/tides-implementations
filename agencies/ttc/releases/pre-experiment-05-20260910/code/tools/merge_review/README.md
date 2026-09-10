# APC–AVL 逐趟审查与单日 Visual Debug

本地、中文优先的证据工作台。冻结审查模式直接读取最终 `full02`；单日调试模式
只重跑 **2023-11-06**，将每次分割与 merge 保存为独立实验版本。
两种模式都不引入 GTFS，不修改原始数据、`full02` 冻结分割或以前的人工标签。

本项目后续实验约定：**用户没有另外指定日期时，仅使用 2023-11-06，并将结果展示在
同一个 8767 工作台**。新实验保存为新版本，不覆盖此前结果或人工笔记。

## 中英文界面切换

右上角 `中文 / EN` 按钮即时切换界面语言，默认中文；浏览器使用独立的
`tides-review-language` 本地偏好记住选择，刷新后继续使用。禁用本地存储时，
仍可在当前页面切换。按钮支持键盘操作及屏幕阅读器。

翻译覆盖操作、参数说明、候选评分、图表提示、四种决策模式和全天总览。
切换只改变显示文字，不刷新数据、不重算匹配、不提交实验或审查；当前筛选、
时间窗、候选颜色／可见性及未保存的参数与笔记保持不变。
原始数据、JSON 诊断、机器状态码、ID、实验名称和用户输入保留原文。
语言资源全部由本地工作台提供，不调用外部翻译服务。

语言运行时测试：`node tools/merge_review/tests/test_i18n_runtime.mjs`（7 项通过）。
相关 Python 回归 827 项、原有中文 GTFS 浏览器回归 99 项通过；独立 8768 工作台的
[双语浏览器验收](../../artifacts/merge-debug-20231106/i18n-browser/second-pass/browser-i18n-results.json)
134 项通过，覆盖动态翻译、状态保留、语言持久化、键盘和移动端布局。
该验收不创建实验或写入审查，正式工作台仍使用实验 03d 的既有结果。

## 实验 01：只按 VehicleID 合并并按时间排序

新增 `vehicle_only` 模式：将同一车辆的 AVL 和 APC **UNION ALL** 成一条观测流。
这里的“merge”是带来源的记录合并与排序，不是两张表仅按车辆号做多对多连接，
也不是时间最近邻匹配。没有调用 trip 分割、空间门槛、时差门槛、插值或证据匹配算法。

- 按规范化后的原始车辆号字符串精确分组，保留双方车辆集合，不因只有一个来源就丢弃车辆。
- AVL 使用原始 `time`，APC 使用原始 `arrival`；仅到站时间缺失时，才明确标记后使用
  `departure`。不修改任何原始到离站时间，不猜测两源时差或运营日。
- 车内按 `event_time` 排序，缺失时刻置于末尾。同刻按固定来源顺序及原始行号／ID
  排序，仅保证显示可复现，不意味着 AVL 或 APC 在现实中先发生。
- 重复原始行保留；不生成每个 AVL × 每个 APC 的笛卡尔积，不加总客流。
- 每辆车是一个**车日观测流**，不是一个 trip。时间缺口仅在轨迹绘图中断线，不切分观测流。
- APC 标记为 `not_evaluated / 未执行匹配`，不是 `unmatched / 匹配失败`，也没有匹配评分。
  APC 不投影到 AVL 距离曲线上；地图／时间线只并列展示真实来源位置与时刻。

在控制台选择“仅 VehicleID 分组＋时间排序”，分割和 merge 参数会停用。
统一观测表交错显示两种来源，可搜索、分页和点击定位。每条原始记录的来源 ID、原始时间
以及日输入中的字段均保留，原始 CSV 不变。与执行了时空匹配的旧版本对照时，不计算
所谓匹配率改善或 trip 边界变化，因为两种实验没有执行同一种判断。

该模式的配置只有：

```json
{"method":"vehicle_only","segmentation":{},"merge":{}}
```

已完成正式运行 `run-20231106-20260908T204034-97eba31b5e7f`，原始版本保留在工作台版本列表。
实际保存 **232,985 条观测 = 213,895 AVL + 19,090 APC**、51 个车日分组，
而不是 51 个已确认 trip。没有生成 trip chunks 或 APC–AVL 匹配记录；3 条跨午夜离站
的 APC 保留了原始离站时间。全日汇集与内存／落盘双遍核对耗时 **33.805 秒**，
来源、字段、日范围、排序和无伪匹配检查均零违规。

完整回归 **483 项通过**，包含 HTTP 创建实验，以及禁止调用分割／匹配算法的检查。
浏览器展示时，超出 JavaScript 精确整数范围的字段（如 UInt64 `payload_hash`）使用
十进制字符串，避免无声舍入；Parquet 中的原值和类型不变。时间使用声明过的本地墙钟轴。
运行检查报告：[summary.json](../../artifacts/merge-debug-20231106/runs/run-20231106-20260908T204034-97eba31b5e7f/summary.json)，
回归报告：[vehicle-only-regression-tests.xml](../../artifacts/merge-debug-20231106/vehicle-only-regression-tests.xml)。

最终浏览器验收 **97 项通过**：完整分页／筛选、双源原始 ID、可读本地到离站时间、
地图原坐标选择、大整数精确显示、APC 不投影、跨模式不可比、审查记录隔离、刷新和
手机布局；无浏览器错误、HTTP 错误或外网请求。原冻结模式另有 **82 项兼容性检查通过**。
验收使用独立测试工作区，首次实际创建实验后复用该运行完成后续检查；没有向正式运行
写入测试笔记。报告：[观测流浏览器验收](../../artifacts/merge-debug-20231106/vehicle-stream-browser-acceptance/final-display/browser-vehicle-stream-results.json)，
截图：[同车时间排序表](../../artifacts/merge-debug-20231106/vehicle-stream-browser-acceptance/final-display/vehicle-source-stream.png)、
[双源时间线与 AVL 距离](../../artifacts/merge-debug-20231106/vehicle-stream-browser-acceptance/final-display/vehicle-time-progress.png)。

复测观测流 UI 时，在独立 `acceptance-workspace` 和测试笔记目录启动 8768，再运行：

```sh
node tools/merge_review/tests/browser_vehicle_stream_smoke.mjs --allow-test-writes
```

只允许专用本地测试端口 8768；可加 `--existing-run <测试运行编号>` 复用既有测试结果，
但仍会在该测试运行保存审查笔记。不要对正式工作区启用此写入测试。

## 实验 01 的 AVL 去重版与全天横向浏览

`vehicle_only_dedup` 是实验 01 的独立派生版本：仍只按 VehicleID 汇集并按源时间排序，
不执行 trip 分割、时空配对或 APC 去重。原 `vehicle_only` 实验及人工笔记保持不变。

去重依据是原 AVL CSV 的 **全部 13 个业务字段逐字段精确相同**，不是只按车辆和时间，
也不单凭 `payload_hash`。每组保留 `source_row` 最小的一条；不同时间、不同字段以及
原始字符串不同的记录均保留。即使 APC 同车同到站时间也不删除，避免误删不同方向的记录。

派生运行的 `observations/` 只包含保留的 AVL 和全部 APC；`deduplication/` 保存原字段
快照与每条原 AVL 到代表记录的映射。网页的地图、时间线、距离曲线和排序表使用去重后
记录，原始 CSV 与实验 01 可用于恢复或回查。去重不是判定记录错误，也不是匹配准确率改善。

双源时间线新增独立的“横向浏览全天”范围滑条。先选择 10／30／60 分钟窗口，再拖动
滑条平移窗口，地图与距离曲线同步；键盘 Home／End 可到当天最早／最晚窗口。下方原有
“时间游标”仍只改变窗口内查看的时刻，不代替范围平移。全日概览下滑条明确停用，可一键
回到 30 分钟详细窗口。统一观测表默认展示全部保留记录，只有勾选“仅当前浏览时间窗”
才随范围滑条筛选，横移本身不会删除数据或重新切分 trip。

配置仍不包含分割和匹配阈值：

```json
{"method":"vehicle_only_dedup","segmentation":{},"merge":{}}
```

正式去重运行 **`run-20231106-20260908T211936-160cab4053e8`**（“实验 01b”）
已设为工作台启动时载入的最新结果：**177,638 AVL + 19,090 APC = 196,728 条观测**，
共 51 辆车，移除 36,257 个 AVL 完全重复副本。原始 213,895 条 AVL 均有可恢复的代表映射。
全日运行与双遍核对耗时 **40.833 秒**；独立回查确认 APC 全部原字段不变、代表均为原字段
完全相同组内的最小源行号，实验 01 的 manifest 和 summary 内容哈希未变。

完整后端回归 **526 项通过**；旧 frozen 浏览器兼容回归 **82 项通过**。
新界面首次实际创建去重实验并通过 **55 项浏览器检查**，覆盖真实鼠标点击／拖动、
Home／End／方向键、全天首末窗口、三图联动、来源映射与手机布局；使用独立测试工作区，
没有写入正式笔记。报告：[去重运行摘要](../../artifacts/merge-debug-20231106/runs/run-20231106-20260908T211936-160cab4053e8/summary.json)、
[完整回归](../../artifacts/merge-debug-20231106/dedup-timeline-regression-tests.xml)、
[实际运行与交互验收](../../artifacts/merge-debug-20231106/dedup-timeline-browser/browser-dedup-timeline-results.json)。
手机总览宽度修复后，复用同一测试运行再通过 **55 项最终检查**（零新实验／笔记写入），
另核对全天总览包含全部保留 AVL 与 APC，而非仅当前窗口。
最终报告：[响应式与全天覆盖检查](../../artifacts/merge-debug-20231106/dedup-timeline-browser/final-responsive/browser-dedup-timeline-results.json)，
截图：[横向滑条](../../artifacts/merge-debug-20231106/dedup-timeline-browser/final-responsive/timeline-first-window.png)、
[手机布局](../../artifacts/merge-debug-20231106/dedup-timeline-browser/final-responsive/timeline-mobile.png)。

复测去重和滑条时仍须使用独立 8768 测试工作区：

```sh
node tools/merge_review/tests/browser_dedup_timeline_smoke.mjs --allow-test-writes
```

此脚本会创建新的测试运行；加 `--existing-run <测试运行编号>` 可只复核已有结果。
脚本不写人工笔记，浏览器外部请求会被拦截。

## 实验 02：过滤 PI/PO，用两源方向分割候选 trip

`direction_destination` 在实验 01b 的 **13 个原字段精确 AVL 去重**基础上，
过滤 AVL `Route` 为 `PI` 或 `PO` 的代表记录。判定仅为去除首尾空白、转大写后精确相等；
不因 APC `Branch` 含字母或 Destination 缺失而过滤。原始 CSV、以前实验及人工笔记不变。
去重映射仍覆盖全部原始 AVL，过滤代表的完整字段及原因另存于 `direction/{vehicle_case}.json`。

本版只做基于时间证据的**候选 trip 分割**，不调用原有时空 matcher、不投影 APC 进度、
不使用 Heading、GTFS、source trip ID 或线路变化来替代本次方向实验。

明确的实验规则：

1. 同车分别读取 AVL `Destination` 开头的 `NORTH / SOUTH` 和 APC `direction`，
   不改写原字段。未知方向保留，不能自动当成反向。
2. AVL 的方向从定位时刻短时延续至下一不同定位时刻，最多 **30 秒**；
   下一条 PI/PO 或未知方向也会截断这种延续，避免过滤后跨过无方向证据区间。
   最后一条 AVL 只提供该时刻的点证据。这是实验假设，不是供应商确认的字段有效期。
3. APC 正序到离站时间采用闭区间。缺失端点或倒序区间只在可用的排序时刻提供点证据，
   并显式记录回落原因；无效／时间歧义记录不提供确定方向，记录本身仍保留。
   方向证据限于本地自然日；原始跨午夜 departure 不裁剪。
4. 按时间批量检查活跃方向集合。只有 NORTH 或只有 SOUTH 时才提供确定方向。
   同时出现两种方向时，保留冲突双方及时间窗，不因同刻稳定显示顺序切出伪短片段。
   冲突期间维持此前候选分段；下一次非冲突确定方向反转时，才建立新候选片段。
5. APC 区间横跨边界时，保留多个 `candidate_segment_ids` 和边界重叠标记。
   落盘观测仍每条仅一份；逐段网页可重复显示该记录用于审查，不能按候选片段重复加总客流。
6. 未知时间段和全天两端保持未决／范围截断说明。长时间无观测不自动证明确实仍在同一 trip，
   本次也不额外按时间缺口切分。方向反转本身并非独立验证的真实 trip 边界。

冲突标注区分两源异向以及同源异向；`conflict_windows` 是按活跃证据集合产生的审查时间窗，
相邻窗口可能属于同一个业务事件，不能当成独立错误次数。地图、时间线、方向片段队列、
统一观测表可回查原始 source rid；上下文与候选成员分别计数。累计距离只来自 AVL 实际位置，
不是候选 trip 完成百分比。

此版本规则固定并记录在 manifest 的 `direction_segmentation`，无隐藏旧 matcher 阈值：

```json
{"method":"direction_destination","segmentation":{},"merge":{}}
```

与 01b 对照时，检查 PI/PO 过滤是否解释全部来源变化、APC 是否完全保留及候选／冲突数量。
不把候选数量或冲突减少解释为匹配准确率提高。

正式运行 **`run-20231106-20260908T220649-9be72540c029`** 已完成，父版本明确指向
实验 01b `run-20231106-20260908T211936-160cab4053e8`，直接复用其已校验的自然日输入。
本次生成 **187,962 条唯一落盘观测 = 168,872 AVL + 19,090 APC**；
过滤 8,766 条去重后的 PI/PO AVL（PI 3,339、PO 5,427）。01b 原来的 36,257 个重复副本
仍由完整去重映射解释，不与此次 PI/PO 过滤混为一类。

51 辆车均有审计入口，50 辆产生共 **541 个方向候选片段**，另一辆只有被过滤记录。
**795 条 APC、13,811 条 AVL** 涉及方向冲突；**267 条 APC** 跨候选边界并保留多归属。
14,127 个冲突证据窗口中，相邻窗口可属于同一业务冲突，不代表 14,127 个独立错误。
本次没有自动删除冲突、丢弃 APC、根据方向选择“更好”的 GPS 匹配点或加总候选客流。

全日运行含去重、源守恒及方向证据的内存／落盘双遍校验，耗时 **96.879 秒**，所有已实现
检查零违规。完整 merge-review / coverage-study / direct-evidence / raw-audit 回归
**502 项通过**；旧 frozen 页面 **82 项浏览器兼容检查通过**，只使用独立临时笔记库。
正式数据的结构可行性通过不等于 trip 准确率已知。

独立对照再次逐列核查：全部保留 AVL/APC 原字段、213,895 条去重映射与 01b 一致，
全部候选成员与冲突来源关系双向一致。网站数据层实际加载 541 个候选＋1 个审计窗口。
全过滤车辆为 **9037**：原 34 条 AVL 去重为 26 条，均为 PO、无 APC；只提供排除审计，
不伪造一趟行程。

优先审查的短片段：车辆 **9047** 的 `direction:9047:0002`，
**07:23:36–07:23:48，12 秒 NORTH 候选**。一条 SOUTH APC 的原区间
07:04:27–07:23:36 结束后，短暂只剩 AVL NORTH 证据；07:23:48 AVL 再变 SOUTH。
当前规则没有最小时长／方向平滑，所以保留此段。这是待审查的实验边界，不是已确认的
12 秒真实 trip；网页保留前一条长 APC 及两侧 AVL 边界证据以供回查。

结果：[实验 02 摘要](../../artifacts/merge-debug-20231106/runs/run-20231106-20260908T220649-9be72540c029/summary.json)、
[后端回归](../../artifacts/merge-debug-20231106/direction-regression-tests.xml)、
[旧 frozen 浏览器兼容](../../artifacts/merge-debug-20231106/direction-frozen-compat/browser-results.json)。

新版真实浏览器成功创建独立测试 02，并通过 **54 项创建／交互验收**。
最后复用同一测试运行完成 **54 项只读复测**，含车辆 9047 的秒级短候选、冲突窗口开闭
端点、跨车辆搜索、切版本加载保护、原字段定位、多候选归属、三图联动及手机布局；
零浏览器／HTTP／外部请求错误，未写正式或测试人工笔记。01b 的独立兼容复测另通过
**55 项**，复用原测试结果，不创建新实验。

报告：[实际创建实验](../../artifacts/merge-debug-20231106/direction-browser/actual-02/browser-direction-results.json)、
[最终只读检查](../../artifacts/merge-debug-20231106/direction-browser/final-short-candidate-fixed/browser-direction-results.json)。
两处界面异步请求问题（切版本覆盖方法、旧搜索回调误用新车辆）已通过加载保护与队列隔离
修复；不会改变已保存的实验方法或结果。测试中产生的旧失败截图及一次误选方法的 01b
测试副本留在独立验收目录，不进入正式工作台。

复测请只指向独立的 8768 测试工作区：

```sh
node tools/merge_review/tests/browser_direction_smoke.mjs --allow-test-writes
```

加入 `--existing-run <测试运行编号>` 会只读复核已有结果，不重新提交实验，也不写审查笔记。

## 单日 Visual Debug（本次新增）

从 `agencies/ttc` 启动：

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.server --debug-date 2023-11-06
```

打开 **http://127.0.0.1:8767**。默认不会占用原冻结网站的 8765 端口。
第一次启动提取单日源数据并运行初始基线；之后复用已缓存的单日输入和已完成版本。
当天原始输入有 213,895 条 AVL、19,090 条 APC，共 51 辆车。

调试流程：

1. 选择已完成版本和用于对照的基线版本。
2. 选择 `vehicle_only` 只汇集来源记录、`vehicle_only_dedup` 先精确去重 AVL，或选择分割方法
   （`gap_only`／`avl_context`／`apc_assisted`）并调整分割和直接证据 merge 参数。
   填写实验名称，重跑当天全部车辆。一次只执行一个后台实验；期间仍可浏览旧版本。
3. 比较候选片段边界、APC 状态及归属变化，再在地图、时间线、累计距离曲线中检查具体源记录。
4. 给当前版本保存独立的人工判断。新版本不会继承旧版本的“可接受”结论。
5. 逐步修改参数并建立新实验；代码与配置指纹记录在版本中，旧结果不被覆盖。
   如果修改了本地算法代码，先重启调试服务再建立实验，避免长驻进程继续使用已导入的旧代码。

对照按原始 APC `rid` 对齐，而不是按可能改变的片段 ID 对齐。片段归属变化依据实际
GPS 成员集合比较：片段 ID 改名本身不是归属变化。诊断变化不自动解释为“改善”。
匹配比例、片段数量和无损保留检查均不是准确率；没有独立真值时，准确率保持未知。

日期范围明确为 **源本地自然日 [2023-11-06 00:00, 2023-11-07 00:00)**，
不是推断后的运营日。AVL 按原始定位时间、APC 优先按到站时间入选；到站缺失时才用
离站时间。APC 跨午夜的原始离站时间仍保留，未裁剪为午夜。默认不加载邻日 GPS，
所以日期边缘属于**数据范围截断**，不代表真实 trip 起终点；不能用本日片段证明完整跨日行程。

本次不提供“推广全量”按钮，也不把浏览后的人工笔记当盲测真值。单日迭代完成后，仍需
另选未用于调参的日期和边界案例验证，才讨论全量应用。

默认实验目录为 `artifacts/merge-debug-20231106`；审查数据库位于该目录的 `reviews/`。
可用 `--debug-dir` 和 `--state-dir` 指定独立位置，`--port` 指定本机端口。
HTTP 接口不接受任意文件路径、任意日期、脚本或命令，只接受有范围限制的已知参数。
关闭服务会中断未完成实验，未完成结果不会作为成功版本载入；已完成版本和审查记录保留。

当前字段展示保留 APC 的 `Route`、`RouteName`、`Branch`；字母分支不会自动改写为
线路号或非载客标签。未关联 APC 窗口不再声称是“只有 APC 的真实单源行程”，窗口内
实际 GPS 与已归属片段的 GPS 分开计数，显示上下文不等于接受关联。

### 本次验收（2026-09-08）

- 完整回归 **437 项通过**，包含单日 runner、数据适配器、HTTP 与原有算法测试。
- 真实单日默认运行耗时 **14.543 秒**，51 辆车、130 个候选片段；19,090 条 APC
  均有唯一处置记录。已实现的源守恒／支持证据检查零违规，准确率仍为未知。
- 单日浏览器验收 **75 项通过**，实际改变参数并创建新版本，检查差异、三图选择、
  笔记保护、跨版本隔离和手机布局；无外部请求或浏览器错误。
- 最终 UI 另有 **15 项只读复核通过**，覆盖边界理由变化泳道、刷新恢复和手机布局，零 POST。
- 原冻结模式浏览器回归 **82 项通过**，使用独立临时审查库，未写真实旧笔记。

本机速度是这一数据切片和配置的测量值，不是所有参数的性能保证。
浏览器验收使用独立 `acceptance-workspace`，测试实验不会出现在正式工作台的运行列表。
结果：[单日浏览器验收](../../artifacts/merge-debug-20231106/browser-acceptance/browser-debug-results.json)、
[边界与记录差异截图](../../artifacts/merge-debug-20231106/browser-acceptance/source-rid-boundary-diff.png)、
[三图截图](../../artifacts/merge-debug-20231106/browser-acceptance/rerun-timeline-progress.png)。

复测单日浏览器时，先用独立工作区和审查目录启动 **8768**，再执行：

```sh
node tools/merge_review/tests/browser_debug_smoke.mjs --allow-test-writes
```

该脚本会创建实验并写入测试笔记，因此只允许专用的 `127.0.0.1:8768`，不要指向正式工作台。

## 冻结审查模式启动

从 `agencies/ttc` 运行，复用现有锁定 Python 环境，无新增依赖、npm 构建或 CDN：

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.server
```

打开 **http://127.0.0.1:8765**。终端按 `Ctrl+C` 停止。若端口被占用，可指定
`--port 8767`。服务只监听本机回环地址，不能直接用于公开部署。

显式指定数据与独立审查记录目录：

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m tools.merge_review.server \
  --run artifacts/apc-avl-direct-evidence-20260908/full02 \
  --state-dir artifacts/merge-review-website-20260908/reviews \
  --port 8765
```

## 怎么检查

1. 左侧选择分割上下文、车辆，再按日期、线路、融合状态或人工审查状态筛选。
   默认使用 `avl_context` 和车辆 9027（存在时）。这只是浏览起点，不是随机评估样本。
2. 用上一趟／下一趟或 `J`／`K` 逐个检查。队列包含冻结 AVL 片段，也包含未关联、
   非唯一归属或未接受关联的 APC 检查窗口，不会只展示成功匹配。
3. 在地图、时间线或明细表中点击 APC 记录，查看同一记录的正证据、反证、竞争候选、
   时空残差和真实源 ID。相邻记录的“上下文”不等于已经关联到当前片段。
4. 长片段可选择 10／30／60 分钟窗口、前后移动或聚焦选中 APC ±5 分钟；
   “邻近上下文”可选 0／±3／±15／±30 分钟，用来检查起终点附近的单源尾段。
   这是浏览范围，不改变归属或 trip 分割。轨迹缺口保持可见，不用连接线补齐。
5. 选择“可接受／有问题／待确认”，填写笔记，保存或“保存并下一趟”。
   `Ctrl/Cmd+S` 保存；左下角可导出带历史的 JSON 审查记录。

审查对象是**候选 trip／观测片段，不是已确认的 `trip performed`**。部分片段可长达
一天，APC 附加窗口按 15 分钟桶组织以便浏览；这些窗口不构成新的 trip 分割或匹配。
跨片段 APC 可能出现在多个检查对象中，不能按队列数量统计独立 trip 或累加客流。

## 三种可视化的含义

| 视图 | 可以检查 | 不代表什么 |
|---|---|---|
| 地图 | 同车实际 GPS 轨迹、APC 位置、支持点、真实插值端点与位置反证 | 路线／trip 身份已经确认 |
| 双源时间线 | 两源覆盖、到离站区间、GPS 缺口、证据锚点 | 两源时钟已经同步，或区间全程连续停站 |
| 时间–距离曲线 | 连续有效 GPS 观测段内的累计距离、APC 的受支持位置 | GTFS 计划完成率、跨缺口的未知路程、真实 trip 总里程 |

累计距离在无效／歧义／同刻位置冲突、超过 90 秒采样间隔或超过 130 km/h
隐含速度处断开，每个新的连续段从零开始。这是可视化的保守几何定义，不改变 merge。
原始重复行仍保留，但相同时间位置不会增加距离。所有时刻在源数据的本地墙钟轴上
展示，不用浏览器时区二次转换，不猜 DST fold。

源客流字段原样显示，未知值为 `—` 而不是零。不根据一条 APC 支持多个 GPS 点复制
计数，不把未确认粒度的 boarding／alighting 累加成全 trip 客流。

## 地图与隐私

默认离线坐标地图，不向外部发送轨迹或下载地图。主动开启“街道底图”后，浏览器才会
请求 OpenStreetMap 当前视窗瓦片，外部服务可获知 IP 与请求的地理范围；不要在不允许
披露地理范围时开启。没有将原始 APC／AVL 数据或源 ID 上传的接口。

底图使用浏览器正常缓存、只请求当前视窗并显示署名；不提供批量下载或离线瓦片抓取。
可用性和使用限制见 [OSM 瓦片服务政策](https://operations.osmfoundation.org/policies/tiles/)。
断网时仍可使用坐标地图、时间线、进度曲线和本地审查记录。

## 保存在哪里

默认保存到 `artifacts/merge-review-website-20260908/reviews/reviews.sqlite3`，
在现有 gitignore 下，目录仅本人可访问。记录以运行 manifest／config／completion
的内容哈希、分割方法、车辆和检查对象共同标识。不同结果版本不会共用同一条审查。

每次保存更新当前判断，并在同一个 SQLite 事务中追加历史。多标签页通过 revision
检查防止静默覆盖；发生冲突会拒绝保存，需重新加载后协调。导出包含当前判断和历史。
这是**看过模型诊断后的非盲审查**，导出明确标记 `unblinded=true`、`ground_truth=false`；
不会自动进入任何训练、方法评分或已冻结的盲标注流程。

服务包含 Host 检查、保存令牌、Origin 检查、固定静态文件白名单、输入长度限制和
禁止外部嵌入策略；这些是本地防护，不是公开网络部署所需的身份认证系统。

## 验证

```sh
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync python \
  -m pytest tools/merge_review/tests -q
UV_CACHE_DIR=/tmp/tides-coverage-uv-cache uv run --offline --no-sync ruff check \
  tools/merge_review
node --check tools/merge_review/static/app.js
```

HTTP 测试使用本机临时端口和临时数据库；受限执行环境需允许 loopback socket。
独立浏览器测试脚本为 `tests/browser_smoke.mjs`，使用本机 Chrome 与 Node 内置
DevTools WebSocket，无需下载依赖。只能对单独的测试记录目录启用写入测试，
不应对真实审查库运行测试保存。

数据适配层按车辆延迟加载并最多缓存两个车辆的数据；首次选择车辆通常需要数秒，
长片段可能返回较大的完整证据 JSON。不会为缩短加载时间静默丢弃原始行或证据。

2026-09-08 本机验收：原项目与新网站共 375 项回归测试通过，ruff／格式与 JS
语法检查通过；独立 Chrome 浏览器 82 项交互检查通过，页面无外部请求、控制台错误、
运行错误或 HTTP 错误。覆盖三图双向选择、长片段浏览、四档上下文、未保存笔记保护、
保存刷新恢复和手机布局；联网瓦片服务未在自动测试中请求。
验证记录：[测试报告](../../artifacts/merge-review-website-20260908/verification-tests.xml)、
[浏览器检查](../../artifacts/merge-review-website-20260908/browser-final01/browser-results.json)、
[界面截图](../../artifacts/merge-review-website-20260908/browser-final01/desktop-1440.png)。

实现入口：[server.py](server.py)、[data.py](data.py)、[store.py](store.py)。
本次 merge 方法与局限见 [直接证据层优化报告](../../docs/apc-avl-direct-evidence-optimization-20260908.md)。
