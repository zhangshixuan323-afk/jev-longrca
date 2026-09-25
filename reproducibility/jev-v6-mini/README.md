# v6 Mini 结果与迁移溯源

原始实验：2026-09-25，200/200 条成功，初筛及缩减均为 3/5/6，六份 v1 原文 prompt 与 v5 一致。模型 `jev-1.13.0`，数据版本记录在原始 `config.json` 中。

| 指标 | 完整 Mini 200 条 |
|---|---:|
| 根因精确 | 43/200，21.50% |
| 责任角色 | 95/200，47.50% |
| 根因 ±5 | 72/200，36.00% |
| MAE | 39.325 |

同 195 条成功样本，v5→v6：根因 42→43、角色 96→94、±5 71→72、MAE 34.53→37.29。初筛候选覆盖增加，但最终覆盖减少，本轮不构成全面改善。详见 [原始报告](report.md) 和 [结果解读](interpretation.md)。

## 文件范围

- `predictions.csv`：全部 200 条预测和评分。
- `paired_*.csv`、`comparisons.json`：与各历史版本按相同成功样本比较。
- `metrics.json`：全量及分组指标；`recall_policy_diagnostics.json`：固定本轮初筛响应的保留数诊断。
- `config.json`、`result_audit.json`：原始配置及原始核验记录，未重写。
- `source/`：原始实验配置引用的源码、prompt 和数据清单快照；保留原始字节，不作为当前运行入口。
- `archive_manifest.json`：完整原始结果目录的逐文件摘要，以及上述原始配置、源码和便携结果的摘要。
- `migration_validation.json`：使用迁移后实现重放原始档案的独立验证记录。它不替换原始 `result_audit.json`。

原始完整结果共 7,335 个文件、162,647,636 字节，复制到本地 `results/jev_v6_mini/` 并逐字节核对。调用正文、逐轮决策、单条预测缓存和数据正文沿用仓库忽略规则，不进入 Git；本目录的报告、逐条 CSV、指标及溯源内容随分支保存。没有复制 API 密钥。

## 使用方式

在拥有完整本地档案和 Mini 数据的工作树中，执行：

```bash
python -X utf8 scripts/verify_jev_v6_archive.py
```

只有源码检出的环境，可先运行离线单元测试；需要数据时用原有 `download_mini.py` 下载固定版本。重新进行真实测评时必须使用新输出目录，例如 `results/jev_v6_ported_mini/`，不得用修改原配置摘要的方式绕过缓存检查。

当前运行入口是仓库根目录的 `scripts/jev_v6.py` 和 `scripts/evaluate_jev_v6.py`，与本目录原源码快照的布局不同；逐字请求重放是本次迁移的行为一致性依据。迁移不新增真实 API 调用，不重新计算或替换历史响应。
