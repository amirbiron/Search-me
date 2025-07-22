# שימוש בתמונת Python רשמית
FROM python:3.11-slim

# הגדרת תיקיית עבודה
WORKDIR /app

# העתקת קבצי התלויות
COPY requirements.txt .

# התקנת תלויות
RUN pip install --no-cache-dir -r requirements.txt

# העתקת קבצי הפרויקט
COPY . .

# יצירת תיקיית נתונים
RUN mkdir -p /var/data

# הגדרת הרשאות
RUN chmod 755 /var/data

# חשיפת הפורט (דינמי)
EXPOSE $PORT

# הפעלת הבוט עם משתנה הסביבה PORT
CMD python main.py
