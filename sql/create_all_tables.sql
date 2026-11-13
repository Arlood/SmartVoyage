-- ============================================================
-- SmartVoyage 全量建表脚本
-- 合并所有业务表的 CREATE TABLE 语句
-- 数据库: travel_rag
-- ============================================================

DROP DATABASE IF EXISTS travel_rag;
CREATE DATABASE travel_rag CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE travel_rag;
SET NAMES utf8mb4;  -- 强制客户端连接使用 UTF-8 编码，防止 Windows 上中文写入乱码

-- ==================== 用户表（登录模块） ====================
DROP TABLE IF EXISTS users;
CREATE TABLE users (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键，自增',
    username VARCHAR(50) NOT NULL COMMENT '用户名',
    password VARCHAR(255) NOT NULL COMMENT '密码',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    UNIQUE KEY unique_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户表';

-- ==================== 用户偏好表 ====================
CREATE TABLE IF NOT EXISTS user_profiles (
    username VARCHAR(50) NOT NULL COMMENT '用户名',
    profile_key VARCHAR(50) NOT NULL COMMENT '偏好键名',
    profile_value VARCHAR(200) NOT NULL COMMENT '偏好值',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (username, profile_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户偏好';

-- ==================== 查询历史表 ====================
CREATE TABLE IF NOT EXISTS query_history (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
    username VARCHAR(50) NOT NULL COMMENT '用户名',
    intent_type VARCHAR(30) NOT NULL COMMENT '意图类型：weather/flight/train等',
    query_content TEXT NOT NULL COMMENT '查询内容JSON',
    query_time DATETIME NOT NULL COMMENT '查询时间',
    INDEX idx_username (username),
    INDEX idx_time (query_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='查询历史';

-- ==================== 短期对话表 ====================
CREATE TABLE IF NOT EXISTS short_term_messages (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
    username VARCHAR(50) NOT NULL COMMENT '用户名',
    role VARCHAR(10) NOT NULL COMMENT '消息角色：user 或 assistant',
    content TEXT NOT NULL COMMENT '消息内容',
    message_time DATETIME NOT NULL COMMENT '消息时间戳 YYYY-MM-DD HH:MM:SS',
    message_order INT NOT NULL COMMENT '消息顺序号',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    INDEX idx_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='短期对话';

-- ==================== 长期记忆表（语义记忆） ====================
-- 长期记忆用于沉淀跨会话可复用的用户事实与偏好，是短期对话记忆之上的「语义记忆」层。
-- 与 user_profiles（手动设置的偏好）不同，long_term_memories 由系统从对话中自动抽取并沉淀。
CREATE TABLE IF NOT EXISTS long_term_memories (
    username VARCHAR(50) NOT NULL COMMENT '用户名',
    memory_key VARCHAR(80) NOT NULL COMMENT '记忆唯一键，如 pref_city / pref_seat',
    memory_type VARCHAR(20) NOT NULL COMMENT '记忆类型：preference(偏好)/fact(事实)/summary(摘要)',
    memory_content VARCHAR(500) NOT NULL COMMENT '记忆内容',
    source_query VARCHAR(200) DEFAULT NULL COMMENT '来源查询（自动沉淀的依据）',
    importance TINYINT DEFAULT 5 COMMENT '重要性 1-10',
    access_count INT DEFAULT 0 COMMENT '被检索注入次数（用于判断记忆活跃度）',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (username, memory_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='长期记忆';
