import io
import os
import re
import csv
import json
import math
import time
import random
import sqlite3
import base64
import requests
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, Response, session, redirect, url_for
from groq import Groq

register_heif_opener()
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sf_9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e")

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

VISION_MODEL = "qwen/qwen3.8-27b"
RECIPE_MODEL = "openai/gpt-oss-120b"

MEALS = {
    "tez": ("⚡", "Tez hazırlanan", "20 dəqiqəyə qədər"),
    "seher": ("🍳", "Səhər yeməyi", "yüngül və doyumlu"),
    "nahar": ("🍲", "Nahar", "doyumlu əsas yemək"),
    "sam": ("🍽️", "Gözəl şam yeməyi", "xüsusi və təsirli"),
    "saglam": ("🥗", "Sağlam / yüngül", "az yağlı, tərəvəzli"),
    "desert": ("🍰", "Desert", "şirin bir şey"),
}

MEAL_HINTS = {
    "tez": "Maksimum 20 dəqiqəyə hazır olan, sadə və sürətli resept.",
    "seher": "Səhər yeməyi üçün yüngül, doyumlu resept.",
    "nahar": "Nahar üçün doyumlu, əsas yemək resepti.",
    "sam": "Axşam üçün xüsusi, gözəl təqdimatlı, restoran səviyyəli şam yeməyi resepti.",
    "saglam": "Sağlam, yüngül, az yağlı və tərəvəzli resept.",
    "desert": "Şirin desert resepti.",
}

EXPIRY_WARN_DAYS = 7


def parse_json(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Cavabda JSON tapılmadı")
    return json.loads(text[start:end + 1])


def ask(model, messages, max_tokens=3000, temperature=0.6, **extra):
    if temperature == 0:
        extra["seed"] = 42
    completion = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_completion_tokens=max_tokens,
        response_format={"type": "json_object"},
        **extra,
    )
    return parse_json(completion.choices[0].message.content)


def image_to_data_url(file):
    img = Image.open(file.stream)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"


def cap(text):
    text = str(text).strip()
    if not text:
        return ""
    first = "İ" if text[0] == "i" else text[0].upper()
    return first + text[1:]


def clean_list(items):
    seen, result = set(), []
    for item in items:
        key = str(item).strip().lower()
        name = cap(item)
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def days_text(days):
    return "bu gün son gündür" if days == 0 else f"{days} gün qalıb"


def read_expiry(chosen):
    today = date.today()
    expiring, expired = [], []
    for key, value in request.form.items():
        if not key.startswith("exp::") or not value:
            continue
        name = cap(key[5:])
        if name not in chosen:
            continue
        try:
            days = (datetime.strptime(value, "%Y-%m-%d").date() - today).days
        except ValueError:
            continue
        if days < 0:
            expired.append(name)
        elif days <= EXPIRY_WARN_DAYS:
            expiring.append((name, days))
    expiring.sort(key=lambda x: x[1])
    return expiring, expired


def render(step, **kwargs):
    return render_template("index.html", step=step, meals=MEALS, **kwargs)


# ====================== AUTHENTICATION (Google Login) ======================

@app.route("/auth")
def auth_page():
    if "user_email" in session:
        return redirect(url_for("home"))
    return render_template("auth.html")


@app.route("/login")
def google_login():
    redirect_uri = url_for("google_callback", _external=True)
    google_auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}&response_type=code&scope=openid%20email%20profile"
    )
    return redirect(google_auth_url)


@app.route("/auth/callback")
def google_callback():
    code = request.args.get("code")
    if not code:
        return redirect(url_for("auth_page"))
    
    redirect_uri = url_for("google_callback", _external=True)
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"
    }
    token_res = requests.post(token_url, data=data)
    token_json = token_res.json()
    access_token = token_json.get("access_token")
    
    if not access_token:
        return render_template("auth.html", error="Google ilə giriş xətası baş verdi.")
    
    userinfo_res = requests.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    userinfo = userinfo_res.json()
    email = userinfo.get("email")
    name = userinfo.get("name", "")
    avatar = userinfo.get("picture", "")
    
    if email:
        with get_db() as c:
            u = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if not u:
                c.execute("INSERT INTO users(email, name, avatar, created_at) VALUES(?,?,?,?)",
                          (email, name, avatar, now_s()))
            session["user_email"] = email
            session["user_name"] = name
            session["user_avatar"] = avatar
            log_event(c, "info", f"İstifadəçi daxil oldu: {email}")
        return redirect(url_for("home"))
    
    return redirect(url_for("auth_page"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth_page"))


# ====================== APP ROUTES ======================

@app.route("/", methods=["GET"])
def home():
    if "user_email" not in session:
        return redirect(url_for("auth_page"))
    return render_template("home.html")


@app.route("/scanner", methods=["GET"])
def scanner():
    if "user_email" not in session:
        return redirect(url_for("auth_page"))
    return render("upload")


@app.route("/analyze", methods=["POST"])
def analyze():
    if "user_email" not in session:
        return redirect(url_for("auth_page"))
    file = request.files.get("image")
    if not file or file.filename == "":
        return render("upload", error="Zəhmət olmasa şəkil seçin.")

    try:
        data_url = image_to_data_url(file)
    except Exception as e:
        print("ŞƏKİL XƏTASI:", e)
        return render("upload", error="Şəkil oxunmadı. JPG, PNG, WebP, HEIC və ya AVIF yükləyin.")

    prompt = """Bu şəkildəki yeməli ərzaqları müəyyən et.
Qaydalar:
- Şəkli hissə-hissə yoxla: üst rəf, orta rəf, alt rəf, tərəvəz çekmeceleri, qapı rəfləri. Heç bir hissəni buraxma.
- Şüşə qab, bankalar və qablaşdırılmış məhsullar daxil olmaqla, içində nə olduğunu təxmin edə bildiklərini də yaz.
- "sure" siyahısına yalnız 100% əmin olduğun, aydın görünən məhsulları yaz.
- "unsure" siyahısına əmin olmadığın, bulanıq və ya qismən görünən məhsulları yaz.
- Şəkildə olmayan heç nəyi yazma.
- Konkret ad yaz: "tərəvəz", "meyvə" kimi ümumi sözlər yazma (pomidor, yerkökü, albalı yaz).
- Hər məhsulu yalnız bir dəfə yaz. Adları sadə Azərbaycan dilində yaz.
Cavabı yalnız bu JSON formatında ver:
{"sure": ["...", "..."], "unsure": ["..."]}"""

    try:
        result = ask(
            VISION_MODEL,
            [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            max_tokens=900,
            temperature=0,
            reasoning_effort="none",
        )
        ingredients = clean_list(result.get("sure", []))
        unsure = [x for x in clean_list(result.get("unsure", [])) if x not in ingredients]
    except Exception as e:
        print("VİZUAL XƏTA:", e)
        return render("upload", error="Şəkil analiz edilə bilmədi. Bir az sonra yenidən cəhd edin.")

    if not ingredients and not unsure:
        return render("upload", error="Şəkildə ərzaq tapılmadı. Soyuducunun və ya ərzaqların real şəklini yükləyin!")

    return render("choose", ingredients=ingredients, unsure=unsure, selected_meal="tez")


@app.route("/recipe", methods=["POST"])
def recipe():
    if "user_email" not in session:
        return redirect(url_for("auth_page"))
    detected = clean_list(request.form.getlist("all_ingredients"))
    chosen = request.form.getlist("ingredients")
    extra = [x.strip() for x in request.form.get("custom", "").split(",") if x.strip()]
    chosen = clean_list(chosen + extra)

    meal = request.form.get("meal", "tez")
    if meal not in MEALS:
        meal = "tez"

    try:
        servings = max(1, min(int(request.form.get("servings", "2")), 15))
    except ValueError:
        servings = 2

    allow_extra = request.form.get("allow_extra") == "on"

    expiring, expired = read_expiry(chosen)
    chosen = [x for x in chosen if x not in expired]

    if not chosen:
        msg = "Ən azı bir məhsul seçin."
        if expired:
            msg = "Seçdiyiniz məhsulların son istifadə tarixi keçib: " + ", ".join(expired) + ". Başqa məhsul seçin."
        return render("choose", ingredients=detected, selected_meal=meal, error=msg)

    extra_rule = ("Seçilmiş məhsullara əlavə olaraq 1-2 sadə məhsul (məsələn, un, düyü, süd) lazım ola bilər, "
                  "bunları 'ingredients' siyahısında ayrıca qeyd et."
                  if allow_extra else
                  "YALNIZ seçilmiş məhsullardan və evdə həmişə olan şeylərdən (duz, istiot, yağ, su) istifadə et.")

    expiry_rule = ""
    if expiring:
        items = ", ".join(f"{n} ({days_text(d)})" for n, d in expiring)
        expiry_rule = ("Bu məhsulların vaxtı tezliklə bitir, resepti MÜMKÜN QƏDƏR əvvəlcə bunlardan qur: "
                       + items + ".")

    prompt = f"""Sən təcrübəli Azərbaycanlı aşpazsan. Aşağıdakı məhsullardan resept hazırla.

Seçilmiş məhsullar: {", ".join(chosen)}
Resept növü: {MEAL_HINTS[meal]}
Porsiya sayı: {servings}
{extra_rule}
{expiry_rule}
Seçilmiş məhsulların hamısını işlətmək məcburi deyil, ən yaxşı nəticə üçün uyğun olanları seç.

DİL QAYDALARI (çox vacib):
- Cavab təmiz, təbii və anlaşılan Azərbaycan dilində olsun. Söz-söz tərcümə kimi səslənməsin.
- Başqa dildən söz qarışdırma. Məhsul adları başqa dildə (ingilis, rus, türk və s.) yazılıbsa, reseptdə onları Azərbaycan dilinə çevir.
- Sadə bişirmə feillərindən istifadə et: doğrayın, yuyun, qızardın, qaynadın, bişirin, qarışdırın, əlavə edin, süzün.
- Hər addım fərqli olsun, heç bir addımı təkrarlama. 4-8 addım yaz.

Cavabı yalnız bu JSON formatında ver:
{{
  "title": "Reseptin adı",
  "intro": "1-2 cümləlik iştahaaçan təsvir",
  "time": "məsələn: 25 dəqiqə",
  "difficulty": "Asan, Orta və ya Çətin",
  "servings": "{servings} nəfərlik",
  "ingredients": [{{"name": "məhsul", "amount": "miqdar"}}],
  "steps": ["1-ci addım", "2-ci addım"],
   "used_expiring": ["vaxtı bitən məhsullardan reseptdə həqiqətən işlətdiklərin, adları mənə verilən siyahıdakı kimi DƏYİŞMƏDƏN yaz; yoxdursa boş siyahı"],
  "tip": "bir qısa faydalı məsləhət"
}}"""

    try:
        data = ask(RECIPE_MODEL, [{"role": "user", "content": prompt}])
    except Exception as e:
        print("RESEPT XƏTASI:", e)
        return render("choose", ingredients=detected or chosen, selected_meal=meal,
                    error="Resept hazırlana bilmədi. Yenidən cəhd edin.")

    allowed = {n.lower() for n, _ in expiring}
    data["used_expiring"] = [x for x in data.get("used_expiring", []) if str(x).strip().lower() in allowed]

    return render("recipe", data=data, meal=MEALS[meal], used=chosen, expired=expired)


@app.route("/chat", methods=["POST"])
def chat():
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    try:
        req_data = request.get_json()
        messages = [
            {"role": m["role"], "content": str(m["content"])[:1000]}
            for m in req_data.get("messages", [])[-12:]
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
        ]
        recipe_context = str(req_data.get("recipe_context", ""))[:3000]
        
        system_prompt = {
            "role": "system",
            "content": (
                "Sən 'SmartFridge AI' tətbiqinin peşəkar süni intellekt aşpazısan. "
                "Sənin vəzifən YALNIZ VƏ YALNIZ mətbəx, yemək reseptləri, ərzaqlar, aşpazlıq və süfrə təamları haqqında danışmaqdır.\n\n"
                "QƏTİ QADAĞA VƏ SƏRHƏDLƏR:\n"
                "1. Mətbəxdən və yeməkdənkənar heç bir suala (tarix, siyasət, coğrafiya, ümumi biliklər və s.) cavab vermə! Əgər kənar sual verilsə, dərhal sərt və nəzakətli şəkildə de: 'Mən yalnız SmartFridge AI aşpazıyam, sənə yalnız yemək və reseptlər üzrə kömək edə bilərəm.'\n"
                "2. Qətiyyən ulduz (`**`) işarəsi işlətmə.\n"
                "3. Hər bir ərzaq, addım və ya bənddən sonra MÜTLƏQ yeni sətrə (enter) keç. Mətni heç vaxt dalbadal, uzun bir sətr şəklində yığma.\n"
                "4. Siyahıları səliqəli nömrələrlə (1., 2., 3.) və ya tire (-) ilə, hər biri ayrı sətrdə olmaqla yaz."
            )
        }
        
        completion = client.chat.completions.create(
            model=RECIPE_MODEL, 
            messages=[system_prompt]
            + ([{"role": "system", "content": "İstifadəçinin hazırda baxdığı resept: " + recipe_context}] if recipe_context else [])
            + messages,
            temperature=0.3,
            max_completion_tokens=3000,
        )
        
        reply = completion.choices[0].message.content
        
        if reply:
            reply = re.sub(r"\|---+\|", "", reply)
            reply = reply.replace("**", "")

        return json.dumps({"reply": reply}), 200, {"Content-Type": "application/json"}
    
    except Exception as e:
        print("CHAT XƏTASI:", e)
        return json.dumps({"error": "Söhbət zamanı xəta baş verdi."}), 500, {"Content-Type": "application/json"}


# ====================== SMART FRIDGE (2-ci hissə) ======================

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smartfridge.db")

ZONES = [
    ("top", "Üst rəf", 4.0, 45),
    ("middle", "Orta rəf", 3.5, 50),
    ("bottom", "Alt rəf", 1.0, 55),
    ("crisper_veg", "Tərəvəz çəkməcəsi", 6.0, 90),
    ("crisper_fruit", "Meyvə çəkməcəsi", 5.0, 80),
    ("door", "Qapı rəfi", 7.0, 45),
    ("freezer", "Dondurucu", -18.0, 40),
]

CATEGORIES = [
    ("frozen", "Dondurulmuş məhsullar", "freezer", -22, -16, 90, "",
     "Dondurulmuş məhsul yalnız dondurucuda saxlanmalıdır.",
     "dondur,buz,pelmeni,nuggets"),
    ("meat", "Ət və quş əti", "bottom", 0, 2, 3, "",
     "Çiy ət tez xarab olur, ən soyuq rəf lazımdır.",
     "ət,toyuq,mal əti,quzu,qiymə,kotlet,şnitsel,filə,sosis,kolbasa,hindiquşu"),
    ("fish", "Balıq və dəniz məhsulları", "bottom", 0, 2, 2, "",
     "Balıq çox tez xarab olur, ən soyuq rəf lazımdır.",
     "balıq,qızılbalıq,kürü,krevet,kalmar,midiya,tuna,siyənək"),
    ("dairy", "Süd məhsulları", "middle", 2, 5, 7, "",
     "Süd məhsulları qapı rəfinin temperatur dəyişkənliyinə həssasdır.",
     "süd,qatıq,kefir,pendir,kərə,qaymaq,xama,ayran,süzmə,motal,yoqurt"),
    ("eggs", "Yumurta", "middle", 2, 5, 21, "",
     "Yumurta sabit temperaturda saxlanmalıdır.",
     "yumurta"),
    ("leftovers", "Hazır yeməklər", "top", 2, 5, 3, "",
     "Bişmiş yemək 3 gün ərzində işlədilməlidir.",
     "plov,şorba,dolma,xörək,qalıq,yemək,lazanya,pizza,sendviç,salat"),
    ("sauces", "Souslar və konservlər", "door", 4, 10, 60, "",
     "Souslar və turşular daha az soyuqluq qəbul edir.",
     "ketçup,mayonez,xardal,sous,tomat,cem,mürəbbə,turşu,zeytun,bal$,pesto,sirkə,tahin"),
    ("drinks", "İçkilər", "door", 4, 10, 30, "",
     "İçkilər qapı rəfində saxlanıla bilər.",
     "içki,kompot,şirə,kola,limonad,pepsi,fanta,sprite,mineral,su$,çay,kofe"),
    ("fruit", "Meyvə", "crisper_fruit", 4, 8, 7, "producer",
     "Meyvələr etilen qazı ayırır, tərəvəzlərdən ayrı saxla.",
     "alma,armud,banan,üzüm,albalı,gilas,çiyələk,shaftalı,nar,portağal,naringi,limon,kivi,heyva,ərik,gavalı,innab,qarpız,yemiş,ananas,mango,xurma,əncir,moruq,qarağat,böyürtkən"),
    ("veg", "Tərəvəz", "crisper_veg", 4, 8, 7, "sensitive",
     "Tərəvəzlər yüksək rütubət istəyir və etilenə həssasdır.",
     "pomidor,xiyar,bibər,badımcan,yerkökü,kök,kələm,kahı,ispanaq,göyərti,keşniş,şüyüd,cəfəri,reyhan,turp,çuğundur,brokoli,balqabaq,qarğıdalı,göbələk,lobya,noxud"),
    ("other", "Digər", "top", 2, 6, 7, "", "Ümumi saxlama.", ""),
]

DEMO = [
    ("Süd", "door", 4), ("Qatıq", "middle", 5), ("Yumurta", "middle", 14),
    ("Pendir", "middle", 10), ("Toyuq filesi", "top", 2), ("Sosis", "bottom", -1),
    ("Balıq", "middle", 2), ("Alma", "crisper_veg", 6), ("Xiyar", "crisper_veg", 5),
    ("Yerkökü", "crisper_veg", 9), ("Çiyələk", "crisper_fruit", 1), ("Plov", "top", 1),
    ("Ketçup", "door", 90), ("Ketçup", "door", 90), ("Ketçup", "door", 90), ("Kompot", "door", 12), ("Dondurma", "freezer", 60),
]
USAGE_WINDOW = 28

USAGE_PROFILE = [
    ("Süd", 0.5), ("Yumurta", 1.2), ("Qatıq", 0.15), ("Pendir", 0.1),
    ("Toyuq filesi", 0.3), ("Alma", 0.2), ("Xiyar", 0.25), ("Yerkökü", 0.1),
    ("Çiyələk", 0.1), ("Kompot", 0.1), ("Ketçup", 0.03),
    ("Çörək", 1.0), ("Banan", 0.3),
]


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_s():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    with get_db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT,
            avatar TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS zones(
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, name TEXT,
            default_temp REAL, setpoint REAL, target_hum REAL,
            temp REAL, humidity REAL, gas REAL DEFAULT 0, flagged INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS categories(
            key TEXT PRIMARY KEY, label TEXT, ideal_zone TEXT, temp_min REAL, temp_max REAL,
            shelf_days INTEGER, ethylene TEXT, tip TEXT, keywords TEXT);
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, category TEXT,
            zone_key TEXT, added_at TEXT, expiry TEXT);
        CREATE TABLE IF NOT EXISTS sensor_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, zone_key TEXT,
            temp REAL, humidity REAL, gas REAL);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, message TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS usage_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, name TEXT, qty REAL);
        """)
        if c.execute("SELECT COUNT(*) FROM zones").fetchone()[0] == 0:
            for key, name, temp, hum in ZONES:
                c.execute(
                    "INSERT INTO zones(key,name,default_temp,setpoint,target_hum,temp,humidity) "
                    "VALUES(?,?,?,?,?,?,?)", (key, name, temp, temp, hum, temp, hum))
        for cat in CATEGORIES:
            c.execute("INSERT OR REPLACE INTO categories VALUES(?,?,?,?,?,?,?,?,?)", cat)


def get_setting(c, key, default=None):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(c, key, value):
    c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))


def log_event(c, kind, message):
    c.execute("INSERT INTO events(ts,kind,message) VALUES(?,?,?)", (now_s(), kind, message))
    c.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - 200")


def az_lower(s):
    return str(s).replace("İ", "i").replace("I", "ı").lower()


def guess_category(cat_rows, name):
    n = az_lower(name)
    tokens = [t for t in re.split(r"[\s,;/()\-]+", n) if t]
    for cat in cat_rows:
        for kw in (k.strip() for k in cat["keywords"].split(",")):
            if not kw:
                continue
            if kw.endswith("$"):
                if kw[:-1] in tokens:
                    return cat["key"]
            elif " " in kw:
                if kw in n:
                    return cat["key"]
            elif any(t.startswith(kw) for t in tokens):
                return cat["key"]
    return "other"


def tick(c):
    now = time.time()
    if now - float(get_setting(c, "last_tick", 0)) < 2.0:
        return
    set_setting(c, "last_tick", now)
    mode = get_setting(c, "mode", "auto")
    
    door_open_state = int(get_setting(c, "door_open_state", 0))

    producers = {}
    rows = c.execute(
        "SELECT p.zone_key AS zk FROM products p JOIN categories k ON k.key = p.category "
        "WHERE k.ethylene = 'producer'").fetchall()
    for r in rows:
        producers[r["zk"]] = producers.get(r["zk"], 0) + 1

    do_log = now - float(get_setting(c, "last_log", 0)) >= 10
    if do_log:
        set_setting(c, "last_log", now)
    ts = now_s()

    for z in c.execute("SELECT * FROM zones").fetchall():
        sp = z["setpoint"]
        
        if door_open_state and z["key"] in ('door', 'top', 'middle'):
            rate = 0.25
        else:
            rate = 0.35 if mode == "auto" else 0.15

        t = z["temp"] + (sp - z["temp"]) * rate + random.uniform(-0.08, 0.08)
        h = z["humidity"] + (z["target_hum"] - z["humidity"]) * 0.2 + random.uniform(-1.0, 1.0)
        h = max(5, min(99, h))
        gas = producers.get(z["key"], 0) * 2.2 + random.uniform(0, 0.5)

        flagged = z["flagged"]
        dev = abs(t - sp)
        if dev > 2.0 and not flagged:
            flagged = 1
        elif dev < 0.8 and flagged:
            flagged = 0

        c.execute("UPDATE zones SET temp=?, humidity=?, gas=?, flagged=? WHERE id=?",
                  (round(t, 2), round(h, 1), round(gas, 1), flagged, z["id"]))
        if do_log:
            c.execute("INSERT INTO sensor_log(ts,zone_key,temp,humidity,gas) VALUES(?,?,?,?,?)",
                      (ts, z["key"], round(t, 2), round(h, 1), round(gas, 1)))
    if do_log:
        c.execute("DELETE FROM sensor_log WHERE id <= (SELECT MAX(id) FROM sensor_log) - 20000")


def build_suggestions(zmap, products, cats):
    out = []
    types = {}
    for p in products:
        e = cats.get(p["category"], cats["other"])["ethylene"]
        if e:
            types.setdefault(p["zone_key"], set()).add(e)

    for p in products:
        c = cats.get(p["category"], cats["other"])
        z = zmap[p["zone_key"]]
        ideal = zmap.get(c["ideal_zone"])
        name = p["name"]

        if c["key"] != "other" and ideal and not (c["temp_min"] - 0.6 <= z["setpoint"] <= c["temp_max"] + 0.6):
            sev = "high" if c["key"] in ("meat", "fish", "dairy", "leftovers") else "mid"
            out.append({
                "type": "move", "severity": sev, "product_id": p["id"], "to_zone": ideal["key"],
                "title": f"«{name}» yeri dəyişilməlidir",
                "text": f"{z['name']} ({z['setpoint']:.0f}°C) bu məhsul üçün uyğun deyil, ideal "
                        f"{c['temp_min']:g}…{c['temp_max']:g}°C. Təklif: {z['name']} → {ideal['name']}. {c['tip']}"})
        elif c["ethylene"] and ideal and p["zone_key"] != c["ideal_zone"]:
            opposite = "sensitive" if c["ethylene"] == "producer" else "producer"
            if opposite in types.get(p["zone_key"], set()):
                why = ("etilen qazı ayırır və eyni bölmədəki tərəvəzlərin tez solmasına səbəb olur."
                       if c["ethylene"] == "producer"
                       else "eyni bölmədəki meyvələrin ayırdığı etilen qazına həssasdır.")
                out.append({
                    "type": "move", "severity": "mid", "product_id": p["id"], "to_zone": ideal["key"],
                    "title": f"«{name}» ayrı bölməyə keçməlidir",
                    "text": f"«{name}» {why} Təklif: {z['name']} → {ideal['name']}."})

        d = p.get("days_left")
        if d is not None:
            if d < 0:
                out.append({"type": "expiry", "severity": "high", "product_id": p["id"], "action": "discard",
                            "title": f"«{name}» vaxtı keçib",
                            "text": f"Son istifadə tarixi {-d} gün əvvəl bitib. Atmaq məsləhətdir."})
            elif d <= 2:
                out.append({"type": "expiry", "severity": "mid", "product_id": p["id"], "action": "recipe",
                            "title": f"«{name}» tezliklə xarab olacaq",
                            "text": f"{days_text(d).capitalize()}. Bu məhsuldan resept hazırla."})

    for z in zmap.values():
        dev = z["temp"] - z["setpoint"]
        if abs(dev) > 2.0:
            out.append({"type": "temp", "severity": "mid", "title": f"{z['name']}: temperatur sapması",
                        "text": f"İndiki {z['temp']:.1f}°C, hədəf {z['setpoint']:.1f}°C. Qapının bağlı olduğunu yoxla."})

    order = {"high": 0, "mid": 1, "low": 2}
    out.sort(key=lambda s: order[s["severity"]])
    return out


def seed_usage(c):
    rng = random.Random(7)
    c.execute("DELETE FROM usage_log")
    today = date.today()
    for i in range(USAGE_WINDOW):
        d = today - timedelta(days=i)
        wk = 1.25 if d.weekday() >= 5 else 1.0
        for name, rate in USAGE_PROFILE:
            r = rate * wk * rng.uniform(0.5, 1.5)
            q = int(r) + (1 if rng.random() < r - int(r) else 0)
            if q:
                c.execute("INSERT INTO usage_log(day,name,qty) VALUES(?,?,?)", (d.isoformat(), name, q))


def build_shopping(c):
    today = date.today()
    since = today - timedelta(days=USAGE_WINDOW - 1)
    cat_rows = c.execute("SELECT * FROM categories ORDER BY rowid").fetchall()
    shelf_of = {r["key"]: r["shelf_days"] for r in cat_rows}

    stock = {}
    for r in c.execute("SELECT name FROM products"):
        k = az_lower(r["name"])
        stock[k] = stock.get(k, 0) + 1

    usage, rows = {}, 0
    for r in c.execute("SELECT day,name,qty FROM usage_log WHERE day >= ?", (since.isoformat(),)):
        try:
            d = date.fromisoformat(r["day"])
        except ValueError:
            continue
        rows += 1
        u = usage.setdefault(az_lower(r["name"]),
                             {"name": cap(r["name"]), "total": 0.0, "first": d, "weeks": [0.0] * 4})
        u["total"] += r["qty"]
        u["first"] = min(u["first"], d)
        w = (today - d).days // 7
        if 0 <= w < 4:
            u["weeks"][w] += r["qty"]

    items = []
    for k, u in usage.items():
        span = max(7, (today - u["first"]).days + 1)
        daily = u["total"] / span
        if daily <= 0:
            continue
        n = stock.get(k, 0)
        shelf = shelf_of.get(guess_category(cat_rows, u["name"]), 7)
        cover = n / daily
        weekly = round(daily * 7, 1)
        need = max(1, math.ceil(daily * min(7, shelf)) - n)
        buy = 0

        if n >= 2 and n > daily * shelf:
            status, sev = "skip", "low"
            text = (f"Lazımdan çoxdur: {n} ədəd var, saxlanma müddətində təxminən "
                    f"{daily * shelf:.0f} ədəd işlədəcəksiniz. Almayın, xarab ola bilər.")
        elif n == 0:
            status, sev, buy = "buy", "high", need
            text = f"Soyuducuda qalmayıb. Orta istifadə: həftədə {weekly:g} ədəd."
        elif cover < 2:
            status, sev, buy = "buy", "high", need
            text = f"Ehtiyat təxminən {cover:.1f} günə çatır. Həftədə {weekly:g} ədəd işlədirsiniz."
        elif cover < 4:
            status, sev, buy = "buy", "mid", need
            text = f"Ehtiyat təxminən {cover:.0f} günə çatır. Tezliklə almaq lazım olacaq."
        elif cover < 7:
            status, sev, buy = "soon", "low", need
            text = f"Ehtiyat təxminən {cover:.0f} günə çatır. Bu həftə almaq olar."
        else:
            status, sev = "ok", "low"
            text = f"Ehtiyat kifayətdir (təxminən {min(cover, 99):.0f} gün)."

        items.append({"name": u["name"], "status": status, "severity": sev, "stock": n,
                      "weekly": weekly, "cover": round(cover, 1), "buy": buy,
                      "weeks": [round(w, 1) for w in u["weeks"]], "text": text})

    rank = {"buy": 0, "soon": 1, "skip": 2, "ok": 3}
    items.sort(key=lambda i: (rank[i["status"]], i["cover"]))
    return {"items": items, "rows": rows}


@app.route("/api/fridge/usage", methods=["POST"])
def fridge_usage():
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    d = request.get_json(silent=True) or {}
    name = cap(str(d.get("name", "")).strip()[:60])
    if not name:
        return jsonify(error="Məhsulun adını yaz."), 400
    with get_db() as c:
        c.execute("INSERT INTO usage_log(day,name,qty) VALUES(?,?,?)", (date.today().isoformat(), name, int(d.get("qty", 1))))
        removed = 0
        for r in c.execute("SELECT id,name FROM products ORDER BY id").fetchall():
            if removed < int(d.get("qty", 1)) and az_lower(r["name"]) == az_lower(name):
                c.execute("DELETE FROM products WHERE id=?", (r["id"],))
                removed += 1
        log_event(c, "info", f"İstifadə qeyd edildi: {name}")
    return jsonify(ok=True, removed=removed)


@app.route("/api/fridge/usage/seed", methods=["POST"])
def fridge_usage_seed():
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    with get_db() as c:
        seed_usage(c)
        log_event(c, "info", f"Nümunə istifadə statistikası yükləndi.")
    return jsonify(ok=True)


@app.route("/api/fridge/usage/clear", methods=["POST"])
def fridge_usage_clear():
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    with get_db() as c:
        c.execute("DELETE FROM usage_log")
        log_event(c, "info", "İstifadə statistikası təmizləndi.")
    return jsonify(ok=True)


@app.route("/api/fridge/shopping/ai", methods=["POST"])
def fridge_shopping_ai():
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    with get_db() as c:
        sh = build_shopping(c)
    if not sh["items"]:
        return jsonify(error="Əvvəlcə istifadə statistikası əlavə edin."), 400
    lines = "\n".join(
        f"- {i['name']}: həftədə {i['weekly']:g} ədəd istifadə, stokda {i['stock']} ədəd, "
        f"status: {i['status']}, təklif olunan alış: {i['buy']}" for i in sh["items"])
    prompt = f"""Sən ağıllı soyuducunun alış məsləhətçisisən. Aşağıda ailənin son 4 həftəlik istifadə statistikası və hazırkı stok var.
status: buy = almaq lazımdır, soon = bu həftə almaq olar, skip = lazımdan çox var (almamaq), ok = kifayətdir.

{lines}

Tapşırıq: 3-4 cümləlik qısa, təbii Azərbaycan dilində alış məsləhəti yaz. Nəyi mütləq almaq lazımdır, nəyi almamaq lazımdır (artıq xərc və israfın qarşısı üçün), bir faydalı qeyd. Ulduz (**) işlətmə.
Cavabı yalnız bu JSON formatında ver: {{"summary": "..."}}"""
    try:
        data = ask(RECIPE_MODEL, [{"role": "user", "content": prompt}], max_tokens=1500, temperature=0.4)
    except Exception as e:
        print("ALIŞ AI XƏTASI:", e)
        return jsonify(error="AI şərhi alına bilmədi. Yenidən cəhd edin."), 502
    return jsonify(summary=str(data.get("summary", "")).replace("**", "").strip())


def build_state(c):
    mode = get_setting(c, "mode", "auto")
    zones = [dict(r) for r in c.execute("SELECT * FROM zones ORDER BY id")]
    cats = {r["key"]: dict(r) for r in c.execute("SELECT * FROM categories ORDER BY rowid")}
    zmap = {z["key"]: z for z in zones}
    today = date.today()
    products, count = [], {}
    for r in c.execute("SELECT * FROM products ORDER BY id DESC"):
        p = dict(r)
        p["category_label"] = cats.get(p["category"], cats["other"])["label"]
        p["zone_name"] = zmap[p["zone_key"]]["name"]
        try:
            p["days_left"] = (date.fromisoformat(p["expiry"]) - today).days
        except (TypeError, ValueError):
            p["days_left"] = None
        products.append(p)
        count[p["zone_key"]] = count.get(p["zone_key"], 0) + 1

    for z in zones:
        dev = abs(z["temp"] - z["setpoint"])
        limit = 2.5 if z["key"] == "freezer" else 1.5
        z["status"] = "ok" if dev <= limit else ("warn" if dev <= 3.5 else "alert")
        z["items"] = count.get(z["key"], 0)

    return {
        "mode": mode,
        "zones": zones,
        "products": products,
        "suggestions": build_suggestions(zmap, products, cats),
        "shopping": build_shopping(c),
        "events": [dict(r) for r in c.execute("SELECT ts,kind,message FROM events ORDER BY id DESC LIMIT 15")],
        "categories": [{"key": k, "label": v["label"]} for k, v in cats.items()],
        "stats": {
            "products": len(products),
            "sensor_rows": c.execute("SELECT COUNT(*) FROM sensor_log").fetchone()[0],
            "events": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        },
    }


@app.route("/fridge")
def fridge_page():
    if "user_email" not in session:
        return redirect(url_for("auth_page"))
    return render_template("fridge.html")


@app.route("/api/fridge/state")
def fridge_state():
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    with get_db() as c:
        tick(c)
        return jsonify(build_state(c))


@app.route("/api/fridge/product", methods=["POST"])
def fridge_add_product():
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    d = request.get_json(silent=True) or {}
    name = cap(str(d.get("name", "")).strip()[:60])
    if not name:
        return jsonify(error="Məhsulun adını yaz."), 400
    with get_db() as c:
        rows = c.execute("SELECT * FROM categories ORDER BY rowid").fetchall()
        cmap = {r["key"]: r for r in rows}
        cat_key = d.get("category") if d.get("category") in cmap else guess_category(rows, name)
        cat = cmap[cat_key]
        zones = {r["key"]: r["name"] for r in c.execute("SELECT key,name FROM zones")}
        zone = d.get("zone") if d.get("zone") in zones else cat["ideal_zone"]
        expiry = str(d.get("expiry") or "").strip()
        try:
            date.fromisoformat(expiry)
        except ValueError:
            expiry = (date.today() + timedelta(days=cat["shelf_days"])).isoformat()
        c.execute("INSERT INTO products(name,category,zone_key,added_at,expiry) VALUES(?,?,?,?,?)",
                  (name, cat_key, zone, now_s(), expiry))
        log_event(c, "info", f"Yeni məhsul: {name} ({cat['label']}) → {zones[zone]}")
    return jsonify(ok=True, category=cat["label"], zone_name=zones[zone])


@app.route("/api/fridge/product/<int:pid>/move", methods=["POST"])
def fridge_move(pid):
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    d = request.get_json(silent=True) or {}
    with get_db() as c:
        z = c.execute("SELECT name FROM zones WHERE key=?", (d.get("zone"),)).fetchone()
        p = c.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
        if not z or not p:
            return jsonify(error="Tapılmadı."), 404
        c.execute("UPDATE products SET zone_key=? WHERE id=?", (d["zone"], pid))
        log_event(c, "ai", f"«{p['name']}» → {z['name']} bölməsinə köçürüldü.")
    return jsonify(ok=True)


@app.route("/api/fridge/product/<int:pid>", methods=["DELETE"])
def fridge_delete(pid):
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    with get_db() as c:
        p = c.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
        if p:
            c.execute("DELETE FROM products WHERE id=?", (pid,))
            log_event(c, "info", f"«{p['name']}» soyuducudan çıxarıldı.")
    return jsonify(ok=True)


@app.route("/api/fridge/apply-all", methods=["POST"])
def fridge_apply_all():
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    with get_db() as c:
        moves = [s for s in build_state(c)["suggestions"] if s["type"] == "move"]
        for s in moves:
            c.execute("UPDATE products SET zone_key=? WHERE id=?", (s["to_zone"], s["product_id"]))
        log_event(c, "ai", f"{len(moves)} məhsul AI təklifinə uyğun yerləşdirildi.")
    return jsonify(ok=True, moved=len(moves))


@app.route("/api/fridge/mode", methods=["POST"])
def fridge_mode():
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode not in ("auto", "manual"):
        return jsonify(error="Yanlış rejim."), 400
    with get_db() as c:
        set_setting(c, "mode", mode)
        if mode == "auto":
            c.execute("UPDATE zones SET setpoint = default_temp")
            log_event(c, "ai", "AI idarəetməni geri aldı, hədəflər standarta qaytarıldı.")
        else:
            log_event(c, "info", "Manual rejim aktivləşdirildi.")
    return jsonify(ok=True)


@app.route("/api/fridge/zone/<key>/setpoint", methods=["POST"])
def fridge_setpoint(key):
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    try:
        v = float((request.get_json(silent=True) or {}).get("value"))
    except (TypeError, ValueError):
        return jsonify(error="Yanlış dəyər."), 400
    with get_db() as c:
        if get_setting(c, "mode", "auto") != "manual":
            return jsonify(error="Əvvəl Manual rejimə keç."), 409
        z = c.execute("SELECT name FROM zones WHERE key=?", (key,)).fetchone()
        if not z:
            return jsonify(error="Bölmə tapılmadı."), 404
        lo, hi = (-24, -12) if key == "freezer" else (-2, 12)
        v = max(lo, min(hi, v))
        c.execute("UPDATE zones SET setpoint=? WHERE key=?", (v, key))
        log_event(c, "info", f"Manual: {z['name']} hədəfi {v:.1f}°C")
    return jsonify(ok=True)


@app.route("/api/fridge/door-open", methods=["POST"])
def fridge_door():
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    
    d = request.get_json(silent=True) or {}
    is_open = d.get("isOpen", True)
    
    with get_db() as c:
        set_setting(c, "door_open_state", 1 if is_open else 0)
        if is_open:
            c.execute("UPDATE zones SET temp = temp + 4, humidity = MIN(99, humidity + 6) WHERE key = 'door'")
            c.execute("UPDATE zones SET temp = temp + 2, humidity = MIN(99, humidity + 4) WHERE key IN ('top','middle')")
            log_event(c, "warn", "Qapı açıldı: temperatur simulyasiya olaraq qalxır.")
        else:
            log_event(c, "info", "Qapı bağlandı: soyuducu yenidən soyumağa başlayır.")
    return jsonify(ok=True, isOpen=is_open)


@app.route("/api/fridge/seed", methods=["POST"])
def fridge_seed():
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    with get_db() as c:
        rows = c.execute("SELECT * FROM categories ORDER BY rowid").fetchall()
        cmap = {r["key"]: r for r in rows}
        c.execute("DELETE FROM products")
        for name, zone, days in DEMO:
            key = guess_category(rows, name)
            expiry = (date.today() + timedelta(days=days)).isoformat()
            c.execute("INSERT INTO products(name,category,zone_key,added_at,expiry) VALUES(?,?,?,?,?)",
                      (name, key, zone, now_s(), expiry))
        seed_usage(c)
        log_event(c, "info", f"Demo məlumat yükləndi ({len(DEMO)} məhsul).")
    return jsonify(ok=True)


@app.route("/api/fridge/clear", methods=["POST"])
def fridge_clear():
    if "user_email" not in session:
        return jsonify(error="Giriş edilməyib."), 401
    with get_db() as c:
        c.execute("DELETE FROM products")
        log_event(c, "info", "Bütün məhsullar təmizləndi.")
    return jsonify(ok=True)


@app.route("/api/fridge/export/<kind>.csv")
def fridge_export(kind):
    if "user_email" not in session:
        return redirect(url_for("auth_page"))
    queries = {
        "products": "SELECT id,name,category,zone_key,added_at,expiry FROM products ORDER BY id",
        "sensors": "SELECT ts,zone_key,temp,humidity,gas FROM sensor_log ORDER BY id",
        "categories": "SELECT key,label,ideal_zone,temp_min,temp_max,shelf_days,ethylene,tip FROM categories",
        "usage": "SELECT id,day,name,qty FROM usage_log ORDER BY id",
    }
    if kind not in queries:
        return jsonify(error="Tapılmadı."), 404
    with get_db() as c:
        cur = c.execute(queries[kind])
        headers = [d[0] for d in cur.description]
        rows = cur.fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows([tuple(r) for r in rows])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={kind}.csv"})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)