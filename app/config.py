"""全局配置：路径、服务参数、密钥存储。

密钥仅保存在本机 config.json，不进入代码库（见 .gitignore）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# 项目根目录（app 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

STORAGE_DIR = PROJECT_ROOT / "storage"
BOOKS_DIR = STORAGE_DIR / "books"
INTERMEDIATE_DIR = STORAGE_DIR / "intermediate"
OUTPUT_DIR = STORAGE_DIR / "output"
RUNS_DIR = STORAGE_DIR / "runs"
TASK_DB = STORAGE_DIR / "tasks.db"
CONFIG_FILE = PROJECT_ROOT / "config.json"

# 本地 Web 服务参数
HOST = "127.0.0.1"
PORT = 8000

# DeepSeek API（OpenAI 兼容接口）
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"


def ensure_dirs() -> None:
    for d in (BOOKS_DIR, INTERMEDIATE_DIR, OUTPUT_DIR, RUNS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 密钥文件仅本人可读写（0600）
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def get_api_key() -> str:
    cfg = load_config()
    secret = cfg.get("deepseek_api_key", "") or os.environ.get("DEEPSEEK_API_KEY", "")
    return secret.strip()


def set_api_key(api_key_value: str) -> None:
    cfg = load_config()
    cleaned = api_key_value.strip()
    cfg.update({"deepseek_api_key": cleaned})
    save_config(cfg)


def clear_api_key() -> None:
    cfg = load_config()
    cfg.pop("deepseek_api_key", None)
    save_config(cfg)
