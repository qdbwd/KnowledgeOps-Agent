"""知识正文与会话原话共用的PG词法检索组件；身份、权限和生命周期仍由业务层处理。

分段直接使用LangChain；分词沿用jieba与DeerFlow的中文预处理思路。
BM25直接交给ParadeDB pg_search，不在Python里手写评分或另存SQLite。
0017迁移也使用本模块的v1分段合同；改变粒度须新增版本并重建派生索引。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import jieba
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sqlalchemy import and_, cast, func, literal, literal_column, or_
from sqlalchemy.types import UserDefinedType

INDEX_CONTRACT = "pg-search-bm25-passages-v1"
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 160
STOP_WORDS = frozenset(
    "的 了 是 在 和 与 及 以及 我 你 我们 这个 这些 请 请问 一下 如何 什么 哪些 怎么 相关".split()
)


@dataclass(frozen=True)
class Passage:
    """原文中的半开字符区间；不是模型token位置，不改写或清洗原文。"""

    start: int
    end: int
    text: str


def split_passages(text: str) -> list[Passage]:
    """先保留段落，再按中英文句界递归拆长段；保留分隔符与可核对偏移。

    1200/160是本项目的有界词法检索初值，不是DeerFlow默认值，也非语义最优值。
    小消息保持完整，长消息/长段落才细分；不把消息之间的不同角色混成一段。
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=[
            "\n\n",
            "\n",
            "。",
            "！",
            "？",
            ". ",
            " ",
            "，",
            "",
        ],
        keep_separator=True,
        strip_whitespace=False,
        add_start_index=True,
    )
    result = []
    for document in splitter.create_documents([text]):
        start = document.metadata["start_index"]
        end = start + len(document.page_content)
        # 防止未来依赖变化让索引位置脱离不可变原文，不能返回看似有效的引用范围。
        if start < 0 or text[start:end] != document.page_content:
            raise ValueError("passage offset does not match source")
        result.append(Passage(start, end, document.page_content))
    return result


def tokenize(value: str) -> str:
    """保留词频及顺序，补充完整技术标识符；不要像旧索引那样去重全部正文词。

    cut_for_search把中文复合词补成可检索子词，仍是词法匹配。数字、否定词与
    条件词保留；停用词不随测试答案调整。查询与索引必须使用同一预处理。
    """
    lowered = value.lower()
    pieces = [
        piece
        for piece in jieba.cut_for_search(lowered, HMM=False)
        if re.search(r"\w", piece) and piece not in STOP_WORDS and piece != "_"
    ]
    existing = set(pieces)
    identifiers = re.findall(r"[a-z][a-z0-9_]*(?:[.+-][a-z0-9_]+)*|\d+(?:\.\d+)?", lowered)
    return " ".join([*pieces, *dict.fromkeys(p for p in identifiers if p not in existing)])


@dataclass(frozen=True)
class LexicalQuery:
    """把自然语言视为数据；不开放SQL或FTS高级表达式注入。"""

    text: str
    terms: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> LexicalQuery:
        bounded = text.strip()[:500].lower()
        return cls(bounded, tuple(dict.fromkeys(tokenize(bounded).split()))[:32])


def weighted_vector(text: str, *, title: str = "", metadata: str = ""):
    """兼容旧search_entries结构供回退；当前检索已使用独立BM25片段索引。"""
    body = func.setweight(func.to_tsvector("simple", tokenize(text)), literal_column("'C'"))
    if not title and not metadata:
        return body
    header = func.setweight(func.to_tsvector("simple", tokenize(title)), literal_column("'A'"))
    tags = func.setweight(func.to_tsvector("simple", tokenize(metadata)), literal_column("'B'"))
    return header.op("||")(tags).op("||")(body)


class _Boost(UserDefinedType):
    """SQLAlchemy对pg_search公开SQL类型的薄适配，不实现检索算法。"""

    cache_ok = True

    def __init__(self, factor: int):
        self.factor = factor

    def get_col_spec(self, **kw):
        return f"pdb.boost({int(self.factor)})"


def search_expressions(chunk, query: LexicalQuery, *, knowledge: bool = False):
    """使用pg_search公开的匹配运算符和pdb.score得到真实BM25分值。

    OR召回允许自然语言里有未命中的词，数字仍作为硬约束。知识标题/元数据
    以3/2倍查询boost参与BM25，正文1倍；会话原话不套用文章字段权重。
    查询先用相同jieba分词，再交给whitespace tokenizer，参数绑定避免把
    用户文本当成高级查询语法。调用方负责先限制可读范围，再去重和限量。
    """
    if not query.terms:
        raise ValueError("empty lexical query")
    tokens = " ".join(query.terms)
    columns = [chunk.body_tokens]
    matches = [chunk.body_tokens.bool_op("|||")(tokens)]
    if knowledge:
        for column, boost in [(chunk.title_tokens, 3), (chunk.metadata_tokens, 2)]:
            columns.append(column)
            matches.append(column.bool_op("|||")(cast(literal(tokens), _Boost(boost))))
    numeric_constraints = [
        or_(*(column.bool_op("===")(term) for column in columns))
        for term in query.terms
        if re.fullmatch(r"\d+(?:\.\d+)?", term)
    ]
    # 0.26版本不再要求唯一key_field。score参数定位索引表，每条片段各有分数，
    # 不以source_id是否重复决定分值；复合主键仍由PG维护。
    return and_(or_(*matches), *numeric_constraints), func.pdb.score(chunk.chunk_index)


def excerpt(text: str, start: int, end: int, query: LexicalQuery, width: int = 600) -> Passage:
    """摘录围绕命中位置，而非总取消息开头；返回偏移供补读工具使用。"""
    segment = text[start:end].lower()
    exact = segment.find(query.text)
    matches = [segment.find(term) for term in query.terms]
    positions = [position for position in matches if position >= 0]
    anchor = exact if exact >= 0 else min(positions, default=0)
    left = min(max(start, start + anchor - 80), max(start, end - width))
    right = min(end, left + width)
    return Passage(left, right, text[left:right])
