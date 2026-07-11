"""JSON 設定ファイルの共通読み書き。"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def load_json(
    path: Path,
    expected_type: type[T],
    *,
    logger: logging.Logger | None = None,
    warning: str = "JSON ファイルを読み込めません(既定値で続行): %s",
) -> T:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, expected_type):
            raise TypeError(
                f"expected {expected_type.__name__}, got {type(data).__name__}"
            )
        return data
    except FileNotFoundError:
        return expected_type()
    except Exception:
        if logger is not None:
            logger.warning(warning, path, exc_info=True)
        return expected_type()


def save_json_atomic(
    path: Path,
    data,
    *,
    ensure_ascii: bool = True,
    indent: int | None = None,
    fsync: bool = False,
    temp_suffix: str = ".tmp",
) -> None:
    tmp = path.with_suffix(temp_suffix)
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
        f.flush()
        if fsync:
            os.fsync(f.fileno())
    tmp.replace(path)
