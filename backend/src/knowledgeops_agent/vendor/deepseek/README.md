# DeepSeek V4.1 tokenizer

直接依赖官方 `deepseek-recipe==0.1.1` 的请求转换、模板和分词接口，不复制推理实现。
`tokenizer-v41.json` 原样取自官方仓库提交
[`8cadfede7063c896b944e7bae05daa3549ae97ea`](https://github.com/deepseek-ai/deepseek-recipe/tree/8cadfede7063c896b944e7bae05daa3549ae97ea/static/tokenizers/v41)，
SHA256：`81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99`。
MIT许可见同目录LICENSE；没有词表修改。随Python包分发，运行时不联网下载。

使用官方V4.1聊天模板对供应商payload计数，包括工具定义与协议消息。
这仍是本地输入估计，不保证与供应商服务端模板完全相同；实际用量使用API返回的usage。
离线测试替身没有供应商序列化器时，使用同词表计算完整JSON，明确标为不同估计方法。
