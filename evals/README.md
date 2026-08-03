# Caddie Evals

这是一套本地优先、可解释的生成质量评测。它补充现有流程测试，回答的是“内容是否可信、具体、与岗位相关”，而不只是“接口有没有成功返回”。

## 快速运行

在 `Caddie/` 目录执行：

```bash
.venv/bin/python -m evals.runner
```

默认是离线 smoke 模式，使用匿名案例中的合格样例验证评分器。它不会联网、不会读取用户数据库。

调用 Caddie 当前配置的模型运行真实评测：

```bash
.venv/bin/python -m evals.runner --live --output output/eval-report.json
```

评测已有输出（每行一个 `{"case_id":"...","output":"..."}`）：

```bash
.venv/bin/python -m evals.runner --responses /path/to/responses.jsonl \
  --output output/eval-report.json --fail-on-regression
```

## 当前质量门禁

- 禁止出现案例明确列出的无依据事实；
- 输出数字必须来自用户确认的材料；
- 资料不足时必须明确标注待补充、待确认或假设；
- 覆盖案例定义的关键事实与岗位相关能力；
- 控制“行业领先、充满热情”等空泛套话。

硬门禁失败时，即使总分达到阈值也不会通过。JSON 报告保留每项检查、命中原因、模型信息与耗时，便于比较模型或提示词版本。

## 添加回归案例

将匿名化案例追加到 `cases/core.jsonl`。建议优先纳入用户指出的编造、遗漏、贡献边界错误和严重改写案例。不要放入姓名、联系方式、未公开公司材料或原始简历全文。

每个案例的核心字段：

- `source_material`：允许模型使用的事实边界；
- `missing_information`：是否要求输出主动暴露信息缺口；
- `supported_numbers`：允许出现在输出中的数字；
- `forbidden_claims`：不可出现的典型编造；
- `required_fact_groups`：每组命中任一表达即算覆盖；
- `relevance_groups`：岗位相关性关键词组；
- `pass_threshold`：该案例的最低总分。

确定性评分器适合做第一道 CI 门禁，但不能替代人工审阅。后续应使用真实用户修改和拒绝记录持续扩充案例，并定期用人工评分校准语义评分器。
