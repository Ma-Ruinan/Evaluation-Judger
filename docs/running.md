# 运行与配置

[返回 README](../README.md) · [评分与证据](scoring.md)

## OpenCode 与模型

默认使用 `opencode_major: 1`。Windows 上，程序从 `.evaluation-judger/opencode-v1/node_modules/opencode-ai/bin/opencode.exe` 查找本地安装，运行时使用独立的 OpenCode 数据、配置与缓存目录。若已有兼容的可执行文件，可在启动终端中设置 `OPENCODE_BIN` 为其绝对路径。这个变量由 Python 进程直接读取，应在终端中设置。

评测和报告工作区从 [`opencode.v1.example.jsonc`](../opencode.v1.example.jsonc) 复制基础配置，并生成相应的角色与工作区读取权限。可以本地调整模板，也可以通过 `OPENCODE_CONFIG_CONTENT` 提供 OpenCode 配置覆盖。项目根目录的 `opencode.jsonc` 不是生成工作区配置的模板。接口地址、提供商标识和模型 ID 要与运行配置中的 `model` 对应。保留 `{env:AIAAA_API_KEY}` 引用，把密钥放在本地 `.env` 或终端环境中。当前适配器要求存在 `AIAAA_API_KEY`，同名终端环境变量优先于 `.env`。模型不支持配置中的变体时，移除 `#变体` 后缀。

配置 `opencode_major: 2` 会改用 [`opencode.example.jsonc`](../opencode.example.jsonc) 并调用 OpenCode 的 `--standalone` 模式。请安装支持该入口的 CLI 并保证网关兼容其请求字段。遇到网关拒绝 `prompt_cache_key` 时，可使用 1.x 兼容模式。

## 目录与 Excel 映射

默认识别 `维度一-名称` 这类维度目录和 `1_名称` 这类题目目录。每道题的内部编号为 `维度编号.题目编号`。通过 `layout` 可适配不同命名。下面的 JSON 片段适用于 `维度1_信息检索/W1_资料核验` 一类目录。

```json
{
  "layout": {
    "dimension_pattern": "^维度(?P<id>[0-9]+)_(?P<name>.+)$",
    "task_pattern": "^[A-Z](?P<id>[0-9]+)_(?P<name>.+)$",
    "question_file": "问题描述.txt",
    "rubric_file": "rubric.md"
  }
}
```

两个正则都需要 `id` 与 `name` 命名捕获组。题目 `id` 需捕获数字。配置了 `dimensions` 时按其顺序执行，否则按维度编号排序。对象前缀匹配不区分大小写。前缀应当明确区分对象，避免同一文件匹配多个对象。

下面是上述题目的测试过程记录映射示例。列映射的键与 `subjects[].id` 一致。工作表没有执行时间列时可省略 `executed_at`，也可用 `execution_dates` 按题号和对象 ID 指定执行时间。

```json
{
  "process_excel": {
    "path": "测试数据集/测试结果记录.xlsx",
    "sheet": "Sheet1",
    "first_row": 3,
    "last_row": 3,
    "task_column": "C",
    "task_id_map": {"W1": "1.1"},
    "subjects": {
      "a": {"runtime": "E", "status": "F", "executed_at": "I"},
      "b": {"runtime": "G", "status": "H", "executed_at": "J"}
    }
  }
}
```

Excel 题号可直接使用 `1.1`，也可用 `task_id_map` 映射 `W1` 等标签。按记录范围填写 `first_row` 和可选的 `last_row`。程序只读工作簿，保留原始题号和行号，空白示例行不覆盖实际记录。

采用“对象标识-题号-原文件名”归档交付时，可设置 `collection_prefixes_are_metadata: true`。文件名评分使用去除本对象前缀及已识别题号后的逻辑名称，原文件和证据路径仍保留。此设置及题号映射进入输入指纹。

## 续跑与排错

同一运行保留原配置、输入和 `run_dir`。单轮 `run` 保存失败信息并继续其他题目，可重新执行以复用通过校验的评分和检查点。自动续跑还会记录运行绑定和尝试预算。更改输入或配置时，为自动续跑指定新的 `run_dir`。同一运行目录只能启动一个调度器。

| 参数或环境变量 | 作用 |
| --- | --- |
| `--auto-resume` | 对 `run` 启用跨轮重试和恢复。 |
| `--max-auto-attempts` | 总尝试轮数，默认 4，包含首轮。再次启动沿用已消耗的轮数。 |
| `--auto-retry-delay` | 轮次间等待秒数，默认 30。 |
| `task_workers` | 同一维度内同时评测的题目数，默认 1，范围 1–8。 |
| `startup_deferred_retry` | 本轮其余题目结束后，串行补试符合条件的 180 秒启动超时，限一次且要求尚无评分检查点。默认关闭。 |
| `OPENCODE_REQUEST_TIMEOUT_SECONDS` | 单次通用模型调用的时限，默认 900 秒。独立复核可配置自己的时限。 |
| `OPENCODE_FIRST_EVENT_TIMEOUT_SECONDS` | 首条输出等待上限，默认 0（关闭）。启用时逐行保存脱敏事件，收到首条事件后仍受请求总时限约束。 |

启动探针会实际读取文件并核对随机标记。遇到无输出的启动超时且尚未开始评分时，程序可用新的隔离目录尝试恢复。超时日志、已完成批次和经过校验的证据复核进度可用于续跑。遇到长响应截断时，OpenCode 1.x 可尝试续接会话，判分可缩小批次，比较关系清单可按交付物和文本区间分段核验。

自动续跑遇到连续三轮没有结果或检查点变化、明确的鉴权或接口迁移错误、或耗尽预算时会停止。查看 `failures.json` 定位未完成项，查看 `operation-records/auto-native.log`、`auto-resume-state.json` 和工作区事件查阅细节。阶段性 Markdown 导出异常会记录日志，已保存的评分仍可复用。

## 报告生成

| `report_mode` | 输出方式 |
| --- | --- |
| `auto`（默认） | 通过 `tasks` 显式筛选且少于四题时生成简报，其余情况生成正式报告。 |
| `pilot` | 生成简报。 |
| `formal` | 生成包含维度分析、对比表、图表与逐题附录的正式报告。 |

报告只使用通过校验且输入指纹仍匹配的完整评分。正式报告由模型依据锁定事实组织正文，再独立复核。若写作或复核失败，可查看标记为 `deterministic_fallback` 的确定性摘要和 `reports/narrative-error.json`。在自动续跑模式下，只有独立复核通过且事实表匹配的正式报告才标为完成。可再次执行 `report` 复用评分和有效正文草稿，继续报告阶段。

可用 `report_scope_note` 描述范围，`subject_models` 按对象 ID 登记受测模型。用时和状态作为测试过程记录归档。执行时间可用作时效核验的证据。图表和表格使用同一套程序复算的分数，图表及数值 JSON 保存在 `reports/charts/`。

### 正文复核与报告恢复

- 报告写作和复核各自读取锁定的事实文件。事实表保留整体、维度和逐题的差值，以及维度内最大绝对差距的题目、方向和并列项。差值先用未舍入数值计算，再按展示精度输出。
- 正文优先用两位小数，校验也接受锁定数字的等价表示和允许的舍入形式。独立复核检查数字与对象、题目、结论的对应关系。草稿和复核结果绑定对应的事实与正文哈希。
- 草稿的结构或数字校验失败时会携带诊断定向纠错。独立复核提出问题时会定向修订，再复核一次。报告阶段的首事件静默超时可有限重试，已保存的合法草稿可接续审核。历史事件和诊断可用于追溯，已解决的报告错误归档到 `reports/attempt-history/`。
- 失分摘要优先展示未通过的具体核验项，缺少逐项明细时保留完整理由。超过 15 题的差值图自动分页，数值文件保留全部数据。
