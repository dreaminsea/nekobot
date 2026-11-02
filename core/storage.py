"""本地 JSON 持久化 —— 原子读写，供 session / memory 共用。"""

import json
import os

from config import DATA_DIR
from core.log import log


def _path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def load_json(name: str, default):
    """读取 DATA_DIR 下的 JSON，不存在或损坏时返回 default。"""
    path = _path(name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log.warning(f"读取 {path} 失败({e!r})，使用默认值")
        return default


def save_json(name: str, obj) -> None:
    """原子写入 DATA_DIR 下的 JSON（先写临时文件再 os.replace）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _path(name)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
