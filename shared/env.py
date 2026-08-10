"""只加载显式白名单环境变量，不引入第三方 dotenv。"""

import os
from pathlib import Path
from typing import Iterable, Optional


def load_selected(keys: Iterable[str], env_file: Optional[str] = None) -> None:
    allowed = set(keys)
    selected = env_file or os.environ.get("TRADINGCAT_ENV_FILE")
    path = (Path(selected).expanduser() if selected
            else Path(__file__).resolve().parents[1] / ".env")
    if not path.is_file():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key not in allowed:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)
    except OSError as exc:
        raise RuntimeError(f"无法读取配置文件 {path}: {exc}") from exc
