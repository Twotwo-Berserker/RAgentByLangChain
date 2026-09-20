-- ============================================
-- 增量迁移脚本：对话反馈（知识溯源与反馈功能）
-- 适用场景：数据库已按旧版 init.sql 建好，不想重建数据时执行本脚本
-- 全新初始化请直接执行 init.sql，无需再跑本脚本
-- ============================================

SET NAMES utf8mb4;

USE db_enterprise_qa;

-- t_chat_history 增加反馈字段
-- 注意：MySQL 8 不支持 ADD COLUMN IF NOT EXISTS，
--       若某列已存在会报 1060，忽略该报错即可，其余列仍会正常添加
ALTER TABLE t_chat_history
    ADD COLUMN feedback TINYINT NOT NULL DEFAULT 0 COMMENT '反馈：1-赞，-1-踩，0-未评价',
    ADD COLUMN feedback_comment VARCHAR(500) DEFAULT '' COMMENT '反馈说明（踩的原因等）',
    ADD COLUMN feedback_time DATETIME DEFAULT NULL COMMENT '反馈时间';

-- 便于按反馈筛选低质量问答，用于后续优化
CREATE INDEX idx_chat_feedback ON t_chat_history (feedback);
