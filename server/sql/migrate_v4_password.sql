-- ============================================
-- 增量迁移脚本：密码哈希算法升级（MD5 → argon2id）
-- 适用场景：数据库已按旧版 init.sql 建好，不想重建数据时执行本脚本
-- 全新初始化请直接执行 init.sql，无需再跑本脚本
-- ============================================

SET NAMES utf8mb4;

USE db_enterprise_qa;

-- 放宽密码列宽度
--
-- 【执行顺序很重要】本脚本必须在新版代码上线【之前】执行。
-- 旧列宽是 VARCHAR(64)，而 argon2id 哈希约97字符，超长会被截断；
-- 若先上线代码再迁移，用户登录触发的哈希升级写入时会报 1406 Data too long，
-- 或（非严格模式下）被静默截断，导致密码永久无法验证。
--
-- 【存量数据无需处理】t_user 里现存的32位十六进制值是无盐MD5，
-- 新版登录逻辑能识别并校验它，校验通过后自动改写为 argon2id 哈希，
-- 用户下次登录即完成升级，期间无需重置密码、也不会感知到变化。
-- 升级进度可这样查看：SELECT COUNT(*) FROM t_user WHERE password NOT LIKE '$argon2id$%';
ALTER TABLE t_user
    MODIFY password VARCHAR(255) NOT NULL COMMENT '密码（argon2id加盐哈希）';
