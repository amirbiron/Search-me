import os
import logging
import threading
import sqlite3
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any
import asyncio
import re
from tavily import TavilyClient

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode
from flask import Flask, jsonify
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import requests

# הגדרת לוגינג
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=os.getenv("LOG_LEVEL", "INFO")
)
logger = logging.getLogger(__name__)

# --- הגדרות ה-API של Tavily ---
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
assert TAVILY_API_KEY and TAVILY_API_KEY.strip(), "TAVILY_API_KEY is missing or empty"
logger.info(f"Tavily key prefix: {TAVILY_API_KEY[:4]}***")  # sanity only
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)

# משתני סביבה
BOT_TOKEN = os.getenv('BOT_TOKEN')
PERPLEXITY_API_KEY = os.getenv('PERPLEXITY_API_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
DB_PATH = os.getenv('DB_PATH', '/var/data/watchbot.db')
PORT = int(os.getenv('PORT', 5000))

# קבועים
MONTHLY_LIMIT = 200  # מגבלת שאילתות חודשית

# יצירת ספריית נתונים אם לא קיימת
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# בדיקת מפתח Perplexity API
if not PERPLEXITY_API_KEY:
    logger.error("PERPLEXITY_API_KEY environment variable is required")
    exit(1)

# הגדרות Perplexity API
PERPLEXITY_BASE_URL = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_HEADERS = {
    "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
    "Content-Type": "application/json"
}

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
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
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
    
    def add_watch_topic(self, user_id: int, topic: str, check_interval: int = 24) -> int:
        """הוספת נושא למעקב"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO watch_topics (user_id, topic, check_interval)
            VALUES (?, ?, ?)
        ''', (user_id, topic, check_interval))
        topic_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return topic_id
    
    def get_user_topics(self, user_id: int) -> List[Dict]:
        """קבלת רשימת נושאים של משתמש"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, topic, check_interval, is_active, created_at, last_checked
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
                'last_checked': row[5]
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
            SELECT wt.id, wt.user_id, wt.topic, wt.last_checked, wt.check_interval
            FROM watch_topics wt
            JOIN users u ON wt.user_id = u.user_id
            WHERE wt.is_active = 1 AND u.is_active = 1
        ''')
        
        topics = []
        for row in cursor.fetchall():
            topic_id, user_id, topic, last_checked, check_interval = row
            
            # בדיקה אם הגיע הזמן לבדוק את הנושא
            should_check = False
            
            if not last_checked:
                should_check = True
            else:
                last_check_time = datetime.fromisoformat(last_checked)
                time_diff = current_time - last_check_time
                
                if time_diff >= timedelta(hours=check_interval):
                    should_check = True
            
            if should_check:
                topics.append({
                    'id': topic_id,
                    'user_id': user_id,
                    'topic': topic,
                    'last_checked': last_checked,
                    'check_interval': check_interval
                })
        
        conn.close()
        return topics
    
    def get_topic_by_id(self, topic_id: int) -> Dict:
        """קבלת פרטי נושא לפי מזהה"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, user_id, topic, check_interval, is_active, created_at, last_checked
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
                'last_checked': row[6]
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
        """עדכון זמן הבדיקה האחרון"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
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

def tavily_search(query: str, **kwargs):
    """
    Enhanced Tavily search with logging and error handling.
    """
    logger.info(f"Tavily query: {query} | kwargs={kwargs}")
    try:
        resp = tavily_client.search(
            query=query,
            include_answer=True,
            max_results=10,
            search_depth="advanced",
            **kwargs
        )
        logger.debug(f"Tavily raw response: {resp}")
        if not resp or not resp.get("results"):
            logger.warning("Tavily returned empty results")
        return resp
    except Exception as e:
        logger.exception("Tavily search failed")
        raise

def perform_search(query: str) -> list[dict]:
    """
    Performs a search using the Tavily API with fallback for empty results.
    Returns a list of dictionaries, each containing 'title' and 'link'.
    """
    try:
        # First attempt
        response = tavily_search(query, max_results=7)
        search_results = response.get('results', [])
        
        # If empty, try with different parameters
        if not search_results:
            logger.warning(f"First Tavily search returned empty results for query: {query}")
            response = tavily_search(query, max_results=10, search_depth="basic")
            search_results = response.get('results', [])
            
            # If still empty, raise clear error
            if not search_results:
                error_msg = f"Tavily returned no results after 2 attempts for query: '{query}'"
                logger.error(error_msg)
                raise ValueError(error_msg)
        
        formatted_results = []
        for result in search_results:
            formatted_results.append({
                'title': result.get('title', 'No Title'),
                'link': result.get('url', '#')
            })
            
        logger.info(f"Successfully retrieved {len(formatted_results)} search results")
        return formatted_results

    except Exception as e:
        logger.error(f"An error occurred during Tavily API call: {e}")
        return []

class SmartWatcher:
    """מחלקה לניהול המעקב החכם עם Perplexity API"""
    
    def __init__(self, db: WatchBotDB):
        self.db = db
    
    def search_and_analyze_topic(self, topic: str, user_id: int = None) -> List[Dict]:
        """חיפוש ואנליזה של נושא עם Perplexity API"""
        # בדיקת מגבלת שימוש אם סופק user_id
        if user_id:
            usage_info = self.db.get_user_usage(user_id)
            if usage_info['remaining'] <= 0:
                return []  # חריגה ממגבלת השימוש
        
        try:
            current_date = datetime.now().strftime("%Y-%m-%d")
            
            prompt = f"""
אתה עוזר מעקב חכם. הנושא למעקב: "{topic}"
התאריך הנוכחי: {current_date}

משימתך:
1. חפש באינטרנט מידע עדכני וחדש על הנושא הזה (חדשות, מאמרים, פוסטים וכו')
2. התמקד בתוכן שפורסם בימים האחרונים או השבועות האחרונים
3. מצא 2-5 מקורות רלוונטיים ואיכותיים
4. לכל מקור, ספק את המידע הבא:
   - כותרת ברורה ומתארת
   - URL מלא ומדויק
   - סיכום קצר של התוכן (2-3 משפטים)
   - נימוק למה זה רלוונטי לנושא

השב בפורמט JSON הבא:
[
  {{
    "title": "כותרת המאמר/חדשה",
    "url": "https://example.com/article",
    "summary": "סיכום קצר של התוכן והרלוונטיות",
    "relevance_score": 9,
    "date_found": "2024-01-XX"
  }}
]

חפש עכשיו ברשת מידע עדכני על: {topic}
"""
            
            # יצירת הבקשה ל-Perplexity API
            payload = {
                "model": "sonar-medium-chat",
                "messages": [
                    {
                        "role": "system", 
                        "content": "You are a smart web researcher with browsing capabilities. Always search the web for current information and return valid JSON format results. Answer in Hebrew when the user writes in Hebrew."
                    },
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 2000,
                "temperature": 0.3,
                "stream": False
            }
            
            response = requests.post(
                PERPLEXITY_BASE_URL,
                json=payload,
                headers=PERPLEXITY_HEADERS,
                timeout=30
            )
            
            response.raise_for_status()
            response_data = response.json()
            
            logger.info(f"🔍 Perplexity raw response for topic '{topic}': {response_data}")
            
            # עדכון המונה רק לאחר שאילתת Perplexity מוצלחת
            if user_id:
                self.db.increment_usage(user_id)
            
            # ניסיון לפרס את ה-JSON
            response_text = response_data['choices'][0]['message']['content']
            if not response_text:
                logger.warning(f"Empty response from Perplexity for topic '{topic}'")
                return []
            
            response_text = response_text.strip()
            
            # ניקוי הטקסט מסימני markdown אם יש
            if "```json" in response_text.lower():
                parts = response_text.lower().split("```json")
                if len(parts) > 1:
                    end_parts = parts[1].split("```")
                    if len(end_parts) > 0:
                        # מצא את המיקום המקורי בטקסט
                        start_idx = response_text.lower().find("```json") + 7
                        end_idx = response_text.find("```", start_idx)
                        if end_idx > start_idx:
                            response_text = response_text[start_idx:end_idx].strip()
            elif "```" in response_text:
                parts = response_text.split("```")
                if len(parts) >= 3:
                    response_text = parts[1].strip()
            
            # הסרת תוכן שאינו JSON
            if response_text.startswith('[') or response_text.startswith('{'):
                # מצא את סוף ה-JSON
                bracket_count = 0
                json_end = 0
                start_char = response_text[0]
                end_char = ']' if start_char == '[' else '}'
                
                for i, char in enumerate(response_text):
                    if char == start_char:
                        bracket_count += 1
                    elif char == end_char:
                        bracket_count -= 1
                        if bracket_count == 0:
                            json_end = i + 1
                            break
                
                if json_end > 0:
                    response_text = response_text[:json_end]
            
            try:
                results = json.loads(response_text)
                if isinstance(results, list):
                    # וידוא שכל התוצאות מכילות את השדות הנדרשים
                    valid_results = []
                    for result in results:
                        if isinstance(result, dict):
                            # וידוא שקיימים השדות הבסיסיים
                            if 'title' not in result:
                                result['title'] = 'ללא כותרת'
                            if 'url' not in result:
                                result['url'] = ''
                            if 'summary' not in result:
                                result['summary'] = 'ללא סיכום'
                            valid_results.append(result)
                        else:
                            logger.warning(f"Skipping non-dict result: {result}")
                    return valid_results
                else:
                    logger.warning(f"Perplexity returned non-list result: {results}")
                    return []
            except json.JSONDecodeError as json_error:
                logger.error(f"Failed to parse JSON response for topic '{topic}'. Error: {json_error}")
                logger.debug(f"Raw response text: {response_text[:500]}...")  # רק 500 תווים ראשונים
                return []
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error in Perplexity API request for topic '{topic}': {e}")
            return []
        except Exception as e:
            logger.error(f"Error in Perplexity search for topic '{topic}': {e}")
            # במקרה של שגיאה, נחזיר רשימה ריקה כדי שהבוט ימשיך לפעול
            return []

# יצירת אובייקטי המערכת
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

def get_main_menu_keyboard():
    """יצירת תפריט הכפתורים הראשי"""
    keyboard = [
        [InlineKeyboardButton("📌 הוסף נושא חדש", callback_data="add_topic")],
        [InlineKeyboardButton("📋 הצג רשימת נושאים", callback_data="list_topics")],
        [InlineKeyboardButton("⏸️ השבת מעקב", callback_data="pause_tracking"),
         InlineKeyboardButton("▶️ הפעל מחדש", callback_data="resume_tracking")],
        [InlineKeyboardButton("📊 שימוש נוכחי", callback_data="usage_stats"),
         InlineKeyboardButton("❓ עזרה", callback_data="help")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_frequency_keyboard():
    """יצירת תפריט בחירת תדירות"""
    keyboard = [
        [InlineKeyboardButton("כל 6 שעות", callback_data="freq_6")],
        [InlineKeyboardButton("כל 12 שעות", callback_data="freq_12")],
        [InlineKeyboardButton("כל 24 שעות (ברירת מחדל)", callback_data="freq_24")],
        [InlineKeyboardButton("כל 48 שעות", callback_data="freq_48")],
        [InlineKeyboardButton("אחת ל-7 ימים", callback_data="freq_168")]
    ]
    return InlineKeyboardMarkup(keyboard)

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

🧠 אני משתמש ב-Perplexity AI עם יכולות גלישה באינטרנט לחיפוש מידע עדכני ורלוונטי.

📊 **מגבלת השימוש החודשית:**
🔍 השתמשת ב-{usage_info['current_usage']} מתוך {usage_info['monthly_limit']} בדיקות
⏳ נותרו לך {usage_info['remaining']} בדיקות החודש

בחרו פעולה מהתפריט למטה:
"""
    
    await update.message.reply_text(welcome_message, reply_markup=get_main_menu_keyboard())

async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת הוספת נושא למעקב"""
    if not context.args:
        await update.message.reply_text("❌ אנא ציינו נושא למעקב.\nדוגמה: /watch Galaxy Tab S11 Ultra")
        return
    
    topic = ' '.join(context.args)
    user_id = update.effective_user.id
    
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
        f"🧠 אני אשתמש ב-Perplexity AI עם browsing לחיפוש מידע עדכני\n\n"
        f"אבדוק אותו כל 24 שעות ואתריע על תוכן חדש."
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת רשימת נושאים"""
    user_id = update.effective_user.id
    topics = db.get_user_topics(user_id)
    
    if not topics:
        message = "📭 אין לכם נושאים במעקב כרגע.\nהשתמשו בכפתור 'הוסף נושא חדש' כדי להתחיל."
        await update.message.reply_text(message, reply_markup=get_main_menu_keyboard())
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
        
        # הוספת כפתור מחיקה לכל נושא
        keyboard.append([InlineKeyboardButton(f"🗑️ מחק '{topic['topic'][:20]}{'...' if len(topic['topic']) > 20 else ''}'", callback_data=f"delete_topic_{topic['id']}")])
    
    # הוספת כפתור חזרה לתפריט
    keyboard.append([InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(message, reply_markup=reply_markup)

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת הסרת נושא"""
    if not context.args:
        await update.message.reply_text("❌ אנא ציינו נושא או מזהה להסרה.\nדוגמה: /remove 1 או /remove Galaxy Tab S11 Ultra")
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
דוגמה: /watch טכנולוגיות AI חדשות

📋 **צפייה ברשימת הנושאים:**
/list

🗑️ **הסרת נושא:**
/remove <מזהה או שם>
דוגמה: /remove 1 או /remove טכנולוגיות

⏸️ **השבתת התראות:**
/pause

▶️ **הפעלת התראות מחדש:**
/resume

🔍 **איך זה עובד?**
• הבוט בודק את הנושאים שלכם כל 24 שעות
• משתמש ב-Perplexity AI עם browsing לחיפוש באינטרנט
• מוצא מידע עדכני ורלוונטי בלבד
• שומר היסטוריה כדי למנוע כפילויות
• שולח לכם רק תוכן חדש שלא ראיתם

💡 **טיפים:**
• השתמשו בנושאים ספציפיים לתוצאות טובות יותר
• הוסיפו שנה או מילות מפתח נוספות (למשל: "AI 2024")
• ניתן לעקוב אחרי מספר נושאים במקביל
• הבוט זוכר מה כבר נשלח אליכם

🧠 **טכנולוגיה:**
הבוט משתמש ב-Perplexity AI עם יכולות browsing מתקדמות לחיפוש והערכה של מידע ברשת.
"""
    
    await update.message.reply_text(help_text, parse_mode='Markdown')

# פקודות אדמין
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """סטטיסטיקות (אדמין בלבד)"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
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
    current_month = datetime.now().strftime("%Y-%m")
    cursor.execute("SELECT COUNT(*), SUM(usage_count) FROM usage_stats WHERE month = ?", (current_month,))
    usage_stats = cursor.fetchone()
    users_with_usage = usage_stats[0] if usage_stats[0] else 0
    total_usage_this_month = usage_stats[1] if usage_stats[1] else 0
    
    # משתמשים שהגיעו למגבלה
    cursor.execute("SELECT COUNT(*) FROM usage_stats WHERE month = ? AND usage_count >= ?", (current_month, MONTHLY_LIMIT))
    users_at_limit = cursor.fetchone()[0]
    
    conn.close()
    
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

🧠 משתמש ב-Perplexity AI עם browsing
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
            message = f"👇 הנה הכלים שמצאתי:\n\n✅ נמצאו {len(results)} תוצאות עבור '{topic}':\n\n"
            for i, result in enumerate(results[:3], 1):
                message += f"{i}. **{result.get('title', 'ללא כותרת')}**\n"
                message += f"🔗 {result.get('url', 'ללא קישור')}\n"
                message += f"📝 {result.get('summary', 'ללא סיכום')}\n\n"
        else:
            message = f"👇 הנה הכלים שמצאתי:\n\n❌ לא נמצאו תוצאות עבור '{topic}'"
        
        await update.message.reply_text(message, parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"❌ שגיאה בבדיקה: {str(e)}")

# פונקציית בדיקה חד-פעמית לנושא חדש
async def check_single_topic_job(context: ContextTypes.DEFAULT_TYPE):
    """בדיקה חד-פעמית של נושא חדש שנוסף"""
    job_data = context.job.data
    topic_id = job_data['topic_id']
    user_id = job_data['user_id']
    
    logger.info(f"Starting one-time check for topic ID: {topic_id}")
    
    # קבלת פרטי הנושא
    topic = db.get_topic_by_id(topic_id)
    if not topic:
        logger.error(f"Topic {topic_id} not found for one-time check")
        return
    
    try:
        logger.info(f"One-time checking topic: {topic['topic']} (ID: {topic_id})")
        
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
                    reply_markup=get_main_menu_keyboard()
                )
            except Exception as e:
                logger.error(f"Failed to send limit notification to user {user_id}: {e}")
            
            return
        
        # חיפוש תוצאות עם Perplexity API
        results = smart_watcher.search_and_analyze_topic(topic['topic'], user_id)
        
        if results:
            # עדכון זמן הבדיקה האחרונה
            db.update_topic_checked(topic_id)
            
            # שמירת התוצאות - עם בדיקת תקינות השדות
            valid_results = []
            for result in results:
                if isinstance(result, dict) and all(key in result for key in ['title', 'url', 'summary']):
                    try:
                        db.save_result(topic_id, result['title'], result['url'], result['summary'])
                        valid_results.append(result)
                    except Exception as save_error:
                        logger.warning(f"Failed to save result for topic {topic_id}: {save_error}")
                        continue
                else:
                    logger.warning(f"Skipping invalid result for topic {topic_id}: missing required fields. Result: {result}")
                    continue
            
            # שליחת התוצאות למשתמש - רק תוצאות תקינות
            if valid_results:
                message = f"👇 הנה הכלים שמצאתי:\n\n🔍 **בדיקה חד-פעמית - תוצאות חדשות עבור:** {topic['topic']}\n\n"
                
                for i, result in enumerate(valid_results[:5], 1):  # מגבלה של 5 תוצאות
                    title = result.get('title', 'ללא כותרת')
                    url = result.get('url', 'ללא קישור')
                    summary = result.get('summary', 'ללא סיכום')
                    
                    message += f"**{i}. {title}**\n"
                    message += f"🔗 {url}\n"
                    message += f"📄 {summary}\n\n"
            
                if len(valid_results) > 5:
                    message += f"📋 ועוד {len(valid_results) - 5} תוצאות נוספות...\n\n"
                
                message += f"⏰ נבדק עכשיו (בדיקה חד-פעמית)\n"
                message += f"🔄 הבדיקות הקבועות יתחילו בהתאם לתדירות שנבחרה"
                
                await context.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    parse_mode='Markdown',
                    disable_web_page_preview=True,
                    reply_markup=get_main_menu_keyboard()
                )
                
                logger.info(f"One-time check completed successfully for topic {topic_id}, found {len(valid_results)} valid results out of {len(results)} total results")
            else:
                # אם לא היו תוצאות תקינות, נתייחס לזה כמו שלא נמצאו תוצאות
                logger.info(f"One-time check completed for topic {topic_id}, no valid results found (had {len(results)} invalid results)")
                
                # עדכון זמן הבדיקה האחרונה גם אם לא נמצאו תוצאות תקינות
                db.update_topic_checked(topic_id)
                
                # שליחת הודעה שלא נמצאו תוצאות תקינות
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"👇 הנה הכלים שמצאתי:\n\n🔍 **בדיקה חד-פעמית הושלמה עבור:** {topic['topic']}\n\n"
                         f"📭 לא נמצאו תוצאות תקינות כרגע\n"
                         f"🔄 הבדיקות הקבועות יתחילו בהתאם לתדירות שנבחרה",
                    parse_mode='Markdown',
                    reply_markup=get_main_menu_keyboard()
                )
        else:
            logger.info(f"One-time check completed for topic {topic_id}, no new results found")
            
            # עדכון זמן הבדיקה האחרונה גם אם לא נמצאו תוצאות
            db.update_topic_checked(topic_id)
            
            # שליחת הודעה שלא נמצאו תוצאות
            await context.bot.send_message(
                chat_id=user_id,
                text=f"👇 הנה הכלים שמצאתי:\n\n🔍 **בדיקה חד-פעמית הושלמה עבור:** {topic['topic']}\n\n"
                     f"📭 לא נמצאו תוצאות חדשות כרגע\n"
                     f"🔄 הבדיקות הקבועות יתחילו בהתאם לתדירות שנבחרה",
                parse_mode='Markdown',
                reply_markup=get_main_menu_keyboard()
            )
        
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
                reply_markup=get_main_menu_keyboard()
            )
        except Exception as send_error:
            logger.error(f"Failed to send error notification to user {user_id}: {send_error}")

# פונקציית המעקב האוטומטית
async def check_topics_job(context: ContextTypes.DEFAULT_TYPE):
    """בדיקת נושאים אוטומטית"""
    logger.info("Starting automatic topics check...")
    
    topics = db.get_active_topics_for_check()
    logger.info(f"Found {len(topics)} topics to check")
    
    for topic in topics:
        try:
            logger.info(f"Checking topic: {topic['topic']} (ID: {topic['id']})")
            
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
                        reply_markup=get_main_menu_keyboard()
                    )
                except Exception as e:
                    logger.error(f"Failed to send limit notification to user {topic['user_id']}: {e}")
                
                continue
            
            # חיפוש תוצאות עם Perplexity API
            results = smart_watcher.search_and_analyze_topic(topic['topic'], topic['user_id'])
            
            if results:
                logger.info(f"Found {len(results)} results for topic {topic['id']}")
                
                # שמירת תוצאות חדשות ושליחה
                new_results_count = 0
                
                for result in results[:3]:  # מקסימום 3 תוצאות
                    result_id = db.save_result(
                        topic['id'],
                        result.get('title', 'ללא כותרת'),
                        result.get('url', ''),
                        result.get('summary', 'ללא סיכום')
                    )
                    
                    if result_id:  # תוצאה חדשה
                        new_results_count += 1
                        
                        # שליחת התראה למשתמש
                        message = f"""
🔔 **עדכון חדש עבור: {topic['topic']}**

📰 {result.get('title', 'ללא כותרת')}

📝 {result.get('summary', 'ללא סיכום')}

🔗 [קישור למקור]({result.get('url', '')})

🎯 רלוונטיות: {result.get('relevance_score', 'N/A')}/10
"""
                        
                        try:
                            await context.bot.send_message(
                                chat_id=topic['user_id'],
                                text=message,
                                parse_mode='Markdown',
                                disable_web_page_preview=False
                            )
                            logger.info(f"Sent notification to user {topic['user_id']} for topic {topic['id']}")
                        except Exception as e:
                            logger.error(f"Failed to send message to user {topic['user_id']}: {e}")
                
                if new_results_count > 0:
                    logger.info(f"Sent {new_results_count} new results for topic {topic['id']}")
                else:
                    logger.info(f"No new results for topic {topic['id']} (all were duplicates)")
            else:
                logger.info(f"No results found for topic {topic['id']}")
            
            # עדכון זמן הבדיקה
            db.update_topic_checked(topic['id'])
            
            # המתנה קצרה בין נושאים למניעת עומס על ה-API
            await asyncio.sleep(2)
            
        except Exception as e:
            logger.error(f"Error checking topic {topic['id']} ('{topic.get('topic', 'unknown')}'): {e}")
            
            # עדכון זמן הבדיקה גם במקרה של שגיאה כדי למנוע לולאת שגיאות
            try:
                db.update_topic_checked(topic['id'])
            except Exception as db_error:
                logger.error(f"Failed to update topic check time after error for topic {topic['id']}: {db_error}")
    
    logger.info(f"Finished checking {len(topics)} topics")

async def check_for_updates(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks for new search results for all users and notifies them."""
    logger.info("Running scheduled job: Checking for updates...")
    users = db.get_all_users()

    for user_doc in users:
        user_id = user_doc['user_id']
        all_new_results_for_user = []

        if 'search_terms' not in user_doc or not user_doc['search_terms']:
            continue

        for search_term_data in user_doc['search_terms']:
            search_term = search_term_data['term']
            keywords = search_term_data.get('keywords', [])
            
            logger.info(f"Searching for term: '{search_term}' for user: {user_id}")
            current_results = perform_search(search_term)
            
            if keywords:
                current_results = [
                    res for res in current_results 
                    if any(keyword.lower() in res['title'].lower() for keyword in keywords)
                ]

            previous_results = db.get_results_for_term(user_id, search_term)
            new_results = [res for res in current_results if res not in previous_results]

            if new_results:
                all_new_results_for_user.append({
                    'term': search_term,
                    'results': new_results
                })
                db.update_results_for_term(user_id, search_term, current_results)
        
        if all_new_results_for_user:
            message = "📢 מצאתי עדכונים חדשים עבורך!\n\n"
            for item in all_new_results_for_user:
                message += f"<b>נושא החיפוש: {item['term']}</b>\n"
                for res in item['results']:
                    message += f"- <a href='{res['link']}'>{res['title']}</a>\n"
                message += "\n"
            
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"Failed to send message to user {user_id}: {e}")

# משתנים גלובליים לניהול מצבים
user_states = {}

# פונקציות callback
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """טיפול בלחיצות כפתורים"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    try:
        if data == "main_menu":
            # חזרה לתפריט הראשי
            usage_info = db.get_user_usage(user_id)
            message = f"""
🤖 תפריט ראשי - בוט המעקב החכם

📊 **מגבלת השימוש החודשית:**
🔍 השתמשת ב-{usage_info['current_usage']} מתוך {usage_info['monthly_limit']} בדיקות
⏳ נותרו לך {usage_info['remaining']} בדיקות החודש

בחרו פעולה:
"""
            await query.edit_message_text(message, reply_markup=get_main_menu_keyboard())
            
        elif data == "add_topic":
            # הוספת נושא חדש
            user_states[user_id] = {"state": "waiting_for_topic"}
            await query.edit_message_text(
                "📝 אנא שלחו את הנושא שתרצו לעקוב אחריו:\n\nדוגמה: Galaxy Tab S11 Ultra",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 ביטול", callback_data="main_menu")]])
            )
            
        elif data == "list_topics":
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
            
        elif data.startswith("freq_"):
            # בחירת תדירות לנושא חדש
            frequency = int(data.split("_")[1])
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
                
                topic_id = db.add_watch_topic(user_id, topic, frequency)
                
                # תזמון בדיקה חד-פעמית דקה לאחר הוספת הנושא
                context.application.job_queue.run_once(
                    check_single_topic_job,
                    when=timedelta(minutes=1),
                    data={'topic_id': topic_id, 'user_id': user_id},
                    name=f"one_time_check_{topic_id}"
                )
                
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
                    f"🧠 אני אשתמש ב-Perplexity AI עם browsing לחיפוש מידע עדכני",
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
            
    except Exception as e:
        logger.error(f"Error in button callback: {e}")
        await query.edit_message_text(
            "❌ אירעה שגיאה. אנא נסו שוב.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לתפריט", callback_data="main_menu")]])
        )

async def show_topics_list(query, user_id):
    """הצגת רשימת נושאים"""
    topics = db.get_user_topics(user_id)
    
    if not topics:
        message = "📭 אין לכם נושאים במעקב כרגע.\nהשתמשו בכפתור 'הוסף נושא חדש' כדי להתחיל."
        await query.edit_message_text(message, reply_markup=get_main_menu_keyboard())
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
        
        # הוספת כפתור מחיקה לכל נושא
        keyboard.append([InlineKeyboardButton(f"🗑️ מחק '{topic['topic'][:20]}{'...' if len(topic['topic']) > 20 else ''}'", callback_data=f"delete_topic_{topic['id']}")])
    
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
• משתמש ב-Perplexity AI עם browsing לחיפוש באינטרנט
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
    
    # אם אין מצב מיוחד, הצגת התפריט הראשי
    await update.message.reply_text(
        "🤖 בחרו פעולה מהתפריט:",
        reply_markup=get_main_menu_keyboard()
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
    
    logger.info("Starting bot with polling...")
    
    # הפעלת הבוט
    application.run_polling(drop_pending_updates=True)

def run_smoke_test():
    """Run smoke test for Tavily integration."""
    try:
        logger.info("🔍 Running Tavily smoke test...")
        
        # Test basic search
        test = tavily_search("What is OpenAI?", include_images=False)
        assert test and test.get("results"), "Smoke test failed: Tavily returned no results"
        
        logger.info(f"✅ Smoke test passed: Found {len(test.get('results', []))} results")
        return True
        
    except Exception as e:
        logger.error(f"❌ Smoke test failed: {e}")
        return False

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        success = run_smoke_test()
        sys.exit(0 if success else 1)
    else:
        main()
