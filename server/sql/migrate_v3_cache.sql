-- ============================================
-- 增量迁移脚本：LLM答案缓存（L2持久层）
-- 适用场景：数据库已按旧版 init.sql 建好，不想重建数据时执行本脚本
-- 全新初始化请直接执行 init.sql，无需再跑本脚本
-- ============================================

SET NAMES utf8mb4;

USE db_enterprise_qa;

-- 问答答案缓存表
-- 与进程内L1缓存（LRU+TTL）组成两级缓存：L1承接高频命中，本表保证重启不丢、多进程共享
-- 注意：hit_count 入库那次算未命中，因此从 0 开始计数
CREATE TABLE IF NOT EXISTS t_answer_cache (
    id INT PRIMARY KEY AUTO_INCREMENT COMMENT '缓存ID',
    kb_id INT NOT NULL COMMENT '知识库ID',
    question_hash CHAR(64) NOT NULL COMMENT '归一化问题的sha256',
    question VARCHAR(500) NOT NULL COMMENT '原始问题（截断，仅用于排查）',
    answer MEDIUMTEXT NOT NULL COMMENT '回答文本',
    source_docs MEDIUMTEXT COMMENT '参考来源JSON',
    hit_count INT NOT NULL DEFAULT 0 COMMENT '命中次数（入库那次算未命中，从0开始）',
    create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    expire_time DATETIME NOT NULL COMMENT '过期时间（L2 TTL）',
    UNIQUE KEY uk_kb_question (kb_id, question_hash),
    KEY idx_expire_time (expire_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='问答答案缓存表（L2）';
