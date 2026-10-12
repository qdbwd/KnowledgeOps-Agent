# DeerMem相关性排序的源码复用

来源：[DeerFlow relevance.py](https://github.com/bytedance/deer-flow/blob/3f7a076972a927b4256df3e3c18b829186dd1355/backend/packages/harness/deerflow/agents/memory/backends/deermem/deermem/core/relevance.py)，核对2026-10-12时main仍为该提交。MIT许可见本目录LICENSE。

memory_relevance.py完整摘取纯函数模块（词法相关性、IDF、置信混合、MMR），仅按本项目格式检查调整空行和换行。没有导入其文件存储、FTS5、后台队列或模型客户端；本项目使用已有PG BM25召回后用相关性/置信函数排序，业务服务负责来源、版本、范围、撤回与2000 token总预算。

上游test_memory_relevance_retrieval.py用于核对中文分词、非修改输入、相关性排序和有界输入合同。本项目tests/test_general_memory.py验证真实PG与业务边界，不把词法排序解释为语义模型效果。
