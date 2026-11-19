# DS Bot Autoloop

在 Chief 对话里提出项目目标。Chief 确认需求与验收标准，PM 生成计划。查看立项卡上的任务和依赖，确认启动。Worker 提交结果后，PM 根据证据验收。项目完成或出现需要干预的异常时，Chief 会主动发出独立消息。

项目 PM 对话可以查看当前任务与证据，并提交方案调整。增删改需要确认，过期提案会重新校验。项目总台提供计划、反馈收件箱、交付文件和验收结论。

## 本机安装

使用 DeepSeek Harness `0.2.2-rc.3`、DS Bot `0.3.8`、Node.js 24、Python 3.11+、Git 以及已登录的 Codex CLI。安装命令以 DSH-assistant 根目录为当前目录。

```sh
python3 -m venv autoloop-plugin/.venv
autoloop-plugin/.venv/bin/python -m pip install -r autoloop-plugin/requirements.txt
DSH_HOME="$PWD/.dsh" ./node_modules/.bin/dsh plugin --profile web add "$PWD/autoloop-plugin"
```

在 Web profile 的 `cordis.patch.yml` 中为 `dsbot-autoloop` 配置 Python 路径和 PM 模型费率。费率以 `provider/model` 为键，单位为美元／百万 token。

```yaml
- id: dsbot-autoloop
  config:
    python: /absolute/path/to/autoloop-plugin/.venv/bin/python
    codex: /absolute/path/to/codex
    maxBudgetUsd: 10
    rates:
      provider/model:
        input: 1.0
        output: 2.0
        cacheRead: 0.1
```

`cacheRead` 和 `cacheWrite` 缺省时使用输入费率。插件预算统计 PM 的模型请求，Codex 按本机登录账户计费。可用 `codexProfile` 指定 CLI profile，用 `codexReasoningEffort` 覆盖推理强度。`codexNetwork` 控制可写 Worker 的网络访问。默认数据目录是 `$DSH_HOME/autoloop`，可用 `dataDir` 指定绝对路径。

Chief 和 PM 可使用宿主提供的工具，工具执行遵循 DS Bot 和 DeepSeek Harness 的原生权限设置。项目验收仍通过 Autoloop 提交。

## 状态与恢复

`TASK_DAG.json` 保存当前项目及验收结果。Python 运行时在共享锁内写入 DAG、`inbox.jsonl` 和 `outbox.jsonl`。Worker 通过 Codex 的只读或项目写入沙盒执行，失败时保留检查点。只有 PM 验收通过的任务才能释放下游。重启时未确认结果的 Worker 需要人工核实，重新验收可复用已经提交的结果。

这一版使用精简后的状态格式。首次启用请使用空数据目录，旧测试状态和旧发布包可移出运行目录。

## 开发验证

在插件目录执行。

```sh
python -m pytest
node --test tests/*.mjs
npm run build
npm pack --dry-run
```
