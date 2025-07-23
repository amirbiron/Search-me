# 🚀 מדריך פריסה מהיר

## 📋 רשימת בדיקות לפני פריסה

### ✅ הכנת המפתחות
- [ ] יצירת בוט טלגרם ב-@BotFather
- [ ] קבלת OpenAI API Key (עם אשראי ותמיכה ב-browsing)
- [ ] קבלת Telegram User ID שלכם (שלחו /start ל-@userinfobot)

### ✅ הכנת הקוד
- [ ] Fork הפרויקט ל-GitHub שלכם
- [ ] Clone לפרויקט המקומי
- [ ] בדיקה שכל הקבצים קיימים:
  - `main.py`
  - `requirements.txt`
  - `Dockerfile`
  - `render.yaml`
  - `.gitignore`

### ✅ בדיקה מקומית (אופציונלי)
```bash
# יצירת סביבה וירטואלית
python -m venv venv
source venv/bin/activate  # Linux/Mac
# או
venv\Scripts\activate     # Windows

# התקנת תלויות
pip install -r requirements.txt

# הגדרת משתני סביבה
export BOT_TOKEN="your_bot_token"
export OPENAI_API_KEY="your_openai_key"
export ADMIN_ID="your_telegram_id"
export DB_PATH="./test.db"
export PORT="5000"

# הפעלת הבוט
python main.py
```

## 🌐 פריסה ב-Render

### שלב 1: יצירת שירות
1. היכנסו ל-[Render.com](https://render.com)
2. לחצו "New" → "Web Service"
3. חברו את המאגר מ-GitHub
4. בחרו את הפרויקט שלכם

### שלב 2: הגדרות בסיסיות
```
Name: watch-bot
Region: Frankfurt (או הקרוב אליכם)
Branch: main
Runtime: Docker
```

### שלב 3: משתני סביבה
הוסיפו במקטע "Environment Variables":

| Key | Value |
|-----|-------|
| `BOT_TOKEN` | Token מ-@BotFather |
| `OPENAI_API_KEY` | Key מ-OpenAI (עם browsing) |
| `ADMIN_ID` | ה-User ID שלכם |

### שלב 4: אחסון קבוע
1. גללו ל-"Persistent Disks"
2. לחצו "Add Disk"
3. הגדירו:
   ```
   Name: watchbot-data
   Mount Path: /var/data
   Size: 1 GB
   ```

### שלב 5: פריסה
1. לחצו "Create Web Service"
2. המתינו לבניית הקונטיינר (~3-5 דקות)
3. בדקו שהסטטוס הפך ל-"Live"

## 🔍 בדיקת תקינות

### בדיקות ראשוניות
1. **URL הבוט פעיל**: לכו ל-URL שקיבלתם מ-Render
2. **תגובה JSON**: צריכה להיות תגובה עם `"status": "Bot is running"`
3. **בוט מגיב**: שלחו `/start` בטלגרם

### בדיקות מתקדמות
```bash
# בדיקת health check
curl https://your-bot-url.onrender.com/health

# בדיקת הסטטוס הכללי
curl https://your-bot-url.onrender.com/
```

### פקודות לבדיקה בבוט
1. `/start` - הודעת ברוכים הבאים
2. `/watch Galaxy Tab S11 Ultra` - הוספת נושא לבדיקה
3. `/list` - צפייה ברשימה
4. `/stats` - סטטיסטיקות (אדמין בלבד)

## 🚨 פתרון תקלות נפוצות

### "Application exited early"
```bash
# בדקו לוגים ב-Render
# סביר שמשתנה סביבה חסר או שגוי
```

### "No open ports detected"
```bash
# ודאו שהשרת Flask רץ
# בדקו שהפורט נחשף נכון ב-Dockerfile
```

### "ModuleNotFoundError"
```bash
# ודאו שכל השינויים ב-git push
git add .
git commit -m "Fix dependencies"
git push origin main
```

### "telegram.error.Conflict"
```bash
# עצרו בוטים אחרים שרצים עם אותו Token
# או שלחו /revoke ל-@BotFather ואז /token
```

## 📊 ניטור ותחזוקה

### בדיקת לוגים
1. Render Dashboard → Service → Logs
2. חפשו שגיאות בצבע אדום
3. בדקו הודעות INFO לתקינות

### עדכון הבוט
```bash
# במקומי
git pull origin main
# ערכו קבצים
git add .
git commit -m "Update bot"
git push origin main

# ב-Render: פרסום אוטומטי יתחיל
```

### מעקב עלויות
- **Render**: בדקו שעות שימוש בחינמית
- **OpenAI**: עקבו אחרי usage ב-dashboard
- **Bing**: 1000 שאילתות חינם/חודש

## 🎯 טיפים לאופטימיזציה

### ביצועים
- השתמשו בנושאים ספציפיים (פחות תוצאות = פחות עלויות)
- הגדירו intervals ארוכים יותר לנושאים לא דחופים

### עלויות
- החלו עם תוכנית החינמית
- עקבו אחרי שימוש ב-API
- שקלו שדרוג רק אם צריך

### אבטחה
- שנו מפתחות API מעת לעת
- הגבילו גישת אדמין למזהה שלכם בלבד
- עקבו אחרי לוגים לפעילות חריגה

---

**בהצלחה עם הפריסה! 🚀**

אם יש בעיות, בדקו את הלוגים ב-Render ואת מדריך פתרון התקלות ב-README.
