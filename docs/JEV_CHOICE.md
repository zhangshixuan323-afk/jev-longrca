# 本分支：JEV Choice v4

本分支保存原版本代码和必要前序依赖。初筛高/中/低置信档保留 3/4/6，缩减保留 3/5/6。v3 明确指修订后的 earliest origin 版本。

```bash
python -X utf8 scripts/evaluate_jev_v4.py --subset mini --audit-only
# 新运行须指定独立结果目录；不要将不同配置混入历史目录
python -X utf8 scripts/evaluate_jev_v4.py --subset mini --output results/v4_mini_new
```

历史目录名为 `jev_v4_mini`。原始数据和调用档案不在 Git 仓库中。旧 v3 默认输出指向最初 v3 目录，实际调用时必须显式传入新的 `--output`。

Adaptive RCTA 使用自身入口与协议，保持独立。[分支对应表](JEV_BRANCHES.md)
