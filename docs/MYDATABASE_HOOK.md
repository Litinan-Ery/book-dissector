# MyDatabase 自动导入 Hook

## 目标

每次正式生成书籍精华 Markdown 后，图书拆解器同步调用 MyDatabase：

1. 优先按原书 SHA-256 标识匹配条目。
2. 首次导入时按规范化书名匹配；重名时仅在作者唯一命中时自动选择。
3. 没有匹配条目时新建 `book` 条目。
4. 将精华写入 `vault/Notes/Book Summaries`，并在 SQLite 的 `notes` / `entry_notes` 中登记。
5. 同一本源文件重复导出只更新同一个笔记，不重复创建条目或笔记。

重名且无法判定时 hook 明确失败，不猜测条目。精华文件仍保留在 `storage/output`，导出接口返回 502，避免把“文件已生成但数据库未导入”误报为成功。

## 本机配置

`config.json` 中加入以下本地配置；该文件不进入 Git：

```json
{
  "mydatabase_hook": {
    "enabled": true,
    "project_root": "/Users/hna/Developer/personal/MyDatabase",
    "database_path": "data/mydatabase.sqlite3",
    "vault_path": "vault",
    "timeout_seconds": 30
  }
}
```

图书拆解器不直接操作 MyDatabase 的 SQLite，而是调用 MyDatabase 自己的 `import-book-summary` 命令。MyDatabase 使用单写者文件锁、稳定源指纹和原子 Markdown 替换完成幂等写入。
