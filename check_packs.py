"""Check country place files: can people find the places they'd look for?

    python -I tools/places/check_packs.py <folder with *.db or *.db.gz>

For each country, tries searches people really type (airport codes, the
capital in English and in the local language) and prints what's missing,
plus how many places have English and other-language names.
"""
import gzip
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from waypack import tokens  # noqa: E402  (same word splitting as the app)

AIRPORT, RAIL, CITY_KINDS = {30}, {10, 11}, {1, 2}

# pack id -> [(search, kinds it should find or None, name should contain)]
CHECKS: dict[str, list[tuple[str, set[int] | None, str]]] = {
    "india": [("ndls", RAIL, "New Delhi"), ("del", AIRPORT, "Indira"),
              ("கோயம்பேடு", None, ""), ("नई दिल्ली", None, "")],
    "new-zealand": [("akl", AIRPORT, "Auckland"), ("auckland", CITY_KINDS, ""),
                    ("wellington", CITY_KINDS, "")],
    "australia": [("syd", AIRPORT, ""), ("sydney", CITY_KINDS, ""),
                  ("melbourne", CITY_KINDS, "")],
    "us": [("jfk", AIRPORT, ""), ("new york", CITY_KINDS, ""),
           ("chicago", CITY_KINDS, ""), ("penn station", RAIL, "")],
    "mexico": [("mex", AIRPORT, ""), ("guadalajara", CITY_KINDS, "")],
    "canada": [("yyz", AIRPORT, ""), ("montreal", CITY_KINDS, ""),
               ("toronto", CITY_KINDS, "")],
    "united-kingdom": [("lhr", AIRPORT, ""), ("heathrow", AIRPORT, ""),
                       ("london", CITY_KINDS, ""), ("caerdydd", CITY_KINDS, "")],
    "sri-lanka": [("cmb", AIRPORT, ""), ("colombo", CITY_KINDS, ""),
                  ("කොළඹ", None, "")],
    "nepal": [("ktm", AIRPORT, ""), ("kathmandu", CITY_KINDS, ""),
              ("काठमाडौं", None, "")],
    "united-arab-emirates": [("dxb", AIRPORT, ""), ("dubai", CITY_KINDS, ""),
                             ("دبي", None, ""), ("abu dhabi", None, "")],
    "qatar": [("doh", AIRPORT, ""), ("doha", CITY_KINDS, ""), ("الدوحة", None, "")],
    "oman": [("mct", AIRPORT, ""), ("muscat", None, "")],
    "bahrain": [("bah", AIRPORT, ""), ("manama", None, "")],
    "kuwait": [("kwi", AIRPORT, ""), ("kuwait", None, "")],
    "saudi-arabia": [("ruh", AIRPORT, ""), ("jed", AIRPORT, ""), ("riyadh", None, ""),
                     ("jeddah", None, ""), ("mecca", None, ""), ("الرياض", None, "")],
    "japan": [("shinjuku", None, ""), ("しんじゅく", None, ""), ("nrt", AIRPORT, ""), ("hnd", AIRPORT, ""), ("東京", None, ""),
              ("tokyo", None, ""), ("とうきょう", None, ""), ("osaka", None, "")],
    "china": [("上海", None, ""), ("pek", AIRPORT, ""), ("北京", None, ""), ("beijing", None, ""),
              ("shanghai", None, "")],
    "south-korea": [("부산", None, ""), ("icn", AIRPORT, ""), ("서울", None, ""), ("seoul", None, ""),
                    ("busan", None, "")],
    "taiwan": [("tpe", AIRPORT, ""), ("台北", None, ""), ("taipei", None, "")],
    "mongolia": [("ulaanbaatar", None, ""), ("улаанбаатар", None, "")],
    "thailand": [("phuket", None, ""), ("bkk", AIRPORT, ""), ("bangkok", None, ""),
                 ("กรุงเทพ", None, ""), ("chiang mai", None, "")],
    "vietnam": [("sgn", AIRPORT, ""), ("han", AIRPORT, ""),
                ("hanoi", None, ""), ("ha noi", None, ""),
                ("ho chi minh", None, ""), ("saigon", None, "")],
    "malaysia": [("kul", AIRPORT, ""), ("kuala lumpur", None, ""), ("penang", None, "")],
    "singapore": [("sin", AIRPORT, ""), ("changi", None, ""), ("orchard", None, "")],
    "brunei": [("bwn", AIRPORT, ""), ("bandar seri begawan", None, "")],
    "indonesia": [("cgk", AIRPORT, ""), ("jakarta", None, ""),
                  ("bali", None, ""), ("dps", AIRPORT, "")],
    "philippines": [("mnl", AIRPORT, ""), ("manila", None, ""),
                    ("cebu", None, "")],
    "germany": [("monaco di baviera", None, ""), ("koln", None, ""), ("cologne", None, ""), ("ミュンヘン", None, ""), ("fra", AIRPORT, ""), ("munchen", None, ""),
                ("munich", None, ""), ("berlin", None, "")],
    "france": [("parigi", None, ""), ("nizza", None, ""), ("marseille", None, ""), ("cdg", AIRPORT, ""), ("paris", None, ""),
               ("gare du nord", RAIL, "")],
    "italy": [("venedig", None, ""), ("venice", None, ""), ("florence", None, ""), ("firenze", None, ""), ("fco", AIRPORT, ""), ("roma", None, ""), ("rome", None, ""),
              ("milano", None, ""), ("milan", None, "")],
    "spain": [("sevilla", None, ""), ("seville", None, ""), ("mad", AIRPORT, ""), ("madrid", None, ""),
              ("barcelona", None, "")],
    "netherlands": [("den haag", None, ""), ("the hague", None, ""), ("ams", AIRPORT, ""), ("amsterdam", None, ""),
                    ("schiphol", None, "")],
    "switzerland": [("genf", None, ""), ("ginevra", None, ""), ("luzern", None, ""), ("zrh", AIRPORT, ""), ("zurich", None, ""),
                    ("geneve", None, ""), ("geneva", None, "")],
    "portugal": [("lissabon", None, ""), ("lis", AIRPORT, ""), ("lisboa", None, ""),
                 ("lisbon", None, ""), ("porto", None, "")],
    "ireland-and-northern-ireland": [("dub", AIRPORT, ""),
                                     ("dublin", None, ""),
                                     ("baile atha cliath", None, "")],
    "austria": [("vienne", None, ""), ("salzburg", None, ""), ("vie", AIRPORT, ""), ("wien", None, ""),
                ("vienna", None, "")],
    "belgium": [("antwerpen", None, ""), ("anvers", None, ""), ("antwerp", None, ""), ("bru", AIRPORT, ""), ("bruxelles", None, ""),
                ("brussel", None, ""), ("brussels", None, "")],
    # Next 30.
    "pakistan": [("khi", AIRPORT, ""), ("karachi", None, ""), ("lahore", None, ""),
                 ("کراچی", None, "")],
    "bangladesh": [("dac", AIRPORT, ""), ("dhaka", None, ""), ("ঢাকা", None, "")],
    "turkey": [("ist", AIRPORT, ""), ("istanbul", None, ""), ("ankara", None, ""),
               ("estambul", None, "")],
    "israel": [("tlv", AIRPORT, ""), ("tel aviv", None, ""),
               ("jerusalem", None, ""), ("ירושלים", None, "")],
    "palestine": [("ramallah", None, ""), ("bethlehem", None, ""), ("gaza", None, "")],
    "iran": [("ika", AIRPORT, ""), ("tehran", None, ""), ("تهران", None, "")],
    "kazakhstan": [("ala", AIRPORT, ""), ("almaty", None, ""), ("алматы", None, "")],
    "cambodia": [("kti", AIRPORT, ""), ("phnom penh", None, ""),
                 ("ភ្នំពេញ", None, ""), ("siem reap", None, "")],
    "myanmar": [("rgn", AIRPORT, ""), ("yangon", None, ""), ("ရန်ကုန်", None, "")],
    "laos": [("vte", AIRPORT, ""), ("vientiane", None, ""), ("ວຽງຈັນ", None, "")],
    "brazil": [("gru", AIRPORT, ""), ("sao paulo", None, ""),
               ("rio de janeiro", None, "")],
    "argentina": [("eze", AIRPORT, ""), ("buenos aires", None, "")],
    "colombia": [("bog", AIRPORT, ""), ("bogota", None, ""), ("medellin", None, "")],
    "chile": [("scl", AIRPORT, ""), ("santiago", None, "")],
    "peru": [("lim", AIRPORT, ""), ("lima", None, ""), ("cusco", None, "")],
    "egypt": [("cai", AIRPORT, ""), ("cairo", None, ""), ("القاهرة", None, "")],
    "morocco": [("cmn", AIRPORT, ""), ("casablanca", None, ""),
                ("marrakech", None, "")],
    "south-africa": [("jnb", AIRPORT, ""), ("johannesburg", None, ""),
                     ("cape town", None, ""), ("kaapstad", None, "")],
    "nigeria": [("los", AIRPORT, ""), ("lagos", None, ""), ("abuja", None, "")],
    "kenya": [("nbo", AIRPORT, ""), ("nairobi", None, ""), ("mombasa", None, "")],
    "russia": [("svo", AIRPORT, ""), ("moscow", None, ""), ("москва", None, ""),
               ("moskau", None, ""), ("saint petersburg", None, "")],
    "poland": [("waw", AIRPORT, ""), ("warsaw", None, ""), ("warszawa", None, ""),
               ("varsavia", None, "")],
    "sweden": [("arn", AIRPORT, ""), ("stockholm", None, "")],
    "norway": [("osl", AIRPORT, ""), ("oslo", None, "")],
    "denmark": [("cph", AIRPORT, ""), ("copenhagen", None, ""),
                ("kobenhavn", None, "")],
    "finland": [("hel", AIRPORT, ""), ("helsinki", None, ""),
                ("helsingfors", None, "")],
    "greece": [("ath", AIRPORT, ""), ("athens", None, ""), ("αθηνα", None, "")],
    "czech-republic": [("prg", AIRPORT, ""), ("prague", None, ""),
                       ("praha", None, ""), ("prag", None, "")],
    "hungary": [("bud", AIRPORT, ""), ("budapest", None, "")],
    "romania": [("otp", AIRPORT, ""), ("bucharest", None, ""),
                ("bucuresti", None, "")],
    "ukraine": [("kbp", AIRPORT, ""), ("kyiv", None, ""), ("київ", None, ""),
                ("kiev", None, "")],
    # Third 30.
    "ethiopia": [("add", AIRPORT, ""), ("addis ababa", None, ""), ("አዲስ አበባ", None, "")],
    "congo-democratic-republic": [("fih", AIRPORT, ""), ("kinshasa", None, "")],
    "tanzania": [("dar", AIRPORT, ""), ("dar es salaam", None, ""), ("zanzibar", None, "")],
    "uganda": [("ebb", AIRPORT, ""), ("kampala", None, "")],
    "ghana": [("acc", AIRPORT, ""), ("accra", None, ""), ("kumasi", None, "")],
    "algeria": [("alg", AIRPORT, ""), ("alger", None, ""), ("algiers", None, ""),
                ("الجزائر", None, "")],
    "tunisia": [("tun", AIRPORT, ""), ("tunis", None, ""), ("تونس", None, "")],
    "mauritius": [("mru", AIRPORT, ""), ("port louis", None, "")],
    "uzbekistan": [("tas", AIRPORT, ""), ("tashkent", None, ""), ("toshkent", None, ""),
                   ("samarkand", None, "")],
    "iraq": [("bgw", AIRPORT, ""), ("baghdad", None, ""), ("بغداد", None, ""),
             ("erbil", None, "")],
    "jordan": [("amm", AIRPORT, ""), ("amman", None, ""), ("عمان", None, ""),
               ("petra", None, "")],
    "maldives": [("mle", AIRPORT, ""), ("male", None, "")],
    "bhutan": [("pbh", AIRPORT, ""), ("thimphu", None, ""), ("paro", None, "")],
    "georgia": [("tbs", AIRPORT, ""), ("tbilisi", None, ""), ("თბილისი", None, ""),
                ("batumi", None, "")],
    "azerbaijan": [("gyd", AIRPORT, ""), ("baku", None, ""), ("baki", None, "")],
    "croatia": [("zag", AIRPORT, ""), ("zagreb", None, ""), ("dubrovnik", None, "")],
    "bulgaria": [("sof", AIRPORT, ""), ("sofia", None, ""), ("софия", None, "")],
    "serbia": [("beg", AIRPORT, ""), ("belgrade", None, ""), ("beograd", None, ""),
               ("београд", None, "")],
    "slovakia": [("bts", AIRPORT, ""), ("bratislava", None, "")],
    "iceland": [("kef", AIRPORT, ""), ("reykjavik", None, "")],
    "cyprus": [("lca", AIRPORT, ""), ("nicosia", None, ""), ("limassol", None, "")],
    "lithuania": [("vno", AIRPORT, ""), ("vilnius", None, ""), ("wilno", None, "")],
    "estonia": [("tll", AIRPORT, ""), ("tallinn", None, "")],
    "cuba": [("hav", AIRPORT, ""), ("havana", None, ""), ("la habana", None, "")],
    "dominican-republic": [("puj", AIRPORT, ""), ("sdq", AIRPORT, ""),
                           ("santo domingo", None, ""), ("punta cana", None, "")],
    "haiti": [("pap", AIRPORT, ""), ("port au prince", None, "")],
    "costa-rica": [("sjo", AIRPORT, ""), ("san jose", None, "")],
    "ecuador": [("uio", AIRPORT, ""), ("quito", None, ""), ("guayaquil", None, "")],
    "venezuela": [("ccs", AIRPORT, ""), ("caracas", None, "")],
    "uruguay": [("mvd", AIRPORT, ""), ("montevideo", None, "")],
    "fiji": [("nan", AIRPORT, ""), ("suva", None, ""), ("nadi", None, "")],
}


def find(db: sqlite3.Connection, query: str, kinds, contains: str):
    words = tokens(query)
    if not words:
        return None
    ids = None
    for w in words:
        found = {r[0] for r in db.execute(
            "SELECT id FROM tokens WHERE tok = ?", (w,))}
        ids = found if ids is None else ids & found
        if not ids:
            return None
    for (i,) in [(i,) for i in sorted(ids)]:
        name, en, kind = db.execute(
            "SELECT name, name_en, kind FROM places WHERE id = ?", (i,)
        ).fetchone()
        if kinds and kind not in kinds:
            continue
        if contains and contains.lower() not in f"{name} {en}".lower():
            continue
        return f"{name}" + (f" ({en})" if en else "")
    return None


def main() -> int:
    folder = Path(sys.argv[1])
    for gz in sorted(folder.glob("*.db.gz")):
        db_path = gz.with_suffix("")
        if gz.name != "base.db.gz" and not db_path.exists():
            with gzip.open(gz, "rb") as g, open(db_path, "wb") as f:
                shutil.copyfileobj(g, f)
    misses = 0
    for db_path in sorted(folder.glob("*.db")):
        pid = db_path.stem
        if pid == "base":
            continue
        db = sqlite3.connect(db_path)
        n = db.execute("SELECT count(*) FROM places").fetchone()[0]
        en = db.execute(
            "SELECT count(*) FROM places WHERE name_en IS NOT NULL").fetchone()[0]
        kinds = dict(db.execute(
            "SELECT kind, count(*) FROM places GROUP BY kind"))
        print(f"\n== {pid}: {n:,} places | airports {kinds.get(30, 0)} "
              f"| rail {kinds.get(10, 0) + kinds.get(11, 0)} "
              f"| bus stops {kinds.get(21, 0):,} | with English name {en:,}")
        for q, k, c in CHECKS.get(pid, []):
            hit = find(db, q, k, c)
            if not hit:
                misses += 1
            print(f"   {'ok ' if hit else 'MISS'} {q!r:26} -> {hit or ''}")
        if pid not in CHECKS:
            print("   (no checks written for this country yet)")
    print(f"\n{misses} missing")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
