# 本地命令执行的参考与摘取

本轮核对 DeerFlow `f71999d99c15630b673e46236cb65dce5d0ef531`（2026-10-10读取 main），采用 MIT 许可，版权与许可全文见本目录 LICENSE。

- [LocalSandbox](https://github.com/bytedance/deer-flow/blob/f71999d99c15630b673e46236cb65dce5d0ef531/backend/packages/harness/deerflow/sandbox/local/local_sandbox.py)：`bounded_capture.py` 摘取 `_BoundedPipeCapture` 的持续排空、按字节限额、显式报告截断设计及相关实现。Linux 单线程使用中去掉 Windows 编码回退/锁，以 bytearray 限制大量小块的对象开销；增加分页读取日志的指引。
- [进程测试](https://github.com/bytedance/deer-flow/blob/f71999d99c15630b673e46236cb65dce5d0ef531/backend/tests/test_local_sandbox_command_timeout.py)：参考真实进程组超时、stdin EOF、后台进程持有管道和输出边界的验证问题。项目测试按自身的取消与同步合同重写，不声称完全复制其行为。
- [LocalSandboxProvider](https://github.com/bytedance/deer-flow/blob/f71999d99c15630b673e46236cb65dce5d0ef531/backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py)：核对了本地对象/目录复用；本项目保留用户选择的共享知识工作区，不引入它的用户/线程缓存及路径替换体系。

`local_execution.py` 为项目适配：采用上游 Popen + 新会话进程组 + killpg 的生命周期处理，在 Linux 用 selector 读取合并 stdout/stderr、每秒接入原 JobService.guard。上游允许后台服务存活，本项目选择工具结束即回收普通后台进程，避免知识同步后继续改文件和无法取消的遗留工作。源码改动和工具语义均在 architecture 中说明。

工具 Schema、文件工具和异步后端入口仍直接使用 Deep Agents 0.7.23；也核对了其 main `9f0e39d55a5f31003b206b31b955154f2f1bc49a` 的 `libs/deepagents/deepagents/backends/local_shell.py`，其 execute 仍使用阻塞 subprocess.run、事后输出截断，没有项目需要的运行中 guard 回调。因此只替换执行内核，未升级全套依赖、复制 DeerFlow 服务或新增容器。64,000字节及60秒/最高300秒沿用本项目已有边界，并非照搬上游10MiB/600秒。
