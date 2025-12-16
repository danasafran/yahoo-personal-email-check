import os
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta
import pytz
import smtplib
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
import re

# -----------------------
# Settings (human scoring)
# -----------------------
LIKELY_THRESHOLD = 6
MAYBE_THRESHOLD = 4

SYSTEM_SENDERS = [
    "usps",
    "informeddelivery",
    "walmart",
    "walmart.com",
    "wm.com",
    "wastemanagement",
    "waste management",
]


MARKETING_SUBJECT_WORDS = [
    "sale", "deal", "offer", "discount", "promo", "newsletter", "save", "clearance", "% off"
]
AUTO_SENDER_PATTERNS = [
    "no-reply", "noreply", "donotreply", "mailer", "notifications", "support@", "info@"
]
FOOTER_SIGNALS = [
    "unsubscribe", "view in browser", "manage preferences"
]
RECEIPT_WORDS = [
    "receipt", "order", "shipped", "delivered", "tracking", "invoice"
]

# -----------------------
# Helpers
# -----------------------
def decode_mime(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="replace")
        else:
            out += text
    return out

def extract_text(msg):
    # Prefer plain text, fallback to HTML stripped to text.
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                content = payload.decode(charset, errors="replace")
            except Exception:
                content = payload.decode("utf-8", errors="replace")

            if ctype == "text/plain":
                return content
        # fallback to HTML
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="replace")
                return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""

def score_email(from_addr, subject, body):
    lf = (from_addr or "").lower()
    ls = (subject or "").lower()
    lb = (body or "").lower()

    score = 0
    reasons = []

    # System / FYI senders: keep them, but do not treat as "personal"
    if any(x in lf for x in SYSTEM_SENDERS):
        score -= 10
        reasons.append("system sender")


    score = 0
    reasons = []

    body_len = len(body.strip())

    # "Human-ish" length
    if 0 < body_len <= 1200:
        score += 2; reasons.append("short body")
    elif 1200 < body_len <= 4000:
        score += 1; reasons.append("medium body")

    # Conversational cues (simple + effective)
    convo_hits = 0
    for pat in [r"\bhi\b", r"\bhey\b", r"\bthanks\b", r"\bthank you\b",
                r"\bcan you\b", r"\bcould you\b", r"\blet me know\b", r"\bplease\b",
                r"\bi\b", r"\bwe\b", r"\byou\b"]:
        if re.search(pat, lb):
            convo_hits += 1
    if convo_hits >= 3:
        score += 2; reasons.append("conversational tone")
    elif convo_hits == 2:
        score += 1; reasons.append("some conversational cues")

    # Penalize automation / marketing patterns
    if any(p in lf for p in AUTO_SENDER_PATTERNS):
        score -= 4; reasons.append("auto-sender pattern")

    if any(w in ls for w in MARKETING_SUBJECT_WORDS):
        score -= 4; reasons.append("marketing subject")

    if any(w in ls or w in lb for w in RECEIPT_WORDS):
        score -= 2; reasons.append("transactional")

    if any(w in lb for w in FOOTER_SIGNALS):
        score -= 4; reasons.append("unsubscribe/footer")

    # Direct ask patterns
    if any(p in lb for p in ["just checking in", "wanted to", "quick question", "are you free", "call me"]):
        score += 2; reasons.append("direct ask")

    return score, reasons

def send_email_smtp(subject, html_body):
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    mail_from = os.environ["MAIL_FROM"]
    mail_to = os.environ["MAIL_TO"]

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(mail_from, [mail_to], msg.as_string())

def main():
    # Time window: yesterday midnight -> 11:59:59 Eastern
    tz = pytz.timezone("America/New_York")
    now = datetime.now(tz)
    start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    # Yahoo IMAP login
    imap_host = "imap.mail.yahoo.com"
    yahoo_user = os.environ["YAHOO_USER"]
    yahoo_app_pass = os.environ["YAHOO_APP_PASSWORD"]

    mail = imaplib.IMAP4_SSL(imap_host)

    # Login
    mail.login(yahoo_user, yahoo_app_pass)

    # Select inbox (must be in SELECTED state before SEARCH)
    status, _ = mail.select("INBOX", readonly=True)
    if status != "OK":
        # Fallbacks some Yahoo accounts use
        status, _ = mail.select("Inbox", readonly=True)

    if status != "OK":
        raise RuntimeError(f"Could not select INBOX. IMAP status={status}")


    # IMAP uses dates (day granularity). We'll fetch yesterday and then filter by exact timestamps.
    imap_date = start.strftime("%d-%b-%Y")  # e.g., 16-Dec-2025
    typ, data = mail.search(None, f'(SINCE "{imap_date}")')
    if typ != "OK":
        raise RuntimeError(f"IMAP search failed: {typ} {data}")
    ids = data[0].split()


    candidates = []
    for eid in ids:
        typ, msg_data = mail.fetch(eid, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        # Parse date
        msg_date = msg.get("Date", "")
        try:
            dt = email.utils.parsedate_to_datetime(msg_date)
            if dt.tzinfo is None:
                dt = tz.localize(dt)
            else:
                dt = dt.astimezone(tz)
        except Exception:
            continue

        if not (start <= dt < end):
            continue

        from_addr = decode_mime(msg.get("From", ""))
        subject = decode_mime(msg.get("Subject", ""))
        body = extract_text(msg)

        score, reasons = score_email(from_addr, subject, body)
        snippet = re.sub(r"\s+", " ", body.strip())[:180]

        candidates.append({
            "date": dt,
            "from": from_addr,
            "subject": subject or "(no subject)",
            "score": score,
            "reasons": reasons,
            "snippet": snippet
        })

    mail.logout()

    # Sort by score, then newest
    candidates.sort(key=lambda x: (x["score"], x["date"]), reverse=True)

    likely = [c for c in likely if "system sender" not in c["reasons"]]
    maybe  = [c for c in maybe  if "system sender" not in c["reasons"]]
    system = [c for c in candidates if "system sender" in c["reasons"]]

    date_label = start.strftime("%a %b %d, %Y")
    email_subject = f"Personal Email Check (Yahoo) — {start.strftime('%b %d')}"

    if not likely and not maybe and not system:
        html = f"""
        <div style="font-family:Arial,sans-serif;line-height:1.4">
          <h3 style="margin:0 0 8px 0;">Personal Email Check (Yahoo)</h3>
          <p style="margin:0;"><b>{date_label}</b></p>
          <p style="margin:12px 0 0 0;">No likely personal emails yesterday.</p>
          <p style="margin:6px 0 0 0;color:#666;">Messages looked automated (marketing, notifications, receipts, etc.).</p>
        </div>
        """
        send_email_smtp(email_subject, html)
        return

    def render_section(title, items):
        if not items:
            return ""
        out = f'<h3 style="margin:18px 0 8px 0;">{title} <span style="color:#666;font-weight:normal">({len(items)})</span></h3><ul style="margin:0 0 8px 18px;padding:0;">'
        for c in items[:12]:
            out += f"""
            <li style="margin:0 0 10px 0;">
              <b>{c["subject"]}</b>
              <div style="color:#333;margin-top:2px;">From: {c["from"]}</div>
              <div style="color:#444;margin-top:2px;">{c["snippet"]}...</div>
              <div style="color:#666;margin-top:2px;font-size:12px;">Score: {c["score"]} — {", ".join(c["reasons"])}</div>
            </li>
            """
        if len(items) > 12:
            out += f'<li style="color:#666">+ {len(items)-12} more</li>'
        out += "</ul>"
        return out

    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.4">
      <h2 style="margin:0 0 8px 0;">Personal Email Check (Yahoo)</h2>
      <p style="margin:0 0 16px 0;"><b>{date_label}</b></p>
      {render_section("Likely personal", likely)}
      {render_section("Maybe personal", maybe)}
      {render_section("System / FYI (orders, mail, services)", system)}
      <hr style="border:none;border-top:1px solid #ddd;margin:16px 0;" />
      <p style="color:#666;margin:0;">This is heuristic scoring. We can tune it after 1–2 runs.</p>
    </div>
    """

    send_email_smtp(email_subject, html)

if __name__ == "__main__":
    main()
