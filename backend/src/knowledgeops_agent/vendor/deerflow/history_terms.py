# Adapted from bytedance/deer-flow commit 127c2c220c30d875b2995e95608b98d86db6bc13.
# MIT license: see LICENSE in this directory. Changes documented in README-continuity.md.
import re


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[^\W_]+", text.casefold())
    tokens: list[str] = []
    for word in words:
        if re.search(r"[\u3400-\u9fff]", word):
            tokens.extend(word[i : i + 2] for i in range(max(1, len(word) - 1)))
        else:
            tokens.append(word)
    return list(dict.fromkeys(tokens))


def terms(text: str) -> list[str]:
    return _tokens(text[:500])[:32]


def index_text(text: str) -> str:
    return " ".join(_tokens(text))
