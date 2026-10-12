"""DeerFlow DDGS配置纯函数摘取，MIT许可见同目录LICENSE。

来源：ee0ba401a6fa566459b85517ead136ebae908b53 / backend/packages/harness/
deerflow/community/ddg_search/tools.py。只摘取后端规范化和Wikipedia地区修正，
移除DeerFlow应用配置/日志/工具依赖，函数行为不变；格式按本项目Ruff整理。
"""

DEFAULT_BACKEND = "auto"
DEFAULT_REGION = "wt-wt"
DEFAULT_WIKIPEDIA_REGION = "us-en"
WIKIPEDIA_BACKENDS = {"auto", "all", "wikipedia"}
WIKIPEDIA_LANGUAGE_ALIASES = {"jp": "ja", "kr": "ko", "tzh": "zh", "wt": "en"}


def _normalize_backend(backend: str | list[str] | tuple[str, ...] | None) -> str:
    if backend is None:
        return DEFAULT_BACKEND
    if isinstance(backend, (list, tuple)):
        return (
            ",".join(str(part).strip() for part in backend if str(part).strip()) or DEFAULT_BACKEND
        )
    return str(backend).strip() or DEFAULT_BACKEND


def _normalize_setting(value: str | None, default: str) -> str:
    return str(value).strip() if value else default


def _backend_includes_wikipedia(backend: str | list[str] | tuple[str, ...] | None) -> bool:
    backend = _normalize_backend(backend)
    return any(part.strip().lower() in WIKIPEDIA_BACKENDS for part in backend.split(","))


def _contains_codepoint(query: str, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= ord(char) <= end for char in query for start, end in ranges)


def _infer_wikipedia_region(query: str) -> str:
    """Pick a valid Wikipedia language region when DDGS' worldwide region is used."""
    if _contains_codepoint(query, ((0x3040, 0x30FF), (0x31F0, 0x31FF))):
        return "jp-ja"
    if _contains_codepoint(query, ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F))):
        return "kr-ko"
    if _contains_codepoint(query, ((0x3400, 0x9FFF),)):
        return "cn-zh"
    if _contains_codepoint(query, ((0x0400, 0x04FF),)):
        return "ru-ru"
    if _contains_codepoint(query, ((0x0370, 0x03FF),)):
        return "gr-el"
    if _contains_codepoint(query, ((0x0590, 0x05FF),)):
        return "il-he"
    if _contains_codepoint(query, ((0x0600, 0x06FF),)):
        return "xa-ar"
    return DEFAULT_WIKIPEDIA_REGION


def _resolve_ddgs_region(
    query: str, region: str | None, backend: str | list[str] | tuple[str, ...] | None
) -> str:
    """DDGS的Wikipedia引擎将region第二段用作子域，wt-wt不能直接传给它。"""
    normalized_region = _normalize_setting(region, DEFAULT_REGION).lower()
    if not _backend_includes_wikipedia(backend):
        return normalized_region
    if normalized_region == DEFAULT_REGION:
        return _infer_wikipedia_region(query)
    if "-" not in normalized_region:
        return DEFAULT_WIKIPEDIA_REGION
    country, language = normalized_region.split("-", 1)
    return f"{country}-{WIKIPEDIA_LANGUAGE_ALIASES.get(language, language)}"
