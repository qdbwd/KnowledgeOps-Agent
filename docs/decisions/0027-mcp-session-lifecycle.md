# 0027：保留官方 MCPAdapter，补齐持续会话管理

日期：2026-10-14。状态：用户确认保留现有库并实施会话复用。

用户要求参考DeerFlow补齐MCP连接与会话管理，不能把返回消息已保存等同于服务进程或浏览器状态已经保留。现有工具调用每次使用`asyncio.run`，并在调用结束退出适配器上下文，缺少应用生命周期内的连接复用。

## 依赖与复用方式

保持`langchain[mcp]==1.4.3`和锁定的FastMCP版本。[独立适配包仓库](https://github.com/langchain-ai/langchain-mcp-adapters)已经归档，官方后续维护转到`langchain.mcp`；用户确认采用现有包。直接使用[MCPAdapter持续上下文](https://docs.langchain.com/oss/python/langchain/mcp/connections)和[AnyIO BlockingPortal](https://anyio.readthedocs.io/en/stable/threads.html#starting-a-blocking-portal)，不另写MCP协议、不替换已用依赖。AnyIO原本已经在锁文件中，本轮把直接使用它的事实声明到项目依赖，未升级解析版本。

核对DeerFlow提交`4e9f6c68ea107265d28017d332285b843b033e57`的`backend/packages/harness/deerflow/mcp/session_pool.py`、`lifecycle.py`与对应session_pool/loop_isolation测试。借鉴连接由同一协程建立和退出、作用域隔离、容量/LRU、配置失效及关闭生命周期。其实现依赖它的客户端和应用身份，本项目使用当前SDK加PG授权修订适配这些职责，不复制整包或引入DeerFlow运行环境。Deep Agents当前MCP文档也将协议接入指向LangChain适配层，子角色身份继续复用本项目真实子Run。

## 应用合同

- 一个worker管理一个惰性池、一个长期事件循环；没有MCP调用则不开线程。每个连接用独立所有者协程持有SDK上下文。
- 主Agent以聊天为作用域，跨同聊天的顺序Run复用；子Agent以子Run隔离，同聊天不同子Run也不共用。服务ID、连接参数、授权代次及凭据指纹参与身份判断，凭据不写进数据库或日志。
- 同连接串行调用。默认上限32、空闲1800秒、维护间隔2秒、关闭等待5秒；Settings从`.env`读取。关闭中的资源继续计入容量，不能通过并发淘汰绕过限制。
- PG继续是启停、配置和许可的事实来源。每次调用前复查冻结配置与工具Schema，等待远端时检查运行控制；维护协程回收空闲、失效及撤回的连接，不新增Redis广播依赖。
- 管理界面发现工具使用独立短连接，不改变正式聊天的会话状态。工具审批、预算、回执与unknown合同保持有效，成功恢复不重做调用。
- 超时、断线、取消或权限撤回后关闭对应连接；池不自动重放当前操作。后续合法调用可新建连接，返回新身份。worker退出先关MCP再关PG。进程重启不恢复活的服务端状态。

## 协议选择与状态边界

FastMCP 4默认`auto`可能协商2026-07-28逐请求协议，这个协议不建立旧式握手会话，也不支持旧ping；保持客户端不等于跨请求共享服务端状态。真实本地计数服务证明了这个差异，不能只测客户端对象相同就宣称状态复用成功。

保留自动协商缺省，新增每个连接可选的`protocol_mode: "legacy"`，直接传给官方Client的`mode`以使用仍受支持的握手会话。这是显式服务兼容设置，不静默降级所有服务，也不保证远端一定以会话隔离业务数据。MCP页面解释配置方法；结果附带本地连接ID、`reused`及实际`protocol_version`。健康检查使用两代协议共有的工具目录读取。

旧JSON未包含`protocol_mode`时仍表示`auto`，保存相同配置不会因新增默认字段就被误判为修改并撤回工具许可。无需数据库表迁移，不改写已有运行快照；新增结果字段兼容旧回执。

## 验证与界限

只使用本地stdio/HTTP服务、隔离PG、确定性模型替身和生命周期测试，不调用收费模型。检查握手会话真实计数延续、自动协商协议差异、跨Run与聊天/子角色隔离、同连接串行、容量、关闭中占用、取消、凭据轮换、配置撤回、断线及unknown不重放。原审批/图恢复和工具策略定向回归；证据集中在[progress](../progress.md)。

默认外部服务仍为空，没有接入实际浏览器或公共MCP，不声称所有服务都具备相同的状态管理行为。强制杀死worker无法承诺执行Python退出清理；已发送远端动作也不可能仅靠关闭连接撤销。
