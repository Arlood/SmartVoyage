"""
SmartVoyage 项目初始化脚本

功能：
  1. 创建 MySQL 数据表（weather_data）
  2. 检查数据库连接是否正常
  3. 验证各项配置是否就绪

使用方式：
  cd 项目代码
  python init_project.py

优先级：环境变量 > config.yaml > 兜底默认值（由 Config 类统一管理）
"""

import sys
import os

# 确保能导入 SmartVoyage 包：把项目代码的上一级目录（04-代码）加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK 编码，emoji/部分中文字符可能无法编码，
# 这里降级为替换字符，避免错误信息本身触发 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


def check_database_connection():
    """检查 MySQL 数据库连接"""
    import mysql.connector
    from SmartVoyage.config import Config

    conf = Config()
    print("=" * 50)
    print("  [1/3] 检查数据库连接")
    print("=" * 50)
    print(f"  主机: {conf.host}")
    print(f"  用户: {conf.user}")
    print(f"  数据库: {conf.database}")

    try:
        conn = mysql.connector.connect(
            host=conf.host,
            user=conf.user,
            password=conf.password,
            database=conf.database,
        )
        conn.close()
        print("  ✅ 数据库连接成功\n")
        return True
    except mysql.connector.Error as e:
        print(f"  ❌ 数据库连接失败: {e}")
        print("  请检查 MySQL 是否已启动，以及 config.yaml 中的数据库配置\n")
        return False



def check_api_keys():
    """检查关键 API 密钥是否已配置"""
    from SmartVoyage.config import Config

    conf = Config()
    print("=" * 50)
    print("  [3/3] 检查 API 密钥")
    print("=" * 50)

    checks = [
        ("DashScope API Key", conf.api_key, True),
        ("和风天气 API Key", getattr(conf, "hefeng_api_key", ""), False),
        ("高德地图 API Key", getattr(conf, "amap_api_key", ""), False),
        ("博查搜索 API Key", getattr(conf, "bocha_api_key", ""), False),
        ("飞常准 API Key", getattr(conf, "variflight_api_key", ""), False),
    ]

    all_ok = True
    for name, value, required in checks:
        if value:
            # 只显示前 6 位 + 掩码
            masked = value[:6] + "****" if len(value) > 6 else "****"
            print(f"  ✅ {name}: {masked}")
        elif required:
            print(f"  ❌ {name}: 未配置（必须设置）")
            all_ok = False
        else:
            print(f"  ⚠️  {name}: 未配置（可选）")

    if not all_ok:
        print("\n  💡 请在 config.yaml 或环境变量中设置必填的 API 密钥")
    print()


def main():
    """主入口"""
    print()
    print("╔══════════════════════════════════════╗")
    print("║     SmartVoyage 项目初始化           ║")
    print("╚══════════════════════════════════════╝")
    print()

    # 第一步：检查数据库连接
    db_ok = check_database_connection()
    if not db_ok:
        print("初始化中止，请先修复数据库连接问题。")
        sys.exit(1)

    # 第二步：检查 API 密钥
    check_api_keys()

    print("=" * 50)
    print("  ✅ 初始化完成！")
    print("=" * 50)
    print()
    print("接下来可以启动服务：")
    print("  命令行客户端:  python SmartVoyage/main.py")
    print("  Web 客户端:    python SmartVoyage/api_server.py")
    print("  统一外部 MCP:  python SmartVoyage/mcp_server/mcp_external_server.py")
    print()


if __name__ == "__main__":
    main()
