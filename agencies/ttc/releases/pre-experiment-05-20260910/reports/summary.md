# 实验 04c 验收记录

范围：2023-11-06，route 29。基于 **04，不是 04b**。

- 输入：`run-20231106-20260910T030622-57e42718b0c1`。
- 输出：`run-20231106-20260910T063603-e4bf6bc60823`。
- 306 个片段 × 四种模式，8 workers，605.634 秒。复用原 04 的投影后重算匹配；
  不是本次从原始 CSV 全量重建的耗时。
- 外层权重：绝对延误 1.0、延误动态 0.5、独立班次位移 1.0。
- 本次没有覆盖或删除 03d / 04 / 04b 的结果，没有生成真实标签。

## 已通过的检查

| 检查 | 结果 |
| --- | --- |
| Python 全套测试（不含另一个旧 labeling UI 项目） | 1,428 passed，46.42 秒 |
| 连续评分前端单元测试 | 6 passed |
| 修改模块 Ruff | PASS |
| 新结果完整性及四模式工作台加载 | 421 个输出 pins 全部通过 |
| 冻结代码 / 配置 / 环境 / 原始输入 | 203 个代码文件、5 个原始输入通过校验 |
| 04c 真浏览器 | 140 项通过；四模式、全天总览、中英文、手机布局 |
| 原始 04 真浏览器回归 | 132 项通过；旧七项分数未被新八项展示替代 |
| 正式 8767 服务原始明细 | 31 辆车分别抽取 AVL 最多的一个全局片段，31 / 31 加载通过 |

浏览器均无 JS / HTTP 错误、外部资源请求或非 GET 请求；只在页面中输入了未提交草稿。
两个浏览器各检查四模式的 306 个片段总览，保留 606 条有效可视化 fragments。

证据：[Python XML](python-tests-final.xml)、
[04c 浏览器](browser-04c/browser-modes-results.json)、
[04 浏览器回归](browser-source04/browser-modes-results.json)、
[04c 全天截图](browser-04c/day-overview-desktop.png)。

## 完整独立审计：PASS

[完整机器报告](full-run-audit.json) 耗时 1,108.983 秒，重新建立共同竞争集合并核对：

- 306 个片段、1,224 个模式结果、36,070 条候选评分和候选 anchor headways。
- 187,481 个检索 cohort、1,311,498 条受保护邻居成员关系。
- 4,129,819 条候选内逐点时间 / 不确定性记录；这是重复候选中的验证次数，
  **不是**独立 AVL 点数。
- 旧 04 和新 04c 各 421 个输出 pins；65 个上游文件内容一致。
- 独立 AVL 92,143 条、APC 13,862 条；四模式选择、投影、APC 区间、原始时钟和
  parent boundaries 均保持不变。审计未重新拟合延误，不是第二次独立重算全套评分。

以下仅是**描述性变化，不是正确率**；每种模式都有 300 个新旧均有评分候选的片段，
另 6 个片段不能进行 top-1 对比。

| 模式 | 诊断 top-1 改变 | 可比候选中旧 headway 中位数 ≥3,500 秒的条数 | 这些候选的新共同间隔范围 / 中位数（秒） |
| --- | ---: | ---: | --- |
| 全局 | 75 / 300 | 113 | 480–900 / 600 |
| 前期 | 52 / 300 | 84 | 480–900 / 600 |
| 中期 | 61 / 300 | 77 | 480–840 / 630 |
| 后期 | 62 / 300 | 85 | 480–900 / 600 |

headway 对比按相同 case / mode / candidate 身份计数，不代表唯一 GTFS trip 数；
同一计划班次可出现在多个片段中。新共同间隔用于班次位移和检索解释，
**不用于缩放运营绝对延误先验**。前后真实通过间隔分别保留，并未强行改写成表中中位数。

## 校准与准确率限制

缺少跨 route **且**跨 service date、独立 operating episode 的真实标签。
因此 exact-trip accuracy、adjacent-occurrence confusion、retrieval recall、
top-3 coverage、non-abstained precision 和 calibration error 为 **null**，不是 0，
也不是改善证明。候选概率仍未校准，不强制确认 scheduled occurrence。

按 headway regime 的独立离线报告：
[全局](offline-calibration-global.json)、[前期](offline-calibration-early.json)、
[中期](offline-calibration-middle.json)、[后期](offline-calibration-late.json)。
报告均为 `UNCALIBRATED` / `activated: false`。排序概率工具不训练运营延误先验；
当前 routine / disruption 参数是工程默认值，不是从本日匹配结果学习的历史。

输入契约：[独立校准标签与运营先验](../../../docs/experiment04c-calibration-inputs.md)。

## 冻结与运行

实际评分时的代码快照：`releases/workbench-04c-20260910T065019-6915fbec`。
最终工作台：`releases/workbench-04c-20260910T065522-fb227b48`，
release SHA-256 为 `e60a8a4ae4b349fc6c014ba6ca08d29a94719d479b8022a55a3a200c125e36ee`。
之后的唯一评分目录生产代码差异是一行离线评价旧 null 字段兼容修复；
未改变评分数学或本次保存分数。保留前一快照用于追溯实际计算代码。

工作台已在 [127.0.0.1:8767](http://127.0.0.1:8767) 使用最终冻结代码运行。
临时浏览器验收服务器已关闭。原审查数据库和 retention registry 的内容哈希未变。

从 `agencies/ttc` 使用 `./workbench.sh serve` 查看保存结果；
`./workbench.sh run --serve --port 8780` 可作为新的独立输出从原始数据重建。
本次并未再次执行完整原始数据重建，也未将重算时间冒充全链路时间。
