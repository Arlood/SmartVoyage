-- ============================================================
-- 授予业务账号 smart_voyage 对所有业务库的访问权限
-- ------------------------------------------------------------
-- 背景：
--   1. travel_rag 库权限本应由 docker-compose 的 MYSQL_USER 环境变量自动授予，
--      但 01_create_tables.sql 开头有 DROP DATABASE travel_rag，此处重新授权以防权限丢失；
--   2. business_mcp 库由 02_business_mcp.sql 创建，MYSQL_USER 默认只授权 MYSQL_DATABASE(travel_rag)，
--      故需在此显式授予 business_mcp 权限。
--   3. 本脚本作为 docker-entrypoint-initdb.d 的 03 号脚本，在两个库创建完成后执行。
-- 注意：账号名与 docker-compose 的 DB_USER 默认值(smart_voyage)保持一致；
--       若通过 DB_USER 自定义账号名，需同步修改本脚本中的 'smart_voyage'。
-- ============================================================

GRANT ALL PRIVILEGES ON travel_rag.* TO 'smart_voyage'@'%';
GRANT ALL PRIVILEGES ON business_mcp.* TO 'smart_voyage'@'%';
FLUSH PRIVILEGES;
