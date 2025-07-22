import os
import logging
import threading
import sqlite3
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any
import asyncio
import aiohttp
import requests
from openai import OpenAI

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from flask import Flask, jsonify
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# הגדרת לוגינג
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# משתני סביבה
BOT_TOKEN = os.getenv('BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
BING_API_KEY = os.getenv('BING_API_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
DB_PATH = os.getenv('DB_PATH', '/var/data/watchbot.db')
PORT = int(os.getenv('PORT', 5000))

# יצירת ספריית נתונים אם לא קיימת
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# יצירת לקוח OpenAI
openai_client = OpenAI(api_key=OPENAI_API_KEY)

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
                search_query TEXT,
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
                snippet TEXT,
                found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_sent BOOLEAN DEFAULT 0,
                FOREIGN KEY (topic_id) REFERENCES watch_topics (id)
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
    
    def add_watch_topic(self, user_id: int, topic: str, search_query: str = None) -> int:
        """הוספת נושא למעקב"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO watch_topics (user_id, topic, search_query)
            VALUES (?, ?, ?)
        ''', (user_id, topic, search_query or topic))
        topic_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return topic_id
    
    def get_user_topics(self, user_id: int) -> List[Dict]:
        """קבלת רשימת נושאים של משתמש"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, topic, is_active, created_at, last_checked
            FROM watch_topics
            WHERE user_id = ? AND is_active = 1
            ORDER BY created_at DESC
        ''', (user_id,))
        
        topics = []
        for row in cursor.fetchall():
            topics.append({
                'id': row[0],
                'topic': row[1],
                'is_active': row[2],
                'created_at': row[3],
                'last_checked': row[4]
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
        """קבלת נושאים פעילים לבדיקה"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT wt.id, wt.user_id, wt.topic, wt.search_query, wt.last_checked
            FROM watch_topics wt
            JOIN users u ON wt.user_id = u.user_id
            WHERE wt.is_active = 1 AND u.is_active = 1
        ''')
        
        topics = []
        for row in cursor.fetchall():
            topics.append({
                'id': row[0],
                'user_id': row[1],
                'topic': row[2],
                'search_query': row[3],
                'last_checked': row[4]
            })
        
        conn.close()
        return topics
    
    def save_result(self, topic_id: int, title: str, url: str, snippet: str):
        """שמירת תוצאה שנמצאה"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # בדיקה אם התוצאה כבר קיימת
        cursor.execute('''
            SELECT id FROM found_results
            WHERE topic_id = ? AND url = ?
        ''', (topic_id, url))
        
        if not cursor.fetchone():
            cursor.execute('''
                INSERT INTO found_results (topic_id, title, url, snippet)
                VALUES (?, ?, ?, ?)
            ''', (topic_id, title, url, snippet))
            conn.commit()
            result_id = cursor.lastrowid
        else:
            result_id = None
        
        conn.close()
        return result_id
    
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

class BingSearchAPI:
    """מחלקה לחיפוש ב-Bing"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.endpoint = "https://api.bing.microsoft.com/v7.0/search"
    
    async def search(self, query: str, count: int = 10) -> List[Dict]:
        """ביצוע חיפוש ב-Bing"""
        headers = {
            'Ocp-Apim-Subscription-Key': self.api_key,
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        params = {
            'q': query,
            'count': count,
            'responseFilter': 'webPages',
            'textFormat': 'HTML',
            'safeSearch': 'Moderate'
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.endpoint, headers=headers, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        results = []
                        
                        if 'webPages' in data and 'value' in data['webPages']:
                            for item in data['webPages']['value']:
                                results.append({
                                    'title': item.get('name', ''),
                                    'url': item.get('url', ''),
                                    'snippet': item.get('snippet', ''),
                                    'datePublished': item.get('datePublished', '')
                                })
                        
                        return results
                    else:
                        logger.error(f"Bing API error: {response.status}")
                        return []
        
        except Exception as e:
            logger.error(f"Error in Bing search: {e}")
            return []

class SmartWatcher:
    """מחלקה לניהול המעקב החכם"""
    
    def __init__(self, db: WatchBotDB, bing_api: BingSearchAPI):
        self.db = db
        self.bing_api = bing_api
    
    def generate_smart_query(self, topic: str) -> str:
        """יצירת שאילתה חכמה עם GPT-4o"""
        try:
            prompt = f"""
אתה עוזר חכם ליצירת שאילתות חיפוש מיטביות.
הנושא למעקב: "{topic}"

צור שאילתת חיפוש באנגלית שתמצא חדשות ומידע עדכני על הנושא.
השאילתה צריכה להיות:
1. ספציפית ורלוונטית
2. מכילה מילות מפתח שיביאו תוצאות עדכניות
3. לא יותר מ-10 מילים

השב רק עם השאילתה, ללא הסברים נוספים.
"""
            
            response = openai_client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a search query optimization expert."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=50,
                temperature=0.7
            )
            
            return response.choices[0].message.content.strip()
        
        except Exception as e:
            logger.error(f"Error generating smart query: {e}")
            return topic
    
    def filter_results_with_gpt(self, topic: str, results: List[Dict]) -> List[Dict]:
        """סינון תוצאות עם GPT-4o"""
        if not results:
            return []
        
        try:
            results_text = json.dumps(results, ensure_ascii=False, indent=2)
            
            prompt = f"""
אתה עוזר לסינון תוצאות חיפוש לפי רלוונטיות.
הנושא למעקב: "{topic}"

תוצאות החיפוש:
{results_text}

סנן את התוצאות ו:
1. השאר רק תוצאות רלוונטיות לנושא
2. הסר תוצאות כפולות או דומות מדי
3. תעדף תוכן חדש ועדכני
4. מיין לפי רלוונטיות (הכי רלוונטי ראשון)

השב בפורמט JSON עם מערך של התוצאות הסופיות.
כל תוצאה צריכה להכיל: title, url, snippet, relevance_score (1-10)
"""
            
            response = openai_client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a content relevance filter. Always respond with valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1500,
                temperature=0.3
            )
            
            filtered_results = json.loads(response.choices[0].message.content)
            return filtered_results if isinstance(filtered_results, list) else []
        
        except Exception as e:
            logger.error(f"Error filtering results with GPT: {e}")
            return results[:3]  # החזרת 3 התוצאות הראשונות במקרה של שגיאה

# יצירת אובייקטי המערכת
db = WatchBotDB(DB_PATH)
bing_api = BingSearchAPI(BING_API_KEY)
smart_watcher = SmartWatcher(db, bing_api)

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

# פקודות הבוט
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת התחלה"""
    user = update.effective_user
    db.add_user(user.id, user.username)
    
    welcome_message = """
🤖 ברוכים הבאים לבוט המעקב החכם!

אני עוזר לכם לעקוב אחרי נושאים שמעניינים אתכם ומתריע כשיש מידע חדש.

פקודות זמינות:
📌 /watch <נושא> - הוספת נושא למעקב
📋 /list - רשימת הנושאים שלכם
🗑️ /remove <נושא/ID> - הסרת נושא
⏸️ /pause - השבתת כל ההתראות
▶️ /resume - הפעלת ההתראות
❓ /help - עזרה מפורטת

דוגמה: /watch בינה מלאכותית
"""
    
    await update.message.reply_text(welcome_message)

async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת הוספת נושא למעקב"""
    if not context.args:
        await update.message.reply_text("❌ אנא ציינו נושא למעקב.\nדוגמה: /watch בינה מלאכותית")
        return
    
    topic = ' '.join(context.args)
    user_id = update.effective_user.id
    
    # יצירת שאילתה חכמה
    smart_query = smart_watcher.generate_smart_query(topic)
    
    # הוספה למסד הנתונים
    topic_id = db.add_watch_topic(user_id, topic, smart_query)
    
    await update.message.reply_text(
        f"✅ הנושא נוסף בהצלחה!\n"
        f"📝 נושא: {topic}\n"
        f"🔍 שאילתת חיפוש: {smart_query}\n"
        f"🆔 מזהה: {topic_id}\n\n"
        f"אבדוק אותו כל 24 שעות ואתריע על תוכן חדש."
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת רשימת נושאים"""
    user_id = update.effective_user.id
    topics = db.get_user_topics(user_id)
    
    if not topics:
        await update.message.reply_text("📭 אין לכם נושאים במעקב כרגע.\nהשתמשו ב-/watch כדי להוסיף נושא.")
        return
    
    message = "📋 הנושאים שלכם במעקב:\n\n"
    
    for i, topic in enumerate(topics, 1):
        status = "🟢" if topic['is_active'] else "🔴"
        last_check = topic['last_checked'] or "מעולם לא"
        
        message += f"{i}. {status} {topic['topic']}\n"
        message += f"   🆔 {topic['id']} | 🕐 נבדק לאחרונה: {last_check}\n\n"
    
    # הוספת כפתורי פעולה
    keyboard = [
        [InlineKeyboardButton("🔄 רענון רשימה", callback_data="refresh_list")],
        [InlineKeyboardButton("➕ הוסף נושא", callback_data="add_topic")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(message, reply_markup=reply_markup)

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """פקודת הסרת נושא"""
    if not context.args:
        await update.message.reply_text("❌ אנא ציינו נושא או מזהה להסרה.\nדוגמה: /remove 1 או /remove בינה מלאכותית")
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
דוגמה: /watch טכנולוגיות חדשות

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
• משתמש בבינה מלאכותית ליצירת שאילתות חיפוש מיטביות
• מסנן תוצאות ושולח רק תוכן חדש ורלוונטי
• שומר היסטוריה כדי למנוע כפילויות

💡 **טיפים:**
• השתמשו בנושאים ספציפיים לתוצאות טובות יותר
• ניתן לעקוב אחרי מספר נושאים במקביל
• הבוט זוכר מה כבר נשלח אליכם
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
    
    conn.close()
    
    stats_message = f"""
📊 **סטטיסטיקות הבוט**

👥 משתמשים: {active_users}/{total_users} (פעילים/סה"כ)
📌 נושאים פעילים: {active_topics}
🔍 תוצאות שנמצאו: {total_results}
"""
    
    await update.message.reply_text(stats_message, parse_mode='Markdown')

# פונקציית המעקב האוטומטית
async def check_topics_job(context: ContextTypes.DEFAULT_TYPE):
    """בדיקת נושאים אוטומטית"""
    logger.info("Starting automatic topics check...")
    
    topics = db.get_active_topics_for_check()
    
    for topic in topics:
        try:
            # חיפוש תוצאות
            results = await bing_api.search(topic['search_query'])
            
            if results:
                # סינון תוצאות עם GPT
                filtered_results = smart_watcher.filter_results_with_gpt(topic['topic'], results)
                
                # שמירת תוצאות חדשות ושליחה
                for result in filtered_results[:3]:  # מקסימום 3 תוצאות
                    result_id = db.save_result(
                        topic['id'],
                        result['title'],
                        result['url'],
                        result['snippet']
                    )
                    
                    if result_id:  # תוצאה חדשה
                        # שליחת התראה למשתמש
                        message = f"""
🔔 **עדכון חדש עבור: {topic['topic']}**

📰 {result['title']}

📝 {result['snippet']}

🔗 [קישור לכתבה]({result['url']})
"""
                        
                        try:
                            await context.bot.send_message(
                                chat_id=topic['user_id'],
                                text=message,
                                parse_mode='Markdown',
                                disable_web_page_preview=False
                            )
                        except Exception as e:
                            logger.error(f"Failed to send message to user {topic['user_id']}: {e}")
            
            # עדכון זמן הבדיקה
            db.update_topic_checked(topic['id'])
            
        except Exception as e:
            logger.error(f"Error checking topic {topic['id']}: {e}")
    
    logger.info(f"Finished checking {len(topics)} topics")

# פונקציות callback
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """טיפול בלחיצות כפתורים"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "refresh_list":
        # רענון רשימת הנושאים
        await list_command(update, context)
    elif query.data == "add_topic":
        await query.edit_message_text("השתמשו בפקודה: /watch <נושא חדש>")

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
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # הוספת מתזמן למשימות אוטומטיות
    scheduler = AsyncIOScheduler()
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

if __name__ == "__main__":
    main()
