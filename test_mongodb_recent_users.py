#!/usr/bin/env python3
"""
סקריפט בדיקה לפונקציונליות recent users עם MongoDB
"""
import os
import sys
from datetime import datetime, timedelta
from pymongo import MongoClient

# הוספת הנתיב הנוכחי ל-Python path
sys.path.append('.')

def test_mongodb_connection():
    """בדיקת חיבור ל-MongoDB"""
    try:
        # ברירת מחדל לבדיקה מקומית
        uri = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
        db_name = os.getenv('MONGODB_DB_NAME', 'watchbot_test')
        
        print(f"מתחבר ל-MongoDB: {uri}")
        client = MongoClient(uri)
        
        # בדיקת החיבור
        client.admin.command('ping')
        print("✅ חיבור ל-MongoDB הצליח!")
        
        # בדיקת בסיס הנתונים
        db = client[db_name]
        print(f"✅ בסיס נתונים: {db_name}")
        
        return client, db
        
    except Exception as e:
        print(f"❌ שגיאה בחיבור ל-MongoDB: {e}")
        return None, None

def create_test_data(db):
    """יצירת נתוני בדיקה"""
    try:
        users_collection = db.users
        topics_collection = db.watch_topics
        usage_collection = db.usage_stats
        
        # מחיקת נתונים קיימים
        users_collection.delete_many({})
        topics_collection.delete_many({})
        usage_collection.delete_many({})
        
        # יצירת משתמשים לדוגמה
        now = datetime.now()
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)
        
        test_users = [
            {"user_id": 1, "username": "test_user_1", "is_active": True, "created_at": now - timedelta(days=2)},
            {"user_id": 2, "username": "test_user_2", "is_active": True, "created_at": week_ago + timedelta(days=1)},
            {"user_id": 3, "username": "old_user", "is_active": True, "created_at": month_ago},
        ]
        
        users_collection.insert_many(test_users)
        print("✅ נוצרו משתמשי בדיקה")
        
        # יצירת נושאים לדוגמה
        test_topics = [
            {"user_id": 1, "topic": "AI news", "created_at": now - timedelta(days=1), "is_active": True},
            {"user_id": 1, "topic": "Tech updates", "created_at": now - timedelta(days=3), "is_active": True},
            {"user_id": 2, "topic": "Sports", "created_at": week_ago + timedelta(days=2), "is_active": True},
            {"user_id": 3, "topic": "Old topic", "created_at": month_ago, "is_active": True},
        ]
        
        topics_collection.insert_many(test_topics)
        print("✅ נוצרו נושאי בדיקה")
        
        # יצירת נתוני שימוש
        current_month = now.strftime("%Y-%m")
        test_usage = [
            {"user_id": 1, "month": current_month, "usage_count": 15},
            {"user_id": 2, "month": current_month, "usage_count": 8},
            {"user_id": 3, "month": current_month, "usage_count": 2},
        ]
        
        usage_collection.insert_many(test_usage)
        print("✅ נוצרו נתוני שימוש לבדיקה")
        
        return True
        
    except Exception as e:
        print(f"❌ שגיאה ביצירת נתוני בדיקה: {e}")
        return False

def test_recent_users_query(db):
    """בדיקת שאילתת משתמשים אחרונים"""
    try:
        from main import WatchBotMongoDB
        
        # יצירת אובייקט הבסיס נתונים
        uri = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
        db_name = os.getenv('MONGODB_DB_NAME', 'watchbot_test')
        
        mongo_db = WatchBotMongoDB(uri, db_name)
        
        # קריאה לפונקציה
        recent_users = mongo_db.get_recent_users_activity()
        
        print(f"\n📊 נמצאו {len(recent_users)} משתמשים אחרונים:")
        print("-" * 50)
        
        for i, user in enumerate(recent_users, 1):
            print(f"{i}. {user['username']} (ID: {user['user_id']})")
            print(f"   📝 נושאים נוספו: {user['topics_added']}")
            print(f"   🔍 שימוש החודש: {user['usage_count']}")
            print(f"   📅 תאריכי פעילות: {user['activity_dates']}")
            print()
        
        return len(recent_users) > 0
        
    except Exception as e:
        print(f"❌ שגיאה בבדיקת שאילתת משתמשים אחרונים: {e}")
        return False

def main():
    """פונקציה ראשית"""
    print("🧪 בדיקת פונקציונליות Recent Users עם MongoDB")
    print("=" * 60)
    
    # בדיקת חיבור
    client, db = test_mongodb_connection()
    if not client:
        return
    
    # יצירת נתוני בדיקה
    if not create_test_data(db):
        return
    
    # בדיקת השאילתה
    if test_recent_users_query(db):
        print("✅ כל הבדיקות עברו בהצלחה!")
    else:
        print("❌ הבדיקה נכשלה")
    
    # ניקוי
    client.close()

if __name__ == "__main__":
    main()