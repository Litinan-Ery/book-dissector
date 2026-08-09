# 图书拆解器 v0.2.1 测试报告

- 日期：2026-08-09
- 工单：M8-01 至 M8-07
- 分支：`codex/v0.1-plus-five`
- 服务：当前 checkout 的 PID 11346，`127.0.0.1:8000`
- 结论：通过，v0.2.1 需求已完成

## 1. 自动化与静态检查

| 检查 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q` | 58 passed，6 条上游 deprecation warning，0 failed |
| `.venv/bin/python -m compileall -q app tests` | 通过 |
| `node --check app/static/app.js` | 通过 |
| `git diff --check` | 通过 |

v0.2.1 专项覆盖：

- pending/running/cancelled/error/done 任务删除、级联单元清理、缓存保留、幂等和重启恢复。
- claim/delete 争用与 create-task/delete-book 争用，分别重复 24 轮和 16 轮。
- 书籍删除的精确 ID 匹配、活跃任务/提取阻塞、缓存清理、交付物保留、文件故障回滚、数据库故障回滚和启动恢复。
- Finder 仅允许 done 任务，严格使用持久化真实路径，中文/空格/shell 元字符作为单一 argv 传递；缺失文件、非 macOS 和 Finder 失败错误码稳定。
- 旧 SQLite 数据库自动增加 `delete_requested` 与 `unit_cache.book_id`，无需人工删库。

## 2. 测试发现与修复

发现一个运行中删除边界缺陷：如果用户在当前 DeepSeek 请求期间删除任务，而该请求返回的文本长度不合格，旧流程可能立即创建下一次长度修正请求。已在每次重试前重新读取持久化停止标记。回归用例在第一次模型调用中注入删除，最终调用数严格为 1。

## 3. ego lite 与 Finder 验收

使用独立验收书籍 `a4eefee37216` 和任务 `task_58776f50438e448b`：

1. 任务队列所有卡片均展示“删除任务”；非完成卡片展示“打开文件夹”的数量为 0。
2. done 验收任务同时展示“打开文件夹”和“删除任务”。Finder 实际选中持久化 `output_path` 指向的 `README.md`。
3. 任务删除弹窗展示书名、task ID、状态和保留边界；取消后任务仍在，再确认后消失。
4. 关联 pending 任务 `task_2e3ccb2c91cb42c1` 存在时，删除书籍被阻止，提示精确包含该任务 ID。
5. 先删除阻塞任务后，书籍弹窗展示完整“将清理/将保留”范围；取消无副作用，确认后书籍消失。
6. 导入来源 `README.md` 的 SHA-256 在全流程前后均为 `2c56df46bec1b78e252288df1a8c330606adca5952165626166bbed5885b9fa3`。
7. 应用副本已移入 `storage/recycle/books/a4eefee37216-2f4a076ae13c/`，删除日志为 `completed`。

ego-browser 验收任务空间已正常关闭。

## 4. 重启与现场状态

- 旧 PID 7738 的 `/health` 为 0.2.0，已终止。
- 用当前 checkout 重启后，PID 11346 监听 `127.0.0.1:8000`，`/health` 返回 `version=0.2.1`。
- OpenAPI 可见任务 DELETE、书籍删除预览/DELETE 和 `reveal-output`。
- 验收书籍与两条验收任务在重启后仍不存在；可恢复文件清单与完成日志保留。
