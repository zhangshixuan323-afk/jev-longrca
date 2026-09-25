# main：JEV Choice v5

v5 使用原始基线的完整步骤/角色提示词，保留自适应候选和历史摘要。初筛高/中/低置信档保留 3/4/6，缩减保留 3/5/6，阈值为 0.70、0.30；每组及最终候选最多 8 个。题级 confidence 非法时按低档处理。角色与根因步骤仍独立判断。

`scripts/jev_v5.py` 直接实现筛选、交接扩展、有限历史和最终推理；`scripts/evaluate_jev_v5.py` 直接实现运行、重试、余额停止、精确缓存、决策记录、离线重放和配对报告。两个模块复用原仓库的数据与评分函数，不再动态加载历史版本入口。

```bash
python -X utf8 scripts/evaluate_jev_v5.py --subset mini --audit-only
# 真实运行，须配置 API，并使用新的独立目录
python -X utf8 scripts/evaluate_jev_v5.py --subset mini --output results/jev_v5_merged_mini
python -X utf8 scripts/evaluate_jev_v5.py --subset mini --output results/jev_v5_merged_mini --verify-only
```

支持 `--subset full`、`--smoke`、`--workers`、`--api-key-file`。数据可通过原有 download_mini/download_full 脚本下载。默认输出目录仍为 `results/jev_v5_<subset>`；遇到不同源码/提示词/数据摘要会拒绝混用。由于本次代码结构简化，旧工作区的 v5 档案应使用原代码重放，不直接作为这个实现的续跑缓存。

输出包括 config、calls、decisions、predictions、部分失败信息、逐条 CSV、指标、配对比较和中文报告。缺少历史档案时对应比较显示不可用，当前运行仍可独立完成。

[历史分支](JEV_BRANCHES.md) · [六份提示词](../reports/jev_v5_prompts.md) · [Mini 配对性能](../reports/mini_v2_v5_performance.md)

本次合并用原 v5 的 195 条成功样本离线重放：2,889 次请求、3,350 条决策和全部预测字段均与精简前一致。另有 7 组固定响应回归覆盖短轨迹、长轨迹、低/中/高/缺失置信度和分片。[验证记录](../reproducibility/jev-v5-merge/validation.json)
