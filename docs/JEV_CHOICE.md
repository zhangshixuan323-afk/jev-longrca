# jev-v6：JEV Choice v6

v6 使用 v1 完整步骤/角色提示词，保留历史摘要。初筛和缩减的高/中/低 confidence 档均保留 3/5/6，阈值为 0.70、0.30。初筛不超过分段选项数；缩减不超过组内数量减一，最终候选最多八个。角色与根因步骤分别判断。

`scripts/jev_v6.py` 直接实现推理，`scripts/evaluate_jev_v6.py` 直接实现运行、重试、余额停止、缓存、离线核验和配对报告。运行 v6 无须动态加载旧版本入口；原有 v5、Adaptive RCTA、Laya 入口保留。

```bash
python -X utf8 scripts/evaluate_jev_v6.py --subset mini --audit-only
# 真实运行需要配置 API，使用新的独立输出目录
python -X utf8 scripts/evaluate_jev_v6.py --subset mini --output results/jev_v6_ported_mini
python -X utf8 scripts/evaluate_jev_v6.py --subset mini --output results/jev_v6_ported_mini --verify-only
```

支持 `--subset full`、`--smoke`、`--workers`、`--api-key-file`。数据使用原有下载脚本；本地工作树已复制校验过的 Mini 数据，Git 不跟踪轨迹正文。

## 已完成的实验与离线重放

原始实验 200/200 成功：根因精确 21.50%、角色 47.50%、±5 命中 36.00%、MAE 39.33。代码迁移本身没有重新请求模型。

完整原始缓存原样复制到本地 `results/jev_v6_mini/`，继续受 `.gitignore` 排除。Git 跟踪的报告、逐条 CSV、指标、配置、原始源码快照和文件清单位于 `reproducibility/jev-v6-mini/`。

```bash
# 原始档案的严格完整性检查，以及用迁移后代码逐条重放（无 API 调用）
python -X utf8 scripts/verify_jev_v6_archive.py
```

该命令需要本地原始缓存和 Mini 数据，检查所有原始文件的字节、原始配置引用的源码摘要、每次请求、筛选决策、最终预测、文本覆盖与评分。

迁移后的源码布局和摘要与原实验不同，因此原档案不能直接作为新运行的续跑缓存。专用重放验证行为一致，不修改原配置或摘要，也不会把旧缓存标为迁移后代码产生的结果。只有 Git 源码和报告的干净检出仍可进行单元测试或启动全新实验；原始调用正文不在 Git 中。

[全部 prompt](../reports/jev_v6_prompts.md) · [结果与迁移记录](../reproducibility/jev-v6-mini/README.md) · [分支对应表](JEV_BRANCHES.md)
