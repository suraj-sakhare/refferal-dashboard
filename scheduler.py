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

schedule.every().day.at("18:32").do(daily_job)

print("Pepmo Scheduler started…")

while True:
    schedule.run_pending()
    time.sleep(30)