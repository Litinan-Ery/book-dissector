# 图书拆解器 v0.2.1 开发工单

- 需求基线：`需求文档.md` v0.2.1，2026-08-09
- 开发范围：M8 本地管理能力
- 当前状态：已完成并验收（2026-08-09）
- 范围边界：只实现单条任务删除、单本书籍删除、完成任务打开文件夹；不实现批量删除、定时清理、用户可见回收站

## 1. 交付目标

本轮完成以下三个用户能力：

1. 删除任务队列中的指定任务，运行中任务先停止创建新请求，再在安全边界完成删除。
2. 删除书库中的指定书籍条目，清理应用管理的数据，同时保留导入来源文件、既有精华 MD 和 MyDatabase 内容。
3. 在完成态任务中使用真实导出路径，通过 macOS Finder 定位精华 Markdown。

既有 AC-1 至 AC-12 是本轮回归基线，不重新设计 v0.1 的一键拆解体验。

## 2. 统一技术约定

### 2.1 API 契约

| API | 成功结果 | 主要失败结果 |
| --- | --- | --- |
| `DELETE /api/tasks/{task_id}` | 非运行任务返回 `200/deleted`；运行任务返回 `202/deleting`；重复删除返回幂等成功 | 非法 ID 返回 400 |
| `GET /api/books/{book_id}/deletion-preview` | 返回书名、book ID、关联任务数量、活跃任务及清理/保留范围 | 书籍不存在返回 404 |
| `DELETE /api/books/{book_id}` | 返回 `200/deleted`；重复删除返回幂等成功 | 存在活跃任务或正在提取时返回 409 |
| `POST /api/tasks/{task_id}/reveal-output` | Finder 已接受定位请求时返回 200 | 非完成态 409；文件缺失 410；非 macOS 501；Finder 失败 502 |

统一返回模型建议为 `DeletionResult`：`resource_id`、`state`（`deleted` / `deleting`）、`message`、`already_absent`。删除 API 不接收客户端文件路径。

### 2.2 删除状态与数据边界

- `tasks` 增加持久化的 `delete_requested`，与普通取消区分。
- 运行中任务收到删除请求后仍保留任务记录，API/UI 显示“正在停止并删除”；worker 到达安全边界后删除记录。
- 删除任务级联删除 `task_units`，但保留 `unit_cache`、书库文件、导出文件和 MyDatabase 内容。
- `unit_cache` 增加 `book_id`，支持删除书籍时精确清理书籍级缓存；迁移时尽可能从现有任务/单元关系回填。
- 书籍文件先进入 `storage/recycle/` 的内部恢复区，再提交数据库清理；本期不提供用户可见的回收站管理界面。
- `storage/output/` 及自定义导出路径不属于书籍删除范围。

### 2.3 安全约定

- 所有 task ID、book ID 必须按标识符解析，不能直接作为 glob、相对路径或 shell 片段。
- Finder 调用固定使用参数数组 `open -R <path>`，禁止 `shell=True` 和字符串命令拼接。
- 仅使用任务结果中已持久化的 `output_path`；客户端不能覆盖路径。
- 删除与打开文件夹均不得触发导出或 MyDatabase Hook。

## 3. 工单总览

| 工单 | 优先级 | 规模 | 依赖 | 交付结果 |
| --- | --- | --- | --- | --- |
| M8-01 持久化删除状态与迁移 | P0 | M | 无 | TaskStore 具备任务删除、书籍删除事务及缓存归属能力 |
| M8-02 任务删除后端状态机 | P0 | L | M8-01 | 各状态任务可安全、幂等删除，运行中任务延迟删除 |
| M8-03 书库条目可恢复删除 | P0 | XL | M8-01、M8-02 | 书籍数据精确清理，失败/重启后可恢复一致性 |
| M8-04 完成任务 Finder 定位 | P0 | S | 无 | 完成任务按真实路径定位导出文件 |
| M8-05 本地管理前端交互 | P0 | L | M8-02、M8-03、M8-04 | 删除确认、删除状态和打开文件夹按钮可用 |
| M8-06 自动化、故障注入与端到端验收 | P0 | L | M8-02 至 M8-05 | AC-13 至 AC-20 和 AC-1 至 AC-12 回归全部通过 |
| M8-07 文档、版本与交付检查 | P1 | S | M8-06 | README/API/版本说明与实际行为一致 |

推荐开发顺序：M8-01 → M8-02 与 M8-04 → M8-03 → M8-05 → M8-06 → M8-07。

交付结果：M8-01 至 M8-07 全部完成；详细命令、故障修复、ego lite/Finder 证据和重启结果见 `docs/TEST_REPORT_V0.2.1.md`。

## 4. 详细工单

### M8-01 持久化删除状态与数据库迁移

**需求映射**：FR-9.3～FR-9.6、FR-9.9、FR-9.12～FR-9.13、NFR-3.5～NFR-3.6。

**目标**：让删除意图、书籍清理事务和缓存归属在服务重启、并发请求和故障注入下保持可判定。

**主要改动**：

- 在 `app/core/task_store.py` 增加向后兼容迁移，不得要求用户手工删除现有 `tasks.db`。
- `tasks` 增加 `delete_requested INTEGER NOT NULL DEFAULT 0`。
- `unit_cache` 增加可索引的 `book_id`；新缓存写入时必须记录 book ID。
- 增加内部 `book_deletions` 日志表，至少记录 book ID、状态、恢复区路径、清单和错误，用于跨文件系统/SQLite 的恢复。
- 增加以下原子方法：请求/完成任务删除、查询书籍关联任务、准备/完成/回滚书籍删除、恢复未完成书籍删除。
- 删除任务依赖现有 `task_units ON DELETE CASCADE`；删除任务时不得删除 `unit_cache`。
- 删除书籍时在一个 `BEGIN IMMEDIATE` 事务内再次检查活跃任务，删除关联终态任务和该书缓存。
- 不强制压紧 `queue_order`；删除后只需保持剩余任务相对顺序稳定。

**涉及文件**：

- `app/core/task_store.py`
- `app/models/schemas.py`
- `tests/test_task_deletion.py`
- `tests/test_book_deletion.py`

**完成定义**：

- 旧数据库启动后自动迁移，原任务、单元与缓存仍可读取。
- 并发迁移、并发删除不会出现 `database is locked`、孤儿 `task_units` 或半提交记录。
- 可从持久化数据区分普通取消与待删除任务。
- 测试覆盖迁移、事务回滚、幂等和并发路径。

### M8-02 任务删除后端状态机

**需求映射**：FR-9.1～FR-9.6、NFR-3.5～NFR-3.6、AC-13～AC-14、AC-17。

**目标**：所有任务状态均可删除；运行中任务不会继续创建新模型请求，并在当前不可中断操作结束后自动消失。

**主要改动**：

- 在 `app/api/tasks.py` 增加 `DELETE /api/tasks/{task_id}`。
- 等待中、已取消、失败和已完成任务在事务内立即删除；已完成任务的 `result.output_path` 和文件本身不删除。
- 运行中任务设置 `delete_requested=1` 与停止标记，返回 202；不得提前删除数据库记录。
- `TaskStatus` 暴露 `delete_requested`，供 UI 显示“正在停止并删除”。
- `_schedule_pending()` 跳过待删除任务；worker 竞争领取任务时，删除或领取只能有一个有效结果。
- 蒸馏器继续使用持久化停止标记，在当前请求前后检查；流水线在阶段切换及导出前增加安全边界检查。
- worker 的 `finally` 检查删除意图并完成物理记录删除，再调度下一任务。
- 启动恢复时，历史 `running + delete_requested` 任务直接完成删除，不恢复为 pending。
- 对已经不存在的 task ID 返回幂等成功，不抛出 500。

**涉及文件**：

- `app/api/tasks.py`
- `app/core/task_store.py`
- `app/core/pipeline.py`
- `app/core/distiller.py`
- `app/models/schemas.py`
- `tests/test_task_deletion.py`

**完成定义**：

- 五种状态（pending、running、cancelled、error、done）均通过删除测试。
- 运行中删除后 DeepSeek 新请求计数不再增加；当前请求结束后任务消失，下一任务可执行。
- 删除任务后书籍、导出、MyDatabase 内容和共享缓存均保持不变。
- 删除与 claim、取消、恢复、服务重启并发时无幽灵任务或 worker 卡死。

### M8-03 书库条目可恢复删除

**需求映射**：FR-9.7～FR-9.13、NFR-3.5、NFR-3.7、AC-15～AC-17。

**目标**：精确删除一本书的应用内条目和关联数据，不误删来源文件、交付物、相似 ID 书籍或外部数据库内容。

**主要改动**：

- 在 `app/api/books.py` 增加删除预览和删除接口。
- 新建 `app/core/library_cleanup.py`，集中负责受管文件枚举、恢复区清单、原子移动、回滚与启动恢复。
- 删除预览返回书名、book ID、关联任务总数、活跃任务列表，以及“将清理/将保留”清单。
- 存在 pending、running 或 `delete_requested` 尚未完成的关联任务时返回 409；前端引导先删除任务。
- 书籍仍在异步提取时返回 409，避免后台线程在删除后重新生成元数据或文本。
- 只枚举并移动以下应用受管文件：上传副本、`{book_id}.txt`、`{book_id}.meta.json`、`{book_id}.*` 中间产物。
- 明确排除 `OUTPUT_DIR`、任务结果中记录的导出、自定义导出路径和 MyDatabase。
- 删除流程采用：持久化准备标记 → 写入文件清单 → 移入内部恢复区 → SQLite 事务删除终态任务与书籍缓存 → 标记完成。
- 文件移动或数据库事务失败时恢复已移动文件并清除准备标记；服务启动时恢复或完成未结束清理。
- `start_disassemble()` 与书籍删除共享持久化保护，删除准备完成后不得再创建该 book ID 的任务。
- 路径匹配必须区分 `book1` 与 `book10`，不得使用未经验证的用户输入拼接 glob。

**涉及文件**：

- `app/api/books.py`
- `app/api/tasks.py`
- `app/core/library_cleanup.py`（新增）
- `app/core/task_store.py`
- `app/main.py`
- `app/models/schemas.py`
- `tests/test_book_deletion.py`

**完成定义**：

- 删除成功后刷新页面、重启服务均不再显示书籍及关联终态任务。
- 应用受管副本和中间产物离开活动目录，书籍缓存被清理。
- 外部来源文件、既有精华 MD、其他书籍、共享配置和 MyDatabase 均不变。
- 文件失败、数据库失败、进程在移动阶段退出三类故障注入均能恢复到可理解、可重试状态。

### M8-04 完成任务 Finder 定位

**需求映射**：FR-8.7～FR-8.10、NFR-2.3、AC-18～AC-20。

**目标**：只对完成任务提供 Finder 定位，并保证路径真实、安全、只读。

**主要改动**：

- 在 `app/api/tasks.py` 增加 `POST /api/tasks/{task_id}/reveal-output`。
- 仅接受 `status=done` 且 `result.output_path` 非空的任务。
- 使用 `Path.resolve(strict=True)` 验证路径存在且为文件；不回退到固定输出目录，不按书名猜测。
- macOS 使用 `subprocess.run(["open", "-R", str(path)], shell=False, ...)`，设置超时并把系统错误转换为明确 API 错误。
- 路径来自任务持久化结果，不接收客户端路径；中文、空格和 shell 元字符只作为单个参数传递。
- 调用前后不得写 TaskStore、重新导出或执行 PostExport Hook。

**涉及文件**：

- `app/api/tasks.py`
- `app/models/schemas.py`
- `tests/test_reveal_output.py`

**完成定义**：

- 完成态真实文件调用参数精确为 `open -R <真实路径>`。
- 文件移动/删除、非完成态、非 macOS、Finder 执行失败均有稳定错误码和中文提示。
- 测试证明任务记录、输出目录文件数和 Hook 调用次数不变。

### M8-05 本地管理前端交互

**需求映射**：FR-8.7、FR-8.10、FR-9.1～FR-9.2、FR-9.6～FR-9.8、FR-9.12、AC-13、AC-15、AC-18～AC-20。

**目标**：在不增加一键拆解主流程步骤的前提下，把三项管理能力放入现有书库和任务队列。

**主要改动**：

- 任务卡片所有状态展示“删除任务”；只有 done 展示“打开文件夹”。
- 待删除运行任务将状态优先显示为“正在停止并删除”，禁用重复操作。
- 书库每行增加“删除书籍”，点击后先获取删除预览。
- 新增一个可复用的 `<dialog>` 确认组件；不能使用原生 `window.confirm`，因为确认按钮必须明确显示“删除任务”或“删除书籍”。
- 任务确认展示书名、task ID、状态和保留范围；书籍确认展示书名、book ID、关联任务数及清理/保留范围。
- 取消确认不得发送 DELETE；提交期间禁用按钮，避免双击重复请求。
- 删除成功后立即刷新任务和书库；202 删除中的任务继续轮询，直到消失。
- 409、410、502 等错误使用明确中文文案展示，失败时不得从列表乐观移除对象。
- 弹窗支持焦点管理、Escape 取消和键盘操作；窄屏按钮仍可完整显示。

**涉及文件**：

- `app/static/index.html`
- `app/static/app.js`
- `app/static/style.css`
- `tests/test_management_ui.py`

**完成定义**：

- 状态—按钮矩阵与需求一致。
- 两类删除确认内容完整，确认按钮文案明确，取消无副作用。
- 打开文件夹失败、活跃任务阻塞删除、运行中延迟删除均有可理解反馈。
- `node --check app/static/app.js` 通过，桌面和窄屏布局均完成浏览器验收。

### M8-06 自动化、故障注入与端到端验收

**需求映射**：AC-1～AC-20，重点 AC-13～AC-20。

**目标**：用单元、API、集成和本地 UI 测试证明删除边界、并发行为和 Finder 行为，而不是只验证按钮存在。

**主要改动**：

- 新增 `tests/test_task_deletion.py`、`tests/test_book_deletion.py`、`tests/test_reveal_output.py`、`tests/test_management_ui.py`。
- 使用临时 `STORAGE_DIR`、临时 SQLite 和 Hook spy，禁止写入用户真实书库、输出目录或 MyDatabase。
- DeepSeek 使用阻塞 fake/spy，精确记录删除请求前后的模型调用数。
- 注入文件移动失败、SQLite 提交失败、Finder 子进程失败和服务重启。
- 用 ego lite 执行本地 UI 验收：删除确认、状态轮询、完成任务按钮；Finder 外部窗口另做人工可见性确认。
- 运行既有任务队列、上传生命周期、端到端流水线、缓存和 MyDatabase Hook 回归。

**涉及文件**：

- `tests/` 下新增测试文件
- `docs/TEST_CASES_V0.2.1.md`

**完成定义**：

- `docs/TEST_CASES_V0.2.1.md` 中 P0 用例全部通过并保留证据。
- AC-13 至 AC-20 每项至少由一个自动化用例覆盖；Finder 窗口可见性由本机人工验收补充。
- AC-1 至 AC-12 相关既有回归全部通过。
- 完整校验通过：`.venv/bin/python -m pytest -q`、`.venv/bin/python -m compileall -q app tests`、`node --check app/static/app.js`、`git diff --check`。

### M8-07 文档、版本与交付检查

**需求映射**：M8 发布完整性。

**目标**：使文档、OpenAPI、版本和实际行为一致，不把测试通过误报为功能已上线。

**主要改动**：

- 更新 README 的任务/书籍删除、数据保留边界和 Finder 行为。
- 记录三个新 API 的状态码和错误语义。
- 将服务版本升级到 v0.2.1，并核对 `/health`。
- 更新需求文档状态：只有代码、自动化、ego lite UI 和 Finder 验收全部通过后，才把 M8 标为完成。
- 如需启动本地服务，核对监听进程来自当前 checkout，避免旧服务冒充新版本。

**涉及文件**：

- `README.md`
- `需求文档.md`
- `app/main.py`
- 必要的测试报告

**完成定义**：

- `/health` 返回 0.2.1，OpenAPI 可见三个新能力。
- 文档明确说明书籍删除不会删除既有导出和 MyDatabase 内容。
- 当前 checkout、测试结果、服务 PID/版本和验收报告可相互核对。

## 5. 全局完成定义

只有同时满足以下条件，本轮才可关闭：

1. M8-01 至 M8-06 全部完成，P0 测试用例全部通过。
2. 任务删除、书籍删除和打开文件夹在 ego lite 中完成一次真实本地操作验收。
3. 删除运行中任务后无新增 DeepSeek 请求；删除失败/重启后无幽灵任务或幽灵书籍。
4. 删除书籍后导入来源文件、既有精华 MD 和 MyDatabase 内容仍存在。
5. 完整自动化、Python 编译、JavaScript 语法和 diff 格式检查通过。
6. 文档与 `/health` 均报告 v0.2.1，且服务确实运行当前 checkout。
