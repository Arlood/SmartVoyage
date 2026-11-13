-- ============================================================
-- 升级脚本：short_term_messages.message_time 统一为完整时间戳
-- 背景：旧版本仅存 HH:MM:SS（VARCHAR(10)），跨天后无法归属日期、不可比较排序；
--       新版本统一为 YYYY-MM-DD HH:MM:SS，列类型改为 DATETIME
--       （与 query_history.query_time 口径一致）。
-- 适用：已按旧版建表脚本部署的存量库（全新部署直接执行 create_all_tables.sql 即可）
-- 说明：旧数据只有时间没有日期，按"当前日期"近似回填
--       （短期对话本身是近端数据且每用户最多 10 条，误差可接受）。
-- ============================================================
USE travel_rag;

-- 1. 先临时加宽列，确保回填后的完整时间戳字符串能容纳
--    （旧列可能是 VARCHAR(10)，在 STRICT_TRANS_TABLES 下直接回填
--     CONCAT(CURDATE(),' ',message_time) 会因超长报 "Data too long"）
ALTER TABLE short_term_messages
    MODIFY message_time VARCHAR(32) NOT NULL;

-- 2. 为旧的"仅时间"数据回填日期（HH:MM:SS → 当天日期 + HH:MM:SS）
UPDATE short_term_messages
SET message_time = CONCAT(CURDATE(), ' ', message_time)
WHERE LENGTH(message_time) <= 8;

-- 3. 列类型改为 DATETIME（完整时间戳 YYYY-MM-DD HH:MM:SS）
ALTER TABLE short_term_messages
    MODIFY message_time DATETIME NOT NULL COMMENT '消息时间戳 YYYY-MM-DD HH:MM:SS';
