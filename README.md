# Evaluation Judger

读取一个数据集中的题目、Rubric-Council `rubric.md`、源素材、受测对象交付和测试过程 Excel，借助 OpenCode 逐项判分，生成可追溯的逐题结果、维度总评和 Word 报告。一次运行处理一个数据集，支持 1–3 个受测对象。

## Pipeline

[![Evaluation Judger pipeline](docs/assets/evaluation-judger-pipeline.png)](docs/assets/evaluation-judger-pipeline.svg)

图示对应当前开发版本。模型独立判断评分项并提供证据；程序校验、计分和汇总。逐题、维度和数据集分别检查结果齐全条件，过程记录独立归档。

[SVG 矢量图](docs/assets/evaluation-judger-pipeline.svg) · [PNG 高清图](docs/assets/evaluation-judger-pipeline.png)

## 安装

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/) 和 OpenCode。当前中转站拒绝 OpenCode v2 自动发送的 `prompt_cache_key`；本项目默认使用隔离安装的 OpenCode 1.18.23，不改变系统中的 OpenCode。

```powershell
uv sync
npm.cmd install --prefix .evaluation-judger/opencode-v1 opencode-ai@1.18.23 --no-save
Copy-Item .env.example .env
```

在本地 `.env` 中填写 `AIAAA_API_KEY`。密钥、数据集、测试记录、运行日志、报告和技术方案均被 Git 忽略。也可直接通过环境变量提供密钥。

## 配置和运行

参考 [`config.example.json`](config.example.json) 配置数据集路径、受测对象名称与文件前缀、纳入的维度、Excel 列映射及模型。当前示例对应 DeepInsight 的第 1、2、4 维度。使用其他数据集时修改本地配置；`tasks` 可限定试跑题号，例如 `"tasks": ["4.1"]`。

如果只选多对象数据集中的一个对象，需在本地配置中填入其他对象的 `excluded_subject_prefixes`，例如 `["workbuddy5.5.6-"]`。目录名不同的数据集可通过 `layout.dimension_pattern`、`layout.task_pattern`、`layout.question_file` 和 `layout.rubric_file` 指定识别规则；两个正则都应包含命名捕获组 `id` 与 `name`。

```powershell
uv run evaluation-judger prepare --config config.example.json
uv run evaluation-judger run --config config.example.json
uv run evaluation-judger status --config config.example.json
uv run evaluation-judger report --config config.example.json
```

`run` 会处理本轮配置的全部题目。单个对象失败时会保存错误并继续其他对象和题目；再次运行会复用已完成结果、从有效的原子批次检查点续跑。只有配置范围内的结果全部齐全，才生成该范围的报告。`report` 仅使用已完成、输入指纹仍匹配的判分结果。一次运行的输出在配置的 `run_dir` 下：

- `workspaces/<题号>/<对象>/<输入指纹>/`：隔离的题目材料、OpenCode 原始事件和逐批检查点。
- `results/<题号>/<对象>.json`：每个原子的状态、理由、证据、比例明细、错误封顶判断和程序复算分数。
- `results/<题号>/compare-result.md` 或 `evaluation-result.md`：可读逐题结果。
- `results/dimension-*-summary.md`：维度总评及过程记录。
- `reports/evaluation-report.docx`：本轮数据集报告。
- `failures.json`：本轮未完成的题目和对象，可据此定位后续续跑范围。

`report_mode` 支持 `auto`、`pilot`、`formal`。`auto` 对通过 `tasks` 明确限定且不足四题的试跑输出简短报告；完整配置范围默认生成正式报告，即使数据集本身题目较少。正式报告从锁定的维度总评与过程数据生成正式章节、逐维度分析和对比表，调用 OpenCode 组织分析文字，并另做一次对照锁定事实的独立复核；表格数字由程序计算。若文字生成或复核失败，使用标记为 `deterministic_fallback` 的确定性摘要完成报告，并在 `reports/narrative-error.json` 留下原因。若只纳入一个维度，报告结论仅适用于该范围。报告阶段不重新判分。

## 评分口径

每题完成率为原子加权和，范围 0–100%；五项质量分分别为 `5 × 加权状态和 / 100`，范围 0–5。错误规则的上限由程序应用；单题质量均分是五项质量分的算术平均。维度和数据集对纳入题目等权平均，完成率不混入质量分。模型只提供逐项事实判定与证据，不直接决定汇总分数。

每个比例类原子必须保留分子、分母和逐项清单；每个原子必须有实际观察、判分理由及定位证据。JSON 保留完整记录；可读 Markdown 提供逐项差异速览，长清单展示全部未通过项、未知判定标签和少量通过示例，只省略可明确识别的正面条目。逐条解释兼容 reason/explanation/note/basis 字段；修正说明允许不同内容形式。Markdown 会转义交付物中的 HTML 标签，避免渲染器将其当作页面标签。验证失败只重做受影响的批次。历史交付按其执行或截止时点核验时效性，复用本身不扣分。

当 rubric 明确规定两个原子使用同一批主张时，程序检查其分母和明细数量；不一致时定向复核相关原子。若原始模型输出只是 JSON 标点损坏，先尝试语法修复，再做严格的原子覆盖、数值和证据字段校验。运行状态与报告只接纳通过跨原子和算分校验的结果。时间类原子优先使用过程表的执行时间；如表中仅有用时和状态，会记录交付文件保留的修改时间作为辅助线索，并明确其局限。

长主张核验（`CLAIM-RATIO`）单独调用，较短条目按 `batch_size` 分批。检查点按打分项编号恢复，调整分批策略后可复用旧批次的有效条目。无效日志不会永久占满重试次数；响应长度耗尽时转为逐项修复，超时输出也会保存。正式报告保存写作初稿，复核发现问题时自动定向修订一次并重新核查，再失败才降级为摘要。

目前文件提取支持文本、HTML、JSON、PDF、DOCX、PPTX 和 XLSX。图片、扫描版 PDF、交互网页等视觉内容需要进一步的专门检查；提取限制会写入材料清单，不能把无法读取当作受测对象缺失。数据集目录布局与对象前缀在配置中约定。运行前可用 `prepare` 核查任务与交付归属。

单次 OpenCode 调用默认最长 900 秒；可在本地 .env 设置 OPENCODE_REQUEST_TIMEOUT_SECONDS 调整。超时保留已捕获日志并进入恢复流程，未完成的评分不会补零。
