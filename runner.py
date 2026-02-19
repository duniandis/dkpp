import os, json, time, random, csv
from pathlib import Path
from datetime import datetime, date, timedelta
import requests

# Endpoints (sesuai app)
LOGIN_URL   = "https://ekinerja.baritoutarakab.go.id/api/presentapp/login"
AUTH_URL    = "https://ekinerja.baritoutarakab.go.id/api/presentapp/auth"
SNDLOC_URL  = "https://ekinerja.baritoutarakab.go.id/api/presentapp/sndloc"
INFODAY_URL = "https://ekinerja.baritoutarakab.go.id/api/presentapp/infoday"

ACCEPT_HEADER = "application/json, text/plain, */*"
USER_AGENT = "okhttp/4.12.0"

# Device identity (set manually here to match your app/device).
# IMPORTANT: keep these stable (do not change) or server will treat it as a new device.
DEVICE_ID_16 = os.getenv("DEVICE_ID_16", "")
DEVICE_NAME = os.getenv("DEVICE_NAME", "")

CACHE_DIR = Path(".cache")
STATE_DIR = Path(".state")
CACHE_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)

COOKIE_FILE = CACHE_DIR / "cookie_ekin.json"
DEVICE_FILE = CACHE_DIR / "device.json"
COORD_FILE  = CACHE_DIR / "coord.json"
LOCK_FILE   = STATE_DIR / "lock.json"

def tz_now():
    # Runner uses UTC; we want Asia/Jakarta (UTC+7) like WIB/Pontianak.
    return datetime.utcnow() + timedelta(hours=7)

def ntfy(title: str, message: str, priority: str = "default"):
    url = os.getenv("NTFY_TOPIC_URL", "").strip()
    if not url:
        return
    try:
        requests.post(url, data=message.encode("utf-8"),
                      headers={"Title": title, "Priority": priority},
                      timeout=15)
    except Exception:
        pass

def load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default

def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def acquire_lock(max_age_seconds: int = 20*60) -> bool:
    now = int(time.time())
    cur = load_json(LOCK_FILE, {})
    if cur and isinstance(cur, dict) and cur.get("ts"):
        age = now - int(cur["ts"])
        if age < max_age_seconds:
            return False
    save_json(LOCK_FILE, {"ts": now, "run_id": os.getenv("GITHUB_RUN_ID", "")})
    return True

def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass

def gen_device_id_16_hex() -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(16))

def get_device_identity():
    # Manual (edit constants near the top). You can still override via env if you want.
    device_id = (os.getenv("DEVICE_ID_16", "") or DEVICE_ID_16).strip()
    device_name = (os.getenv("DEVICE_NAME", "") or DEVICE_NAME).strip()

    if not device_id or len(device_id) != 16:
        raise RuntimeError("DEVICE_ID_16 harus 16 karakter. Edit di runner.py (atau set env DEVICE_ID_16).")
    if not device_name:
        raise RuntimeError("DEVICE_NAME kosong. Edit di runner.py (atau set env DEVICE_NAME).")

    return device_id, device_name

def load_cookie() -> str | None:
    data = load_json(COOKIE_FILE, {})
    c = (data.get("cookie") or "").strip()
    return c or None

def save_cookie(cookie: str | None):
    if cookie:
        save_json(COOKIE_FILE, {"cookie": cookie})
    else:
        save_json(COOKIE_FILE, {"cookie": ""})

def extract_cookie(resp: requests.Response, prev_cookie: str | None):
    # mimic app: take ekinbarut=... from Set-Cookie
    set_cookies = resp.headers.get("Set-Cookie", "")
    if not set_cookies:
        return prev_cookie
    # may contain multiple cookies; split roughly by comma only if it looks like multiple Set-Cookie merged
    parts = [p.strip() for p in set_cookies.split(",") if p.strip()]
    for raw in parts:
        c = raw.split(";", 1)[0].strip()
        if c.startswith("ekinbarut="):
            return c
    # fallback: maybe header already single cookie
    c = set_cookies.split(";", 1)[0].strip()
    if c.startswith("ekinbarut="):
        return c
    return prev_cookie

def sess_headers(cookie: str | None):
    h = {"accept": ACCEPT_HEADER, "User-Agent": USER_AGENT}
    if cookie:
        h["Cookie"] = cookie
    return h

def post_form(session: requests.Session, url: str, data: dict, cookie: str | None):
    return session.post(url, data=data, headers=sess_headers(cookie), timeout=30)

def auth_initial(session: requests.Session, cookie: str | None):
    r = post_form(session, AUTH_URL, {"token": "null", "akun": "null"}, cookie)
    new_cookie = extract_cookie(r, cookie)
    save_cookie(new_cookie)
    return new_cookie

def login(session: requests.Session, user: str, pw: str, imei: str, dname: str, cookie: str | None):
    r = post_form(session, LOGIN_URL, {"user": user, "pass": pw, "imei": imei, "dname": dname}, cookie)
    new_cookie = extract_cookie(r, cookie)
    save_cookie(new_cookie)
    try:
        obj = r.json()
    except Exception:
        return False, "Login respon tidak valid", None, None, new_cookie
    status = int(obj.get("status", 0) or 0)
    msg = str(obj.get("msg", "") or "")
    if status == 1:
        return True, "Login berhasil", str(obj.get("token", "") or ""), str(obj.get("akun", "") or ""), new_cookie
    return False, f"Login gagal: {msg}", None, None, new_cookie

def auth(session: requests.Session, token: str, akun: str, cookie: str | None):
    r = post_form(session, AUTH_URL, {"token": token, "akun": akun}, cookie)
    new_cookie = extract_cookie(r, cookie)
    save_cookie(new_cookie)
    try:
        obj = r.json()
        status = int(obj.get("status", 0) or 0)
        return (status == 1), ("AUTH ok" if status == 1 else "AUTH gagal"), new_cookie
    except Exception:
        return False, "AUTH respon tidak valid", new_cookie

def infoday(session: requests.Session, token: str, akun: str, cookie: str | None):
    r = post_form(session, INFODAY_URL, {"token": token, "akun": akun}, cookie)
    new_cookie = extract_cookie(r, cookie)
    save_cookie(new_cookie)
    if not r.ok:
        return False, {"displayText": f"Server jadwal error ({r.status_code})", "jmasuk": "", "jpulang": "", "ket": ""}, new_cookie
    try:
        obj = r.json()
    except Exception:
        return False, {"displayText": "Gagal membaca data jadwal", "jmasuk": "", "jpulang": "", "ket": ""}, new_cookie

    today = obj.get("today", "-")
    data = obj.get("data") or {}
    nama = data.get("nama", "-")
    jmasuk = data.get("jmasuk", "-")
    imasuk = data.get("masuk", "-")
    jpulang = data.get("jpulang", "-")
    ipulang = data.get("pulang", "-")
    ket = data.get("ket", "") or ""
    text = f"{today}\n{nama}\n\nMasuk: {jmasuk}\nAnda Masuk: {imasuk}\nPulang: {jpulang}\nAnda Pulang: {ipulang}"
    if ket.strip():
        text += f"\n\n{ket}"
    return True, {"displayText": text, "jmasuk": str(jmasuk), "jpulang": str(jpulang), "ket": str(ket)}, new_cookie

def looks_like_time_window(s: str | None) -> bool:
    if not s:
        return False
    import re
    return re.search(r"\b\d{2}:\d{2}:\d{2}\b", s) is not None

def normalize_ket(ket: str) -> str:
    t = (ket or "").strip()
    if not t:
        return ""
    if all(c == "-" for c in t):
        return ""
    return t

def load_coords_from_csv(path: str = "koordinat.csv"):
    # If repo doesn't have it, try to export from android raw file.
    p = Path(path)
    if not p.exists():
        # try android path (if user copied full project)
        alt = Path("app/src/main/res/raw/koordinat.csv")
        if alt.exists():
            p = alt
        else:
            raise FileNotFoundError("koordinat.csv tidak ditemukan.")
    coords = []
    with p.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lat = row.get("lat") or row.get("Lat") or row.get("LAT")
            lng = row.get("lang") or row.get("lng") or row.get("lon") or row.get("Lang") or row.get("LNG")
            if lat is None or lng is None:
                continue
            try:
                coords.append((float(lat), float(lng)))
            except Exception:
                continue
    if not coords:
        raise RuntimeError("Isi koordinat.csv kosong atau format tidak sesuai.")
    return coords

def get_or_pick_coord():
    cur = load_json(COORD_FILE, {})
    lat = cur.get("lat")
    lng = cur.get("lng")
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        return lat, lng
    coords = load_coords_from_csv()
    lat, lng = random.choice(coords)
    save_json(COORD_FILE, {"lat": lat, "lng": lng})
    return lat, lng

def parse_time(t: str) -> int:
    # seconds since midnight
    h, m, s = [int(x) for x in t.split(":")]
    return h*3600 + m*60 + s

def in_window(now_local: datetime, start: str, end: str) -> bool:
    sec = now_local.hour*3600 + now_local.minute*60 + now_local.second
    return parse_time(start) <= sec <= parse_time(end)

def state_path_for(day: date, key: str) -> Path:
    return STATE_DIR / f"{day.isoformat()}_{key}.json"

def ensure_daily_target(day: date, key: str, start: str, end: str):
    p = state_path_for(day, key)
    st = load_json(p, {})
    if st and "target_sec" in st:
        return st
    s = parse_time(start); e = parse_time(end)
    if e <= s:
        target = s
    else:
        target = random.randint(s, e)
    st = {"target_sec": target, "done_count": 0, "picked_at": tz_now().isoformat(sep=" ", timespec="seconds")}
    save_json(p, st)
    return st

def run_type(type_name: str):
    user = os.getenv("EKIN_USER", "").strip()
    pw   = os.getenv("EKIN_PASS", "").strip()
    if not user or not pw:
        raise RuntimeError("Secrets EKIN_USER / EKIN_PASS belum diset.")

    device_id, device_name = get_device_identity()
    lat, lng = get_or_pick_coord()

    session = requests.Session()
    cookie = load_cookie()

    cookie = auth_initial(session, cookie)
    ok, msg, token, akun, cookie = login(session, user, pw, device_id, device_name, cookie)
    if not ok or not token or not akun:
        return False, msg

    ok2, msg2, cookie = auth(session, token, akun, cookie)
    if not ok2:
        return False, msg2

    ok3, info_before, cookie = infoday(session, token, akun, cookie)
    ket = normalize_ket(info_before.get("ket",""))
    if len(ket) > 2:
        return True, f"SKIP karena keterangan: {ket}"

    jadwal = info_before.get("jmasuk") if type_name == "MASUK" else info_before.get("jpulang")
    if not looks_like_time_window(jadwal):
        short = (jadwal or "").strip()
        if len(short) > 60:
            short = short[:60] + "…"
        return True, f"SKIP: tidak ada jam jadwal ({short})"

    stimo = 1 if type_name == "MASUK" else 2
    r = post_form(session, SNDLOC_URL, {
        "token": token,
        "akun": akun,
        "lat": str(lat),
        "lang": str(lng),
        "stimo": str(stimo),
        "status": "1",
    }, cookie)
    cookie = extract_cookie(r, cookie)
    save_cookie(cookie)
    try:
        obj = r.json()
        ret = int(obj.get("ret", 0) or 0)
        nama = str(obj.get("nama","") or "")
        ok_send = (ret == 1)
        msg_send = (f"Berhasil: {nama}" if ok_send else f"Gagal: {nama}")
    except Exception:
        ok_send = False
        msg_send = "SNDLOC respon tidak valid"

    # infoday after (this is what we will notify to ntfy)
ok4, info_after, cookie = infoday(session, token, akun, cookie)

# Return sndloc result + infoday-after response
try:
    info_after_text = json.dumps(info_after, ensure_ascii=False)
except Exception:
    info_after_text = str(info_after)

return ok_send, msg_send, info_after_text

def main():
    now = tz_now()
    today = now.date()

    # Prepare targets + check windows
    masuk_start = os.getenv("WIN_MASUK_START","06:15:00")
    masuk_end   = os.getenv("WIN_MASUK_END","06:30:00")
    pulang_start= os.getenv("WIN_PULANG_START","16:00:00")
    pulang_end  = os.getenv("WIN_PULANG_END","17:00:00")

    masuk_target = int(os.getenv("MASUK_TARGET_COUNT","1"))
    pulang_target= int(os.getenv("PULANG_TARGET_COUNT","1"))

    # Decide which type(s) should run now based on target time
    tasks = []
    if in_window(now, masuk_start, masuk_end):
        st = ensure_daily_target(today, "MASUK", masuk_start, masuk_end)
        if st.get("done_count",0) < masuk_target:
            now_sec = now.hour*3600 + now.minute*60 + now.second
            if now_sec >= int(st["target_sec"]):
                tasks.append(("MASUK", st))
    if in_window(now, pulang_start, pulang_end):
        st = ensure_daily_target(today, "PULANG", pulang_start, pulang_end)
        if st.get("done_count",0) < pulang_target:
            now_sec = now.hour*3600 + now.minute*60 + now.second
            if now_sec >= int(st["target_sec"]):
                tasks.append(("PULANG", st))

    if not tasks:
        print(f"[{now}] Outside target or already done. Exit.")
        return

    if not acquire_lock():
        print("Lock aktif (ada job lain sedang jalan). Exit.")
        return

    try:
        # small jitter so it feels random even with polling
        time.sleep(random.randint(5, 40))

        for type_name, st in tasks:
            ok, msg, info_after = run_type(type_name)
            # update state
            st["done_count"] = int(st.get("done_count",0)) + 1
            st["last_run_at"] = tz_now().isoformat(sep=" ", timespec="seconds")
            st["last_result_ok"] = bool(ok)
            st["last_message"] = msg
            st["last_infoday_after"] = info_after
            save_json(state_path_for(today, type_name), st)

            title = f"DKPP_AUTO {type_name} {'OK' if ok else 'GAGAL'}"
            prio = "default" if ok else "high"
            ntfy(title, f"{type_name}: {msg}
Waktu: {st['last_run_at']}

INFODAY_AFTER:
{info_after}", priority=prio)
            print(f"{type_name}: {ok} - {msg}")

    finally:
        release_lock()

if __name__ == "__main__":
    main()
