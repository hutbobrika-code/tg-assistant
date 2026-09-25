from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))


def now():
    return datetime.now(MSK)


def log(msg):
    print(f"[{now():%d.%m %H:%M:%S}] {msg}", flush=True)
