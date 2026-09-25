# JEV Choice 分支

| Git 分支 | JEV Choice 版本 | 推理与评测入口 |
|---|---|---|
| `main` | v5 | `scripts/jev_v5.py` / `scripts/evaluate_jev_v5.py` |
| `jev-v1` | 原始基线 | `scripts/evaluate.py`，Full 为 `scripts/evaluate_full.py` |
| `jev-v2` | 阶段提示词、历史摘要、3/4/6 保留 | `scripts/evaluate_jev_v2.py` |
| `jev-v3` | 修订后的六份独立提示词 | `scripts/evaluate_jev_v3.py` |
| `jev-v4` | 缩减题删两项警示、中置信档保留 5 个 | `scripts/evaluate_jev_v4.py` |

`jev-v3` 对应历史 `jev_v3_mini_reduce_origin`，不是最初 v3。以上是 JEV Choice 的版本，不是目标仓库 Adaptive JEV-RCTA 的版本号。各分支都保留现有 Adaptive RCTA、Laya、托管运行和审计资料。

历史分支保留原版本脚本及必要前序依赖，用于对照与追溯；main 将 v5 实现收敛为两个直接导入的模块，避免为运行 v5 加载 v2/v3/v4 版本入口。不再使用额外的版本管理 CLI 或版本目录，Git 分支负责版本选择。

```bash
git switch jev-v3
python -X utf8 scripts/evaluate_jev_v3.py --subset mini --audit-only
git switch main
python -X utf8 scripts/evaluate_jev_v5.py --subset mini --audit-only
```

数据审计需要已下载的数据。真实运行须使用新的独立 `--output`。main 的 v5 请求、提示词和筛选行为保持一致，但简化后源码摘要不同，因此旧 v5 的调用档案不能直接当作新实现的续跑目录；原工作区保留了旧代码和档案。本项目不修改旧摘要以绕过校验。

[Mini 配对性能](../reports/mini_v2_v5_performance.md) 使用共同成功的 195 条样本；各版完成数另列。历史性能不等同于本次重新运行模型所得结果。
