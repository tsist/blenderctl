# 流水线、错误码、脚本回执与事务发布

## 中断后怎么接着跑

| 遇到的情况 | 怎么处理 |
|---|---|
| 单步命令失败或被取消 | 读日志查明原因，修好请求或输入后从源副本重新提交 |
| `pipeline` 流水线中断 | 用原 `--jobs-dir` 跑 `pipeline resume <pipeline_job_id>`；改过清单就跑 `pipeline run --reuse-job <旧job_id>` |
| `workflow` 顺序工作单元中断 | 先 `workflow preflight --requests` 预检，再用原目录跑 `workflow resume` |
| `transaction` 文件发布事务中断 | 用原 `--transactions-dir` 和 `--jobs-dir` 跑 `transaction status <tx_id>` 查看状态，再按需跑 `recover` 或 `rollback` |

异步提交示例：

```powershell
& $ctl --jobs-dir $jobs --timeout 600 --async pipeline run --manifest $manifest
# 记下返回的 job_id，用有界 wait 等待终态或进度
& $ctl --jobs-dir $jobs --compact job wait $jobId --wait-seconds 30
& $ctl --jobs-dir $jobs job result $jobId
```

遇到作业长时间没动静，先读该作业自己的 `status.json`、`supervisor.log` 和 `stderr.log` 确认归属；要取消只对自己的 `job_id` 跑 `job cancel`，异常退出的隔离残留用 `job cleanup-isolation` 清理。同一错误连撞两次就缩成最小请求定位根因。

## 退出码速查

| 退出码与错误名 | 下一步动作 |
|---|---|
| `2 INVALID_REQUEST` | 运行对应子命令 `--help` 或查 Schema，核对位置参数、字段名和绝对路径 |
| `3 NOT_FOUND` | 核对源文件、外部贴图或运行时路径是否存在 |
| `4 CONFLICT` | 源文件已被改动（重新读取最新文件与 SHA）、目标被锁或前置结果尚未就绪 |
| `5 WORKER_FAILED` / `8 VALIDATION_FAILED` | 打开作业目录日志与验证报告，定位具体报错的对象或参数 |
| `6 TIMEOUT` / `7 CANCELLED` | 确认作业已进入终态，调大 `--timeout` 或拆分大任务后从源副本重跑 |
| `9 IO_ERROR` / `10 UNSUPPORTED` | 检查磁盘空间、文件占用，或用 `discover` 查当前宿主支持的替代入口 |

## 扩展脚本审阅回执（`extension.run`）

确需通过 `extension` 执行受审阅的 Python 脚本时，先取得用户明确授权，按静态审阅 → 批准生成回执 → 带回执运行三步走：

1. `extension approve` 返回里的 `data.document` 就是回执描述符（里面指明了 `extension-approval-receipt.json` 的路径与 SHA）。
2. 在 `request` 或 `pipeline` 的 JSON 里，直接把整个 `data.document` 对象赋给 `params.review_receipt`。
3. 走命令行 `extension run --review-receipt <path>` 时，把这个 `data.document` 对象存成一份 JSON 文件再传路径。只获得代码审查授权时，做到 `extension review` 即停步汇报。

## 正式文件复制与资产库发布

工作目录里的候选确认过关后，再通过事务落到正式目标路径：

- **写入全新的目标路径**：确保父目录已创建，运行 `project plan-copy <源> --output <新目标>` 生成事务计划，再用 `transaction apply <transaction_id>` 提交。
- **替换已获授权的目标文件或发布进 `<ASSET_LIBRARY>`**：先按资产规范跑 `asset prepare`、`asset index`、`asset plan` 检查 Catalog UUID、外部依赖闭包（连同序列帧、VDB、USD、物理缓存成组保留）与影响面，确认目标自接手以来未被外部修改后，运行 `transaction apply` 并用 `transaction status` 读回核实。`recover` 会顺着事务日志完成既定提交或回滚；`rollback` 在目标文件仍等于本事务输出时将其安全退回原状。
