import os, ssl, smtplib
from dotenv import load_dotenv

load_dotenv()

host = os.getenv("SMTP_HOST", "smtp.ukr.net").strip()
port = int(os.getenv("SMTP_PORT", "465"))
user_env = os.getenv("SMTP_USER", "").strip()
pwd = os.getenv("SMTP_PASS", "").strip()

candidates = [user_env]
if "@" in user_env:
    candidates.append(user_env.split("@")[0])  # спробуємо без домену

print("SMTP_HOST:", host, "PORT:", port)
print("USER candidates:", candidates)

def try_login(user):
    try:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
            s.login(user, pwd)
        print(f"[OK] Увійшли як: {user!r}")
    except smtplib.SMTPAuthenticationError as e:
        print(f"[FAIL] {user!r}: SMTPAuthenticationError -> {e.smtp_code} {e.smtp_error!r}")
    except Exception as e:
        print(f"[FAIL] {user!r}: {type(e).__name__} -> {e!r}")

for u in candidates:
    try_login(u)
