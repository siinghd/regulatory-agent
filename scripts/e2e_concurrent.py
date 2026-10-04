"""Fire several real requests at once through the live agent and time each reply."""
import concurrent.futures as cf, sys, time
from e2e_send import send, wait_replies

CASES = [
    ("Other docs", "Can you send me the Other Documents for M12383?"),
    ("Exhibits", "Please send the Exhibits for M12383"),
    ("Key docs", "Could you send me the Key Documents for M12205?"),
    ("Other docs", "Please send me the Other Documents for M12205"),
]

def run(case):
    subject, body = case
    t = time.time(); mid = send(subject, body)
    replies = wait_replies(mid, 2, 600)
    out = {"case": body, "ack_s": None, "reply_s": None, "first_line": ""}
    for dt, msg in replies:
        if msg["Message-ID"].startswith("<ack."): out["ack_s"] = round(dt, 1)
        else:
            out["reply_s"] = round(dt, 1)
            text = msg.get_body(preferencelist=("plain",)).get_content()
            out["first_line"] = next((l for l in text.splitlines() if "downloaded" in l or "couldn't" in l or "no " in l.lower()), "")[:150]
    return out

if __name__ == "__main__":
    t0 = time.time()
    with cf.ThreadPoolExecutor(len(CASES)) as ex:
        for r in ex.map(run, CASES): print(r)
    print(f"wall clock: {time.time()-t0:.1f}s")
