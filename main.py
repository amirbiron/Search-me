from openai import OpenAI
import os, subprocess
VERSION = "2025-01-27-15:00"  # עדכן בכל דיפלוי - תיקון MongoDB persistence
print(f"[BOOT] VERSION={VERSION}")
print(f"[BOOT] RENDER_GIT_COMMIT={os.getenv('RENDER_GIT_COMMIT')}")
try:
    print(f"[BOOT] HEAD_SHA={subprocess.getoutput('git rev-parse HEAD')[:8]}")
except Exception:
    pass

import os
import logging
import threading
import sqlite3
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any
import asyncio
import re
import requests
from pymongo import MongoClient
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode
from flask import Flask, jsonify
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Future-proof LinkPreviewOptions handling for PTB compatibility
try:
    # PTB >=21
    from telegram import LinkPreviewOptions  # type: ignore
    _LP_KW = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
except Exception:
    # PTB 20.x fallback
    _LP_KW = {"disable_web_page_preview": True}

# הגדרת לוגינג - default to INFO level to reduce noise
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# הפחתת רעש מספריות רועשות - quieter logs
for noisy in ("httpcore", "httpx", "urllib3", "apscheduler", "werkzeug", "telegram", "requests", "openai", "pymongo"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# --- הגדרות ה-API של Perplexity ---
API_KEY = os.getenv("PERPLEXITY_API_KEY")
client = OpenAI(api_key=API_KEY, base_url="https://api.perplexity.ai")

# משתני סביבה
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
DB_PATH = os.getenv('DB_PATH', '/var/data/watchbot.db')
PORT = int(os.getenv('PORT', 5000))

# MongoDB configuration
MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
MONGODB_DB_NAME = os.getenv('MONGODB_DB_NAME', 'watchbot')
USE_MONGODB = os.getenv('USE_MONGODB', 'false').lower() == 'true'

# לוג משתני סביבה חשובים
logger.info(f"Environment variables loaded - ADMIN_ID: {ADMIN_ID}, BOT_TOKEN: {'SET' if BOT_TOKEN else 'NOT SET'}")

# קבועים
MONTHLY_LIMIT = 200  # מגבלת שאילתות חודשית
DEFAULT_PROVIDER = "perplexity"

# יצירת ספריית נתונים אם לא קיימת
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

class WatchBotDB:
    """מחלקה לניהול בסיס הנתונים"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        """יצירת טבלאות בסיס הנתונים"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # טבלת משתמשים
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # טבלת נושאים למעקב
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS watch_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                topic TEXT NOT NULL,
                check_interval INTEGER DEFAULT 24,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_checked TIMESTAMP,
                checks_remaining INTEGER DEFAULT NULL,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # הוספת עמודת checks_remaining לטבלאות קיימות
        try:
            cursor.execute('ALTER TABLE watch_topics ADD COLUMN checks_remaining INTEGER DEFAULT NULL')
            conn.commit()
        except sqlite3.OperationalError:
            # העמודה כבר קיימת
            pass
        
        # טבלת תוצאות שנמצאו
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS found_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER,
                title TEXT,
                url TEXT,
                content_summary TEXT,
                content_hash TEXT,
                found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_sent BOOLEAN DEFAULT 0,
                FOREIGN KEY (topic_id) REFERENCES watch_topics (id)
            )
        ''')
        
        # טבלת סטטיסטיקת שימוש
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS usage_stats (
                user_id INTEGER,
                month TEXT,
                usage_count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, month),
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def add_user(self, user_id: int, username: str = None):
        """הוספת משתמש חדש"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username)
            VALUES (?, ?)
        ''', (user_id, username))
        conn.commit()
        conn.close()
    
    def add_watch_topic(self, user_id: int, topic: str, check_interval: int = 24, checks_remaining: int = None) -> int:
        """הוספת נושא למעקב"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO watch_topics (user_id, topic, check_interval, checks_remaining)
            VALUES (?, ?, ?, ?)
        ''', (user_id, topic, check_interval, checks_remaining))
        topic_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return topic_id
    
    def get_user_topics(self, user_id: int) -> List[Dict]:
        """קבלת רשימת נושאים של משתמש"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, topic, check_interval, is_active, created_at, last_checked, checks_remaining
            FROM watch_topics
            WHERE user_id = ? AND is_active = 1
            ORDER BY created_at DESC
        ''', (user_id,))
        
        topics = []
        for row in cursor.fetchall():
            topics.append({
                'id': row[0],
                'topic': row[1],
                'check_interval': row[2],
                'is_active': row[3],
                'created_at': row[4],
                'last_checked': row[5],
                'checks_remaining': row[6]
            })
        
        conn.close()
        return topics
    
    def remove_topic(self, user_id: int, topic_identifier: str) -> bool:
        """הסרת נושא (לפי ID או שם)"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # ניסיון ראשון - לפי ID
        if topic_identifier.isdigit():
            cursor.execute('''
                UPDATE watch_topics SET is_active = 0
                WHERE user_id = ? AND id = ? AND is_active = 1
            ''', (user_id, int(topic_identifier)))
        else:
            # ניסיון שני - לפי שם הנושא
            cursor.execute('''
                UPDATE watch_topics SET is_active = 0
                WHERE user_id = ? AND topic LIKE ? AND is_active = 1
            ''', (user_id, f'%{topic_identifier}%'))
        
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success
    
    def update_topic_text(self, user_id: int, topic_id: str, new_text: str) -> bool:
        """עדכון טקסט הנושא"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE watch_topics 
            SET topic = ? 
            WHERE user_id = ? AND id = ? AND is_active = 1
        ''', (new_text, user_id, int(topic_id)))
        
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success
    
    def update_topic_frequency(self, user_id: int, topic_id: str, new_frequency: int) -> bool:
        """עדכון תדירות בדיקת הנושא"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE watch_topics 
            SET check_interval = ? 
            WHERE user_id = ? AND id = ? AND is_active = 1
        ''', (new_frequency, user_id, int(topic_id)))
        
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success
    
    def toggle_user_status(self, user_id: int, is_active: bool):
        """הפעלה/השבתה של משתמש"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users SET is_active = ?
            WHERE user_id = ?
        ''', (is_active, user_id))
        conn.commit()
        conn.close()
    
    def get_active_topics_for_check(self) -> List[Dict]:
        """קבלת נושאים פעילים לבדיקה לפי תדירות"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        current_time = datetime.now()
        
        cursor.execute('''
            SELECT wt.id, wt.user_id, wt.topic, wt.last_checked, wt.check_interval, wt.checks_remaining
            FROM watch_topics wt
            JOIN users u ON wt.user_id = u.user_id
            WHERE wt.is_active = 1 AND u.is_active = 1
        ''')
        
        topics = []
        for row in cursor.fetchall():
            topic_id, user_id, topic, last_checked, check_interval, checks_remaining = row
            
            # בדיקה אם הגיע הזמן לבדוק את הנושא
            should_check = False
            
            # אם יש מגבלת בדיקות ונגמרו, לא לבדוק
            if checks_remaining is not None and checks_remaining <= 0:
                should_check = False
            elif not last_checked:
                should_check = True
            else:
                last_check_time = datetime.fromisoformat(last_checked)
                time_diff = current_time - last_check_time
                
                # אם זה בדיקות של 5 דקות, בדוק כל 5 דקות
                if check_interval == 0.0833:  # 5 דקות בשעות (5/60)
                    if time_diff >= timedelta(minutes=5):
                        should_check = True
                elif time_diff >= timedelta(hours=check_interval):
                    should_check = True
            
            if should_check:
                topics.append({
                    'id': topic_id,
                    'user_id': user_id,
                    'topic': topic,
                    'last_checked': last_checked,
                    'check_interval': check_interval,
                    'checks_remaining': checks_remaining
                })
        
        conn.close()
        return topics
    
    def get_topic_by_id(self, topic_id: int) -> Dict:
        """קבלת פרטי נושא לפי מזהה"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, user_id, topic, check_interval, is_active, created_at, last_checked, checks_remaining
            FROM watch_topics
            WHERE id = ?
        ''', (topic_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'id': row[0],
                'user_id': row[1],
                'topic': row[2],
                'check_interval': row[3],
                'is_active': row[4],
                'created_at': row[5],
                'last_checked': row[6],
                'checks_remaining': row[7]
            }
        return None
    
    def save_result(self, topic_id: int, title: str, url: str, content_summary: str) -> int:
        """שמירת תוצאה שנמצאה"""
        try:
            # וידוא שהפרמטרים תקינים
            if not title:
                title = 'ללא כותרת'
            if not url:
                url = ''
            if not content_summary:
                content_summary = 'ללא סיכום'
            
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # יצירת hash ייחודי לתוכן למניעת כפילויות
            content_hash = str(hash(f"{title}{url}{content_summary}"))
            
            # בדיקה אם התוצאה כבר קיימת
            cursor.execute('''
                SELECT id FROM found_results
                WHERE topic_id = ? AND (url = ? OR content_hash = ?)
            ''', (topic_id, url, content_hash))
            
            if not cursor.fetchone():
                cursor.execute('''
                    INSERT INTO found_results (topic_id, title, url, content_summary, content_hash)
                    VALUES (?, ?, ?, ?, ?)
                ''', (topic_id, title, url, content_summary, content_hash))
                conn.commit()
                result_id = cursor.lastrowid
            else:
                result_id = None
            
            conn.close()
            return result_id
            
        except Exception as e:
            logger.error(f"Error saving result for topic {topic_id}: {e}")
            return None
    
    def update_topic_checked(self, topic_id: int):
        """עדכון זמן הבדיקה האחרון וספירת בדיקות נותרות"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # קבלת מספר הבדיקות הנותרות הנוכחי
        cursor.execute('SELECT checks_remaining FROM watch_topics WHERE id = ?', (topic_id,))
        result = cursor.fetchone()
        
        if result and result[0] is not None:
            checks_remaining = result[0]
            if checks_remaining > 1:
                # הפחתת מספר הבדיקות הנותרות
                cursor.execute('''
                    UPDATE watch_topics 
                    SET last_checked = CURRENT_TIMESTAMP, checks_remaining = checks_remaining - 1
                    WHERE id = ?
                ''', (topic_id,))
            elif checks_remaining == 1:
                # זו הבדיקה האחרונה - הפוך את הנושא ללא פעיל
                cursor.execute('''
                    UPDATE watch_topics 
                    SET last_checked = CURRENT_TIMESTAMP, checks_remaining = 0, is_active = 0
                    WHERE id = ?
                ''', (topic_id,))
            else:
                # אם כבר נגמרו הבדיקות, הפוך את הנושא ללא פעיל
                cursor.execute('''
                    UPDATE watch_topics 
                    SET last_checked = CURRENT_TIMESTAMP, is_active = 0
                    WHERE id = ?
                ''', (topic_id,))
        else:
            # בדיקות רגילות ללא מגבלה
            cursor.execute('''
                UPDATE watch_topics SET last_checked = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (topic_id,))
        
        conn.commit()
        conn.close()
    
    def get_user_usage(self, user_id: int) -> Dict[str, int]:
        """קבלת נתוני שימוש של משתמש"""
        current_month = datetime.now().strftime("%Y-%m")
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT usage_count FROM usage_stats
            WHERE user_id = ? AND month = ?
        ''', (user_id, current_month))
        
        result = cursor.fetchone()
        current_usage = result[0] if result else 0
        
        conn.close()
        return {
            'current_usage': current_usage,
            'monthly_limit': MONTHLY_LIMIT,
            'remaining': MONTHLY_LIMIT - current_usage
        }
    
    def get_recent_users_activity(self) -> List[Dict[str, Any]]:
        """קבלת רשימת משתמשים שהשתמשו השבוע"""
        # תאריך לפני שבוע
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # שאילתה לקבלת משתמשים שהיו פעילים השבוע
        cursor.execute("""
            SELECT DISTINCT u.user_id, u.username, 
                   DATE(wt.created_at) as activity_date,
                   COUNT(wt.id) as topics_added
            FROM users u
            LEFT JOIN watch_topics wt ON u.user_id = wt.user_id 
            WHERE DATE(wt.created_at) >= ?
            GROUP BY u.user_id, DATE(wt.created_at)
            ORDER BY wt.created_at DESC
        """, (week_ago,))
        
        activity_results = cursor.fetchall()
        
        # שאילתה נוספת לקבלת פעילות שימוש
        cursor.execute("""
            SELECT u.user_id, u.username, us.usage_count,
                   DATE(u.created_at) as join_date
            FROM users u
            LEFT JOIN usage_stats us ON u.user_id = us.user_id 
            WHERE DATE(u.created_at) >= ? OR us.month = ?
            ORDER BY u.created_at DESC
        """, (week_ago, datetime.now().strftime("%Y-%m")))
        
        usage_results = cursor.fetchall()
        
        conn.close()
        
        # עיבוד התוצאות
        users_activity = {}
        
        # עיבוד פעילות נושאים
        for user_id, username, activity_date, topics_count in activity_results:
            if user_id not in users_activity:
                users_activity[user_id] = {
                    'user_id': user_id,
                    'username': username or f"User_{user_id}",
                    'activity_dates': [],
                    'usage_count': 0,
                    'topics_added': 0
                }
            # המרת תאריך מ-YYYY-MM-DD ל-DD/MM/YYYY
            if activity_date:
                try:
                    date_obj = datetime.strptime(activity_date, "%Y-%m-%d")
                    formatted_date = date_obj.strftime("%d/%m/%Y")
                    users_activity[user_id]['activity_dates'].append(formatted_date)
                except:
                    users_activity[user_id]['activity_dates'].append(activity_date)
            users_activity[user_id]['topics_added'] += topics_count
        
        # עיבוד נתוני שימוש
        for user_id, username, usage_count, join_date in usage_results:
            if user_id not in users_activity:
                users_activity[user_id] = {
                    'user_id': user_id,
                    'username': username or f"User_{user_id}",
                    'activity_dates': [],
                    'usage_count': 0,
                    'topics_added': 0
                }
            if usage_count:
                users_activity[user_id]['usage_count'] = usage_count
            
            # הוספת תאריך הצטרפות אם השבוע
            if join_date >= week_ago:
                try:
                    date_obj = datetime.strptime(join_date, "%Y-%m-%d")
                    formatted_date = date_obj.strftime("%d/%m/%Y")
                    users_activity[user_id]['activity_dates'].append(formatted_date)
                except:
                    users_activity[user_id]['activity_dates'].append(join_date)
        
        return list(users_activity.values())
    
    def increment_usage(self, user_id: int) -> bool:
        """עדכון שימוש של משתמש - מחזיר True אם עדיין יש מקום"""
        current_month = datetime.now().strftime("%Y-%m")
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # בדיקת השימוש הנוכחי
        cursor.execute('''
            SELECT usage_count FROM usage_stats
            WHERE user_id = ? AND month = ?
        ''', (user_id, current_month))
        
        result = cursor.fetchone()
        current_usage = result[0] if result else 0
        
        if current_usage >= MONTHLY_LIMIT:
            conn.close()
            return False
        
        # עדכון השימוש
        cursor.execute('''
            INSERT OR REPLACE INTO usage_stats (user_id, month, usage_count)
            VALUES (?, ?, ?)
        ''', (user_id, current_month, current_usage + 1))
        
        conn.commit()
        conn.close()
        return True
    
    def check_user_usage(self, user_id: int) -> bool:
        """בדיקה אם המשתמש יכול לבצע חיפוש נוסף - גרסת SQLite"""
        return self.increment_usage(user_id)

def log_search(provider: str, topic_id: int, query: str):
    """Log search with trimmed query - reduced logging"""
    # Removed frequent search logging to reduce noise
    pass

def decrement_credits(user_id: int, used: int = 1) -> int:
    """Atomic-like function to decrement credits and return new value"""
    prev_usage = db.get_user_usage(user_id)
    prev = prev_usage['remaining']
    new_val = max(prev - used, 0)
    
    if USE_MONGODB:
        # MongoDB version
        try:
            current_month = datetime.now().strftime("%Y-%m")
            
            # Update usage count atomically
            result = db.usage_stats_collection.find_one_and_update(
                {"user_id": user_id, "month": current_month},
                {
                    "$inc": {"usage_count": used},
                    "$setOnInsert": {"created_at": datetime.now()}
                },
                upsert=True,
                return_document=True  # Return updated document
            )
            
            new_usage_count = result.get('usage_count', used)
            return max(MONTHLY_LIMIT - new_usage_count, 0)
            
        except Exception as e:
            logger.error(f"Error updating credits in MongoDB: {e}")
            return new_val
    else:
        # SQLite version (original code)
        current_month = datetime.now().strftime("%Y-%m")
        conn = sqlite3.connect(db.db_path)
        cursor = conn.cursor()
        
        # Get current usage
        cursor.execute('''
            SELECT usage_count FROM usage_stats
            WHERE user_id = ? AND month = ?
        ''', (user_id, current_month))
        
        result = cursor.fetchone()
        current_usage = result[0] if result else 0
        
        # Set new usage count
        new_usage_count = min(current_usage + used, MONTHLY_LIMIT)
        cursor.execute('''
            INSERT OR REPLACE INTO usage_stats (user_id, month, usage_count)
            VALUES (?, ?, ?)
        ''', (user_id, current_month, new_usage_count))
        
        conn.commit()
        conn.close()
        
        return max(MONTHLY_LIMIT - new_usage_count, 0)
    
    def get_stats(self) -> Dict[str, int]:
        """קבלת סטטיסטיקות כלליות - גרסת SQLite"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            current_month = datetime.now().strftime("%Y-%m")
            
            # ספירת משתמשים
            cursor.execute("SELECT COUNT(*) FROM users")
            total_users = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM users WHERE is_active = 1")
            active_users = cursor.fetchone()[0]
            
            # ספירת נושאים
            cursor.execute("SELECT COUNT(*) FROM watch_topics WHERE is_active = 1")
            active_topics = cursor.fetchone()[0]
            
            # ספירת תוצאות
            cursor.execute("SELECT COUNT(*) FROM found_results")
            total_results = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM found_results WHERE found_at > datetime('now', '-24 hours')")
            results_today = cursor.fetchone()[0]
            
            # סטטיסטיקות שימוש חודשיות
            cursor.execute("SELECT COUNT(*), SUM(usage_count) FROM usage_stats WHERE month = ?", (current_month,))
            usage_stats = cursor.fetchone()
            users_with_usage = usage_stats[0] if usage_stats[0] else 0
            total_usage_this_month = usage_stats[1] if usage_stats[1] else 0
            
            # משתמשים שהגיעו למגבלה
            cursor.execute("SELECT COUNT(*) FROM usage_stats WHERE month = ? AND usage_count >= ?", (current_month, MONTHLY_LIMIT))
            users_at_limit = cursor.fetchone()[0]
            
            return {
                "total_users": total_users,
                "active_users": active_users,
                "users_with_usage": users_with_usage,
                "users_at_limit": users_at_limit,
                "active_topics": active_topics,
                "total_results": total_results,
                "results_today": results_today,
                "total_usage_this_month": total_usage_this_month
            }
            
        finally:
            conn.close()

def run_topic_search(topic) -> List[Dict[str, str]]:
    """Main search function that uses Perplexity - Hebrew only output"""
    provider = "perplexity"
    used = 1
    
    log_search(provider, topic.id, topic.query)
    # Calling Perplexity API (reduced logging noise)
    
    try:
        perplexity_results = perform_search(topic.query)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[Perplexity] raw response: %s", perplexity_results)
        
        # הפונקציה perform_search כבר מחזירה את הפורמט הנכון עם סיכומים
        results = perplexity_results
        
        # Perplexity API success (reduced logging noise)
        return results
    except Exception as e:
        logger.error("Perplexity search failed for topic %s: %s", topic.id, e)
        raise
    finally:
        # Credits - decrement once per search
        try:
            prev = db.get_user_usage(topic.user_id)['remaining']
            new_val = decrement_credits(topic.user_id, used)
            # Removed frequent credit logging to reduce noise
        except Exception as cred_err:
            logger.error("[CREDITS] failed to decrement: %s", cred_err)

def normalize_perplexity(perplexity_results: list) -> List[Dict]:
    """Convert Perplexity results to expected format - Hebrew only"""
    formatted_results = []
    
    for result in perplexity_results:
        # Build Hebrew message ourselves
        title = result.get('title', 'ללא כותרת')
        url = result.get('link', '')
        
        # תרגום הכותרת לעברית
        hebrew_title = translate_title_to_hebrew(title)
        
        # Create Hebrew summary
        summary = f"מקור מידע זמין בקישור - {hebrew_title[:100]}{'...' if len(hebrew_title) > 100 else ''}"
        
        formatted_results.append({
            'title': hebrew_title,
            'url': url,
            'summary': summary,
            'relevance_score': 8,
            'date_found': datetime.now().strftime("%Y-%m-%d")
        })
    
    return formatted_results

def is_valid_result(r: dict) -> bool:
    """Check if result has required title and url fields"""
    return bool(r.get("title") and r.get("url"))

def validate_url(url: str, timeout: int = 5) -> bool:
    """
    בדיקת תקינות URL - בודק אם הקישור נגיש ולא מחזיר 404
    """
    if not url or not url.startswith(('http://', 'https://')):
        return False
    
    try:
        # בדיקת תווים לא חוקיים
        if any(char in url for char in [' ', '\n', '\r', '\t']):
            return False
            
        # בדיקה מהירה עם HEAD request
        response = requests.head(url, timeout=timeout, allow_redirects=True, 
                               headers={'User-Agent': 'Mozilla/5.0 (compatible; WatchBot/1.0)'})
        
        # אם HEAD לא עובד, נסה GET עם טווח מוגבל
        if response.status_code == 405:  # Method Not Allowed
            response = requests.get(url, timeout=timeout, allow_redirects=True,
                                  headers={'User-Agent': 'Mozilla/5.0 (compatible; WatchBot/1.0)',
                                          'Range': 'bytes=0-1023'})  # רק 1KB ראשון
        
        # קבל קישורים עם status codes תקינים
        return response.status_code in [200, 206, 301, 302, 303, 307, 308]
        
    except (requests.RequestException, requests.Timeout, Exception) as e:
        logger.debug(f"URL validation failed for {url}: {e}")
        return False

def is_relevant_result(result: dict, query: str) -> bool:
    """בדיקת רלוונטיות של תוצאת חיפוש לשאילתה המקורית - משופרת"""
    if not result or not query:
        return False
    
    title = result.get('title', '').lower()
    summary = result.get('summary', '').lower()
    url = result.get('url', '').lower()
    query_lower = query.lower()
    
    # בדיקת סוג התוכן לפי URL - סינון תוכן לא רלוונטי
    irrelevant_patterns = [
        'youtube.com/watch',  # סרטוני יוטיוב כשלא מבקשים מדריכי וידאו
        'tiktok.com',
        'instagram.com',
        'facebook.com/posts',
        'twitter.com',
        'reddit.com/r/',  # פוסטים ברדיט
        'pinterest.com',
        'linkedin.com/posts'
    ]
    
    # בדיקה אם השאילתה מבקשת ספציפית מדריכי וידאו
    video_request = any(word in query_lower for word in [
        'וידאו', 'סרטון', 'מדריך וידאו', 'video', 'tutorial video', 'youtube'
    ])
    
    # אם לא מבקשים וידאו אבל התוצאה היא וידאו - דחה
    if not video_request and any(pattern in url for pattern in irrelevant_patterns[:1]):  # רק יוטיוב
                    logger.debug(f"Filtered out video result when not requested: {title[:50]}")
        return False
    
    # בדיקת התאמה בין סוג השאילתה לסוג התוכן
    query_intent = analyze_query_intent(query)
    
    # אם מבקשים עדכוני תוכנה אבל מקבלים מדריך כללי - בדוק קפדנית יותר
    if query_intent['type'] in ['software_update', 'news_update', 'troubleshooting']:
        if any(word in title + summary for word in ['מדריך כללי', 'הדרכה בסיסית', 'למתחילים', 'tutorial', 'basic guide']):
            # בדוק אם יש גם מילים רלוונטיות לעדכונים/תיקונים
            relevant_words = ['עדכון', 'גרסה', 'תיקון', 'באג', 'פתרון', 'update', 'version', 'fix', 'bug', 'changelog', 'release notes']
            if not any(word in title + summary for word in relevant_words):
                logger.debug(f"Filtered out generic tutorial for update/troubleshooting query: {title[:50]}")
                return False
        
        # סינון נוסף לעדכוני תוכנה - דחיית מדריכי וידאו כשמחפשים עדכונים
        if query_intent['type'] == 'software_update':
            video_indicators = ['וידאו', 'סרטון', 'צפייה', 'video', 'watch', 'tutorial video']
            if any(indicator in title + summary for indicator in video_indicators):
                logger.debug(f"Filtered out video tutorial for software update query: {title[:50]}")
                return False
    
    # מילים שצריך להתעלם מהן בבדיקת רלוונטיות (מילות עצירה)
    stop_words = {
        # עברית
        'של', 'על', 'את', 'עם', 'אל', 'מן', 'כל', 'זה', 'זו', 'אם', 'או', 'גם', 'כי', 'לא', 'היא', 'הוא', 
        'אני', 'אתה', 'אתם', 'הם', 'הן', 'כך', 'כן', 'לכן', 'אך', 'אבל', 'רק', 'עוד', 'פה', 'שם', 
        'איך', 'מה', 'מי', 'איפה', 'מתי', 'למה', 'כמה', 'אחד', 'אחת', 'שני', 'שתי', 'יש', 'אין',
        'היה', 'הייתה', 'יהיה', 'תהיה', 'בין', 'תחת', 'מעל', 'ליד', 'אצל', 'נגד', 'בלי', 'חוץ',
        # אנגלית
        'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'from', 'is', 'are', 
        'was', 'were', 'be', 'been', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could', 
        'should', 'may', 'might', 'can', 'must', 'shall', 'this', 'that', 'these', 'those', 'a', 'an',
        'about', 'after', 'all', 'also', 'any', 'because', 'before', 'being', 'between', 'both', 'each',
        'few', 'first', 'get', 'here', 'how', 'into', 'its', 'just', 'like', 'make', 'most', 'new', 'no',
        'not', 'now', 'only', 'other', 'our', 'out', 'over', 'said', 'same', 'see', 'some', 'take', 'than',
        'them', 'through', 'time', 'two', 'up', 'use', 'very', 'way', 'well', 'what', 'where', 'which',
        'who', 'work', 'year', 'years', 'your'
    }
    
    # חילוץ מילות מפתח מהשאילתה
    query_keywords = []
    for word in query_lower.split():
        if len(word) > 2 and word not in stop_words:  # התעלם ממילים קצרות ומילות עצירה
            query_keywords.append(word)
    
    if not query_keywords:
        return True  # אם אין מילות מפתח, קבל את התוצאה
    
    # בדיקה אם לפחות מילת מפתח אחת מופיעה בכותרת או בסיכום
    content_to_check = f"{title} {summary}"
    matches = 0
    partial_matches = 0
    
    for keyword in query_keywords:
        if keyword in content_to_check:
            matches += 1
        elif len(keyword) > 4:  # בדיקת התאמה חלקית למילים ארוכות
            for word in content_to_check.split():
                if keyword in word or word in keyword:
                    partial_matches += 0.5
                    break
    
    total_score = matches + partial_matches
    
    # דרוש לפחות 30% התאמה למילות המפתח (הוגבר מ-25%)
    relevance_threshold = max(1, len(query_keywords) * 0.3)
    is_relevant = total_score >= relevance_threshold
    
    # לוגינג לדיבוג
    if not is_relevant:
        logger.debug(f"Relevance check failed: query='{query}', title='{title[:50]}', score={total_score}, threshold={relevance_threshold}")
    
    return is_relevant

def rank_results_by_relevance(results: List[Dict], query: str) -> List[Dict]:
    """דירוג תוצאות החיפוש לפי רלוונטיות"""
    if not results or not query:
        return results
    
    query_lower = query.lower()
    query_keywords = [word for word in query_lower.split() if len(word) > 2]
    
    def calculate_relevance_score(result: Dict) -> float:
        title = result.get('title', '').lower()
        summary = result.get('summary', '').lower()
        content = f"{title} {summary}"
        
        score = 0.0
        
        # ניקוד לפי מילות מפתח בכותרת (משקל גבוה יותר)
        for keyword in query_keywords:
            if keyword in title:
                score += 3.0
            elif keyword in summary:
                score += 1.5
        
        # ניקוד נוסף למילות מפתח חשובות מהשאילתה המקורית
        important_words = [word for word in query_keywords if len(word) > 4]  # מילים ארוכות יותר בדרך כלל חשובות יותר
        for important_word in important_words:
            if important_word in content:
                score += 2.0
        
        # ניקוד לאורך הסיכום (סיכומים ארוכים יותר בדרך כלל יותר מידעיים)
        summary_length = len(result.get('summary', ''))
        if summary_length > 100:
            score += 1.0
        elif summary_length > 50:
            score += 0.5
        
        return score
    
    # חישוב ניקוד לכל תוצאה
    for result in results:
        result['relevance_score'] = calculate_relevance_score(result)
    
    # מיון לפי ניקוד רלוונטיות (גבוה לנמוך)
    sorted_results = sorted(results, key=lambda x: x.get('relevance_score', 0), reverse=True)
    
    return sorted_results

def translate_title_to_hebrew(title: str) -> str:
    """תרגום כותרת מאנגלית לעברית - תרגום פשוט של מילות מפתח נפוצות"""
    if not title:
        return "מידע חדש"
    
    # אם הכותרת כבר מכילה עברית, החזר אותה כמו שהיא
    hebrew_chars = any(ord(char) >= 0x0590 and ord(char) <= 0x05FF for char in title)
    if hebrew_chars:
        return title
    
    # מילון תרגום למילות מפתח נפוצות
    translations = {
        # טכנולוגיה
        "technology": "טכנולוגיה",
        "tech": "טכנולוגיה", 
        "AI": "בינה מלאכותית",
        "artificial intelligence": "בינה מלאכותית",
        "smartphone": "סמארטפון",
        "tablet": "טאבלט",
        "laptop": "מחשב נייד",
        "computer": "מחשב",
        "software": "תוכנה",
        "hardware": "חומרה",
        "app": "אפליקציה",
        "application": "אפליקציה",
        "update": "עדכון",
        "release": "שחרור",
        "launch": "השקה",
        "announcement": "הכרזה",
        "review": "ביקורת",
        "news": "חדשות",
        "report": "דיווח",
        "analysis": "ניתוח",
        "feature": "תכונה",
        "features": "תכונות",
        "price": "מחיר",
        "cost": "עלות",
        "sale": "מבצע",
        "deal": "עסקה",
        "discount": "הנחה",
        "new": "חדש",
        "latest": "חדש ביותר",
        "upcoming": "עתיד",
        "future": "עתיד",
        "Samsung": "סמסונג",
        "Apple": "אפל",
        "Google": "גוגל",
        "Microsoft": "מיקרוסופט",
        "IDM": "IDM",
        "Internet Download Manager": "מנהל הורדות אינטרנט",
        "download manager": "מנהל הורדות",
        "download": "הורדה",
        "downloads": "הורדות",
        "bug": "באג",
        "bugs": "באגים",
        "fix": "תיקון",
        "fixes": "תיקונים",
        "error": "שגיאה",
        "errors": "שגיאות",
        "issue": "בעיה",
        "issues": "בעיות",
        "problem": "בעיה",
        "problems": "בעיות",
        "solution": "פתרון",
        "solutions": "פתרונות",
        "troubleshooting": "פתרון בעיות",
        "guide": "מדריך",
        "tutorial": "הדרכה",
        "help": "עזרה",
        "support": "תמיכה",
        "iPhone": "אייפון",
        "iPad": "אייפד",
        "Galaxy": "גלקסי",
        "Tab": "טאב",
        "Ultra": "אולטרה",
        "Pro": "פרו",
        "Max": "מקס",
        "Plus": "פלוס"
    }
    
    # תרגום מילות מפתח
    hebrew_title = title
    for eng, heb in translations.items():
        # תרגום מילים שלמות בלבד (לא חלקי מילים)
        import re
        pattern = r'\b' + re.escape(eng) + r'\b'
        hebrew_title = re.sub(pattern, heb, hebrew_title, flags=re.IGNORECASE)
    
    # אם הכותרת עדיין באנגלית לחלוטין, נוסיף תיאור עברי
    if hebrew_title == title and all(ord(char) < 128 for char in title if char.isalpha()):
        return f"מידע חדש: {title}"
    
    return hebrew_title

def make_hebrew_list(results: List[Dict[str, str]]) -> str:
    """Create Hebrew-only consolidated message from results with summaries"""
    lines = []
    for r in results:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        summary = (r.get("summary") or "").strip()
        
        if not url:
            continue
            
        # הכותרת כבר מתורגמת, פשוט נשתמש בה
        line = f"• {title}"
        if summary:
            line += f"\n📄 {summary}"
        line += f"\n🔗 {url}"
        lines.append(line)
    return "\n\n".join(lines)

async def send_results_hebrew_only(bot, chat_id: int, topic_text: str, results: List[Dict[str, str]]):
    """
    Send ONE compact Hebrew message with all results,
    without English snippets and without Telegram link previews.
    """
    if not results:
        return
        
    items = make_hebrew_list(results)
    msg = f"🔔 עדכון חדש עבור הנושא: {topic_text}\n\n📰 מצאתי מידע חדש ורלוונטי:\n\n{items}\n\n⏰ נבדק ברגע זה"
    
    try:
        await bot.send_message(chat_id, msg, **_LP_KW)
        logger.info("Sent Hebrew-only consolidated message to user %s", chat_id)
    except Exception as e:
        logger.error("Failed to send Hebrew message to user %s: %s", chat_id, e)

def analyze_query_intent(query: str) -> dict:
    """ניתוח כוונת השאילתה לשיפור החיפוש - משופר"""
    query_lower = query.lower()
    intent_info = {
        'type': 'general',
        'keywords': [],
        'search_modifiers': [],
        'software_name': None
    }
    
    # זיהוי שם תוכנה ספציפית
    software_patterns = [
        'idm', 'internet download manager', 'chrome', 'firefox', 'windows', 'office', 
        'photoshop', 'vlc', 'winrar', 'telegram', 'whatsapp', 'zoom', 'teams'
    ]
    
    for software in software_patterns:
        if software in query_lower:
            intent_info['software_name'] = software
            break
    
    # זיהוי סוגי שאילתות שונים - בסדר עדיפות
    
    # עדכוני תוכנה - עדיפות גבוהה
    if any(word in query_lower for word in ['עדכון', 'עדכונים', 'גרסה', 'update', 'updates', 'version', 'latest version']):
        if intent_info['software_name'] or any(word in query_lower for word in ['תוכנה', 'תוכנת', 'software', 'program', 'app']):
            intent_info['type'] = 'software_update'
            intent_info['search_modifiers'].extend(['עדכון תוכנה', 'גרסה חדשה', 'changelog'])
        else:
            intent_info['type'] = 'news_update'
            intent_info['search_modifiers'].extend(['עדכונים', 'חדשות'])
    
    # פתרון בעיות - עדיפות גבוהה
    elif any(word in query_lower for word in ['בעיה', 'באג', 'שגיאה', 'תקלה', 'לא עובד', 'תיקון', 'פתרון', 'problem', 'error', 'bug', 'issue', 'fix', 'solve']):
        intent_info['type'] = 'troubleshooting'
        intent_info['search_modifiers'].extend(['פתרון', 'תיקון', 'פתרון בעיות'])
        if intent_info['software_name']:
            intent_info['search_modifiers'].append(f'פתרון בעיות {intent_info["software_name"]}')
    
    # מדריכים והדרכות
    elif any(word in query_lower for word in ['איך', 'כיצד', 'מה הדרך', 'how to', 'how can', 'tutorial', 'guide']):
        intent_info['type'] = 'how_to'
        intent_info['search_modifiers'].extend(['מדריך', 'הדרכה', 'הוראות'])
    
    # הגדרות והסברים
    elif any(word in query_lower for word in ['מה זה', 'מה הם', 'what is', 'what are', 'הגדרה', 'definition']):
        intent_info['type'] = 'definition'
        intent_info['search_modifiers'].extend(['הגדרה', 'הסבר', 'מה זה'])
    
    # ביקורות ודעות
    elif any(word in query_lower for word in ['ביקורת', 'דעה', 'חוות דעת', 'review', 'opinion', 'rating']):
        intent_info['type'] = 'review'
        intent_info['search_modifiers'].extend(['ביקורת', 'דעות', 'חוות דעת'])
    
    # חדשות כלליות
    elif any(word in query_lower for word in ['חדש', 'אחרון', 'חדשות', 'new', 'latest', 'news']):
        intent_info['type'] = 'news_update'
        intent_info['search_modifiers'].extend(['חדשות', 'מידע עדכני'])
    
    return intent_info

def perform_search(query: str) -> list[dict]:
    """
    Performs a search using the Perplexity API with the 'sonar-pro' model.
    Enhanced with query intent analysis for better results.
    """
    if not API_KEY:
        logger.error("PERPLEXITY_API_KEY environment variable is not set or empty.")
        return []
    
    # ניתוח כוונת השאילתה
    intent_info = analyze_query_intent(query)
    logger.info(f"Query intent analysis: type='{intent_info['type']}', modifiers={intent_info['search_modifiers']}")

    # התאמת ההנחיות לפי סוג השאילתה
    system_content = (
        "אתה עוזר חיפוש מומחה מתמחה במציאת תוכן רלוונטי ומדויק. "
        "עליך להחזיר רק מערך JSON של 5-7 תוצאות חיפוש המתמקדות ישירות בנושא הספציפי שהמשתמש ביקש. "
        "כל תוצאה חייבת להיות אובייקט JSON עם השדות הבאים בדיוק: 'title', 'url', 'summary'. "
    )
    
    # הוספת הנחיות ספציפיות לפי סוג השאילתה
    if intent_info['type'] == 'how_to':
        system_content += "התמקד במדריכים, הדרכות ומקורות שמסבירים איך לעשות את הדבר המבוקש. "
    elif intent_info['type'] == 'definition':
        system_content += "התמקד בהגדרות, הסברים ומקורות שמסבירים מה זה הדבר המבוקש. "
    elif intent_info['type'] == 'troubleshooting':
        system_content += "התמקד בפתרונות, תיקונים ומקורות שעוזרים לפתור בעיות. "
    elif intent_info['type'] == 'software_update':
        system_content += "התמקד בעדכוני תוכנה, גרסאות חדשות, changelog, ותיקוני באגים. אל תכלול מדריכים כלליים או סרטוני הדרכה. "
    elif intent_info['type'] == 'news_update':
        system_content += "התמקד בחדשות, עדכונים ומידע עדכני על הנושא. "
    elif intent_info['type'] == 'review':
        system_content += "התמקד בביקורות, דעות וחוות דעת על הנושא. "
    
    system_content += (
        "חשוב מאוד: "
        "1. התמקד רק בתוכן הרלוונטי ישירות לנושא הספציפי - אל תכלול תוכן כללי או לא קשור "
        "2. ה-'title' חייב להיות בעברית (תרגם את הכותרת המקורית לעברית) "
        "3. ה-'summary' חייב להיות תיאור קצר של 1-2 משפטים בעברית המסביר בדיוק מה מכיל הקישור ואיך זה קשור לנושא "
        "4. כל הקישורים חייבים להיות קישורים מלאים ותקינים שמתחילים ב-'https://' ועובדים "
        "5. אל תכלול מקורות שהם רק בערך קשורים - רק מקורות שעוסקים ישירות בנושא "
        "החזר רק את מערך ה-JSON, ללא טקסט נוסף."
    )
    
    # בניית שאילתת החיפוש המשופרת
    enhanced_query = query
    if intent_info['search_modifiers']:
        enhanced_query += " " + " ".join(intent_info['search_modifiers'])
    
    messages = [
        {
            "role": "system",
            "content": system_content,
        },
        {
            "role": "user",
            "content": f"חפש מידע רלוונטי ומדויק על הנושא הספציפי הזה: {enhanced_query}. חשוב מאוד: התמקד רק במקורות שעוסקים ישירות ובאופן ספציפי בנושא המבוקש. אל תכלול מקורות כלליים, מקורות שעוסקים בנושאים דומים או קשורים, או מקורות שרק מזכירים את הנושא בהקשר אחר. כל מקור חייב להיות ממוקד ורלוונטי במישרין לשאילתה.",
        },
    ]

    try:
        response = client.chat.completions.create(
            model="sonar-pro",
            messages=messages,
        )
        content = response.choices[0].message.content
        
        # ניסיון לפרסר JSON
        try:
            results_json = json.loads(content)
            results = []
            
            for item in results_json:
                if isinstance(item, dict) and 'title' in item and 'url' in item:
                    # בדיקת תקינות הקישור - בסיסית ומתקדמת
                    url = item.get('url', '').strip()
                    if not url.startswith(('http://', 'https://')):
                        continue
                        
                    # בדיקה שהקישור לא מכיל תווים לא תקינים
                    if any(char in url for char in [' ', '\n', '\r', '\t']):
                        continue
                    
                    # בדיקת נגישות הקישור (עם timeout קצר)
                    if not validate_url(url, timeout=3):
                        logger.debug(f"Skipping inaccessible URL: {url}")
                        continue
                    
                    title = item.get('title', 'ללא כותרת').strip()
                    summary = item.get('summary', '').strip()
                    
                    # וידוא שהכותרת בעברית - אם לא, נתרגם אותה
                    hebrew_title = translate_title_to_hebrew(title)
                    
                    # יצירת תוצאה זמנית לבדיקת רלוונטיות
                    temp_result = {
                        'title': hebrew_title,
                        'url': url,
                        'summary': summary if summary else f"מקור מידע זמין - {hebrew_title[:50]}{'...' if len(hebrew_title) > 50 else ''}"
                    }
                    
                    # בדיקת רלוונטיות לפני הוספה
                    if is_relevant_result(temp_result, query):
                        results.append(temp_result)
                    else:
                        logger.info(f"Filtered out irrelevant result: {hebrew_title[:50]}")
                        continue
            
            # אם אין מספיק תוצאות רלוונטיות, נסה חיפוש נוסף עם שאילתה מעודנת
            if len(results) < 3:
                logger.info(f"Only {len(results)} relevant results found, trying refined search...")
                try:
                    refined_query = f'"{query}" עדכונים חדשים מידע אחרון'
                    refined_messages = [
                        {
                            "role": "system",
                            "content": (
                                "אתה עוזר חיפוש מומחה. עליך למצוא מידע ספציפי ועדכני על הנושא המבוקש. "
                                "החזר רק מערך JSON של 3-5 תוצאות נוספות עם השדות: 'title', 'url', 'summary'. "
                                "התמקד במקורות אמינים ועדכניים הקשורים ישירות לנושא."
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"מצא מידע נוסף על: {refined_query}",
                        },
                    ]
                    
                    refined_response = client.chat.completions.create(
                        model="sonar-pro",
                        messages=refined_messages,
                    )
                    refined_content = refined_response.choices[0].message.content
                    refined_results_json = json.loads(refined_content)
                    
                    for item in refined_results_json:
                        if isinstance(item, dict) and 'title' in item and 'url' in item and len(results) < 7:
                            url = item.get('url', '').strip()
                            if not url.startswith(('http://', 'https://')):
                                continue
                            if any(char in url for char in [' ', '\n', '\r', '\t']):
                                continue
                            
                            # בדיקת נגישות הקישור גם בחיפוש המעודן
                            if not validate_url(url, timeout=3):
                                logger.debug(f"Skipping inaccessible refined URL: {url}")
                                continue
                            
                            title = item.get('title', 'ללא כותרת').strip()
                            summary = item.get('summary', '').strip()
                            hebrew_title = translate_title_to_hebrew(title)
                            
                            temp_result = {
                                'title': hebrew_title,
                                'url': url,
                                'summary': summary if summary else f"מקור מידע נוסף - {hebrew_title[:50]}{'...' if len(hebrew_title) > 50 else ''}"
                            }
                            
                            if is_relevant_result(temp_result, query):
                                results.append(temp_result)
                                
                except Exception as refined_e:
                    logger.warning(f"Refined search failed: {refined_e}")
             
            # דירוג התוצאות לפי רלוונטיות
            ranked_results = rank_results_by_relevance(results, query)
            
            logger.info(f"Search completed: {len(ranked_results)} relevant results found for query: '{query[:50]}{'...' if len(query) > 50 else ''}')")
            return ranked_results
            
        except json.JSONDecodeError:
            # אם JSON לא תקין, ננסה לפרסר כמרקדאון (fallback)
            logger.warning("Failed to parse JSON response, trying markdown fallback")
            results = []
            matches = re.findall(r'\[(.*?)\]\((https?://[^\s\)]+)\)', content)
            
            for match in matches:
                title, link = match
                # בדיקת תקינות הקישור
                link = link.strip()
                if any(char in link for char in [' ', '\n', '\r', '\t']):
                    continue
                
                # בדיקת נגישות הקישור גם בfallback
                if not validate_url(link, timeout=3):
                    logger.debug(f"Skipping inaccessible fallback URL: {link}")
                    continue
                    
                title_clean = title.strip()
                hebrew_title = translate_title_to_hebrew(title_clean)
                
                # יצירת תוצאה זמנית לבדיקת רלוונטיות
                temp_result = {
                    'title': hebrew_title,
                    'url': link,
                    'summary': f"מקור מידע זמין - {hebrew_title[:50]}{'...' if len(hebrew_title) > 50 else ''}"
                }
                
                # בדיקת רלוונטיות לפני הוספה
                if is_relevant_result(temp_result, query):
                    results.append(temp_result)
                else:
                    logger.info(f"Filtered out irrelevant fallback result: {hebrew_title[:50]}")
            
            # דירוג התוצאות לפי רלוונטיות גם בfallback
            ranked_results = rank_results_by_relevance(results, query)
            
            logger.info(f"Fallback search completed: {len(ranked_results)} relevant results found for query: '{query[:50]}{'...' if len(query) > 50 else ''}')")
            return ranked_results

    except Exception as e:
        logger.error(f"An error occurred while calling the Perplexity API: {e}")
        return []


class WatchBotMongoDB:
    """מחלקה לניהול בסיס נתונים MongoDB"""
    
    def __init__(self, uri: str, db_name: str):
        self.client = MongoClient(uri)
        self.db = self.client[db_name]
        self.users_collection = self.db.users
        self.watch_topics_collection = self.db.watch_topics
        self.usage_stats_collection = self.db.usage_stats
        self.found_results_collection = self.db.found_results
        
        # יצירת אינדקסים
        self._create_indexes()
        
        # מיגרציה של נתוני שימוש מ-SQLite אם קיימים
        self._migrate_usage_data_if_needed()
    
    def _create_indexes(self):
        """יצירת אינדקסים לביצועים טובים יותר"""
        try:
            # אינדקס על user_id
            self.users_collection.create_index("user_id", unique=True)
            self.watch_topics_collection.create_index("user_id")
            self.usage_stats_collection.create_index([("user_id", 1), ("month", 1)], unique=True)
            
            # אינדקס על תאריכים
            self.watch_topics_collection.create_index("created_at")
            self.users_collection.create_index("created_at")
            
            logger.info("MongoDB indexes created successfully")
        except Exception as e:
            logger.error(f"Error creating MongoDB indexes: {e}")
    
    def _migrate_usage_data_if_needed(self):
        """מיגרציה של נתוני שימוש מ-SQLite ל-MongoDB אם נדרש"""
        try:
            # בדיקה אם יש נתוני שימוש ב-MongoDB כבר
            existing_count = self.usage_stats_collection.count_documents({})
            if existing_count > 0:
                logger.info(f"MongoDB already has {existing_count} usage records, skipping migration")
                return
            
            # ניסיון לקרוא מ-SQLite
            sqlite_db_path = DB_PATH
            if not os.path.exists(sqlite_db_path):
                logger.info("No SQLite database found, no migration needed")
                return
            
            conn = sqlite3.connect(sqlite_db_path)
            cursor = conn.cursor()
            
            # בדיקה אם יש טבלת usage_stats
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='usage_stats'")
            if not cursor.fetchone():
                logger.info("No usage_stats table in SQLite, no migration needed")
                conn.close()
                return
            
            # קריאת כל נתוני השימוש מ-SQLite
            cursor.execute("SELECT user_id, month, usage_count FROM usage_stats WHERE usage_count > 0")
            sqlite_data = cursor.fetchall()
            conn.close()
            
            if not sqlite_data:
                logger.info("No usage data in SQLite to migrate")
                return
            
            # העברת הנתונים ל-MongoDB
            documents_to_insert = []
            for user_id, month, usage_count in sqlite_data:
                documents_to_insert.append({
                    "user_id": user_id,
                    "month": month,
                    "usage_count": usage_count,
                    "created_at": datetime.now(),
                    "migrated_from_sqlite": True
                })
            
            if documents_to_insert:
                self.usage_stats_collection.insert_many(documents_to_insert)
                logger.info(f"Successfully migrated {len(documents_to_insert)} usage records from SQLite to MongoDB")
            
        except Exception as e:
            logger.error(f"Error during usage data migration: {e}")
            # לא נעצור את התהליך בגלל שגיאה במיגרציה
    
    def get_recent_users_activity(self) -> List[Dict[str, Any]]:
        """קבלת רשימת משתמשים שהשתמשו השבוע - גרסת MongoDB"""
        try:
            # תאריך לפני שבוע
            week_ago = datetime.now() - timedelta(days=7)
            current_month = datetime.now().strftime("%Y-%m")
            
            # Pipeline לאיחוד נתונים מכמה קולקשנים
            pipeline = [
                {
                    "$lookup": {
                        "from": "watch_topics",
                        "localField": "user_id",
                        "foreignField": "user_id",
                        "as": "topics"
                    }
                },
                {
                    "$lookup": {
                        "from": "usage_stats",
                        "let": {"user_id": "$user_id"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$eq": ["$user_id", "$$user_id"]},
                                            {"$eq": ["$month", current_month]}
                                        ]
                                    }
                                }
                            }
                        ],
                        "as": "usage"
                    }
                },
                {
                    "$addFields": {
                        "recent_topics": {
                            "$filter": {
                                "input": "$topics",
                                "cond": {"$gte": ["$$this.created_at", week_ago]}
                            }
                        },
                        "usage_count": {
                            "$ifNull": [{"$arrayElemAt": ["$usage.usage_count", 0]}, 0]
                        }
                    }
                },
                {
                    "$match": {
                        "$or": [
                            {"recent_topics": {"$ne": []}},  # יש נושאים מהשבוע
                            {"created_at": {"$gte": week_ago}},  # נרשם השבוע
                            {"usage_count": {"$gt": 0}}  # השתמש החודש
                        ]
                    }
                },
                {
                    "$project": {
                        "user_id": 1,
                        "username": 1,
                        "created_at": 1,
                        "topics_added": {"$size": "$recent_topics"},
                        "usage_count": 1,
                        "activity_dates": {
                            "$map": {
                                "input": "$recent_topics",
                                "as": "topic",
                                "in": {
                                    "$dateToString": {
                                        "format": "%d/%m/%Y",
                                        "date": "$$topic.created_at"
                                    }
                                }
                            }
                        }
                    }
                },
                {
                    "$addFields": {
                        "activity_dates": {
                            "$cond": {
                                "if": {"$gte": ["$created_at", week_ago]},
                                "then": {
                                    "$setUnion": [
                                        "$activity_dates",
                                        [{
                                            "$dateToString": {
                                                "format": "%d/%m/%Y",
                                                "date": "$created_at"
                                            }
                                        }]
                                    ]
                                },
                                "else": "$activity_dates"
                            }
                        }
                    }
                },
                {
                    "$sort": {"created_at": -1}
                }
            ]
            
            results = list(self.users_collection.aggregate(pipeline))
            
            # עיבוד התוצאות לפורמט הנדרש
            users_activity = []
            for user in results:
                users_activity.append({
                    'user_id': user['user_id'],
                    'username': user.get('username', f"User_{user['user_id']}"),
                    'topics_added': user.get('topics_added', 0),
                    'usage_count': user.get('usage_count', 0),
                    'activity_dates': list(set(user.get('activity_dates', [])))  # הסרת כפילויות
                })
            
            logger.info(f"Found {len(users_activity)} recent users")
            return users_activity
            
        except Exception as e:
            logger.error(f"Error getting recent users activity from MongoDB: {e}")
            return []
    
    def add_user(self, user_id: int, username: str = None):
        """הוספת משתמש חדש"""
        try:
            user_doc = {
                "user_id": user_id,
                "username": username,
                "is_active": True,
                "created_at": datetime.now()
            }
            
            self.users_collection.update_one(
                {"user_id": user_id},
                {"$setOnInsert": user_doc},
                upsert=True
            )
            logger.info(f"User {user_id} added/updated in MongoDB")
            
        except Exception as e:
            logger.error(f"Error adding user to MongoDB: {e}")
    
    def add_watch_topic(self, user_id: int, topic: str, check_interval: int = 24, checks_remaining: int = None) -> int:
        """הוספת נושא למעקב"""
        try:
            topic_doc = {
                "user_id": user_id,
                "topic": topic,
                "check_interval": check_interval,
                "is_active": True,
                "created_at": datetime.now(),
                "last_checked": None,
                "checks_remaining": checks_remaining
            }
            
            result = self.watch_topics_collection.insert_one(topic_doc)
            logger.info(f"Topic added to MongoDB with ID: {result.inserted_id}")
            return str(result.inserted_id)
            
        except Exception as e:
            logger.error(f"Error adding watch topic to MongoDB: {e}")
            return None
    
    def get_user_usage(self, user_id: int) -> Dict[str, int]:
        """קבלת נתוני שימוש של משתמש - גרסת MongoDB"""
        try:
            current_month = datetime.now().strftime("%Y-%m")
            
            usage_doc = self.usage_stats_collection.find_one({
                "user_id": user_id,
                "month": current_month
            })
            
            current_usage = usage_doc['usage_count'] if usage_doc else 0
            
            return {
                'current_usage': current_usage,
                'monthly_limit': MONTHLY_LIMIT,
                'remaining': MONTHLY_LIMIT - current_usage
            }
            
        except Exception as e:
            logger.error(f"Error getting user usage from MongoDB: {e}")
            # Fallback to default values
            return {
                'current_usage': 0,
                'monthly_limit': MONTHLY_LIMIT,
                'remaining': MONTHLY_LIMIT
            }
    
    def check_user_usage(self, user_id: int) -> bool:
        """בדיקה אם המשתמש יכול לבצע חיפוש נוסף - גרסת MongoDB"""
        try:
            current_month = datetime.now().strftime("%Y-%m")
            
            usage_doc = self.usage_stats_collection.find_one({
                "user_id": user_id,
                "month": current_month
            })
            
            current_usage = usage_doc['usage_count'] if usage_doc else 0
            
            if current_usage >= MONTHLY_LIMIT:
                return False
            
            # עדכון השימוש
            self.usage_stats_collection.update_one(
                {"user_id": user_id, "month": current_month},
                {
                    "$inc": {"usage_count": 1},
                    "$setOnInsert": {"created_at": datetime.now()}
                },
                upsert=True
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Error checking user usage in MongoDB: {e}")
            return False


class SmartWatcher:
    """מחלקה לניהול המעקב החכם עם Perplexity API"""
    
    def __init__(self, db: WatchBotDB):
        self.db = db
    
    def search_and_analyze_topic(self, topic: str, user_id: int = None) -> List[Dict[str, str]]:
        """חיפוש ואנליזה של נושא עם Perplexity API בלבד"""
        # בדיקת מגבלת שימוש אם סופק user_id
        if user_id:
            usage_info = self.db.get_user_usage(user_id)
            if usage_info['remaining'] <= 0:
                return []  # חריגה ממגבלת השימוש

        # Create a simple topic object for compatibility
        class TopicObj:
            def __init__(self, query, user_id, topic_id):
                self.query = query
                self.user_id = user_id
                self.id = topic_id or user_id  # Use user_id as fallback for topic_id
        
        topic_obj = TopicObj(topic, user_id, user_id)
        return run_topic_search(topic_obj)
    
    def update_topic_text(self, user_id: int, topic_id: str, new_text: str) -> bool:
        """עדכון טקסט הנושא"""
        try:
            result = self.watch_topics_collection.update_one(
                {"user_id": user_id, "_id": ObjectId(topic_id), "is_active": True},
                {"$set": {"topic": new_text}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating topic text: {e}")
            return False
    
    def update_topic_frequency(self, user_id: int, topic_id: str, new_frequency: int) -> bool:
        """עדכון תדירות בדיקת הנושא"""
        try:
            result = self.watch_topics_collection.update_one(
                {"user_id": user_id, "_id": ObjectId(topic_id), "is_active": True},
                {"$set": {"check_interval": new_frequency}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating topic frequency: {e}")
            return False
    
    def get_stats(self) -> Dict[str, int]:
        """קבלת סטטיסטיקות כלליות - גרסת MongoDB"""
        try:
            current_month = datetime.now().strftime("%Y-%m")
            
            # ספירת משתמשים
            total_users = self.users_collection.count_documents({})
            active_users = self.users_collection.count_documents({"is_active": True})
            
            # ספירת נושאים
            active_topics = self.watch_topics_collection.count_documents({"is_active": True})
            
            # ספירת תוצאות
            total_results = self.found_results_collection.count_documents({})
            
            # תוצאות מהיום האחרון
            yesterday = datetime.now() - timedelta(hours=24)
            results_today = self.found_results_collection.count_documents({
                "found_at": {"$gte": yesterday}
            })
            
            # סטטיסטיקות שימוש חודשיות
            usage_pipeline = [
                {"$match": {"month": current_month}},
                {"$group": {
                    "_id": None,
                    "users_with_usage": {"$sum": 1},
                    "total_usage_this_month": {"$sum": "$usage_count"},
                    "users_at_limit": {
                        "$sum": {"$cond": [{"$gte": ["$usage_count", MONTHLY_LIMIT]}, 1, 0]}
                    }
                }}
            ]
            
            usage_stats = list(self.usage_stats_collection.aggregate(usage_pipeline))
            if usage_stats:
                users_with_usage = usage_stats[0].get("users_with_usage", 0)
                total_usage_this_month = usage_stats[0].get("total_usage_this_month", 0)
                users_at_limit = usage_stats[0].get("users_at_limit", 0)
            else:
                users_with_usage = total_usage_this_month = users_at_limit = 0
            
            return {
                "total_users": total_users,
                "active_users": active_users,
                "users_with_usage": users_with_usage,
                "users_at_limit": users_at_limit,
                "active_topics": active_topics,
                "total_results": total_results,
                "results_today": results_today,
                "total_usage_this_month": total_usage_this_month
            }
            
        except Exception as e:
            logger.error(f"Error getting stats from MongoDB: {e}")
            return {
                "total_users": 0,
                "active_users": 0,
                "users_with_usage": 0,
                "users_at_limit": 0,
                "active_topics": 0,
                "total_results": 0,
                "results_today": 0,
                "total_usage_this_month": 0
            }

# יצירת אובייקטי המערכת
if USE_MONGODB:
    logger.info("Using MongoDB database")
    db = WatchBotMongoDB(MONGODB_URI, MONGODB_DB_NAME)
else:
    logger.info("Using SQLite database")
    db = WatchBotDB(DB_PATH)

smart_watcher = SmartWatcher(db)

# שרת Flask ל-Keep-Alive
app = Flask(__name__)

@app.route('/')
def health_check():
    return jsonify({"status": "Bot is running", "timestamp": datetime.now().isoformat()})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

def run_flask():
    """הרצת שרת Flask ברקע"""
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

def get_main_menu_keyboard(user_id=None):
    """יצירת תפריט הכפתורים הראשי"""
    keyboard = []
    

    
    keyboard.extend([
        [InlineKeyboardButton("📌 הוסף נושא חדש", callback_data="add_topic")],
        [InlineKeyboardButton("📋 הצג רשימת נושאים", callback_data="list_topics")],
        [InlineKeyboardButton("⏸️ השבת מעקב", callback_data="pause_tracking"),
         InlineKeyboardButton("▶️ הפעל מחדש", callback_data="resume_tracking")],
        [InlineKeyboardButton("📊 שימוש נוכחי", callback_data="usage_stats"),
         InlineKeyboardButton("❓ עזרה", callback_data="help")]
    ])
    
    # הוספת כפתור אדמין אם המשתמש הוא אדמין
    logger.info(f"Checking admin access: user_id={user_id}, ADMIN_ID={ADMIN_ID}, match={user_id == ADMIN_ID}")
    if user_id == ADMIN_ID:
        logger.info("Adding admin button for recent users")
        keyboard.append([InlineKeyboardButton("👥 משתמשים אחרונים", callback_data="recent_users")])
    else:
        # לוג זמני לדיבוג - הסר אחרי שהבעיה תיפתר
        logger.info(f"User {user_id} is not admin (ADMIN_ID={ADMIN_ID})")
    
    return InlineKeyboardMarkup(keyboard)

def get_quick_commands_keyboard(user_id=None):
    """יצירת תפריט פקודות מהירות (רק לאדמין)"""
    keyboard = [
        [InlineKeyboardButton("👤 /whoami - מידע עליי", callback_data="run_whoami")],
        [InlineKeyboardButton("👥 /recent_users - משתמשים אחרונים", callback_data="run_recent_users")],
        [InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]
    ]
    
    return InlineKeyboardMarkup(keyboard)

def get_frequency_keyboard():
    """יצירת תפריט בחירת תדירות"""
    keyboard = [
        [InlineKeyboardButton("כל 5 דקות (5 פעמים בלבד)", callback_data="freq_5min")],
        [InlineKeyboardButton("כל 6 שעות", callback_data="freq_6")],
        [InlineKeyboardButton("כל 12 שעות", callback_data="freq_12")],
        [InlineKeyboardButton("כל 24 שעות (ברירת מחדל)", callback_data="freq_24")],
        [InlineKeyboardButton("כל 48 שעות", callback_data="freq_48")],
        [InlineKeyboardButton("אחת ל-7 ימים", callback_data="freq_168")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def show_frequency_selection(query, user_id, topic_id, is_edit=False):
    """הצגת תפריט בחירת תדירות לעריכה"""
    # קבלת פרטי הנושא
    topics = db.get_user_topics(user_id)
    topic = next((t for t in topics if str(t['id']) == str(topic_id)), None)
    
    if not topic:
        await query.edit_message_text(
            "❌ הנושא לא נמצא.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
        )
        return
    
    current_freq = {
        6: "כל 6 שעות",
        12: "כל 12 שעות", 
        24: "כל 24 שעות",
        48: "כל 48 שעות",
        168: "אחת לשבוע"
    }.get(topic['check_interval'], f"כל {topic['check_interval']} שעות")
    
    message = f"""⏰ עריכת תדירות עדכון

📝 נושא: {topic['topic']}
🕐 תדירות נוכחית: {current_freq}

בחרו תדירות חדשה:"""
    
    keyboard = [
        [InlineKeyboardButton("כל 6 שעות", callback_data=f"update_freq_{topic_id}_6")],
        [InlineKeyboardButton("כל 12 שעות", callback_data=f"update_freq_{topic_id}_12")],
        [InlineKeyboardButton("כל 24 שעות (ברירת מחדל)", callback_data=f"update_freq_{topic_id}_24")],
        [InlineKeyboardButton("כל 48 שעות", callback_data=f"update_freq_{topic_id}_48")],
        [InlineKeyboardButton("אחת ל-7 ימים", callback_data=f"update_freq_{topic_id}_168")],
        [InlineKeyboardButton("🔙 חזרה לעריכה", callback_data=f"edit_topic_{topic_id}")],
        [InlineKeyboardButton("🏠 תפריט ראשי", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

# פקודות הבוט
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת התחלה"""
    user = update.effective_user
    db.add_user(user.id, user.username)
    
    # קבלת נתוני שימוש
    usage_info = db.get_user_usage(user.id)
    
    welcome_message = f"""
🤖 ברוכים הבאים לבוט המעקב החכם!

אני עוזר לכם לעקוב אחרי נושאים שמעניינים אתכם ומתריע כשיש מידע חדש.

🧠 אני משתמש ב-Perplexity בינה מלאכותית עם יכולות גלישה באינטרנט לחיפוש מידע עדכני ורלוונטי.

📊 **מגבלת השימוש החודשית:**
🔍 השתמשת ב-{usage_info['current_usage']} מתוך {usage_info['monthly_limit']} בדיקות
⏳ נותרו לך {usage_info['remaining']} בדיקות החודש

📞 לכל תקלה או ביקורת ניתן לפנות ל-@moominAmir בטלגרם

בחרו פעולה מהתפריט למטה:
"""
    
    await update.message.reply_text(welcome_message, reply_markup=get_main_menu_keyboard(user.id))

async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת הוספת נושא למעקב"""
    if not context.args:
        await update.message.reply_text("❌ אנא ציינו נושא למעקב.\nדוגמה: /watch גלקסי טאב S11 אולטרה")
        return
    
    topic = ' '.join(context.args)
    user = update.effective_user
    user_id = user.id
    
    # הוספת המשתמש למאגר הנתונים (אם לא קיים)
    db.add_user(user_id, user.username)
    
    # הוספה למסד הנתונים
    topic_id = db.add_watch_topic(user_id, topic)
    
    # תזמון בדיקה חד-פעמית דקה לאחר הוספת הנושא
    context.application.job_queue.run_once(
        check_single_topic_job,
        when=timedelta(minutes=1),
        data={'topic_id': topic_id, 'user_id': user_id},
        name=f"one_time_check_{topic_id}"
    )
    
    await update.message.reply_text(
        f"✅ הנושא נוסף בהצלחה!\n"
        f"📝 נושא: {topic}\n"
        f"🆔 מזהה: {topic_id}\n"
        f"🔍 בדיקה חד-פעמית תתבצע בעוד דקה\n"
        f"🧠 אני אשתמש ב-Perplexity בינה מלאכותית עם גלישה לחיפוש מידע עדכני\n\n"
        f"אבדוק אותו כל 24 שעות ואתריע על תוכן חדש."
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת רשימת נושאים"""
    user = update.effective_user
    user_id = user.id
    
    # הוספת המשתמש למאגר הנתונים (אם לא קיים)
    db.add_user(user_id, user.username)
    
    topics = db.get_user_topics(user_id)
    
    if not topics:
        message = "📭 אין לכם נושאים במעקב כרגע.\nהשתמשו בכפתור 'הוסף נושא חדש' כדי להתחיל."
        await update.message.reply_text(message, reply_markup=get_main_menu_keyboard(user_id))
        return
    
    message = "📋 הנושאים שלכם במעקב:\n\n"
    keyboard = []
    
    for i, topic in enumerate(topics, 1):
        status = "🟢"  # כל הנושאים פעילים (אחרת הם לא מוצגים)
        last_check = topic['last_checked'] or "מעולם לא"
        if last_check != "מעולם לא":
            # קיצור התאריך להצגה נוחה יותר
            try:
                last_check = datetime.fromisoformat(last_check).strftime("%d/%m %H:%M")
            except:
                pass
        
        # הוספת מידע על תדירות הבדיקה
        if topic['check_interval'] == 0.0833:
            freq_text = f"כל 5 דקות ({topic.get('checks_remaining', 0)} נותרו)" if topic.get('checks_remaining') else "כל 5 דקות (הושלם)"
        else:
            freq_text = {
                6: "כל 6 שעות",
                12: "כל 12 שעות", 
                24: "כל 24 שעות",
                48: "כל 48 שעות",
                168: "אחת לשבוע"
            }.get(topic['check_interval'], f"כל {topic['check_interval']} שעות")
        
        message += f"{i}. {status} {topic['topic']}\n"
        message += f"   🆔 {topic['id']} | ⏰ {freq_text}\n"
        message += f"   🕐 נבדק: {last_check}\n\n"
        
        # הוספת כפתורי עריכה ומחיקה לכל נושא
        topic_name_short = topic['topic'][:15] + ('...' if len(topic['topic']) > 15 else '')
        keyboard.append([
            InlineKeyboardButton(f"✏️ ערוך '{topic_name_short}'", callback_data=f"edit_topic_{topic['id']}"),
            InlineKeyboardButton(f"🗑️ מחק '{topic_name_short}'", callback_data=f"delete_topic_{topic['id']}")
        ])
    
    # הוספת כפתור חזרה לתפריט
    keyboard.append([InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(message, reply_markup=reply_markup)

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת הסרת נושא"""
    if not context.args:
        await update.message.reply_text("❌ אנא ציינו נושא או מזהה להסרה.\nדוגמה: /remove 1 או /remove גלקסי טאב S11 אולטרה")
        return
    
    identifier = ' '.join(context.args)
    user_id = update.effective_user.id
    
    success = db.remove_topic(user_id, identifier)
    
    if success:
        await update.message.reply_text(f"✅ הנושא '{identifier}' הוסר בהצלחה!")
    else:
        await update.message.reply_text(f"❌ לא נמצא נושא '{identifier}' ברשימה שלכם.")

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת השבתת התראות"""
    user_id = update.effective_user.id
    db.toggle_user_status(user_id, False)
    await update.message.reply_text("⏸️ ההתראות הושבתו. השתמשו ב-/resume להפעלה מחדש.")

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת הפעלת התראות"""
    user_id = update.effective_user.id
    db.toggle_user_status(user_id, True)
    await update.message.reply_text("▶️ ההתראות הופעלו מחדש!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת עזרה"""
    help_text = """
🤖 מדריך השימוש בבוט המעקב החכם

📌 **הוספת נושא למעקב:**
/watch <נושא>
דוגמה: /watch טכנולוגיות בינה מלאכותית חדשות

📋 **צפייה ברשימת הנושאים:**
/list

🗑️ **הסרת נושא:**
/remove <מזהה או שם>
דוגמה: /remove 1 או /remove טכנולוגיות

⏸️ **השבתת התראות:**
/pause

▶️ **הפעלת התראות מחדש:**
/resume

📊 **סטטיסטיקות שימוש:**
/stats

👥 **משתמשים אחרונים (אדמין בלבד):**
/recent_users

🆔 **מידע אישי:**
/whoami

🔍 **איך זה עובד?**
• הבוט בודק את הנושאים שלכם כל 24 שעות
• משתמש ב-Perplexity בינה מלאכותית עם גלישה לחיפוש באינטרנט
• מוצא מידע עדכני ורלוונטי בלבד
• שומר היסטוריה כדי למנוע כפילויות
• שולח לכם רק תוכן חדש שלא ראיתם

💡 **טיפים:**
• השתמשו בנושאים ספציפיים לתוצאות טובות יותר
• הוסיפו שנה או מילות מפתח נוספות (למשל: "בינה מלאכותית 2024")
• ניתן לעקוב אחרי מספר נושאים במקביל
• הבוט זוכר מה כבר נשלח אליכם

🧠 **טכנולוגיה:**
הבוט משתמש ב-Perplexity בינה מלאכותית עם יכולות גלישה מתקדמות לחיפוש והערכה של מידע ברשת.
"""
    
    await update.message.reply_text(help_text, parse_mode='Markdown')

# פקודות אדמין
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """סטטיסטיקות (אדמין בלבד)"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    # קבלת סטטיסטיקות מבסיס הנתונים
    stats = db.get_stats()
    
    total_users = stats["total_users"]
    active_users = stats["active_users"]
    users_with_usage = stats["users_with_usage"]
    users_at_limit = stats["users_at_limit"]
    active_topics = stats["active_topics"]
    total_results = stats["total_results"]
    results_today = stats["results_today"]
    total_usage_this_month = stats["total_usage_this_month"]
    
    stats_message = f"""
📊 **סטטיסטיקות הבוט**

👥 **משתמשים:**
• סה"כ: {total_users}
• פעילים: {active_users}
• השתמשו החודש: {users_with_usage}
• הגיעו למגבלה: {users_at_limit}

📌 **נושאים:**
• נושאים פעילים: {active_topics}

🔍 **תוצאות:**
• סה"כ תוצאות: {total_results}
• תוצאות היום: {results_today}

📊 **שימוש Perplexity החודש:**
• סה"כ שאילתות: {total_usage_this_month}
• ממוצע למשתמש: {total_usage_this_month/users_with_usage if users_with_usage > 0 else 0:.1f}

🧠 משתמש ב-Perplexity בינה מלאכותית עם גלישה
"""
    
    await update.message.reply_text(stats_message, parse_mode='Markdown')

async def test_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת בדיקה מהירה (אדמין בלבד)"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text("❌ /test_search <נושא לבדיקה>")
        return
    
    topic = ' '.join(context.args)
    await update.message.reply_text(f"🔍 בודק נושא: {topic}\nרגע...")
    
    try:
        # אדמין לא מוגבל במכסה
        results = smart_watcher.search_and_analyze_topic(topic)
        
        if results:
            message = f"✅ נמצאו {len(results)} תוצאות עבור '{topic}':\n\n"
            for i, result in enumerate(results[:3], 1):
                message += f"{i}. **{result.get('title', 'ללא כותרת')}**\n"
                message += f"🔗 {result.get('url', 'ללא קישור')}\n"
                message += f"📝 {result.get('summary', 'ללא סיכום')}\n\n"
        else:
            message = f"❌ לא נמצאו תוצאות עבור '{topic}'"
        
        await update.message.reply_text(message, parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"❌ שגיאה בבדיקה: {str(e)}")

# פונקציית בדיקה חד-פעמית לנושא חדש
async def check_single_topic_job(context: ContextTypes.DEFAULT_TYPE):
    """בדיקה חד-פעמית של נושא חדש שנוסף"""
    job_data = context.job.data
    topic_id = job_data['topic_id']
    user_id = job_data['user_id']
    
    # Starting one-time check (reduced logging noise)
    
    # קבלת פרטי הנושא
    topic = db.get_topic_by_id(topic_id)
    if not topic:
        logger.error(f"Topic {topic_id} not found for one-time check")
        return
    
    try:
        # One-time checking topic (reduced logging noise)
        
        # בדיקת מגבלת שימוש לפני הבדיקה
        usage_info = db.get_user_usage(user_id)
        if usage_info['remaining'] <= 0:
            logger.info(f"User {user_id} has reached monthly limit, skipping one-time check for topic {topic_id}")
            
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📊 הגעת למכסת {MONTHLY_LIMIT} הבדיקות החודשיות שלך.\n"
                         f"הבדיקה החד-פעמית לנושא החדש לא בוצעה.\n\n"
                         f"🔍 להצגת פרטי השימוש: /start ← 📊 שימוש נוכחי",
                    reply_markup=get_main_menu_keyboard(user_id),
                    **_LP_KW
                )
            except Exception as e:
                logger.error(f"Failed to send limit notification to user {user_id}: {e}")
            
            return
        
                    # חיפוש תוצאות עם Perplexity API
        # Create topic object for the new run_topic_search function
        class TopicObj:
            def __init__(self, query, user_id, topic_id):
                self.query = query
                self.user_id = user_id
                self.id = topic_id
        
        topic_obj = TopicObj(topic['topic'], user_id, topic_id)
        results = run_topic_search(topic_obj)
        
        if results:
            # עדכון זמן הבדיקה האחרונה
            db.update_topic_checked(topic_id)
            
            # שמירת התוצאות - עם בדיקת תקינות השדות
            valid_results = []
            for result in results:
                if isinstance(result, dict) and is_valid_result(result):
                    try:
                        db.save_result(topic_id, result['title'], result['url'], result.get('summary', ''))
                        valid_results.append(result)
                    except Exception as save_error:
                        logger.warning(f"Failed to save result for topic {topic_id}: {save_error}")
                        continue
                else:
                    logger.warning(f"Skipping invalid result for topic {topic_id}: missing required fields. Result: {result}")
                    continue
            
            # שליחת התוצאות למשתמש - רק תוצאות תקינות
            if valid_results:
                # שימוש בפונקציה המאוחדת לשליחת הודעה עברית אחת
                await send_results_hebrew_only(context.bot, user_id, topic['topic'], valid_results)
                # Only log when results are actually sent
            if valid_results:
                logger.info(f"One-time check: sent {len(valid_results)} results for topic {topic_id}")
            else:
                # אם לא היו תוצאות תקינות, שלח הודעה על כך
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🔍 בדיקה חד-פעמית הושלמה עבור: {topic['topic']}\n\n"
                         f"📭 לא נמצאו תוצאות חדשות כרגע\n"
                         f"🔄 הבדיקות הקבועות יתחילו בהתאם לתדירות שנבחרה",
                    **_LP_KW
                )
                # Removed noisy "no valid results" logging
        else:
            # אם לא נמצאו תוצאות כלל
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🔍 בדיקה חד-פעמית הושלמה עבור: {topic['topic']}\n\n"
                     f"📭 לא נמצאו תוצאות חדשות כרגע\n"
                     f"🔄 הבדיקות הקבועות יתחילו בהתאם לתדירות שנבחרה",
                **_LP_KW
            )
            # Removed noisy "no new results" logging
        
        # עדכון זמן הבדיקה האחרונה תמיד
        db.update_topic_checked(topic_id)
        
    except Exception as e:
        logger.error(f"Error in one-time topic check for topic {topic_id}: {e}")
        
        # עדכון זמן הבדיקה גם במקרה של שגיאה כדי למנוע לולאת שגיאות
        try:
            db.update_topic_checked(topic_id)
        except Exception as db_error:
            logger.error(f"Failed to update topic check time after error for topic {topic_id}: {db_error}")
        
        try:
            # נסה לקבל את שם הנושא בצורה בטוחה
            topic_name = topic.get('topic', 'נושא לא זמין') if topic else 'נושא לא זמין'
            
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ אירעה שגיאה בבדיקה החד-פעמית של הנושא: {topic_name}\n"
                     f"הבדיקות הקבועות יפעלו כרגיל.",
                reply_markup=get_main_menu_keyboard(user_id),
                **_LP_KW
            )
        except Exception as send_error:
            logger.error(f"Failed to send error notification to user {user_id}: {send_error}")

# פונקציית המעקב האוטומטית
async def check_topics_job(context: ContextTypes.DEFAULT_TYPE):
    """בדיקת נושאים אוטומטית"""
    # Starting automatic topics check (reduced logging)
    topics = db.get_active_topics_for_check()
    # Only log if there are topics to check
    if topics:
        logger.info(f"Checking {len(topics)} active topics")
    
    for topic in topics:
        try:
            # Reduced per-topic logging noise
            
            # בדיקת מגבלת שימוש לפני הבדיקה
            usage_info = db.get_user_usage(topic['user_id'])
            if usage_info['remaining'] <= 0:
                logger.info(f"User {topic['user_id']} has reached monthly limit, skipping topic {topic['id']}")
                
                # שליחת הודעה למשתמש שהגיע למגבלה (פעם אחת בחודש)
                try:
                    await context.bot.send_message(
                        chat_id=topic['user_id'],
                        text=f"📊 הגעת למכסת {MONTHLY_LIMIT} הבדיקות החודשיות שלך.\n"
                             f"המעקב יתחדש אוטומטיות בתחילת החודש הבא.\n\n"
                             f"🔍 להצגת פרטי השימוש: /start ← 📊 שימוש נוכחי",
                        reply_markup=get_main_menu_keyboard(topic['user_id']),
                        **_LP_KW
                    )
                except Exception as e:
                    logger.error(f"Failed to send limit notification to user {topic['user_id']}: {e}")
                
                continue
            
            # חיפוש תוצאות עם Perplexity API
            # Create topic object for the new run_topic_search function
            class TopicObj:
                def __init__(self, query, user_id, topic_id):
                    self.query = query
                    self.user_id = user_id
                    self.id = topic_id
            
            topic_obj = TopicObj(topic['topic'], topic['user_id'], topic['id'])
            results = run_topic_search(topic_obj)
            
            if results:
                # Only log when there are actually new results to send
                
                # שמירת תוצאות חדשות ושליחה - Hebrew consolidated message
                new_results = []
                
                for result in results[:3]:  # מקסימום 3 תוצאות
                    result_id = db.save_result(
                        topic['id'],
                        result.get('title', 'ללא כותרת'),
                        result.get('url', ''),
                        result.get('title', 'ללא סיכום')  # Use title as summary since we ignore English content
                    )
                    
                    if result_id:  # תוצאה חדשה
                        new_results.append(result)
                
                # Send ONE consolidated Hebrew message for all new results
                if new_results:
                    await send_results_hebrew_only(context.bot, topic['user_id'], topic['topic'], new_results)
                    logger.info("Sent %d new results for topic %d", len(new_results), topic['id'])
                # Removed noisy "no results" logging
            
            # בדיקה אם זו הבדיקה האחרונה לנושא עם מגבלת בדיקות
            checks_remaining = topic.get('checks_remaining')
            is_last_check = checks_remaining is not None and checks_remaining == 1
            
            # עדכון זמן הבדיקה
            db.update_topic_checked(topic['id'])
            
            # שליחת הודעה מיוחדת אם זו הבדיקה האחרונה
            if is_last_check:
                try:
                    await context.bot.send_message(
                        chat_id=topic['user_id'],
                        text=f"✅ הושלמו 5 הבדיקות עבור הנושא: {topic['topic']}\n\n"
                             f"🔍 המעקב עבור נושא זה הסתיים\n"
                             f"💡 תוכל להוסיף אותו שוב אם תרצה להמשיך במעקב",
                        reply_markup=get_main_menu_keyboard(topic['user_id']),
                        **_LP_KW
                    )
                    logger.info(f"Sent completion notification for topic {topic['id']}")
                except Exception as e:
                    logger.error(f"Failed to send completion notification for topic {topic['id']}: {e}")
            
            # המתנה קצרה בין נושאים למניעת עומס על ה-API
            await asyncio.sleep(2)
            
        except Exception as e:
            logger.error("Error checking topic %d ('%s'): %s", topic['id'], topic.get('topic', 'unknown'), e)
            
            # עדכון זמן הבדיקה גם במקרה של שגיאה כדי למנוע לולאת שגיאות
            try:
                db.update_topic_checked(topic['id'])
            except Exception as db_error:
                logger.error("Failed to update topic check time after error for topic %d: %s", topic['id'], db_error)
    
    logger.info("Finished checking %d topics", len(topics))

# הפונקציה הזו הוסרה כי היא לא נחוצה ועלולה לגרום לשליחת הודעות כפולות
# הבוט משתמש בפונקציה check_topics_job במקום

# משתנים גלובליים לניהול מצבים
user_states = {}

# פונקציות callback
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """טיפול בלחיצות כפתורים"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    user_id = user.id
    data = query.data
    
    # הוספת המשתמש למאגר הנתונים (אם לא קיים)
    db.add_user(user_id, user.username)
    
    try:
        if data == "main_menu":
            # חזרה לתפריט הראשי
            usage_info = db.get_user_usage(user_id)
            message = f"""
🤖 תפריט ראשי - בוט המעקב החכם

📊 **מגבלת השימוש החודשית:**
🔍 השתמשת ב-{usage_info['current_usage']} מתוך {usage_info['monthly_limit']} בדיקות
⏳ נותרו לך {usage_info['remaining']} בדיקות החודש

📞 לכל תקלה או ביקורת ניתן לפנות ל-@moominAmir בטלגרם

בחרו פעולה:
"""
            await query.edit_message_text(message, reply_markup=get_main_menu_keyboard(user_id))
            
        elif data == "add_topic":
            # הוספת נושא חדש
            user_states[user_id] = {"state": "waiting_for_topic"}
            await query.edit_message_text(
                "📝 אנא שלחו את הנושא שתרצו לעקוב אחריו:\n\nדוגמה: Galaxy Tab S11 Ultra",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 ביטול", callback_data="main_menu")]])
            )
            
        elif data == "list_topics" or data == "show_topics":
            # הצגת רשימת נושאים
            await show_topics_list(query, user_id)
            
        elif data == "pause_tracking":
            # השבתת מעקב
            db.toggle_user_status(user_id, False)
            await query.edit_message_text(
                "⏸️ המעקב הושבת בהצלחה!\nלא תקבלו התראות עד להפעלה מחדש.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
            )
            
        elif data == "resume_tracking":
            # הפעלת מעקב מחדש
            db.toggle_user_status(user_id, True)
            await query.edit_message_text(
                "▶️ המעקב הופעל מחדש!\nתקבלו התראות על עדכונים חדשים.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
            )
            
        elif data == "usage_stats":
            # הצגת סטטיסטיקות שימוש
            await show_usage_stats(query, user_id)
            
        elif data == "help":
            # הצגת עזרה
            await show_help(query)
            
        elif data == "quick_commands":
            # הצגת תפריט פקודות מהירות
            await query.edit_message_text(
                "☰ **פקודות מהירות**\n\nבחרו פקודה להרצה מהירה:",
                reply_markup=get_quick_commands_keyboard(user_id)
            )
            
        elif data == "run_whoami":
            # הרצת פקודת /whoami
            await run_whoami_inline(query, user_id)
            
        elif data == "run_recent_users":
            # הרצת פקודת /recent_users (רק לאדמין)
            if user_id == ADMIN_ID:
                await show_recent_users(query, from_quick_commands=True)
            else:
                await query.edit_message_text(
                    "❌ אין לך הרשאה לפקודה זו.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לפקודות מהירות", callback_data="quick_commands")]])
                )
            
        elif data == "recent_users":
            # הצגת משתמשים אחרונים (רק לאדמין)
            if user_id == ADMIN_ID:
                await show_recent_users(query)
            else:
                await query.edit_message_text(
                    "❌ אין לך הרשאה לצפות במידע זה.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
                )
            
        elif data.startswith("freq_"):
            # בחירת תדירות לנושא חדש
            if data == "freq_5min":
                frequency = 0.0833  # 5 דקות בשעות (5/60)
                checks_remaining = 5
            else:
                frequency = int(data.split("_")[1])
                checks_remaining = None
                
            if user_id in user_states and "pending_topic" in user_states[user_id]:
                topic = user_states[user_id]["pending_topic"]
                
                # בדיקת מגבלת שימוש
                usage_info = db.get_user_usage(user_id)
                if usage_info['remaining'] <= 0:
                    await query.edit_message_text(
                        f"❌ הגעת למכסת {MONTHLY_LIMIT} הבדיקות החודשיות שלך.\nתוכל להמשיך בתחילת החודש הבא.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
                    )
                    return
                
                topic_id = db.add_watch_topic(user_id, topic, frequency, checks_remaining)
                
                # תזמון בדיקה חד-פעמית דקה לאחר הוספת הנושא
                context.application.job_queue.run_once(
                    check_single_topic_job,
                    when=timedelta(minutes=1),
                    data={'topic_id': topic_id, 'user_id': user_id},
                    name=f"one_time_check_{topic_id}"
                )
                
                if frequency == 0.0833:
                    freq_text = "כל 5 דקות (5 פעמים בלבד)"
                else:
                    freq_text = {
                        6: "כל 6 שעות",
                        12: "כל 12 שעות", 
                        24: "כל 24 שעות",
                        48: "כל 48 שעות",
                        168: "אחת ל-7 ימים"
                    }.get(frequency, f"כל {frequency} שעות")
                
                await query.edit_message_text(
                    f"✅ הנושא נוסף בהצלחה!\n\n"
                    f"📝 נושא: {topic}\n"
                    f"🆔 מזהה: {topic_id}\n"
                    f"⏰ תדירות בדיקה: {freq_text}\n"
                    f"🔍 בדיקה חד-פעמית תתבצע בעוד דקה\n\n"
                    f"🧠 אני אשתמש ב-Perplexity בינה מלאכותית עם גלישה לחיפוש מידע עדכני",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
                )
                
                # ניקוי מצב המשתמש
                del user_states[user_id]
        
        elif data.startswith("delete_topic_"):
            # מחיקת נושא
            topic_id = data.split("_")[2]
            success = db.remove_topic(user_id, topic_id)
            
            if success:
                await query.edit_message_text(
                    f"✅ הנושא נמחק בהצלחה!",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
                )
            else:
                await query.edit_message_text(
                    f"❌ שגיאה במחיקת הנושא. אנא נסו שוב.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
                )
        
        elif data.startswith("edit_topic_"):
            # עריכת נושא
            topic_id = data.split("_")[2]
            await show_edit_topic_menu(query, user_id, topic_id)
        
        elif data.startswith("edit_text_"):
            # עריכת טקסט הנושא
            topic_id = data.split("_")[2]
            user_states[user_id] = {'action': 'edit_topic_text', 'topic_id': topic_id}
            
            # קבלת פרטי הנושא הנוכחי
            topics = db.get_user_topics(user_id)
            topic = next((t for t in topics if str(t['id']) == str(topic_id)), None)
            
            if topic:
                await query.edit_message_text(
                    f"✏️ עריכת טקסט הנושא\n\n"
                    f"הטקסט הנוכחי: {topic['topic']}\n\n"
                    f"אנא שלחו את הטקסט החדש:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ ביטול", callback_data="main_menu")]])
                )
            else:
                await query.edit_message_text(
                    "❌ הנושא לא נמצא.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
                )
        
        elif data.startswith("edit_freq_"):
            # עריכת תדירות העדכון
            topic_id = data.split("_")[2]
            await show_frequency_selection(query, user_id, topic_id, is_edit=True)
        
        elif data.startswith("update_freq_"):
            # עדכון תדירות הנושא
            parts = data.split("_")
            topic_id = parts[2]
            new_frequency = int(parts[3])
            
            # עדכון התדירות בבסיס הנתונים
            success = db.update_topic_frequency(user_id, topic_id, new_frequency)
            
            freq_text = {
                6: "כל 6 שעות",
                12: "כל 12 שעות", 
                24: "כל 24 שעות",
                48: "כל 48 שעות",
                168: "אחת לשבוע"
            }.get(new_frequency, f"כל {new_frequency} שעות")
            
            if success:
                await query.edit_message_text(
                    f"✅ התדירות עודכנה בהצלחה!\n\nתדירות חדשה: {freq_text}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📋 חזרה לרשימת נושאים", callback_data="show_topics")],
                        [InlineKeyboardButton("🏠 תפריט ראשי", callback_data="main_menu")]
                    ])
                )
            else:
                await query.edit_message_text(
                    "❌ שגיאה בעדכון התדירות. אנא נסו שוב.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
                )
            
    except Exception as e:
        logger.error(f"Error in button callback: {e}")
        await query.edit_message_text(
            "❌ אירעה שגיאה. אנא נסו שוב.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
        )

async def recent_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת /recent_users - הצגת משתמשים אחרונים (רק לאדמין)"""
    user_id = update.effective_user.id
    
    # בדיקה שהמשתמש הוא אדמין
    if user_id != ADMIN_ID:
        await update.message.reply_text(f"❌ פקודה זו זמינה רק למנהל המערכת.\n\nה-ID שלך הוא: `{user_id}`\nADMIN_ID המוגדר כרגע: `{ADMIN_ID}`", parse_mode='Markdown')
        return
    
    try:
        recent_users = db.get_recent_users_activity()
        
        if not recent_users:
            message = """
👥 **משתמשים אחרונים**

📭 אין פעילות השבוע
לא נמצאו משתמשים שהיו פעילים בשבוע האחרון.
"""
        else:
            total_weekly_users = len(recent_users)
            total_topics_added = sum(user['topics_added'] for user in recent_users)
            total_monthly_usage = sum(user['usage_count'] for user in recent_users)
            
            message = f"""👥 **משתמשים אחרונים (שבוע אחרון)**

📊 **סיכום כללי:**
• 👤 סה"כ משתמשים פעילים השבוע: **{total_weekly_users}**
• 📝 סה"כ נושאים נוספו השבוע: **{total_topics_added}**
• 🔍 סה"כ שימוש החודש: **{total_monthly_usage}**

📋 **פירוט משתמשים:**

"""
            
            for i, user in enumerate(recent_users[:10], 1):  # מגביל ל-10 משתמשים
                username = user['username']
                user_id = user['user_id']
                topics_added = user['topics_added']
                usage_count = user['usage_count']
                activity_dates = user['activity_dates']
                
                # פורמט תאריכי פעילות
                if activity_dates:
                    unique_dates = list(set(activity_dates))
                    unique_dates.sort(key=lambda x: datetime.strptime(x, "%d/%m/%Y"), reverse=True)
                    last_activity = unique_dates[0]
                    activity_days = len(unique_dates)
                    
                    if activity_days == 1:
                        activity_text = f"📅 פעיל ב: {last_activity}"
                    else:
                        activity_text = f"📅 פעילות אחרונה: {last_activity} ({activity_days} ימים)"
                else:
                    activity_text = "📅 ללא פעילות השבוע"
                
                message += f"""
{i}. **{username}** (ID: {user_id})
   📝 נושאים השבוע: {topics_added}
   🔍 שימוש החודש: {usage_count}
   {activity_text}
"""
        
        await update.message.reply_text(
            message, 
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error showing recent users: {e}")
        await update.message.reply_text("❌ שגיאה בטעינת רשימת המשתמשים.")

async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת /whoami - הצגת מידע על המשתמש"""
    user = update.effective_user
    user_id = user.id
    username = user.username or "לא מוגדר"
    first_name = user.first_name or "לא מוגדר"
    
    # הוספת המשתמש למאגר הנתונים (אם לא קיים)
    db.add_user(user_id, username)
    
    is_admin = user_id == ADMIN_ID
    admin_status = "✅ כן" if is_admin else "❌ לא"
    
    message = f"""
👤 **פרטי המשתמש**

🆔 **User ID:** `{user_id}`
👤 **שם משתמש:** @{username}
📝 **שם פרטי:** {first_name}
🔑 **הרשאות אדמין:** {admin_status}

ℹ️ **מידע נוסף:**
• ADMIN_ID המוגדר במערכת: `{ADMIN_ID}`
• כדי לקבל הרשאות אדמין, יש להגדיר את ADMIN_ID ל-{user_id}
"""
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def run_whoami_inline(query, user_id):
    """הרצת פקודת /whoami בתפריט אינליין"""
    try:
        user = query.from_user
        username = user.username or "לא מוגדר"
        first_name = user.first_name or "לא מוגדר"
        
        is_admin = user_id == ADMIN_ID
        admin_status = "✅ כן" if is_admin else "❌ לא"
        
        message = f"""
👤 **פרטי המשתמש**

🆔 **User ID:** `{user_id}`
👤 **שם משתמש:** @{username}
📝 **שם פרטי:** {first_name}
🔑 **הרשאות אדמין:** {admin_status}

ℹ️ **מידע נוסף:**
• ADMIN_ID המוגדר במערכת: `{ADMIN_ID}`
• כדי לקבל הרשאות אדמין, יש להגדיר את ADMIN_ID ל-{user_id}
"""
        
        await query.edit_message_text(
            message, 
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לפקודות מהירות", callback_data="quick_commands")]])
        )
    except Exception as e:
        logger.error(f"Error in run_whoami_inline: {e}")
        await query.edit_message_text(
            "❌ שגיאה בהצגת פרטי המשתמש",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
        )

async def show_recent_users(query, from_quick_commands=False):
    """הצגת משתמשים אחרונים (רק לאדמין)"""
    try:
        recent_users = db.get_recent_users_activity()
        
        if not recent_users:
            message = """
👥 **משתמשים אחרונים**

📭 אין פעילות השבוע
לא נמצאו משתמשים שהיו פעילים בשבוע האחרון.
"""
        else:
            total_weekly_users = len(recent_users)
            total_topics_added = sum(user['topics_added'] for user in recent_users)
            total_monthly_usage = sum(user['usage_count'] for user in recent_users)
            
            message = f"""👥 **משתמשים אחרונים (שבוע אחרון)**

📊 **סיכום כללי:**
• 👤 סה"כ משתמשים פעילים השבוע: **{total_weekly_users}**
• 📝 סה"כ נושאים נוספו השבוע: **{total_topics_added}**
• 🔍 סה"כ שימוש החודש: **{total_monthly_usage}**

📋 **פירוט משתמשים:**

"""
            
            for i, user in enumerate(recent_users[:10], 1):  # מגביל ל-10 משתמשים
                username = user['username']
                user_id = user['user_id']
                topics_added = user['topics_added']
                usage_count = user['usage_count']
                activity_dates = user['activity_dates']
                
                # פורמט תאריכי פעילות
                if activity_dates:
                    unique_dates = list(set(activity_dates))
                    unique_dates.sort(key=lambda x: datetime.strptime(x, "%d/%m/%Y"), reverse=True)
                    last_activity = unique_dates[0]
                    activity_days = len(unique_dates)
                    
                    if activity_days == 1:
                        activity_text = f"📅 פעיל ב: {last_activity}"
                    else:
                        activity_text = f"📅 פעילות אחרונה: {last_activity} ({activity_days} ימים)"
                else:
                    activity_text = "📅 ללא פעילות השבוע"
                
                message += f"""
{i}. **{username}** (ID: {user_id})
   📝 נושאים השבוע: {topics_added}
   🔍 שימוש החודש: {usage_count}
   {activity_text}
"""
        
        back_button_text = "🔙 חזרה לפקודות מהירות" if from_quick_commands else "🔙 חזרה לתפריט"
        back_button_callback = "quick_commands" if from_quick_commands else "main_menu"
        
        await query.edit_message_text(
            message, 
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(back_button_text, callback_data=back_button_callback)]])
        )
        
    except Exception as e:
        logger.error(f"Error showing recent users: {e}")
        await query.edit_message_text(
            "❌ שגיאה בטעינת רשימת המשתמשים.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
        )

async def show_edit_topic_menu(query, user_id, topic_id):
    """הצגת תפריט עריכת נושא"""
    # קבלת פרטי הנושא
    topics = db.get_user_topics(user_id)
    topic = next((t for t in topics if str(t['id']) == str(topic_id)), None)
    
    if not topic:
        await query.edit_message_text(
            "❌ הנושא לא נמצא.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
        )
        return
    
    # הצגת פרטי הנושא הנוכחיים
    freq_text = {
        6: "כל 6 שעות",
        12: "כל 12 שעות", 
        24: "כל 24 שעות",
        48: "כל 48 שעות",
        168: "אחת לשבוע"
    }.get(topic['check_interval'], f"כל {topic['check_interval']} שעות")
    
    message = f"""✏️ עריכת נושא מעקב

📝 נושא נוכחי: {topic['topic']}
⏰ תדירות נוכחית: {freq_text}

מה תרצו לערוך?"""
    
    keyboard = [
        [InlineKeyboardButton("📝 שנה את הטקסט", callback_data=f"edit_text_{topic_id}")],
        [InlineKeyboardButton("⏰ שנה תדירות", callback_data=f"edit_freq_{topic_id}")],
        [InlineKeyboardButton("🔙 חזרה לרשימה", callback_data="show_topics")],
        [InlineKeyboardButton("🏠 תפריט ראשי", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_topics_list(query, user_id):
    """הצגת רשימת נושאים"""
    topics = db.get_user_topics(user_id)
    
    if not topics:
        message = "📭 אין לכם נושאים במעקב כרגע.\nהשתמשו בכפתור 'הוסף נושא חדש' כדי להתחיל."
        await query.edit_message_text(message, reply_markup=get_main_menu_keyboard(user_id))
        return
    
    message = "📋 הנושאים שלכם במעקב:\n\n"
    keyboard = []
    
    for i, topic in enumerate(topics, 1):
        status = "🟢"
        last_check = topic['last_checked'] or "מעולם לא"
        if last_check != "מעולם לא":
            try:
                last_check = datetime.fromisoformat(last_check).strftime("%d/%m %H:%M")
            except:
                pass
        
        if topic['check_interval'] == 0.0833:
            freq_text = f"כל 5 דקות ({topic.get('checks_remaining', 0)} נותרו)" if topic.get('checks_remaining') else "כל 5 דקות (הושלם)"
        else:
            freq_text = {
                6: "כל 6 שעות",
                12: "כל 12 שעות", 
                24: "כל 24 שעות",
                48: "כל 48 שעות",
                168: "אחת לשבוע"
            }.get(topic['check_interval'], f"כל {topic['check_interval']} שעות")
        
        message += f"{i}. {status} {topic['topic']}\n"
        message += f"   🆔 {topic['id']} | ⏰ {freq_text}\n"
        message += f"   🕐 נבדק: {last_check}\n\n"
        
        # הוספת כפתורי עריכה ומחיקה לכל נושא
        topic_name_short = topic['topic'][:15] + ('...' if len(topic['topic']) > 15 else '')
        keyboard.append([
            InlineKeyboardButton(f"✏️ ערוך '{topic_name_short}'", callback_data=f"edit_topic_{topic['id']}"),
            InlineKeyboardButton(f"🗑️ מחק '{topic_name_short}'", callback_data=f"delete_topic_{topic['id']}")
        ])
    
    # הוספת כפתור חזרה לתפריט
    keyboard.append([InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(message, reply_markup=reply_markup)

async def show_usage_stats(query, user_id):
    """הצגת סטטיסטיקות שימוש"""
    usage_info = db.get_user_usage(user_id)
    current_month = datetime.now().strftime("%B %Y")
    
    percentage = (usage_info['current_usage'] / usage_info['monthly_limit']) * 100
    
    # יצירת בר התקדמות
    filled_blocks = int(percentage / 10)
    progress_bar = "█" * filled_blocks + "░" * (10 - filled_blocks)
    
    message = f"""
📊 **סטטיסטיקות השימוש שלכם**

📅 חודש נוכחי: {current_month}

🔍 **שאילתות Perplexity:**
{progress_bar} {percentage:.1f}%

📈 השתמשת: {usage_info['current_usage']} / {usage_info['monthly_limit']}
⏳ נותרו: {usage_info['remaining']} בדיקות

💡 **טיפ:** כל בדיקה (אוטומטית או ידנית) נחשבת כשאילתה אחת.
"""
    
    await query.edit_message_text(
        message, 
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
    )

async def show_help(query):
    """הצגת מסך עזרה"""
    help_text = """
🤖 **מדריך השימוש בבוט המעקב החכם**

🔍 **איך זה עובד?**
• הבוט בודק את הנושאים שלכם לפי התדירות שבחרתם
• משתמש ב-Perplexity בינה מלאכותית עם גלישה לחיפוש באינטרנט
• מוצא מידע עדכני ורלוונטי בלבד
• שולח לכם רק תוכן חדש שלא ראיתם

📊 **מגבלת שימוש:**
• 200 בדיקות Perplexity לחודש לכל משתמש
• המגבלה מתאפסת בתחילת כל חודש
• כל בדיקה (אוטומטית/ידנית) נספרת

⏰ **תדירויות בדיקה זמינות:**
• כל 6 שעות - לנושאים דחופים
• כל 12 שעות - לחדשות חמות  
• כל 24 שעות - ברירת מחדל
• כל 48 שעות - למעקב רגיל
• אחת ל-7 ימים - לנושאים כלליים

💡 **טיפים לשימוש יעיל:**
• השתמשו בנושאים ספציפיים
• הוסיפו מילות מפתח נוספות
• בחרו תדירות מתאימה לסוג הנושא
"""
    
    await query.edit_message_text(
        help_text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
    )

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """טיפול בהודעות טקסט"""
    user_id = update.effective_user.id
    text = update.message.text
    
    # בדיקה אם המשתמש במצב המתנה להוספת נושא
    if user_id in user_states and user_states[user_id].get("state") == "waiting_for_topic":
        # שמירת הנושא ובקשה לבחירת תדירות
        user_states[user_id] = {"pending_topic": text}
        
        await update.message.reply_text(
            f"📝 הנושא שנבחר: {text}\n\nאנא בחרו תדירות בדיקה:",
            reply_markup=get_frequency_keyboard()
        )
        return
    
    # בדיקה אם המשתמש במצב עריכת טקסט נושא
    if user_id in user_states and user_states[user_id].get("action") == "edit_topic_text":
        topic_id = user_states[user_id].get("topic_id")
        
        # עדכון הטקסט בבסיס הנתונים
        success = db.update_topic_text(user_id, topic_id, text)
        
        if success:
            await update.message.reply_text(
                f"✅ הטקסט עודכן בהצלחה!\n\nהטקסט החדש: {text}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 חזרה לרשימת נושאים", callback_data="show_topics")],
                    [InlineKeyboardButton("🏠 תפריט ראשי", callback_data="main_menu")]
                ])
            )
        else:
            await update.message.reply_text(
                "❌ שגיאה בעדכון הטקסט. אנא נסו שוב.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
            )
        
        # ניקוי מצב המשתמש
        del user_states[user_id]
        return
    
    # אם אין מצב מיוחד, הצגת התפריט הראשי
    await update.message.reply_text(
        "🤖 שלום! בחרו פעולה מהתפריט הראשי:",
        reply_markup=get_main_menu_keyboard(user_id)
    )

def main():
    """פונקציה ראשית"""
    # הפעלת שרת Flask ברקע
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask server started on port {PORT}")
    
    # יצירת אפליקציית הבוט
    application = Application.builder().token(BOT_TOKEN).build()
    
    # הוספת handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("watch", watch_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("test_search", test_search_command))
    application.add_handler(CommandHandler("recent_users", recent_users_command))
    application.add_handler(CommandHandler("whoami", whoami_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    # הוספת מתזמן למשימות אוטומטיות
    job_queue = application.job_queue
    
    # הפעלת בדיקה אוטומטית כל 24 שעות
    job_queue.run_repeating(
        check_topics_job,
        interval=timedelta(hours=24),
        first=timedelta(minutes=1)  # בדיקה ראשונה אחרי דקה
    )
    
    # הפעלת בדיקה מהירה כל 5 דקות לנושאים מיוחדים
    job_queue.run_repeating(
        check_topics_job,
        interval=timedelta(minutes=5),
        first=timedelta(minutes=2)  # בדיקה ראשונה אחרי 2 דקות
    )
    
    logger.info("Starting bot with polling...")
    
    # הפעלת הבוט
    application.run_polling(drop_pending_updates=True)

def run_smoke_test():
    """Run smoke test for Perplexity integration."""
    try:
        logger.info("🔍 Running Perplexity smoke test...")
        
        # Test basic search
        test = perform_search("What is OpenAI?")
        assert test and len(test) > 0, "Smoke test failed: Perplexity returned no results"
        
        logger.info(f"✅ Smoke test passed: Found {len(test)} results")
        return True
        
    except Exception as e:
        logger.error(f"❌ Smoke test failed: {e}")
        return False

# Smoke test (runs once at startup)
if os.getenv("RUN_SMOKE_TEST", "true").lower() == "true":
    test = perform_search("What is OpenAI?")
    assert test and len(test) > 0, "Smoke test failed – empty"
    logger.info("Smoke test passed ✅")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        success = run_smoke_test()
        sys.exit(0 if success else 1)
    else:
        main()
