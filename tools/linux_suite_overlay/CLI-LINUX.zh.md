# Linux 本地后台作业与候选发布

这套入口运行在独立的 Linux Blender 5.3 工作目录。只支持已获准的本地场景或模型工作副本，不上传、不覆盖旧工程、不做渲染。

- 提交：`blenderctl job submit --domain scene --manifest <绝对路径.json> --source <绝对路径.blend> --sha <源SHA256>`。新建工程可省略源和哈希；`--domain model` 可运行本地建模工作单。
- 查询：`blenderctl job status <JobID>`；终态后用 `blenderctl job result <JobID>` 获取实际候选和哈希；`blenderctl job cancel <JobID>` 取消自己提交的作业。
- 另行发布：`blenderctl transaction publish --job-id <JobID> --source <绝对路径.blend> --output <绝对路径的新文件.blend>`。无源新场景省略 `--source`。发布目标目录必须私有且归当前用户；同名文件不会被覆盖。
- `blenderctl transaction status` 以相同参数查看独立发布证据。发布后仍需用回执确认目的地和候选 SHA；发布并不替代美术审核，也不自动替换正式版。
- 后台作业依赖存活的 Linux 执行会话；如果一次性执行容器在提交后随即结束，会终止其进程，不能把这样的作业算作成功。云工作工具应在一个活的终端会话内提交并等待终态，可直接用 `job submit-and-wait`，它会先输出作业号，再输出最终结果。
