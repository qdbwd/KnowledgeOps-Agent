# LangGraph 保存器读取适配

`checkpoint_cache.py` 的检查点头部/未完成写入 SQL 和配置定位逻辑参考已锁定的
`langgraph-checkpoint-postgres 3.1.2` 的 `PostgresSaver.get_tuple`、`BasePostgresSaver.SELECT_SQL`。
本项目改为先查询头部，再按确定检查点和通道版本通过 Redis/PG 读取复杂内容。
官方保存器仍作为依赖使用，没有复制其迁移、写入、历史列表或图运行代码。

原许可证保留于本目录 LICENSE。升级该依赖时须核对缓存适配与恢复测试。
