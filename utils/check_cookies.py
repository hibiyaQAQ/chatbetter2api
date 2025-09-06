import requests
import json
import time
import schedule
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from typing import List, Optional

# 导入数据库相关模块
from db import get_db
from models.tokens import Token
from utils.register import refresh_silent_cookies, signin_with_access_token, fetch_auth_info

# 导入Redis缓存相关模块
from utils.redis_cache import test_connection as test_redis_connection
from utils.account_manager import token_to_dict, cache_account, remove_cached_account

# ================= 修改后的日志配置 =================
# 🚀 只输出到控制台 (stdout)，避免只读文件系统错误
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("cookies_checker")
# ===================================================

# 常量设置
# 刷新调度间隔（秒），每10分钟执行一次
CHECK_INTERVAL_SECONDS = 600
EXPIRY_WARNING_DAYS = 7    # 过期警告天数
# 多线程刷新时的最大线程数
MAX_WORKER_THREADS = 20

def find_expiring_accounts(db: Session) -> List[Token]:
    """
    查找即将过期的账号（cookies过期时间在7天内）
    """
    now = datetime.now()
    expiry_date = now + timedelta(days=EXPIRY_WARNING_DAYS)
    
    query = db.query(Token).filter(
        Token.enable == 1,
        Token.deleted_at == None,
        Token.cookies_expires <= expiry_date,
        Token.cookies_expires >= now
    )
    
    accounts = query.all()
    logger.info(f"发现 {len(accounts)} 个cookies即将过期的账号")
    return accounts

def parse_cookies_to_dict(cookies_str: str) -> dict:
    """
    将cookies字符串解析为字典
    """
    if not cookies_str:
        return {}
    
    try:
        return json.loads(cookies_str)
    except json.JSONDecodeError:
        logger.error(f"解析cookies字符串失败: {cookies_str[:100]}")
        return {}

def refresh_cookies(account: Token, db: Session) -> bool:
    """
    尝试刷新账号的cookies
    """
    if not account.silent_cookies:
        logger.error(f"账号 {account.account} 没有cookies")
        return False
    
    cookies = parse_cookies_to_dict(account.silent_cookies)
    if not cookies:
        logger.error(f"账号 {account.account} 的cookies无效")
        return False
    
    try:
        success, updated_cookies, access_token = refresh_silent_cookies(cookies)
        
        if not success or not updated_cookies or not access_token:
            return False
        
        account.silent_cookies = json.dumps(updated_cookies)
        account.access_token = access_token
        account.cookies_expires = datetime.now() + timedelta(days=30)
        account.updated_at = datetime.now()
        account.token_expires = datetime.now() + timedelta(minutes=15)
        account.updated_at = datetime.now()
        account.enable = 1
        db.commit()

        if account.access_token and not account.token:
            auth0 = signin_with_access_token(account.access_token)
            account.token = auth0.get('token')

        auth_data = fetch_auth_info(account.token, account.access_token)

        if not auth_data:
            logger.error(f"账号 {account.account} 的auth为空")
        else:
            import json as _json
            account.auth = _json.dumps(auth_data, ensure_ascii=False)
            account.account_type = auth_data.get("account_type", None)

        return True
    
    except Exception as e:
        logger.exception(f"账号 {account.account} 刷新时发生异常: {str(e)}")
    
    return False

def disable_account(account: Token, db: Session):
    """禁用账号"""
    account.enable = 0
    db.commit()
    try:
        if test_redis_connection():
            remove_cached_account(account.id, is_paid=False)
            if account.account_type == 'paid':
                remove_cached_account(account.id, is_paid=True)
    except Exception as e:
        logger.error(f"从Redis缓存移除账号失败: {str(e)}")

def enable_account(account: Token, db: Session):
    """启用账号"""
    account.enable = 1
    db.commit()
    try:
        if test_redis_connection():
            account_data = token_to_dict(account)
            cache_account(account.id, account_data, is_paid=False)
            if account.account_type == 'paid':
                cache_account(account.id, account_data, is_paid=True)
    except Exception as e:
        logger.error(f"更新Redis缓存账号失败: {str(e)}")

def refresh_single_account(account_id: int):
    """在独立的线程中刷新单个账号"""
    db = None
    try:
        db = next(get_db())
        account = db.query(Token).filter(Token.id == account_id).first()
        
        if not account:
            logger.error(f"找不到ID为 {account_id} 的账号")
            return
        
        success = refresh_cookies(account, db)
        
        if success:
            enable_account(account, db)
        else:
            disable_account(account, db)
            logger.info(f"账号 {account.account} 刷新失败并已禁用")
            
    except Exception as e:
        logger.exception(f"刷新账号 ID {account_id} 时发生错误: {str(e)}")
    finally:
        if db:
            db.close()

def check_and_refresh_accounts():
    """批量刷新所有账号"""
    logger.info("开始执行批量刷新任务...")

    db = None
    try:
        db = next(get_db())
        accounts = db.query(Token).filter(Token.deleted_at == None).all()
        
        if not accounts:
            logger.info("没有账号需要刷新")
            return
            
        logger.info(f"找到 {len(accounts)} 个账号需要刷新")
        
        with ThreadPoolExecutor(max_workers=MAX_WORKER_THREADS) as executor:
            for account in accounts:
                executor.submit(refresh_single_account, account.id)
                
        logger.info(f"已完成 {len(accounts)} 个账号的刷新任务")

    except Exception as e:
        logger.exception(f"批量刷新账号时发生错误: {str(e)}")
    finally:
        if db:
            db.close()

def reset_account_counts():
    """重置所有账号的使用次数"""
    logger.info("开始执行每日账号使用次数重置...")
    
    db = None
    try:
        db = next(get_db())
        accounts = db.query(Token).filter(Token.enable == 1, Token.deleted_at == None).all()
        
        if not accounts:
            logger.info("没有需要重置的账号")
            return
            
        count = 0
        for account in accounts:
            if account.count > 0:
                account.count = 0
                count += 1
                try:
                    if test_redis_connection():
                        account_data = token_to_dict(account)
                        cache_account(account.id, account_data, is_paid=False)
                        if account.account_type == 'paid':
                            cache_account(account.id, account_data, is_paid=True)
                except Exception as e:
                    logger.error(f"更新Redis缓存账号失败: {str(e)}")
        
        db.commit()
        logger.info(f"成功重置 {count} 个账号的使用次数为0")
        
    except Exception as e:
        logger.exception(f"重置账号使用次数时发生错误: {str(e)}")
    finally:
        if db:
            db.close()

_running = False

def run_scheduler():
    """运行定时任务调度器"""
    global _running
    if _running:
        logger.warning("调度器已在运行中")
        return
    
    _running = True
    
    try:
        schedule.every(CHECK_INTERVAL_SECONDS).seconds.do(check_and_refresh_accounts)
        schedule.every().day.at("00:00").do(reset_account_counts)
        
        logger.info(f"批量刷新调度器已启动，每 {CHECK_INTERVAL_SECONDS} 秒执行一次")
        logger.info("账号使用次数重置调度器已启动，将在每天0点执行")
        
        while _running:
            schedule.run_pending()
            time.sleep(60)
    
    except Exception as e:
        logger.exception(f"调度器发生未处理的异常: {str(e)}")
    finally:
        _running = False

def stop_scheduler():
    """停止调度器"""
    global _running
    _running = False
    logger.info("Cookie检查调度器已停止")

if __name__ == "__main__":
    try:
        logger.info("Cookies检查服务启动")
        check_and_refresh_accounts()
        run_scheduler()
    except KeyboardInterrupt:
        logger.info("服务被手动停止")
        stop_scheduler()
    except Exception as e:
        logger.exception(f"服务发生未处理的异常: {str(e)}")
