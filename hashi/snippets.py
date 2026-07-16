"""スニペット(よく使うコマンド)のモデルと永続化。"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .config import config_dir
from .jsonio import load_json, save_json_atomic

logger = logging.getLogger(__name__)

_VAR_PATTERN = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")


@dataclass
class Snippet:
    """1 件のスニペット。"""

    name: str = ""
    body: str = ""
    send_enter: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Snippet":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def find_variables(body: str) -> list[str]:
    """本文中の {{変数名}} を出現順に重複を除いて返す。"""
    seen: set[str] = set()
    variables: list[str] = []
    for m in _VAR_PATTERN.finditer(body):
        v = m.group(1)
        if v not in seen:
            seen.add(v)
            variables.append(v)
    return variables


def expand_snippet(body: str, values: dict[str, str]) -> str:
    """変数を置換した本文を返す。値が無い変数は元の {{...}} のままにする。"""

    def repl(m: re.Match) -> str:
        return values.get(m.group(1), m.group(0))

    return _VAR_PATTERN.sub(repl, body)


class SnippetStore:
    """スニペット一覧の永続化。"""

    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "snippets.json")
        self.snippets: list[Snippet] = []
        self.load()

    def load(self) -> None:
        self.snippets = []
        try:
            data = load_json(
                self.path,
                list,
                logger=logger,
                warning="snippets.json を読み込めません(空で続行): %s",
            )
            for d in data:
                self.snippets.append(Snippet.from_dict(d))
        except Exception:
            logger.warning(
                "snippets.json を読み込めません(無視して続行): %s",
                self.path,
                exc_info=True,
            )
            self.snippets = []

    def save(self) -> None:
        save_json_atomic(
            self.path,
            [asdict(s) for s in self.snippets],
            ensure_ascii=False,
            indent=2,
        )

    def add(self, snippet: Snippet) -> None:
        self.snippets.append(snippet)
        self.save()

    def update(self, index: int, snippet: Snippet) -> None:
        self.snippets[index] = snippet
        self.save()

    def remove(self, index: int) -> None:
        del self.snippets[index]
        self.save()

    def move_up(self, index: int) -> None:
        if index > 0:
            self.snippets[index - 1], self.snippets[index] = (
                self.snippets[index],
                self.snippets[index - 1],
            )
            self.save()

    def move_down(self, index: int) -> None:
        if 0 <= index < len(self.snippets) - 1:
            self.snippets[index], self.snippets[index + 1] = (
                self.snippets[index + 1],
                self.snippets[index],
            )
            self.save()
