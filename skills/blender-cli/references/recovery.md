# 流水线、取消、恢复与发布

| 情况 | 入口 |
| --- | --- |
| 普通worker失败/取消 | 保留原请求，从完整不可变源重新提交；没有通用job resume |
| pipeline失败/取消 | 同一jobs根 `pipeline resume <pipeline_job_id>`；修改manifest后用 `pipeline run --reuse-job <旧job_id>` |
| 事务中断 | 同一transactions根 `transaction status/recover/rollback <transaction_id>` |

resume重新评估DAG，不恢复进程内状态；缓存烘焙不承诺从中间帧续算。输入/输出/实现/闭包不匹配就重跑，不改旧报告求命中。

```powershell
& $ctl --jobs-dir $jobs --timeout 600 --async pipeline run --manifest '<manifest.json>'
# submit的ok仅表示提交成功；使用返回job_id。
& $ctl --jobs-dir $jobs job status '<job_id>'
& $ctl --jobs-dir $jobs job cancel '<job_id>'
# cancel只是登记请求，之后有界等待并确认终态/结果。
& $ctl --jobs-dir $jobs job result '<job_id>'
& $ctl --jobs-dir $jobs --timeout 600 pipeline resume '<job_id>'
```

不用高速轮询。stale时先读自己job的status.json/supervisor.log/stderr.log/result.json确认归属；不按进程名终止全部Blender或删除锁。取消仅作用于已授权管理的job。隔离任务异常退出按job cleanup-isolation帮助处理自己job残留，不降级无隔离执行。

| 错误 | 处理 |
| --- | --- |
| 2 INVALID_REQUEST | 核对当前help/Schema、位置参数和绝对路径，不改guard |
| 3 NOT_FOUND | 检查运行时/源/资源路径，不自动下载或造同名替身 |
| 4 CONFLICT | 分辨SHA、文件身份、锁、闭包或结果未就绪；不简单重算SHA掩盖输入变化 |
| 5 WORKER_FAILED / 8 VALIDATION_FAILED | 读具体日志与报告，独立检查失败点；中间候选不交付 |
| 6 TIMEOUT / 7 CANCELLED | 确认终态、保留诊断，从不可变输入重跑或对应恢复 |
| 9 IO_ERROR / 10 UNSUPPORTED | 核对目录、空间、占用或支持域，不静默降级格式/设备/隔离/容差 |

同原因重复失败缩小为最小请求，仍失败停止该路线并报告证据/选项，不能无限重放写操作。调度资源预算不等于硬磁盘/VRAM配额。

工作候选不自动发布。新目标用 `project plan-copy <源> --output <新目标>`，父目录须存在。已有目标替换/混合资源先读工程 `docs/cli/schemas/transaction-plan.schema.json`和transaction-plan.schema.json。已授权目标直接推进；未授权替换、冲突或影响面变化先给逐项计划，再取得所需授权。

脚本回执是独立审阅之后取得的文件描述符。已获对应执行授权时，直连用 `extension run --review-receipt <descriptor.json>`；公共request/pipeline步骤使用 `params.review_receipt: {file, expected_sha256}`，不要把它放进manifest，也不要把review/approve放进DAG。回执并不让extension.run可复用，恢复会再次执行副作用。仅授权检查时，到静态review为止。

不要多包一层：`extension approve`返回的 `data.document`就是该描述符，其中file指向真正的`extension-approval-receipt.json`。JSON/pipeline直接把**data.document对象**赋给params.review_receipt。仅CLI的--review-receipt参数需把这个对象写到descriptor.json后传其文件路径；不能在对象的file里再指向descriptor.json，也不能拿描述符包装文件的SHA冒充回执SHA。

计划data.transaction_id用于transaction status/apply/recover/rollback；apply的job_id用于作业管理。--transactions-dir和--jobs-dir始终保持。recover沿journal既定方向：可能继续提交或已开始的回滚，不是“一键撤销”。rollback要求目标仍为本事务输出，后续编辑冲突必须停下；失败不等于自动回滚。

相对引用用正确blend重映射事务，不能盲目字节复制。资产发布按规范验证Catalog/索引/来源/依赖，保留UUID。外部frames/VDB/usdc/缓存和候选成组保留；目录扫描不证明闭包。无全局反向消费者图，来源库影响面须明确审阅。
