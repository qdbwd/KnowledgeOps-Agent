-- 业务、真实模型额度账本、离线测试各用独立数据库。
-- 测试只允许清理 knowledgeops_test；不能重置 accounting。
CREATE DATABASE knowledgeops_accounting OWNER knowledgeops;
CREATE DATABASE knowledgeops_test OWNER knowledgeops;
