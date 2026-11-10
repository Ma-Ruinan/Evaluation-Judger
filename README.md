# Evaluation Judger

Evaluation Judger 按固定的 `rubric.md` 评测智能体应用的交付物。一次运行处理一个数据集，支持单对象评测和最多三个对象的横向比较，输出逐题证据与评分、维度汇总和 Word 报告。

搭配使用时，先用 [Rubric Council](https://github.com/Ma-Ruinan/Rubric-Council) 根据问题和源素材生成并审核每题的 `rubric.md`，再把交付物和测试记录加入数据集，由 Evaluation Judger 使用同一套规则评测各对象。

基于 OpenCode 实现的评测多智能体负责核验证据并给出逐项判定，Python 程序负责校验、按权重复算、应用错误封顶和汇总。

[查看评测流程图](docs/assets/evaluation-judger-pipeline.svg)

## 1. 安装与模型配置

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)、Git、OpenCode，以及支持工具调用和工具结果续写的模型接口。以下是 Windows PowerShell 的 OpenCode 1.x 兼容模式安装方式。Node.js 和 npm 用于安装项目内的 OpenCode。

```powershell
git clone https://github.com/Ma-Ruinan/Evaluation-Judger.git
cd Evaluation-Judger
uv sync
npm.cmd install --prefix .evaluation-judger/opencode-v1 opencode-ai@1.18.23 --no-save
Copy-Item .env.example .env
Copy-Item config.example.json config.eval.local.json
```

在项目根目录的 `.env` 中填入 `AIAAA_API_KEY`。示例配置使用 AI AAA 的接口和 DeepSeek 模型。换接口时，在 [`opencode.v1.example.jsonc`](opencode.v1.example.jsonc) 中设置 `baseURL` 和模型定义，同时在本地运行配置中设置对应的 `model`（格式为 `提供商标识/模型 ID`，可带 `#变体`）。评测和报告工作区会复制这个模板，密钥通过环境变量引用。其他 OpenCode 模式、超时和本地覆盖方式见[运行与配置](docs/running.md)。

## 2. 准备数据集

以下示例使用默认目录命名。项目会把 `维度一-信息检索/1_资料核验` 识别为题号 `1.1`。

```text
测试数据集/
├── 测试结果记录.xlsx
└── 维度一-信息检索/
    └── 1_资料核验/
        ├── 问题描述.txt
        ├── rubric.md
        ├── 问题源素材.pdf
        ├── 受测对象A交付文件.docx/pdf/html/xlsx...
        └── 受测对象B交付文件.docx/pdf/html/xlsx...
```

- 每题需要 UTF-8 的 `问题描述.txt`、结构化 `rubric.md`，以及每个纳入对象的至少一份交付物。按题放入所需的源素材。
- 程序按配置的前缀归属交付物，例如 `受测对象A`。多个文件也可以放在带前缀的题目直属目录里。只评部分对象时，用 `excluded_subject_prefixes` 排除其余交付物。未归属且未排除的文件会按源素材读取。
- 测试记录 Excel 是必需输入。为每道纳入的题提供一行，按对象映射用时、状态列。可另外指定执行时间列，供涉及时效性的评分项取证。

在 `config.eval.local.json` 中调整以下字段。仓库中的[示例配置](config.example.json)展示的是一份 DeepInsight 数据集的列映射和范围。

| 字段 | 填写内容 |
| --- | --- |
| `dataset`、`run_dir` | 数据集路径和本轮输出目录。相对路径以运行命令时的目录为准。 |
| `subjects` | 每个对象的唯一 `id`、显示名 `name`、交付前缀 `prefix`。 |
| `process_excel` | Excel 路径、工作表、记录起始行、题号列和每个对象的列映射。 |
| `dimensions`、`tasks` | 可选的范围筛选。保留示例配置中的值时，仅评相应维度或题号。单题试跑可设 `"tasks": ["1.1"]`。 |
| `layout` | 可选的目录正则与题目、Rubric 文件名。使用 `维度1_…/W1_…` 等布局或 Excel 别名时见[运行与配置](docs/running.md#目录与-excel-映射)。 |

## 3. 预检、运行与恢复

在项目根目录执行。先用 `prepare` 检查题目范围、Rubric、交付归属和 Excel 映射，确认无误后开始评测。

```powershell
uv run evaluation-judger prepare --config config.eval.local.json
uv run evaluation-judger run --config config.eval.local.json --auto-resume
```

`run` 会保存有效评分和检查点。启用 `--auto-resume` 后，程序在有限轮次内自动重试未完成的部分，评分齐全后生成本轮报告。手动中断后，保留输入、配置和 `run_dir`，执行相同的命令即可继续。可在本地配置中设 `task_workers`（默认 1，范围 1–8），并发处理同一维度中的不同题目，同题内的各对象依次评测。

```powershell
uv run evaluation-judger status --config config.eval.local.json
uv run evaluation-judger report --config config.eval.local.json
```

`status` 列出有效评分数和缺失项。`report` 使用已有的完整评分重新生成报告，正式报告阶段可能调用模型写作和复核。自动续跑预算、报告模式和重试规则见[运行与配置](docs/running.md)。

## 4. 查看结果

以下路径相对于 `run_dir`。

| 路径 | 内容 |
| --- | --- |
| `results/<题号>/<对象>.json` | 逐项判定、理由、证据、封顶规则与复算分数 |
| `results/<题号>/compare-result.md` 或 `evaluation-result.md` | 可读的逐题对比或单对象结果 |
| `results/dimension-*-summary.md` 和 `.json` | 维度评分汇总 |
| `reports/evaluation-report.docx` | 本轮 Word 报告 |
| `process-records/`、`operation-records/` | 受测对象的测试记录与评测程序自身的运行记录 |
| `workspaces/`、`failures.json` | 隔离工作区、模型事件、检查点与未完成项 |

报告分别呈现任务完成率（0–100%）和五项质量指标（各 0–5 分）。质量均分取五项质量指标的算术平均。评分计算、材料核验和证据复核见[评分与证据](docs/scoring.md)。

## 离线测试

```powershell
uv run python -X utf8 -m unittest discover -s tests -v
```
