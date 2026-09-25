# 在 jev-v1 分支运行完整 JEV v5 Mini

`jev-v1` 是目标仓库的 Git 分支名，算法始终是 **JEV Choice v5**，协议 `jev-v1-prompts-adaptive-v5`。本地和 fork 的贡献分支名为 `jevv5`。

## 环境

推荐 Python 3.11/3.12。JEV v5 仅使用 Python 标准库，无需 GPU 或安装 Laya/PyTorch。以下命令在仓库根目录执行。

## 下载和检查 Mini

```bash
python scripts/download_mini.py
python -X utf8 scripts/evaluate_jev_v5.py --subset mini --audit-only
python -X utf8 -B -m unittest discover -s tests -p test_jev_v5.py -v
```

下载器获取固定版本的 200 条 Mini 数据并校验 SHA-256。audit-only 验证数据及提示词，不读取 API 密钥、不请求模型。

## 配置 API 并运行全部 200 条

设置 `TYPESAFE_API_KEY` 或 `JEV_API_KEY` 环境变量，也可按根目录 `.env.example` 创建私有 `.env`。PowerShell 示例：

```powershell
$env:JEV_API_KEY = "填入你自己的 API key"
python -X utf8 scripts/evaluate_jev_v5.py --subset mini --workers 4 --output results/jev_v5_mini_new
```

不加 `--smoke` 就运行完整 Mini。小样本接口检查可加 `--smoke`，并使用另一个输出目录。可用 `--api-key-file <私有文件>` 指定密钥来源；密钥不写入配置和结果。

## 断点恢复、结果和校验

恢复时重新执行相同命令；成功预测会先重放原调用和决策校验，已成功请求不会重复发送。余额不足停止新请求和重试；源码、提示词、数据或策略不同会拒绝复用缓存。新配置应使用新的输出目录。

```bash
python -X utf8 scripts/evaluate_jev_v5.py --subset mini --output results/jev_v5_mini_new --verify-only
```

输出目录包含 `config.json`、`calls/`、`decisions/`、`predictions/`、`errors/`、`summary.json`、`metrics.json`、`predictions.csv`、`report.md`，离线校验后生成 `result_audit.json`。部分失败会明确保留完成数量和状态，不冒充全量成功。

推理入口为 `scripts/jev_v5.py`，完整评测入口为 `scripts/evaluate_jev_v5.py`。共用的数据解析、评分与恢复辅助函数来自现有 `evaluate.py`、`evaluate_full.py`、`resume_full.py`，它们均在本仓库内，无需旧仓库或 v2–v4 模块。

[历史 Mini 结果](../reports/jev_v5_mini_report.md) · [六份 v5 提示词](../reports/jev_v5_prompts.md)
