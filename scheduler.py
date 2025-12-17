import time
import schedule
from app import send_daily_excel_report

def daily_job():
    recipients = [
        "suraj.sakhare@payppy.co",
        "satyen.aghor@payppy.co",
        "sakshi.aghor@payppy.co",
        "mohini.aghor@payppy.co"
    ]
    print("Sending daily Excel report…")
    send_daily_excel_report(recipients)

# 7:30 AM IST = 02:00 UTC
schedule.every().day.at("02:00").do(daily_job)

print("Pepmo Scheduler started…")

while True:
    schedule.run_pending()
    time.sleep(30)