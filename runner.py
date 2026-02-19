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

CACHE_DIR = Path(".cache")
STATE_DIR = Path(".state")
CACHE_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)

COOKIE_FILE = CACHE_DIR / "cookie_ekin.json"
COORD_FILE  = CACHE_DIR / "coord.json"
LOCK_FILE   = STATE_DIR / "lock.json"

def tz_now_wib() -> datetime:
    # WIB/Asia/Jakarta (UTC+7)
    return datetime.utcnow() + timedelta(hours=7)

def ntfy(title: str, message: str, priority: str = "default"):
    url = os.getenv("NTFY_TOPIC_URL", "").strip()
    if not url:
        return
    try:
        requests.post(
            url,
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=15,
        )
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
    if isinstance(cur, dict) and cur.get("ts"):
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

def get_device_identity_from_env():
    device_id = os.getenv("DEVICE_ID_16", "").strip()
    device_name = os.getenv("DEVICE_NAME", "").strip()

    if not device_id or len(device_id) != 16:
        raise RuntimeError("ENV DEVICE_ID_16 wajib 16 karakter (tanam di .yml).")
    if not device_name:
        raise RuntimeError("ENV DEVICE_NAME wajib diisi (tanam di .yml).")

    return device_id, device_name

def load_cookie() -> str | None:
    data = load_json(COOKIE_FILE, {})
    c = (data.get("cookie") or "").strip()
    return c or None

def save_cookie(cookie: str | None):
    save_json(COOKIE_FILE, {"cookie": cookie or ""})

def extract_cookie(resp: requests.Response, prev_cookie: str | None):
    set_cookies = resp.headers.get("Set-Cookie", "")
    if not set_cookies:
        return prev_cookie

    parts = [p.strip() for p in set_cookies.split(",") if p.strip()]
    for raw in parts:
        c = raw.split(";", 1)[0].strip()
        if c.startswith("ekinbarut="):
            return c

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

def normalize_ket(ket: str) -> str:
    t = (ket or "").strip()
    if not t:
        return ""
    # anggap "-" / "----" kosong
    if all(ch == "-" for ch in t):
        return ""
    return t

def parse_time_to_sec(t: str) -> int:
    h, m, s = [int(x) for x in t.split(":")]
    return h*3600 + m*60 + s

def in_window(now_local: datetime, start: str, end: str) -> bool:
    sec = now_local.hour*3600 + now_local.minute*60 + now_local.second
    return parse_time_to_sec(start) <= sec <= parse_time_to_sec(end)

def state_path_for(day: date, key: str) -> Path:
    return STATE_DIR / f"{day.isoformat()}_{key}.json"

def already_done(day: date, key: str, target_count: int) -> bool:
    st = load_json(state_path_for(day, key), {})
    done = int(st.get("done_count", 0) or 0)
    return done >= target_count

def mark_done(day: date, key: str, ok: bool, msg: str, infoday_after: dict | None):
    p = state_path_for(day, key)
    st = load_json(p, {}) or {}
    st["done_count"] = int(st.get("done_count", 0) or 0) + 1
    st["last_run_at"] = tz_now_wib().isoformat(sep=" ", timespec="seconds")
    st["last_result_ok"] = bool(ok)
    st["last_message"] = msg
    st["last_infoday_after"] = infoday_after
    save_json(p, st)

def load_coords_from_csv(path: str = "koordinat.csv"):
    p = Path(path)
    if not p.exists():
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
        raise RuntimeError("Isi koordinat.csv kosong atau format tidak sesuai (butuh kolom lat,lang).")
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

def infoday_raw(session: requests.Session, token: str, akun: str, cookie: str | None):
    r = post_form(session, INFODAY_URL, {"token": token, "akun": akun}, cookie)
    new_cookie = extract_cookie(r, cookie)
    save_cookie(new_cookie)

    if not r.ok:
        return False, {"error": f"HTTP {r.status_code}"}, new_cookie

    try:
        obj = r.json()
        return True, obj, new_cookie
    except Exception:
        return False, {"error": "JSON parse error"}, new_cookie

def extract_infodata(obj: dict):
    data = obj.get("data") or {}
    return {
        "today": obj.get("today", "-"),
        "nama": data.get("nama", "-"),
        "jmasuk": str(data.get("jmasuk", "-") or "-"),
        "jpulang": str(data.get("jpulang", "-") or "-"),
        "masuk": str(data.get("masuk", "-") or "-"),
        "pulang": str(data.get("pulang", "-") or "-"),
        "ket": str(data.get("ket", "") or ""),
        "isma": str(data.get("isma", "") or ""),
        "raw": obj,
    }

def should_skip_by_ket(ket: str) -> tuple[bool, str]:
    k = normalize_ket(ket)
    if len(k) > 2:
        return True, f"SKIP karena keterangan: {k}"
    return False, ""

def is_marked(value: str) -> bool:
    # server pakai "-" artinya belum ada
    v = (value or "").strip()
    return bool(v) and v != "-"

def run_once(type_name: str):
    user = os.getenv("EKIN_USER", "").strip()
    pw   = os.getenv("EKIN_PASS", "").strip()
    if not user or not pw:
        raise RuntimeError("Secrets EKIN_USER / EKIN_PASS belum diset.")

    device_id, device_name = get_device_identity_from_env()
    lat, lng = get_or_pick_coord()

    session = requests.Session()
    cookie = load_cookie()

    cookie = auth_initial(session, cookie)

    ok, msg, token, akun, cookie = login(session, user, pw, device_id, device_name, cookie)
    if not ok or not token or not akun:
        return False, msg, None

    ok2, msg2, cookie = auth(session, token, akun, cookie)
    if not ok2:
        return False, msg2, None

    ok3, info_obj_before, cookie = infoday_raw(session, token, akun, cookie)
    if not ok3:
        return False, f"INFODAY error: {info_obj_before}", info_obj_before

    before = extract_infodata(info_obj_before)

    # 1) ket filter
    skip_ket, reason = should_skip_by_ket(before["ket"])
    if skip_ket:
        return True, reason, before["raw"]

    # 2) sudah masuk/pulang? skip sndloc
    if type_name == "MASUK" and is_marked(before["masuk"]):
        return True, f"SKIP: sudah MASUK ({before['masuk']})", before["raw"]
    if type_name == "PULANG" and is_marked(before["pulang"]):
        return True, f"SKIP: sudah PULANG ({before['pulang']})", before["raw"]

    # sndloc
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

    ok_send = False
    msg_send = "SNDLOC respon tidak valid"
    try:
        obj = r.json()
        ret = int(obj.get("ret", 0) or 0)
        nama = str(obj.get("nama", "") or "")
        ok_send = (ret == 1)
        msg_send = (f"Berhasil: {nama}" if ok_send else f"Gagal: {nama}")
    except Exception:
        pass

    # infoday terakhir setelah sndloc (yang dikirim ke ntfy)
    ok4, info_obj_after, cookie = infoday_raw(session, token, akun, cookie)
    if not ok4:
        # tetap kirim sndloc result, tapi after gagal
        return ok_send, f"{msg_send} | INFODAY_AFTER error: {info_obj_after}", info_obj_after

    return ok_send, msg_send, info_obj_after

def main():
    now = tz_now_wib()
    today = now.date()

    # Window harus dari workflow .yml (wajib ada)
    masuk_start  = os.environ["WIN_MASUK_START"].strip()
    masuk_end    = os.environ["WIN_MASUK_END"].strip()
    pulang_start = os.environ["WIN_PULANG_START"].strip()
    pulang_end   = os.environ["WIN_PULANG_END"].strip()

    masuk_target = int(os.getenv("MASUK_TARGET_COUNT", "1"))
    pulang_target= int(os.getenv("PULANG_TARGET_COUNT","1"))

    tasks = []
    if in_window(now, masuk_start, masuk_end) and not already_done(today, "MASUK", masuk_target):
        tasks.append("MASUK")
    if in_window(now, pulang_start, pulang_end) and not already_done(today, "PULANG", pulang_target):
        tasks.append("PULANG")

    if not tasks:
        print(f"[{now}] Di luar window / sudah DONE hari ini. Exit.")
        return

    # concurrency sudah bikin antri antar workflow,
    # lock ini pengaman tambahan kalau ada edge case.
    if not acquire_lock():
        print("Lock aktif (ada job lain sedang jalan). Exit.")
        return

    try:
        # jitter kecil biar tidak “nabrak” persis di menit yang sama
        time.sleep(random.randint(2, 10))

        for tname in tasks:
            ok, msg, infoday_after_obj = run_once(tname)

            # simpan state agar tidak double walau cron 1 menit sekali
            mark_done(today, tname, ok, msg, infoday_after_obj)

            # notif: yang dikirim adalah INFODAY_AFTER (raw JSON) setelah sndloc
            try:
                after_text = json.dumps(infoday_after_obj, ensure_ascii=False)
            except Exception:
                after_text = str(infoday_after_obj)

            title = f"DKPP_AUTO {tname} {'OK' if ok else 'GAGAL'}"
            prio = "default" if ok else "high"
            ntfy(
                title,
                f"{tname}: {msg}\nWaktu: {tz_now_wib().isoformat(sep=' ', timespec='seconds')}\n\nINFODAY_AFTER:\n{after_text}",
                priority=prio
            )
            print(f"{tname}: {ok} - {msg}")

    finally:
        release_lock()

if __name__ == "__main__":
    main()
