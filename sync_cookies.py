#!/usr/bin/env python3
"""
Автоматическая синхронизация Google кук из Firefox в базу данных Gemini роутера
с последующим шифрованием и пушем в GitHub Actions.

Использование:
  python3 sync_cookies.py           # авто-забор из Firefox + шифрование + push
  python3 sync_cookies.py --no-push # только локальное обновление и шифрование
"""
import glob
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "gemini.db"
STATE_DIR = BASE_DIR / "state"
ENC_PATH = STATE_DIR / "gemini.db.enc"
KEY_FILE = Path.home() / ".gemini_state_key"

WANT = [
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-1PSIDTS",
    "__Secure-3PSID", "__Secure-3PSIDTS",
    "SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC"
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT NOT NULL DEFAULT '',      
    cookie        TEXT NOT NULL,                 
    status        TEXT NOT NULL DEFAULT 'enabled', 
    note          TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER NOT NULL DEFAULT 0,     
    last_ok_at    INTEGER NOT NULL DEFAULT 0,     
    last_error    TEXT NOT NULL DEFAULT '',
    fail_count    INTEGER NOT NULL DEFAULT 0,     
    proxy_id      INTEGER NOT NULL DEFAULT 0      
);
CREATE INDEX IF NOT EXISTS idx_accounts_pick ON accounts(status, last_used_at);
"""


def extract_firefox_cookies():
    patterns = [
        os.path.expanduser("~/.config/mozilla/firefox/*.default*/cookies.sqlite"),
        os.path.expanduser("~/.mozilla/firefox/*.default*/cookies.sqlite"),
    ]
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat))

    if not files:
        raise RuntimeError("Не найден профиль Firefox с cookies.sqlite")

    # Берём самый свежий файл по дате модификации
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    src_db = files[0]
    print(f"[*] Читаем профиль Firefox: {src_db}")

    tmp_copy = f"/tmp/ff_cookies_{int(time.time())}.sqlite"
    shutil.copyfile(src_db, tmp_copy)

    cookies = {}
    try:
        con = sqlite3.connect(tmp_copy)
        cur = con.cursor()
        query = """
        SELECT name, value FROM moz_cookies 
        WHERE (host LIKE '%google.com' OR host LIKE '%.google.com')
        """
        for name, val in cur.execute(query):
            if name in WANT and val:
                # Берём наиболее длинное значение (как правило, более свежее)
                if name not in cookies or len(val) > len(cookies[name]):
                    cookies[name] = val
        con.close()
    finally:
        if os.path.exists(tmp_copy):
            os.remove(tmp_copy)

    return cookies


def get_state_key():
    if "STATE_KEY" in os.environ and os.environ["STATE_KEY"].strip():
        return os.environ["STATE_KEY"].strip()

    if KEY_FILE.exists():
        for line in KEY_FILE.read_text().splitlines():
            if "=" in line and "STATE_KEY" in line:
                return line.split("=", 1)[1].strip()
            elif line.strip() and not line.startswith("#"):
                return line.strip()

    raise RuntimeError(f"Не найден STATE_KEY (ни в env, ни в {KEY_FILE})")


def update_database(cookie_str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()
    cur.executescript(SCHEMA)

    cur.execute("SELECT id FROM accounts LIMIT 1")
    row = cur.fetchone()
    now = int(time.time())

    if row:
        acc_id = row[0]
        cur.execute(
            "UPDATE accounts SET cookie = ?, status = 'enabled', last_error = '', fail_count = 0 WHERE id = ?",
            (cookie_str, acc_id)
        )
        print(f"[+] Обновлены куки для аккаунта #{acc_id} в {DB_PATH}")
    else:
        cur.execute(
            "INSERT INTO accounts (label, cookie, status, created_at) VALUES (?, ?, 'enabled', ?)",
            ("Firefox Google Account", cookie_str, now)
        )
        print(f"[+] Добавлен новый аккаунт с куками в {DB_PATH}")

    con.commit()
    con.close()


def encrypt_database(state_key):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt",
        "-pass", "env:STATE_KEY",
        "-in", str(DB_PATH),
        "-out", str(ENC_PATH)
    ]
    env = dict(os.environ, STATE_KEY=state_key)
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Ошибка openssl enc: {res.stderr}")
    print(f"[+] Зашифровано: {ENC_PATH} ({os.path.getsize(ENC_PATH)} байт)")


def git_push():
    print("[*] Фиксация и отправка в GitHub...")
    subprocess.run(["git", "add", "state/gemini.db.enc"], cwd=str(BASE_DIR), check=True)
    msg = f"update: gemini session cookies ({time.strftime('%Y-%m-%d %H:%M')})"
    diff = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=str(BASE_DIR))
    if diff.returncode != 0:
        subprocess.run(["git", "commit", "-m", msg], cwd=str(BASE_DIR), check=True)
        subprocess.run(["git", "pull", "--rebase"], cwd=str(BASE_DIR), check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=str(BASE_DIR), check=True)
        print("[✓] Успешно запушено в GitHub!")
    else:
        print("[i] Нет изменений для коммита в state/gemini.db.enc")


def main():
    no_push = "--no-push" in sys.argv
    print("=== Синхронизация Google Gemini кук ===")

    cookies = extract_firefox_cookies()
    print(f"[*] Найдено необходимых кук: {len(cookies)} из {len(WANT)}")
    print(f"    Куки: {', '.join(sorted(cookies.keys()))}")

    required = ["SID", "HSID", "SSID", "__Secure-1PSID"]
    missing = [k for k in required if k not in cookies]
    if missing:
        print(f"[!] Ошибка: не найдены обязательные куки: {missing}")
        print("    Пожалуйста, откройте Firefox и авторизуйтесь на https://gemini.google.com/app")
        sys.exit(1)

    cookie_str = "; ".join(f"{k}={cookies[k]}" for k in WANT if k in cookies)
    print(f"[*] Длина строки кук: {len(cookie_str)} символов")

    update_database(cookie_str)
    state_key = get_state_key()
    encrypt_database(state_key)

    if not no_push:
        git_push()

    print("\n[✓] Всё готово! Роутер в GitHub Actions автоматически использует эти куки и продлевает их каждые 10 минут.")


if __name__ == "__main__":
    main()
