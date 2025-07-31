# הגדרת MongoDB עבור WatchBot

## סקירה כללית

המערכת תומכת כעת בשני סוגי בסיסי נתונים:
- **SQLite** (ברירת מחדל) - לפיתוח מקומי ופרויקטים קטנים
- **MongoDB** - לסביבות ייצור ופרויקטים גדולים

## הגדרת משתני הסביבה

### עבור MongoDB:
```bash
# חובה - כתובת חיבור ל-MongoDB
MONGODB_URI=mongodb://localhost:27017/
# או עבור MongoDB Atlas:
# MONGODB_URI=mongodb+srv://username:password@cluster.mongodb.net/

# שם בסיס הנתונים
MONGODB_DB_NAME=watchbot

# הפעלת MongoDB (במקום SQLite)
USE_MONGODB=true
```

### עבור SQLite (ברירת מחדל):
```bash
USE_MONGODB=false
# או פשוט לא להגדיר את המשתנה
```

## הגדרה ברנדר (Render)

עדכן את קובץ `render.yaml` או הגדר במשתני הסביבה של רנדר:

```yaml
envVars:
  - key: MONGODB_URI
    sync: false  # הגדר בממשק רנדר
  - key: MONGODB_DB_NAME
    value: watchbot
  - key: USE_MONGODB
    value: true  # שנה ל-true כדי להפעיל MongoDB
```

## פונקציונליות Recent Users

### מה הפונקציה עושה:
- מחפשת משתמשים שהיו פעילים בשבוע האחרון
- מציגה כמות נושאים שנוספו
- מציגה שימוש חודשי
- מציגה תאריכי פעילות

### במבנה MongoDB:
הנתונים מאוחסנים ב-3 קולקשנים:
- `users` - פרטי משתמשים
- `watch_topics` - נושאים למעקב
- `usage_stats` - סטטיסטיקות שימוש

### שאילתת MongoDB:
הפונקציה משתמשת ב-Aggregation Pipeline מתקדם עם:
- `$lookup` - איחוד נתונים מקולקשנים שונים
- `$filter` - סינון נתונים לפי תאריך
- `$project` - עיצוב התוצאות

## בדיקת הפונקציונליות

הרץ את סקריפט הבדיקה:
```bash
python test_mongodb_recent_users.py
```

הסקריפט יבדוק:
- חיבור ל-MongoDB
- יצירת נתוני בדיקה
- הרצת שאילתת recent users
- הצגת התוצאות

## פתרון בעיות

### שגיאות חיבור:
1. וודא ש-MongoDB פועל
2. בדוק את ה-URI
3. וודא הרשאות גישה

### שגיאות אינדקס:
הפונקציה יוצרת אינדקסים אוטומטית, אבל אם יש בעיה:
```javascript
// בחיבור MongoDB ישיר:
db.users.createIndex({user_id: 1}, {unique: true})
db.watch_topics.createIndex({user_id: 1})
db.watch_topics.createIndex({created_at: 1})
```

## ביצועים

### SQLite vs MongoDB:
- **SQLite**: מהיר לפרויקטים קטנים, קובץ יחיד
- **MongoDB**: מהיר יותר לנתונים גדולים, תמיכה בשאילתות מורכבות

### אופטימיזציה:
- האינדקסים נוצרים אוטומטית
- השאילתות מותאמות לביצועים
- תמיכה בסינון מתקדם

## מעבר מ-SQLite ל-MongoDB

1. הגדר משתני סביבה
2. הרץ מיגרציה (אם נדרש)
3. שנה `USE_MONGODB=true`
4. הפעל מחדש את הבוט

## תמיכה

אם יש בעיות:
1. בדוק את הלוגים
2. הרץ את סקריפט הבדיקה
3. וודא הגדרות משתני סביבה