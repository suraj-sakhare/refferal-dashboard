from flask import Flask, Response, render_template, request, redirect, url_for, jsonify, send_file, session
import requests, json, io, os, csv, smtplib, schedule, threading, time
from datetime import date, datetime, timedelta
from openpyxl import Workbook
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders

app = Flask(__name__)
API_BASE_URL = "https://nexus.payppy.app/api/referral-dashboard"

app.secret_key = "pepmo-admin-dashboard-secret-key"  # required for session

# ensure tmp directory exists
os.makedirs("tmp", exist_ok=True)

@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        referral_code = request.form.get("referral_code")
        return redirect(url_for('show_dashboard', referral_code=referral_code))
    return render_template("home.html")


@app.route("/dashboard/<referral_code>")
def show_dashboard(referral_code):
    try:
        api_url = f"{API_BASE_URL}/{referral_code}"
        response = requests.get(api_url)

        if response.status_code == 200:
            data = response.json()
            return render_template("dashboard.html", data=data)
        else:
            return render_template("dashboard.html", error="Referral code not found.")
    except Exception as e:
        return render_template("dashboard.html", error=str(e))


API_URL = "https://nexus.payppy.app/api/dashboard/v2/voucher-transactions"
DETAIL_API_URL = "https://nexus.payppy.app/api/dashboard/v2/voucher-transactions"

TRANSACTION_CACHE = []   # stored for drawer merging

# ---------------------------------------------------------
# 🧠 Get Previous Day Closing Balance
# ---------------------------------------------------------
def get_previous_day_closing_balance(current_date_str):
    try:
        current_date = datetime.strptime(current_date_str, "%Y-%m-%d").date()
    except:
        current_date = datetime.strptime(current_date_str, "%d/%m/%Y").date()

    prev_date = current_date - timedelta(days=1)
    prev_date_str = prev_date.strftime("%Y-%m-%d")

    url = f"{API_URL}?date={prev_date_str}&provider=pinelabs"

    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None

        data = r.json().get("data", [])
        if not data:
            return None

        # newest → oldest
        data.sort(
            key=lambda x: datetime.strptime(f"{x['date']} {x['time']}", "%Y-%m-%d %H:%M:%S"),
            reverse=True
        )

        for txn in data:
            if txn.get("voucher_status") == "SUCCESS":
                return float(txn.get("svc_balance"))

        return None

    except:
        return None

# ---------------------------------------------------------
# 🔎 Fetch provider data (Pinelabs or Gyftr)
# ---------------------------------------------------------
def fetch_provider_data(query_date, provider_param=None):
    if provider_param == "gyftr":
        url = f"{API_URL}?date={query_date}&provider=gyftr"
    else:
        url = f"{API_URL}?date={query_date}"

    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            return r.json()
        return {"data": [], "total_amount": 0, "total_volume": 0}
    except:
        return {"data": [], "total_amount": 0, "total_volume": 0}



@app.route("/voucher-transactions")
def voucher_transactions():
    query_date = request.args.get("date", str(date.today()))
    provider = request.args.get("provider")

    # ----------------------------
    # Combine data if provider=all
    # ----------------------------
    if provider == "all":
        d1 = fetch_provider_data(query_date, "pinelabs")
        d2 = fetch_provider_data(query_date, "gyftr")

        for t in d1.get("data", []):
            t["provider"] = "pinelabs"
        for t in d2.get("data", []):
            t["provider"] = "gyftr"

        all_txns = d1["data"] + d2["data"]

        total_amount = float(d1.get("total_amount", 0)) + float(d2.get("total_amount", 0))
        total_volume = float(d1.get("total_volume", 0)) + float(d2.get("total_volume", 0))

        data = {
            "data": all_txns,
            "total_amount": total_amount,
            "total_volume": total_volume,
        }
    else:
        data = fetch_provider_data(query_date, provider)
        for t in data.get("data", []):
            t["provider"] = provider if provider else "pinelabs"

    # ----------------------------
    # Sort newest → oldest for display
    # ----------------------------
    transactions = data.get("data", [])
    for txn in transactions:
        txn["_dt"] = datetime.strptime(
            f"{txn['date']} {txn['time']}",
            "%Y-%m-%d %H:%M:%S"
        )
    transactions.sort(key=lambda x: x["_dt"], reverse=True)

    # ==========================================================
    # 🧠 PINELABS BALANCE + DEPOSIT LOGIC  (oldest → newest)
    # ==========================================================
    pinelabs_txns = [t for t in transactions if t["provider"] == "pinelabs"]

    # process in chronological order
    pinelabs_sorted = sorted(pinelabs_txns, key=lambda x: x["_dt"])

    prev_closing = get_previous_day_closing_balance(query_date)  # can be None
    balance_map = {}

    for txn in pinelabs_sorted:
        raw_svc = txn.get("svc_balance")

        try:
            svc_balance = float(raw_svc)
        except (TypeError, ValueError):
            svc_balance = None

        # ----- Opening / Closing -----
        opening = prev_closing  # first one might be None, that's fine

        if svc_balance is None:
            # if API failed to send svc_balance, assume no change
            closing = opening
        else:
            closing = svc_balance

        # ----- Deposit calculation (KEY FIX) -----
        deposit = 0
        try:
            if opening is not None and closing is not None:
                svc_ded = float(txn.get("svc_deduction") or 0)

                expected_closing = opening - svc_ded
                diff = closing - expected_closing

                # small epsilon to avoid float noise
                if diff > 0.0001:
                    deposit = diff
        except Exception:
            deposit = 0

        prev_closing = closing  # update running balance

        balance_map[txn["order_id"]] = {
            "opening": opening,
            "closing": closing,
            "deposit": deposit,
        }

    # ----------------------------
    # Apply balances to all txns
    # ----------------------------
    for txn in transactions:
        if txn["provider"] == "pinelabs":
            bal = balance_map.get(txn["order_id"], {})
            txn["opening_balance"] = bal.get("opening")
            txn["closing_balance"] = bal.get("closing")
            txn["deposit"] = bal.get("deposit", 0)
        else:
            txn["opening_balance"] = None
            txn["closing_balance"] = None
            txn["deposit"] = None

    # ----------------------------
    # Top card latest SVC balance
    # ----------------------------
    pin_only = [t for t in transactions if t["provider"] == "pinelabs"]
    latest_svc_balance = pin_only[0]["closing_balance"] if pin_only else 0

    global TRANSACTION_CACHE
    TRANSACTION_CACHE = transactions

    def fmt(n):
        try:
            return f"{float(n):,.1f}"
        except Exception:
            return n

    return render_template(
        "voucher_dashboard.html",
        data={**data, "data": transactions},
        query_date=query_date,
        provider=provider,
        latest_svc_balance=fmt(latest_svc_balance),
        total_amount=fmt(data.get("total_amount", 0)),
        total_volume=fmt(data.get("total_volume", 0)),
    )

# ---------------------------------------------------------
# 🧠 Drawer — Single Voucher Merge API
# ---------------------------------------------------------
@app.route("/single-voucher-transactions/<user_id>/<order_id>")
def voucher_transaction_detail(user_id, order_id):
    provider = request.args.get("provider")

    api_url = f"{DETAIL_API_URL}/{user_id}/{order_id}"
    if provider == "gyftr":
        api_url += "?provider=gyftr"

    try:
        detail = requests.get(api_url, timeout=30).json()
    except:
        return jsonify({"error": "Detail API failed"}), 500

    matched = next((t for t in TRANSACTION_CACHE if t["order_id"] == order_id), None)

    if not matched:
        return jsonify(detail)

    merged = {
        **detail,
        **{
            "order_id": matched.get("order_id"),
            "date": matched.get("date"),
            "time": matched.get("time"),
            "timestamp": f"{matched.get('date')} {matched.get('time')}",

            "provider": matched.get("provider"),
            "brand": matched.get("brand"),
            "user_name": matched.get("user_name"),

            "requested_amount": matched.get("requested_amount"),
            "paid_by_user": matched.get("paid_by_user"),
            "denomination": matched.get("denomination"),
            "qty": matched.get("qty"),

            "payment_method": matched.get("payment_method"),
            "payment_status": matched.get("payment_status"),
            "voucher_status": matched.get("voucher_status"),
            "refund_status": matched.get("refund_status"),

            "svc_balance": matched.get("svc_balance"),
            "svc_deduction": matched.get("svc_deduction"),
            "opening_balance": matched.get("opening_balance"),
            "closing_balance": matched.get("closing_balance"),
            "deposit": matched.get("deposit", 0),
        }
    }

    return jsonify(merged)

# ---------------------------------------------------------
# 🧠 Export Voucher Transactions to CSV
# ---------------------------------------------------------
@app.route("/voucher-transactions/export")
def export_voucher_transactions():
    """
    Download current dashboard data as CSV.

    Uses TRANSACTION_CACHE, so it exports whatever was last
    loaded on the /voucher-transactions page (same date/provider filter).
    """
    if not TRANSACTION_CACHE:
        return "No data to export. Please open /voucher-transactions first.", 400

    # You can tweak column order / fields as you like
    fieldnames = [
        "date",
        "time",
        "order_id",
        "provider",
        "user_name",
        "brand",
        "denomination",
        "qty",
        "requested_amount",
        "paid_by_user",
        "svc_deduction",
        "opening_balance",
        "closing_balance",
        "deposit",
        "payment_method",
        "payment_status",
        "voucher_status",
        "refund_status",
        "svc_balance",
    ]

    output = io.StringIO()
    writer = csv.writer(output)

    # header row
    writer.writerow(fieldnames)

    for txn in TRANSACTION_CACHE:
        row = [
            txn.get("date"),
            txn.get("time"),
            txn.get("order_id"),
            txn.get("provider"),
            txn.get("user_name"),
            txn.get("brand"),
            txn.get("denomination"),
            txn.get("qty"),
            txn.get("requested_amount"),
            txn.get("paid_by_user"),
            txn.get("svc_deduction"),
            txn.get("opening_balance"),
            txn.get("closing_balance"),
            txn.get("deposit"),
            txn.get("payment_method"),
            txn.get("payment_status"),
            txn.get("voucher_status"),
            txn.get("refund_status"),
            txn.get("svc_balance"),
        ]
        writer.writerow(row)

    csv_data = output.getvalue()
    output.close()

    filename = f"voucher-transactions-{date.today().isoformat()}.csv"

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}"
        },
    )

# ---------------------------------------------------------
# 🧠 Export Voucher Transactions to EXCEL For Email
# ---------------------------------------------------------
def generate_excel(transactions):
    wb = Workbook()
    ws = wb.active
    ws.title = "Transactions"

    headers = [
        "date", "time", "order_id", "provider", "user_name",
        "brand", "denomination", "qty", "requested_amount",
        "paid_by_user", "svc_deduction", "opening_balance",
        "closing_balance", "deposit", "payment_method",
        "payment_status", "voucher_status", "refund_status",
        "svc_balance"
    ]

    ws.append(headers)

    for txn in transactions:
        ws.append([
            txn.get("date"),
            txn.get("time"),
            txn.get("order_id"),
            txn.get("provider"),
            txn.get("user_name"),
            txn.get("brand"),
            txn.get("denomination"),
            txn.get("qty"),
            txn.get("requested_amount"),
            txn.get("paid_by_user"),
            txn.get("svc_deduction"),
            txn.get("opening_balance"),
            txn.get("closing_balance"),
            txn.get("deposit"),
            txn.get("payment_method"),
            txn.get("payment_status"),
            txn.get("voucher_status"),
            txn.get("refund_status"),
            txn.get("svc_balance"),
        ])

    # save to memory
    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()


# ---------------------------------------------------------
# 🧠 Send Email with Attachment
# ---------------------------------------------------------
def send_email_with_attachment(to_email, subject, body, file_bytes, filename):
    SMTP_HOST = "smtp.hostinger.com"
    SMTP_PORT = 587
    SMTP_USER = "user@pepmo.app"
    SMTP_PASS = "user@White#Cloud&9357"

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg["Subject"] = subject

    msg.attach(MIMEText(body, "html"))

    part = MIMEBase("application", "octet-stream")
    part.set_payload(file_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f"attachment; filename={filename}")

    msg.attach(part)

    server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.send_message(msg)
    server.quit()

# ---------------------------------------------------------
# 🧠 Fetch Yesterday's Transactions
# ---------------------------------------------------------
def fetch_yesterday_transactions():
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

    # pinelabs + gyftr
    d1 = fetch_provider_data(yesterday, "pinelabs")
    d2 = fetch_provider_data(yesterday, "gyftr")

    for t in d1.get("data", []): t["provider"] = "pinelabs"
    for t in d2.get("data", []): t["provider"] = "gyftr"

    all_txns = d1["data"] + d2["data"]
    return all_txns, yesterday

# ---------------------------------------------------------
# 🧠 Enrich Transactions with Balance and Deposit Logic
# ---------------------------------------------------------
def enrich_with_balance_and_deposit(transactions, query_date):
    # Use EXACT SAME LOGIC from voucher_transactions route    
    pinelabs_txns = [t for t in transactions if t["provider"] == "pinelabs"]
    pinelabs_sorted = sorted(pinelabs_txns, key=lambda x: datetime.strptime(f"{x['date']} {x['time']}", "%Y-%m-%d %H:%M:%S"))

    prev_closing = get_previous_day_closing_balance(query_date)
    balance_map = {}

    for i, txn in enumerate(pinelabs_sorted):
        # Safe svc balance parse
        is_success = txn.get("voucher_status") == "SUCCESS"
        raw_svc = txn.get("svc_balance")
        svc_balance = float(raw_svc) if is_success and raw_svc not in [None, ""] else None

        # First txn
        if i == 0:
            opening = prev_closing
            closing = svc_balance if svc_balance is not None else opening
            prev_closing = closing
            balance_map[txn["order_id"]] = {"opening": opening, "closing": closing, "deposit": 0}
            continue

        # Next ones
        opening = prev_closing
        closing = svc_balance if svc_balance is not None else opening
        prev_closing = closing

        # Deposit detection
        prev_txn = pinelabs_sorted[i - 1]
        prev_bal = balance_map.get(prev_txn["order_id"], {}).get("closing")

        deposit = 0
        if (
            opening is not None and
            prev_bal is not None and
            opening > prev_bal and
            str(txn.get("svc_deduction")) in ["0", "0.0", None, "None"]
        ):
            deposit = opening - prev_bal

        balance_map[txn["order_id"]] = {
            "opening": opening,
            "closing": closing,
            "deposit": deposit
        }

    # Attach results
    for txn in transactions:
        if txn["provider"] == "pinelabs":
            bal = balance_map.get(txn["order_id"], {})
            txn["opening_balance"] = bal.get("opening")
            txn["closing_balance"] = bal.get("closing")
            txn["deposit"] = bal.get("deposit", 0)
        else:
            txn["opening_balance"] = None
            txn["closing_balance"] = None
            txn["deposit"] = None

    return transactions

# ---------------------------------------------------------
# 🧠 Fetch Yesterday's Transactions and Generate Report
# ---------------------------------------------------------
def send_daily_excel_report(recipients):
    txns, date_str = fetch_yesterday_transactions()
    if not txns:
        return {"status": "No data"}

    txns = enrich_with_balance_and_deposit(txns, date_str)
    sorted_txns = sorted(txns, key=lambda x: datetime.strptime(f"{x['date']} {x['time']}", "%Y-%m-%d %H:%M:%S"), reverse=True)
    excel_file = generate_excel(sorted_txns)

    subject = f"Pepmo Daily Transactions Report — {date_str}"
    body = f"""
        <h3>Pepmo Daily Report</h3>
        <p>Attached is the Excel report for <b>{date_str}</b>.</p>
    """

    for email in recipients:
        send_email_with_attachment(
            email,
            subject,
            body,
            excel_file,
            f"Pepmo_Report_{date_str}.xlsx"
        )

    return {"status": "sent"}

# Test route to trigger email sending
@app.route("/test-send-report")
def test_send_report():
    recipients = [
        "suraj.sakhare@payppy.co",
    ]
    result = send_daily_excel_report(recipients)
    return jsonify(result)

# ---------------------------------------------------------
# 🧠 Schedule Daily Report at Midnight
# ---------------------------------------------------------
def schedule_midnight_report():
    print("LIVE MODE: Running daily at 00:00")
    schedule.every().day.at("00:00").do(
        lambda: send_daily_excel_report([
            "suraj.sakhare@payppy.co"
        ])
    )

    while True:
        schedule.run_pending()
        time.sleep(30)

# # ----------------- Helpers -----------------
# def fmt_money(v):
#     try:
#         return f"{float(v):,.2f}"
#     except Exception:
#         return v

# def fmt_int(v):
#     try:
#         return f"{int(v):,}"
#     except Exception:
#         return v

# USERS_API_URL = "http://127.0.0.1:5001/api/dashboard/v2/users"

# # Users list
# @app.route("/users")
# def users():
#     # Support simple pagination if upstream adds cursor in future
#     resp = requests.get(USERS_API_URL)
#     payload = resp.json()

#     rows = payload.get("data", [])
#     # format values for UI
#     for u in rows:
#         u["_total_spent_fmt"] = fmt_money(u.get("total_spent", 0))
#         u["_last_amt_fmt"] = fmt_money(u.get("last_txn_amt", 0))
#         # normalize phone
#         if not u.get("phone") or u["phone"] == "N/A":
#             u["phone"] = "N/A"

#     return render_template(
#         "users_dashboard.html",
#         active_tab="users",
#         users=rows,
#         count=payload.get("count", 0),
#         next_cursor=payload.get("next_cursor"),
#         scanned_txns=payload.get("scanned_txns"),
#     )


# USER_OVERVIEW_API_BASE = "http://127.0.0.1:5001/api/dashboard/v2/users"
# @app.route("/api/dashboard/v2/users/<user_id>/overview")
# def user_overview(user_id):
#     cursor = request.args.get("cursor")  # optional for pagination
#     upstream = f"{USER_OVERVIEW_API_BASE}/{user_id}/overview"
#     try:
#         r = requests.get(upstream, params={"cursor": cursor} if cursor else None, timeout=15)
#         if r.status_code != 200:
#             return {"error": f"Upstream error {r.status_code}"}, 400
#         return r.json()
#     except requests.RequestException as e:
#         return {"error": f"Request failed: {e}"}, 400    


# --------------------------------------
# 🧮 Customer Segregation Dashboard Page
# --------------------------------------
CUSTOMER_SEGREGATION_API = (
    "https://nexus.payppy.app/api/dashboard/v2/customer-segregation"
)

@app.route("/customer-segregation")
def customer_segregation():
    try:
        resp = requests.get(CUSTOMER_SEGREGATION_API, timeout=30)
        if resp.status_code != 200:
            return render_template(
                "customer_segregation.html",
                error=f"Upstream error {resp.status_code}"
            )
        data = resp.json().get("customerSegregation", [])
        return render_template("customer_segregation.html", data=data)
    except Exception as e:
        return render_template("customer_segregation.html", error=str(e))


# --------------------------------------
# 🧠 User Cohort Analytics Dashboard
# --------------------------------------
@app.route("/user-cohorts")
def user_cohorts():
    try:
        # call your API that now returns the precomputed rows
        resp = requests.get("https://nexus.payppy.app/api/dashboard/v2/user-cohorts", timeout=60)
        if resp.status_code != 200:
            return render_template("user_cohorts.html", error=f"Upstream error {resp.status_code}")
        data = resp.json().get("userCohorts", []) or []
        return render_template("user_cohorts.html", data=data)
    except Exception as e:
        return render_template("user_cohorts.html", error=str(e))


API_BASE = "https://nexus.payppy.app/api"

@app.route("/notification", methods=["GET"])
def notification():
    return render_template("send_notification.html")

 # ------------------------------------------------
# 🔹 Send notification to ALL users
# ------------------------------------------------
@app.route("/send/all", methods=["POST"])
def send_to_all():
    title = request.form.get("title")
    body = request.form.get("body")
    data_payload_raw = request.form.get("data_payload")  # raw text

    try:
        # Safely parse JSON payload
        try:
            data_payload = json.loads(data_payload_raw) if data_payload_raw else {}
        except json.JSONDecodeError:
            return render_template("send_notification.html", error_message="❌ Invalid JSON in Data Payload")

        payload = {
            "title": title,
            "body": body,
            "data_payload": data_payload
        }

        resp = requests.post(f"{API_BASE}/notifications/broadcast", json=payload)
        result = resp.json()

        if resp.status_code == 200:
            return render_template(
                "send_notification.html",
                success_message="✅ Broadcast sent successfully!",
                result=result
            )
        else:
            return render_template(
                "send_notification.html",
                error_message=f"❌ Failed: {result}"
            )

    except Exception as e:
        return render_template("send_notification.html", error_message=f"Error: {str(e)}")

# ------------------------------------------------
# 🔹 Send notification to SPECIFIC user
# ------------------------------------------------
@app.route("/send/user", methods=["POST"])
def send_to_user():
    user_id = request.form.get("user_id")
    title = request.form.get("title")
    body = request.form.get("body")
    data_payload_raw = request.form.get("data_payload")  # raw text

    try:
        # Safely parse JSON payload
        try:
            data_payload = json.loads(data_payload_raw) if data_payload_raw else {}
        except json.JSONDecodeError:
            return render_template("send_notification.html", error_message="❌ Invalid JSON in Data Payload")
        
        payload = {
            "user_id": user_id,
            "title": title,
            "body": body,
            "data_payload": data_payload
        }
        resp = requests.post(f"{API_BASE}/notifications/user", json=payload)
        result = resp.json()
        if resp.status_code == 200:
            return render_template("send_notification.html", success_message="✅ Notification sent to user successfully!", result=result)
        else:
            return render_template("send_notification.html", error_message=f"❌ Failed: {result}")
    except Exception as e:
        return render_template("send_notification.html", error_message=f"Error: {str(e)}")
    

# ------------------------------------------------
# 🔹 DELETE Elastic Search data
# ------------------------------------------------
@app.route("/elastic/delete", methods=["POST"])
def delete_elastic():
    try:
        resp = requests.delete("https://nexus.payppy.app/api/delete_elastic_data?index=strapi_gift_card_brands")
        result = resp.json()
        if resp.status_code == 200:
            return render_template("send_notification.html", 
                                   success_elastic="🗑️ Deleted Elastic data successfully!",
                                   elastic_result=result)
        else:
            return render_template("send_notification.html", 
                                   error_elastic=f"❌ Failed: {result}")
    except Exception as e:
        return render_template("send_notification.html", 
                               error_elastic=f"Error: {str(e)}")


# ------------------------------------------------
# 🔹 FETCH & UPDATE Elastic Search (Strapi → ES)
# ------------------------------------------------
@app.route("/elastic/update", methods=["POST"])
def update_elastic():
    try:
        resp = requests.post("https://nexus.payppy.app/api/strapi-data")
        result = resp.json()
        if resp.status_code == 200:
            return render_template("send_notification.html", 
                                   success_elastic="🔄 Elastic data updated successfully!",
                                   elastic_result=result)
        else:
            return render_template("send_notification.html", 
                                   error_elastic=f"❌ Failed: {result}")
    except Exception as e:
        return render_template("send_notification.html", 
                               error_elastic=f"Error: {str(e)}")

# ------------------------------------------------
# 🔹 Refresh User Cohort Data
# ------------------------------------------------
@app.route("/cohort/update", methods=["POST"])
def update_cohort():
    try:
        resp = requests.post("https://nexus.payppy.app/api/dashboard/v2/user-cohorts/refresh")
        result = resp.json()
        if resp.status_code == 200:
            return render_template("send_notification.html",
                                   success_cohort="📊 User Cohorts refreshed successfully!",
                                   cohort_result=result)
        else:
            return render_template("send_notification.html",
                                   error_cohort=f"❌ Failed: {result}")
    except Exception as e:
        return render_template("send_notification.html",
                               error_cohort=f"Error: {str(e)}")


# =======================================
# 🔹 REFRESH PINELABS BRANDS (NO DOWNLOAD)
# =======================================
@app.route("/brands/pinelabs", methods=["POST"])
def fetch_pinelabs():
    try:
        resp = requests.get("https://nexus.payppy.app/api/fetch-store-brands")
        return render_template("send_notification.html",
                               success_brands="📦 Pinelabs Brands refreshed successfully!")
    except Exception as e:
        return render_template("send_notification.html", error_brands=str(e))


# =======================================
# 🔹 REFRESH GYFTR BRANDS (NO DOWNLOAD)
# =======================================
@app.route("/brands/gyftr", methods=["POST"])
def fetch_gyftr():
    try:
        resp = requests.get("https://nexus.payppy.app/api/gyftr/fetch-store-brands")
        return render_template("send_notification.html",
                               success_brands="🎁 Gyftr Brands refreshed successfully!")
    except Exception as e:
        return render_template("send_notification.html", error_brands=str(e))


# =======================================
# 🔹 Fetch Pinelabs Brand DETAILS
# =======================================
@app.route("/brands/details/pinelabs", methods=["POST"])
def fetch_pinelabs_details():
    try:
        resp = requests.get("https://nexus.payppy.app/api/giftcard")
        result = resp.json()

        # SAVE JSON TO TEMP FILE
        filepath = "tmp/pinelab-details.json"
        with open(filepath, "w") as f:
            json.dump(result, f, indent=4)

        # store only filename in session
        session["brand_file"] = filepath
        session["brand_source"] = "pinelab"

        return render_template("send_notification.html",
                               success_brand_details="📦 Pinelabs Brand Details fetched!",
                               brand_details=result,
                               brand_source="pinelab")

    except Exception as e:
        return render_template("send_notification.html", error_brand_details=str(e))


# =======================================
# 🔹 Fetch Gyftr Brand DETAILS
# =======================================
@app.route("/brands/details/gyftr", methods=["POST"])
def fetch_gyftr_details():
    try:
        resp = requests.get("https://nexus.payppy.app/api/giftcard?provider=gyftr")
        result = resp.json()

        # SAVE JSON TO TEMP FILE
        filepath = "tmp/gyftr-details.json"
        with open(filepath, "w") as f:
            json.dump(result, f, indent=4)

        session["brand_file"] = filepath
        session["brand_source"] = "gyftr"

        return render_template("send_notification.html",
                               success_brand_details="🎁 Gyftr Brand Details fetched!",
                               brand_details=result,
                               brand_source="gyftr")

    except Exception as e:
        return render_template("send_notification.html", error_brand_details=str(e))


# =======================================
# 🔹 DOWNLOAD JSON (from file, not session)
# =======================================
@app.route("/brands/details/download", methods=["POST"])
def download_brand_details_json():
    filepath = session.get("brand_file")
    source = session.get("brand_source", "brand-details")

    if not filepath or not os.path.exists(filepath):
        return "No JSON file available to download", 400

    # Load JSON from file
    with open(filepath, "r") as f:
        json_data = json.load(f)

    timestamp = datetime.now().strftime("%d-%m-%Y-%I-%M-%p")
    filename = f"{timestamp}-{source}-brand-details.json"

    buf = io.BytesIO()
    buf.write(json.dumps(json_data, indent=4).encode("utf-8"))
    buf.seek(0)

    return send_file(
        buf,
        mimetype="application/json",
        download_name=filename,
        as_attachment=True
    )

if __name__ == "__main__":
    # Start the scheduled report thread
    #threading.Thread(target=schedule_midnight_report, daemon=True).start()
    app.run(host="0.0.0.0", port=3001, debug=False)

