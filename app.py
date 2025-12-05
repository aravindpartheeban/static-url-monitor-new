import hashlib
import io
import smtplib
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import urlparse, urlunparse

import requests
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
)
from flask_sqlalchemy import SQLAlchemy
from openpyxl import Workbook

# ---------------------------
# Flask & Database setup
# ---------------------------

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-secret-key"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///monitor.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# ---------------------------
# Database Models
# ---------------------------

class URLMonitor(db.Model):
    __tablename__ = "url_monitor"

    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(512), unique=True, nullable=False)
    original_hash = db.Column(db.String(32))  # first hash recorded
    latest_hash = db.Column(db.String(32))    # most recent hash
    last_checked = db.Column(db.DateTime)
    changed = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f"<URLMonitor {self.url}>"


class Settings(db.Model):
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    frequency_minutes = db.Column(db.Integer, default=60)

    smtp_host = db.Column(db.String(256))
    smtp_port = db.Column(db.Integer, default=587)
    smtp_user = db.Column(db.String(256))
    smtp_password = db.Column(db.String(256))
    smtp_use_tls = db.Column(db.Boolean, default=True)
    smtp_from = db.Column(db.String(256))
    smtp_to = db.Column(db.String(256))  # comma-separated if multiple

    proxy_enabled = db.Column(db.Boolean, default=False)
    proxy_url = db.Column(db.String(256))
    proxy_username = db.Column(db.String(128))
    proxy_password = db.Column(db.String(128))

    api_key = db.Column(db.String(256))

    # run status for UI
    last_run_started = db.Column(db.DateTime)
    last_run_finished = db.Column(db.DateTime)
    last_run_ok = db.Column(db.Boolean)  # None = never / in-progress
    last_run_message = db.Column(db.String(512))

    @staticmethod
    def get():
        settings = Settings.query.first()
        if not settings:
            settings = Settings()
            db.session.add(settings)
            db.session.commit()
        return settings


with app.app_context():
    db.create_all()


# ---------------------------
# Template context
# ---------------------------

@app.context_processor
def inject_now():
    return {"datetime": datetime}


# ---------------------------
# Utility / Core Logic
# ---------------------------

def build_proxy_url(base_url, username, password):
    if not base_url:
        return None

    parsed = urlparse(base_url)
    if username and password and not parsed.username:
        netloc = f"{username}:{password}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(
            (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
        )
    return base_url


def fetch_content(url, settings, max_retries=3, backoff_sec=2):
    """
    Fetches URL content (PDF/HTML/binary), with simple retry + backoff.
    Returns raw bytes.
    """
    session = requests.Session()

    proxies = None
    if settings.proxy_enabled and settings.proxy_url:
        proxy_url = build_proxy_url(
            settings.proxy_url,
            settings.proxy_username,
            settings.proxy_password,
        )
            # both http and https through same proxy
        proxies = {"http": proxy_url, "https": proxy_url}

    headers = {"User-Agent": "URLMonitorBot/1.0"}

    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            print(f"[Fetch] {url} (attempt {attempt}/{max_retries})")
            resp = session.get(url, timeout=30, headers=headers, proxies=proxies)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            last_exc = e
            print(f"[Fetch] Error fetching {url}: {e}")
            if attempt < max_retries:
                sleep_for = backoff_sec * attempt
                time.sleep(sleep_for)
    raise last_exc


def generate_excel_report():
    """
    Creates an Excel file in memory with URL monitoring data.
    Returns raw bytes.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "URL Monitor Report"

    ws.append(["URL", "Original MD5", "Latest MD5", "Changed", "Last Checked", "Comment"])

    rows = URLMonitor.query.order_by(URLMonitor.id).all()
    for entry in rows:
        last_checked_str = (
            entry.last_checked.strftime("%Y-%m-%d %H:%M:%S")
            if entry.last_checked else ""
        )

        if entry.original_hash is None:
            comment = "Not yet checked"
        elif entry.changed:
            comment = "Changed"
        else:
            comment = "No change detected"

        ws.append([
            entry.url,
            entry.original_hash or "",
            entry.latest_hash or "",
            "Yes" if entry.changed else "No",
            last_checked_str,
            comment,
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


def send_email_with_attachment(settings, file_bytes):
    """
    Sends an email with the Excel report attached, using configured SMTP settings.
    """
    if not settings.smtp_host or not settings.smtp_from or not settings.smtp_to:
        print("[Email] SMTP not fully configured, skipping email.")
        return

    msg = EmailMessage()
    msg["Subject"] = "URL Monitor Report"
    msg["From"] = settings.smtp_from
    msg["To"] = settings.smtp_to
    msg.set_content("Attached is the latest URL monitor report.")

    msg.add_attachment(
        file_bytes,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="url_monitor_report.xlsx",
    )

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port or 587) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_user and settings.smtp_password:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        print("[Email] Report sent successfully.")
    except Exception as e:
        print(f"[Email] Error sending email: {e}")


def index_new_urls():
    """
    For any URLs with no original_hash yet, fetch content once
    and store the initial MD5 (original & latest). No Excel/email here.
    """
    settings = Settings.get()
    new_entries = URLMonitor.query.filter_by(original_hash=None).all()
    if not new_entries:
        return

    print(f"[Index] Indexing {len(new_entries)} new URLs")

    for entry in new_entries:
        try:
            content = fetch_content(entry.url, settings)
            md5_hash = hashlib.md5(content).hexdigest()
            entry.original_hash = md5_hash
            entry.latest_hash = md5_hash
            entry.changed = False
            entry.last_checked = datetime.now()
            db.session.add(entry)
        except Exception as e:
            print(f"[Index] Error indexing {entry.url}: {e}")

    db.session.commit()


def run_monitoring(record_status=True):
    """
    Full monitoring run:
    - For ALL URLs, fetch content, compute MD5
    - Detect changes vs previous latest
    - Generate Excel & email (if SMTP configured)
    """
    settings = Settings.get()

    if record_status:
        settings.last_run_started = datetime.now()
        settings.last_run_finished = None
        settings.last_run_ok = None
        settings.last_run_message = "Running..."
        db.session.add(settings)
        db.session.commit()

    try:
        urls = URLMonitor.query.all()
        print(f"[Monitor] Running for {len(urls)} URLs")

        for entry in urls:
            try:
                content = fetch_content(entry.url, settings)
                md5_hash = hashlib.md5(content).hexdigest()

                if not entry.original_hash:
                    entry.original_hash = md5_hash
                    entry.latest_hash = md5_hash
                    entry.changed = False
                else:
                    entry.changed = (
                        entry.latest_hash is not None
                        and entry.latest_hash != md5_hash
                    )
                    entry.latest_hash = md5_hash

                entry.last_checked = datetime.now()
                db.session.add(entry)
            except Exception as e:
                print(f"[Monitor] Error processing {entry.url}: {e}")

        db.session.commit()

        report_bytes = generate_excel_report()
        send_email_with_attachment(settings, report_bytes)

        if record_status:
            settings.last_run_finished = datetime.now()
            settings.last_run_ok = True
            settings.last_run_message = "Completed successfully."
            db.session.add(settings)
            db.session.commit()

        return report_bytes

    except Exception as e:
        if record_status:
            settings.last_run_finished = datetime.now()
            settings.last_run_ok = False
            settings.last_run_message = f"Failed: {e}"
            db.session.add(settings)
            db.session.commit()
        raise


# ---------------------------
# Background Scheduler Thread
# ---------------------------

def scheduler_loop():
    while True:
        with app.app_context():
            settings = Settings.get()
            interval = settings.frequency_minutes or 60
            print(f"[Scheduler] Running monitoring job (interval {interval} min)")
            try:
                run_monitoring(record_status=True)
            except Exception as e:
                print(f"[Scheduler] Error during monitoring: {e}")
        time.sleep(interval * 60)


def start_background_thread():
    if app.config.get("SCHEDULER_STARTED"):
        return
    app.config["SCHEDULER_STARTED"] = True
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    print("[Scheduler] Background scheduler started")


# ---------------------------
# Flask Routes
# ---------------------------

@app.route("/")
def index():
    return redirect(url_for("dashboard"))


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST":
        # Single box: handles one or many URLs, one per line
        raw = request.form.get("bulk_urls", "")
        urls_raw = [u.strip() for u in raw.splitlines() if u.strip()]
        if not urls_raw:
            flash("Please enter at least one URL.", "warning")
            return redirect(url_for("dashboard"))

        added = 0
        skipped = 0

        for url in urls_raw:
            existing = URLMonitor.query.filter_by(url=url).first()
            if existing:
                skipped += 1
                continue
            entry = URLMonitor(url=url)
            db.session.add(entry)
            added += 1

        db.session.commit()

        # Immediately index hashes for newly added URLs (no Excel/email)
        try:
            index_new_urls()
            msg_extra = ""
        except Exception as e:
            msg_extra = f" (initial indexing error: {e})"

        flash(f"Added {added} URL(s), skipped {skipped}.{msg_extra}", "success")
        return redirect(url_for("dashboard"))

    urls = URLMonitor.query.order_by(URLMonitor.id.desc()).all()
    settings = Settings.get()
    return render_template(
        "dashboard.html",
        urls=urls,
        settings=settings,
        active_tab="dashboard",
    )


@app.route("/delete-url/<int:url_id>", methods=["POST"])
def delete_url(url_id):
    entry = URLMonitor.query.get_or_404(url_id)
    db.session.delete(entry)
    db.session.commit()
    flash("URL removed from monitoring.", "success")
    return redirect(url_for("dashboard"))


@app.route("/run-now", methods=["POST"])
def run_now():
    """
    Ad-hoc monitoring run triggered by user.
    Generates Excel and (optionally) sends email.
    """
    try:
        report_bytes = run_monitoring(record_status=True)
        flash("Monitoring job completed.", "success")
    except Exception as e:
        flash(f"Monitoring job failed: {e}", "danger")
        report_bytes = generate_excel_report()

    return send_file(
        io.BytesIO(report_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="url_monitor_report.xlsx",
    )


@app.route("/export", methods=["GET"])
def export():
    """
    Export current data as Excel without rerunning monitoring.
    """
    report_bytes = generate_excel_report()
    return send_file(
        io.BytesIO(report_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="url_monitor_report.xlsx",
    )


@app.route("/settings", methods=["GET", "POST"])
def settings():
    settings = Settings.get()

    if request.method == "POST":
        try:
            settings.frequency_minutes = int(request.form.get("frequency_minutes") or 60)
        except ValueError:
            settings.frequency_minutes = 60

        settings.smtp_host = request.form.get("smtp_host") or None
        smtp_port_val = request.form.get("smtp_port") or "587"
        try:
            settings.smtp_port = int(smtp_port_val)
        except ValueError:
            settings.smtp_port = 587

        settings.smtp_user = request.form.get("smtp_user") or None
        settings.smtp_password = request.form.get("smtp_password") or None
        settings.smtp_from = request.form.get("smtp_from") or None
        settings.smtp_to = request.form.get("smtp_to") or None
        settings.smtp_use_tls = bool(request.form.get("smtp_use_tls"))

        settings.proxy_enabled = bool(request.form.get("proxy_enabled"))
        settings.proxy_url = request.form.get("proxy_url") or None
        settings.proxy_username = request.form.get("proxy_username") or None
        settings.proxy_password = request.form.get("proxy_password") or None

        settings.api_key = request.form.get("api_key") or None

        db.session.add(settings)
        db.session.commit()
        flash("Settings updated successfully.", "success")
        return redirect(url_for("settings"))

    return render_template("settings.html", settings=settings, active_tab="settings")


@app.route("/faq")
def faq():
    return render_template("faq.html", active_tab="faq")


# ---------------------------
# Main entrypoint
# ---------------------------

if __name__ == "__main__":
    start_background_thread()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
