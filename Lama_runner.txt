import os, json, time, random, csv, re
from pathlib import Path
from datetime import datetime, date, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ------------------ Endpoints ------------------
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


# ------------------ waktu (WIB) ------------------
def tz_now_wib() -> datetime:
    return datetime.utcnow() + timedelta(hours=7)

def parse_time_to_sec(t: str) -> int:
    h, m, s = [int(x) for x in t.split(":")]
    return h * 3600 + m * 60 + s

def in_window(now_local: datetime, start: str, end: str) -> bool:
    sec = now_local.hour * 3600 + now_local.minute * 60 + now_local.second
    return parse_time_to_sec(start) <= sec <= parse_time_to_sec(end)


# ------------------ ntfy (hanya dipakai saat sukses pertama) ------------------
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


# ------------------ json io ------------------
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


# ------------------ lock (pengaman tambahan) ------------------
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


# ------------------ identity dari ENV ------------------
def get_device_identity_from_env():
    device_id = os.getenv("DEVICE_ID_16", "").strip()
    device_name = os.getenv("DEVICE_NAME", "").strip()
    if not device_id or len(device_id) != 16:
        raise RuntimeError("ENV DEVICE_ID_16 wajib 16 karakter (tanam di .yml).")
    if not device_name:
        raise RuntimeError("ENV DEVICE_NAME wajib diisi (tanam di .yml).")
    return device_id, device_name


# ------------------ cookie persisten ------------------
def load_cookie_value() -> str | None:
    data = load_json(COOKIE_FILE, {})
    c = (data.get("cookie") or "").strip()
    return c or None

def save_cookie_value(cookie_value: str | None):
    save_json(COOKIE_FILE, {"cookie": cookie_value or ""})

def set_session_cookie(session: requests.Session, cookie_value: str | None):
    if not cookie_value or "=" not in cookie_value:
        return
    name, val = cookie_value.split("=", 1)
    session.cookies.set(name, val)

def extract_ekin_cookie_from_session(session: requests.Session) -> str | None:
    for c in session.cookies:
        if c.name == "ekinbarut":
            return f"{c.name}={c.value}"
    return None


# ------------------ session retry ------------------
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def base_headers():
    return {
        "Accept": ACCEPT_HEADER,
        "User-Agent": USER_AGENT,
        "Connection": "Keep-Alive",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    }

def post_form(session: requests.Session, url: str, data: dict):
    return session.post(url, data=data, headers=base_headers(), timeout=30)


# ------------------ rules ket + marked ------------------
def normalize_ket(ket: str) -> str:
    t = (ket or "").strip()
    if not t:
        return ""
    if all(ch == "-" for ch in t):
        return ""
    return t

def should_skip_by_ket(ket: str) -> tuple[bool, str]:
    k = normalize_ket(ket)
    if len(k) > 2:
        return True, f"SKIP karena keterangan: {k}"
    return False, ""

def is_marked(value: str) -> bool:
    v = (value or "").strip()
    return bool(v) and v != "-"


# ------------------ state per hari ------------------
def state_path_for(day: date, key: str) -> Path:
    return STATE_DIR / f"{day.isoformat()}_{key}.json"

def load_state(day: date, key: str) -> dict:
    return load_json(state_path_for(day, key), {}) or {}

def save_state(day: date, key: str, st: dict):
    save_json(state_path_for(day, key), st)

def done_count(day: date, key: str) -> int:
    st = load_state(day, key)
    return int(st.get("done_count", 0) or 0)

def already_done(day: date, key: str, target_count: int) -> bool:
    return done_count(day, key) >= target_count

def update_last(day: date, key: str, ok: bool, msg: str, infoday_obj: dict | None):
    st = load_state(day, key)
    st["last_run_at"] = tz_now_wib().isoformat(sep=" ", timespec="seconds")
    st["last_result_ok"] = bool(ok)
    st["last_message"] = msg
    st["last_infoday"] = infoday_obj
    save_state(day, key, st)

def mark_done(day: date, key: str, ok: bool, msg: str, infoday_obj: dict | None):
    st = load_state(day, key)
    st["done_count"] = int(st.get("done_count", 0) or 0) + 1
    st["done_at"] = tz_now_wib().isoformat(sep=" ", timespec="seconds")
    st["last_result_ok"] = bool(ok)
    st["last_message"] = msg
    st["last_infoday"] = infoday_obj
    save_state(day, key, st)

def success_notified(day: date, key: str) -> bool:
    st = load_state(day, key)
    return bool(st.get("notified_success", False))

def mark_success_notified(day: date, key: str):
    st = load_state(day, key)
    st["notified_success"] = True
    st["notified_at"] = tz_now_wib().isoformat(sep=" ", timespec="seconds")
    save_state(day, key, st)


# ------------------ koordinat ------------------
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
        raise RuntimeError("Isi koordinat.csv kosong / format salah (butuh kolom lat,lang).")
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


# ------------------ API wrappers ------------------
def auth_initial(session: requests.Session):
    post_form(session, AUTH_URL, {"token": "null", "akun": "null"})

def login(session: requests.Session, user: str, pw: str, imei: str, dname: str):
    r = post_form(session, LOGIN_URL, {"user": user, "pass": pw, "imei": imei, "dname": dname})
    try:
        obj = r.json()
    except Exception:
        return False, "Login respon tidak valid", None, None

    status = int(obj.get("status", 0) or 0)
    msg = str(obj.get("msg", "") or "")
    if status == 1:
        return True, "Login berhasil", str(obj.get("token", "") or ""), str(obj.get("akun", "") or "")
    return False, f"Login gagal: {msg}", None, None

def auth(session: requests.Session, token: str, akun: str):
    r = post_form(session, AUTH_URL, {"token": token, "akun": akun})
    try:
        obj = r.json()
        status = int(obj.get("status", 0) or 0)
        return (status == 1), ("AUTH ok" if status == 1 else "AUTH gagal")
    except Exception:
        return False, "AUTH respon tidak valid"

def infoday_raw(session: requests.Session, token: str, akun: str):
    r = post_form(session, INFODAY_URL, {"token": token, "akun": akun})
    if not r.ok:
        return False, {"error": f"HTTP {r.status_code}"}
    try:
        return True, r.json()
    except Exception:
        return False, {"error": "JSON parse error"}

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

def sndloc(session: requests.Session, token: str, akun: str, lat: float, lng: float, type_name: str):
    stimo = 1 if type_name == "MASUK" else 2
    r = post_form(session, SNDLOC_URL, {
        "token": token,
        "akun": akun,
        "lat": str(lat),
        "lang": str(lng),
        "stimo": str(stimo),
        "status": "1",
    })
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
    return ok_send, msg_send


# ------------------ parse window dari INFODAY ------------------
_WINDOW_RE = re.compile(r"(\d{2}:\d{2}:\d{2}).*?(\d{2}:\d{2}:\d{2})")

def parse_infoday_window(jstr: str) -> tuple[str, str] | None:
    s = (jstr or "").strip()
    m = _WINDOW_RE.search(s)
    if not m:
        return None
    return m.group(1), m.group(2)


# ------------------ core run_once ------------------
def run_once(type_name: str):
    user = os.getenv("EKIN_USER", "").strip()
    pw   = os.getenv("EKIN_PASS", "").strip()
    if not user or not pw:
        raise RuntimeError("Secrets DKPP*_USER / DKPP*_PASS belum diset.")

    device_id, device_name = get_device_identity_from_env()
    lat, lng = get_or_pick_coord()

    session = make_session()
    set_session_cookie(session, load_cookie_value())

    # AUTH initial (refresh cookie)
    auth_initial(session)
    save_cookie_value(extract_ekin_cookie_from_session(session))

    # LOGIN
    ok, msg, token, akun = login(session, user, pw, device_id, device_name)
    save_cookie_value(extract_ekin_cookie_from_session(session))
    if not ok or not token or not akun:
        return False, msg, None, None

    # AUTH
    ok2, msg2 = auth(session, token, akun)
    save_cookie_value(extract_ekin_cookie_from_session(session))
    if not ok2:
        return False, msg2, None, None

    # INFODAY sebelum
    ok3, info_before_obj = infoday_raw(session, token, akun)
    save_cookie_value(extract_ekin_cookie_from_session(session))
    if not ok3:
        return False, f"INFODAY error: {info_before_obj}", info_before_obj, None

    before = extract_infodata(info_before_obj)

    # ket filter
    skip_ket, reason = should_skip_by_ket(before["ket"])
    if skip_ket:
        return True, reason, before["raw"], before

    # sudah masuk/pulang?
    if type_name == "MASUK" and is_marked(before["masuk"]):
        return True, f"SKIP: sudah MASUK ({before['masuk']})", before["raw"], before
    if type_name == "PULANG" and is_marked(before["pulang"]):
        return True, f"SKIP: sudah PULANG ({before['pulang']})", before["raw"], before

    # SNDLOC
    ok_send, msg_send = sndloc(session, token, akun, lat, lng, type_name)
    save_cookie_value(extract_ekin_cookie_from_session(session))

    # INFODAY setelah
    ok4, info_after_obj = infoday_raw(session, token, akun)
    save_cookie_value(extract_ekin_cookie_from_session(session))
    if not ok4:
        return ok_send, f"{msg_send} | INFODAY_AFTER error: {info_after_obj}", info_after_obj, before

    return ok_send, msg_send, info_after_obj, before


# ------------------ main ------------------
def main():
    now = tz_now_wib()
    today = now.date()

    # fallback window dari ENV (batas lebar)
    env_masuk_start  = os.environ.get("WIN_MASUK_START", "06:15:00").strip()
    env_masuk_end    = os.environ.get("WIN_MASUK_END",   "08:15:00").strip()
    env_pulang_start = os.environ.get("WIN_PULANG_START","15:00:00").strip()
    env_pulang_end   = os.environ.get("WIN_PULANG_END",  "18:00:00").strip()

    masuk_target  = int(os.getenv("MASUK_TARGET_COUNT", "1"))
    pulang_target = int(os.getenv("PULANG_TARGET_COUNT", "1"))

    # hemat: cek window lebar dulu (biar tidak hit INFODAY kalau di luar jam)
    possible = []
    if in_window(now, env_masuk_start, env_masuk_end) and not already_done(today, "MASUK", masuk_target):
        possible.append("MASUK")
    if in_window(now, env_pulang_start, env_pulang_end) and not already_done(today, "PULANG", pulang_target):
        possible.append("PULANG")

    if not possible:
        print(f"[{now}] Di luar window lebar / sudah DONE hari ini. Exit.")
        return

    if not acquire_lock():
        print("Lock aktif (ada job lain sedang jalan). Exit.")
        return

    try:
        time.sleep(random.randint(2, 10))  # jitter kecil

        for tname in possible:
            ok, msg, infoday_obj, before_parsed = run_once(tname)

            # window final dari INFODAY (kalau bisa), fallback ke ENV
            final_start, final_end = None, None
            if isinstance(before_parsed, dict):
                w = parse_infoday_window(before_parsed.get("jmasuk" if tname == "MASUK" else "jpulang", ""))
                if w:
                    final_start, final_end = w

            if not final_start or not final_end:
                final_start, final_end = (env_masuk_start, env_masuk_end) if tname == "MASUK" else (env_pulang_start, env_pulang_end)

            # jika ternyata di luar window final, jangan DONE / jangan NTFY
            if not in_window(now, final_start, final_end):
                update_last(today, tname, ok, f"SKIP luar window FINAL {final_start}-{final_end} | {msg}", infoday_obj)
                print(f"[{now}] {tname}: di luar window FINAL ({final_start}-{final_end}). Skip.")
                continue

            # simpan status terakhir
            update_last(today, tname, ok, msg, infoday_obj)

            # sukses sndloc?
            is_success = ok and (msg or "").strip().lower().startswith("berhasil:")

            # DONE hanya kalau sukses dan belum done hari ini
            if is_success and not already_done(today, tname, 1):
                mark_done(today, tname, ok, msg, infoday_obj)

                # NTFY hanya sekali saat sukses pertama
                if not success_notified(today, tname):
                    try:
                        payload = json.dumps(infoday_obj, ensure_ascii=False)
                    except Exception:
                        payload = str(infoday_obj)

                    ntfy(
                        f"DKPP_AUTO {tname} OK",
                        f"{tname}: {msg}\nWaktu: {tz_now_wib().isoformat(sep=' ', timespec='seconds')}\n\nINFODAY_AFTER:\n{payload}",
                        priority="default",
                    )
                    mark_success_notified(today, tname)

            print(f"{tname}: {ok} - {msg}")

    finally:
        release_lock()


if __name__ == "__main__":
    main()
