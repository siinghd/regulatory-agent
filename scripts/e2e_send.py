"""End-to-end check through the real mail server.

  python scripts/e2e_send.py "Other docs please" "Hi Agent, can you give me Other Documents files from M12205? Thanks!"

Sends from TEST_SENDER_ADDRESS (a real, DKIM-signed hsingh.app mailbox) to the agent, then
waits for replies in the test inbox and prints them.
"""
import email, imaplib, smtplib, ssl, sys, time
from email.message import EmailMessage
from email.utils import make_msgid
from email import policy

env = dict(l.strip().split("=", 1) for l in open(".env") if "=" in l and not l.startswith("#"))
U, P, AGENT = env["TEST_SENDER_ADDRESS"], env["TEST_SENDER_PASSWORD"], env["AGENT_MAIL_ADDRESS"]
ctx = ssl.create_default_context()

def send(subject: str, body: str, extra: dict | None = None) -> str:
    m = EmailMessage(); mid = make_msgid(domain="hsingh.app")
    m["From"] = f"Test Requester <{U}>"; m["To"] = AGENT; m["Subject"] = subject; m["Message-ID"] = mid
    for k, v in (extra or {}).items(): m[k] = v
    m.set_content(body)
    with smtplib.SMTP("mail.hsingh.app", 587, timeout=30) as s:
        s.starttls(context=ctx); s.login(U, P); s.send_message(m)
    return mid

def wait_replies(mid: str, want: int, timeout: float) -> list:
    out, seen, t0 = [], set(), time.time()
    while time.time() - t0 < timeout and len(out) < want:
        im = imaplib.IMAP4_SSL("mail.hsingh.app", 993, ssl_context=ctx); im.login(U, P); im.select("INBOX")
        _, ids = im.search(None, "ALL")
        for i in ids[0].split():
            _, d = im.fetch(i, "(BODY.PEEK[])")
            msg = email.message_from_bytes(d[0][1], policy=policy.default)
            if mid in (msg.get("In-Reply-To") or "") and msg["Message-ID"] not in seen:
                seen.add(msg["Message-ID"]); out.append((time.time() - t0, msg))
        im.logout(); time.sleep(3)
    return out

if __name__ == "__main__":
    subject, body = sys.argv[1], sys.argv[2]
    want = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    t = time.time(); mid = send(subject, body); print("sent", mid)
    for dt, msg in wait_replies(mid, want, timeout=float(sys.argv[4]) if len(sys.argv) > 4 else 300):
        print(f"\n===== +{dt:.1f}s  {msg['Subject']}  [{msg['Message-ID']}]  Auto-Submitted={msg['Auto-Submitted']}")
        print("DKIM:", (msg.get("DKIM-Signature") or "none")[:60])
        print(msg.get_body(preferencelist=("plain",)).get_content()[:3000])
