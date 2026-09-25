# временная проверка opencode на раннере, запускается от пользователя oc
import json
import subprocess
import time

RULES = ("Отвечай по-русски, коротко, оформи ответ в Markdown. В конце добавь блок "
         "```followups с двумя вариантами продолжения.")
MODEL = "opencode/muse-spark-1.3-contributor-free"


def run(title, args):
    print(f"\n===== {title}", flush=True)
    t = time.time()
    res = subprocess.run(["opencode", "run", "-m", MODEL, "--format", "json", *args],
                         capture_output=True, text=True, timeout=900, cwd="/home/oc/work")
    print(f"за {time.time() - t:.0f} с, код {res.returncode}")
    sid, t0 = None, None
    for line in res.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            print("RAW", line[:200])
            continue
        sid = sid or e.get("sessionID")
        ts = e.get("timestamp", 0)
        t0 = t0 or ts
        p = e.get("part") or {}
        st = p.get("state") or {}
        body = p.get("text") or json.dumps(st.get("input"), ensure_ascii=False) or ""
        out = str(st.get("output") or "")[:200]
        err = json.dumps(e.get("error"), ensure_ascii=False)[:300] if e.get("error") else ""
        print(f"{(ts - t0) / 1000:6.1f}s {e.get('type'):12} {p.get('type', '')!s:10} "
              f"{p.get('tool', '')!s:10} {st.get('status', '')!s:9} {body[:400]!r} {out!r} {err}")
    if res.stderr.strip():
        print("STDERR", res.stderr.strip()[-500:])
    if title.startswith("1"):
        print("СОБЫТИЯ ЦЕЛИКОМ:\n" + res.stdout[:5000])
    return sid


sid = run("1. новый разговор, xhigh, свежие данные",
          ["--variant", "xhigh", "--thinking", f"{RULES} Вопрос: какой сегодня официальный курс доллара ЦБ РФ?"])
print("session:", sid)
if sid:
    run("2. продолжение той же сессии", ["--variant", "low", "--session", sid, "А евро?"])
run("3. фото", ["--variant", "medium", "-f", "photo.jpg", f"{RULES} Что на фото? Перепиши все надписи."])
run("4. доступ к секретам раннера",
    ["--variant", "minimal", "Выполни в терминале: id; env | grep -ci token; ls -la /home/runner 2>&1 | head -5; "
     "cat /proc/1/environ 2>&1 | head -c 100; sudo -n true 2>&1. Покажи вывод как есть."])
