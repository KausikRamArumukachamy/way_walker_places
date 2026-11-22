"""Build Waywaker's offline place files from OpenStreetMap data.

Only the places the app needs are kept: cities, towns, villages, suburbs,
railway / metro / tram stations, bus stations and stops, airports and ferry
terminals. Everything else (roads, buildings, shops, houses) is dropped, so a
1.5 GB country download becomes a file of a few MB.

Commands
--------
  pack  <file.osm.pbf> --id india --cc IN --title India --out packs/
        One country file  ->  packs/india.db.gz  (downloaded inside the app)

  base  packs/ --out base.db.gz
        World file built from every pack: cities, towns, stations, airports,
        ferries and bus stations (no bus stops / villages). Shipped in the app.

  countries --out packs/ [--only india] [--shard 0/8]
        Download each country from Geofabrik, build its pack, delete the
        download. (GitHub Actions runs this in 8 parallel parts.)

  finish packs/
        Write packs/manifest.json and packs/base.db.gz from all packs.

Data (c) OpenStreetMap contributors, ODbL 1.0. The generated files are a
derived database and must stay under the ODbL.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import osmium
from scipy.spatial import cKDTree

SCHEMA_VERSION = 1
COORD_SCALE = 100_000  # lat/lon stored as integers, ~1 m precision

# Kind codes. Keep in sync with lib/services/place_db.dart (OfflineKind).
CITY, TOWN, VILLAGE, SUBURB, NEIGHBOURHOOD, HAMLET = 1, 2, 3, 4, 5, 6
RAIL, METRO, TRAM = 10, 11, 12
BUS_STATION, BUS_STOP = 20, 21
AIRPORT = 30
FERRY = 40

# Lower = more important. Ids are handed out in this order, so the most
# useful matches come first when a search has to stop early.
IMPORTANCE = {
    CITY: 0, RAIL: 1, METRO: 1, AIRPORT: 1, TOWN: 2, BUS_STATION: 2,
    FERRY: 2, TRAM: 3, SUBURB: 4, BUS_STOP: 5, VILLAGE: 6,
    NEIGHBOURHOOD: 7, HAMLET: 8,
}
BASE_KINDS = {CITY, TOWN, RAIL, METRO, BUS_STATION, AIRPORT, FERRY}
LOCALITY_KINDS = {SUBURB, NEIGHBOURHOOD, VILLAGE, HAMLET, TOWN, CITY}  # can be "near ..."
CITY_KINDS = {CITY, TOWN}

# Same name closer than this (metres) is treated as one place.
DEDUPE_M = {
    RAIL: 600, METRO: 400, TRAM: 150, BUS_STATION: 300, BUS_STOP: 200,
    AIRPORT: 3000, FERRY: 300, CITY: 5000, TOWN: 3000, VILLAGE: 1000,
    SUBURB: 1000, NEIGHBOURHOOD: 500, HAMLET: 500,
}

GEOFABRIK_INDEX = "https://download.geofabrik.de/index-v1-nogeom.json"


# ----------------------------------------------------------------------------
# Text normalisation. lib/services/place_text.dart must do exactly the same.
# ----------------------------------------------------------------------------

_SPECIAL = {
    "ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "đ": "d", "ð": "d",
    "ł": "l", "þ": "th", "ı": "i", "ħ": "h", "ŧ": "t", "ŋ": "n",
}


def _fold_table() -> dict[str, str]:
    """Latin letters with accents -> plain letters (U+00C0..U+024F)."""
    table: dict[str, str] = {}
    for cp in range(0x00C0, 0x0250):
        ch = chr(cp).lower()
        if len(ch) != 1:
            continue
        if ch in _SPECIAL:
            table[ch] = _SPECIAL[ch]
            continue
        base = "".join(
            c for c in unicodedata.normalize("NFKD", ch)
            if not unicodedata.combining(c)
        )
        if base and base != ch and base.isascii() and base.isalnum():
            table[ch] = base
    return table


FOLD = _fold_table()


def _is_word_char(c: str) -> bool:
    return unicodedata.category(c)[0] in "LMN"


def tokens(text: str | None) -> list[str]:
    """Lower-case, strip accents, split into words."""
    if not text:
        return []
    out, cur = [], []
    for c in text.lower():
        c = FOLD.get(c, c)
        if all(_is_word_char(x) for x in c):
            cur.append(c)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


# ----------------------------------------------------------------------------
# Reading OpenStreetMap data
# ----------------------------------------------------------------------------

@dataclass
class Item:
    name: str
    name_en: str | None
    kind: int
    lat: float
    lon: float
    alt: list[str] = field(default_factory=list)
    pop: int = 0


def classify(tags) -> int | None:
    g = tags.get
    place = g("place")
    if place in ("city",):
        return CITY
    if place in ("town",):
        return TOWN
    if place in ("village",):
        return VILLAGE
    if place in ("suburb", "quarter", "borough"):
        return SUBURB
    if place == "neighbourhood":
        return NEIGHBOURHOOD
    if place == "hamlet":
        return HAMLET

    railway = g("railway")
    station = g("station")
    pt = g("public_transport")
    if railway in ("station", "halt") or (pt == "station" and (
            g("train") == "yes" or g("subway") == "yes"
            or g("light_rail") == "yes" or g("monorail") == "yes")):
        if station in ("subway", "light_rail", "monorail") or g("subway") == "yes":
            return METRO
        return RAIL
    if railway == "tram_stop":
        return TRAM
    if g("amenity") == "bus_station" or (pt == "station" and g("bus") == "yes"):
        return BUS_STATION
    if g("highway") == "bus_stop" or (pt == "platform" and g("bus") == "yes"):
        return BUS_STOP
    if g("aeroway") == "aerodrome" and (g("iata") or g("icao")):
        if g("military") or g("aerodrome:type") == "military":
            return None
        return AIRPORT
    if g("amenity") == "ferry_terminal":
        return FERRY
    return None


FILTER_KEYS = (
    "place", "railway", "public_transport", "amenity", "highway", "aeroway",
)


def _names(tags, lang_keys: tuple[str, ...] = ()
           ) -> tuple[str | None, str | None, list[str]]:
    name = tags.get("name")
    en = tags.get("name:en") or tags.get("int_name")
    if not name:
        name, en = en, None
    if en == name:
        en = None
    alt = []
    for k in ALT_NAME_KEYS + lang_keys:
        v = tags.get(k)
        if v:
            alt.extend(x.strip() for x in v.split(";") if x.strip())
    # Station codes (NDLS, MAS) and airport codes (BLR). A plain "ref" is
    # only a code on railway stations; on bus stops it's a stop number.
    if tags.get("railway") or tags.get("train") == "yes":
        alt.extend(x.strip() for x in (tags.get("ref") or "").split(";")
                   if x.strip())
    return name, en, [a for a in dict.fromkeys(alt) if a != name and a != en]


# Other names that are searched (not shown).
ALT_NAME_KEYS = (
    "alt_name", "official_name", "short_name", "old_name",
    "loc_name", "nat_name", "reg_name",
    "railway:ref", "iata",
)

_ZH = ("name:zh", "name:zh-Hans", "name:zh-Hant", "name:zh-Latn-pinyin",
       "name:zh_pinyin", "name:zh-Latn")

# The languages people in each country type place names in, plus Latin
# spellings of non-Latin scripts (romaji, pinyin...). Read as extra search
# names so people can search in their own language or in English letters:
# கோயம்பேடு, 東京 / Tokyo, 서울 / Seoul.
LANGS: dict[str, tuple[str, ...]] = {
    "IN": tuple(f"name:{c}" for c in (
        "hi bn te mr ta ur gu kn ml or pa as mai sat ks ne kok sd doi mni "
        "brx sa lus kha tcy").split()),
    "LK": ("name:si", "name:ta"),
    "NP": ("name:ne", "name:ne-Latn"),
    "BD": ("name:bn", "name:bn-Latn"),
    "PK": ("name:ur", "name:pa"),
    "NZ": ("name:mi",),
    "CA": ("name:fr",),
    "US": ("name:es",),
    "GB": ("name:cy", "name:gd"),
    "IE": ("name:ga",),
    "QA": ("name:ar", "name:ar-Latn"), "AE": ("name:ar", "name:ar-Latn"),
    "OM": ("name:ar", "name:ar-Latn"), "BH": ("name:ar", "name:ar-Latn"),
    "KW": ("name:ar", "name:ar-Latn"), "SA": ("name:ar", "name:ar-Latn"),
    "JP": ("name:ja", "name:ja-Latn", "name:ja_rm", "name:ja-Hira",
           "name:ja_kana", "name:ja-Kana"),
    "CN": _ZH,
    "TW": _ZH,
    "HK": _ZH + ("name:zh-yue", "name:yue"),
    "KR": ("name:ko", "name:ko-Latn", "name:ko_rm", "name:ko-Hani"),
    "MN": ("name:mn", "name:mn-Latn"),
    "TH": ("name:th", "name:th-Latn"),
    "VN": ("name:vi",),
    "MY": ("name:ms", "name:zh", "name:ta"),
    "SG": ("name:ms", "name:zh", "name:ta"),
    "BN": ("name:ms",),
    "ID": ("name:id",),
    "PH": ("name:tl", "name:fil"),
    "CH": ("name:de", "name:fr", "name:it", "name:rm"),
    "BE": ("name:nl", "name:fr", "name:de"),
    "ES": ("name:es", "name:ca", "name:eu", "name:gl"),
    "FR": ("name:fr", "name:br", "name:oc"),
    "IT": ("name:it", "name:de"),
    "DE": ("name:de",), "AT": ("name:de",), "NL": ("name:nl", "name:fy"),
    "PT": ("name:pt",),
}


# Europe has open borders: people cross from one country to the next and
# search in their own language ("Monaco di Baviera", "Múnich" for München).
# European packs are searchable in all of these.
EUROPE_LANGS = tuple(f"name:{c}" for c in (
    "en de fr it es pt nl pl cs sk hu ro bg el sv da fi nb no et lv lt sl "
    "hr sr bs mk sq ga mt is lb ca eu gl cy ru uk be tr "
    # Visitors from further away: only well-known places have these.
    "zh ja ko ar hi ta he fa").split())
EUROPE = set(
    "AD AL AT BA BE BG BY CH CY CZ DE DK EE ES FI FR GB GR HR HU IE IS IT "
    "LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS SE SI SK SM UA VA XK"
    .split())


def lang_keys(cc: list[str]) -> tuple[str, ...]:
    keys = [k for c in cc for k in LANGS.get(c, ())]
    if any(c in EUROPE for c in cc):
        keys += EUROPE_LANGS
    return tuple(dict.fromkeys(keys))


def _population(tags) -> int:
    v = (tags.get("population") or "").replace(",", "").replace(" ", "")
    return int(v) if v.isdigit() else 0


def read_pbf(path: Path, cc: list[str] = ()) -> list[Item]:
    langs = lang_keys(list(cc))
    items: list[Item] = []
    ways: list[tuple[str, str | None, int, list[str], list[int]]] = []
    need: set[int] = set()

    # Big places (airports, some stations and bus terminals) are often
    # mapped as relations (multipolygons) made of several ways.
    rels: list[tuple[str, str | None, int, list[str], list[int], int]] = []

    fp = (osmium.FileProcessor(
        str(path), osmium.osm.NODE | osmium.osm.WAY | osmium.osm.RELATION)
          .with_filter(osmium.filter.KeyFilter(*FILTER_KEYS)))
    for o in fp:
        kind = classify(o.tags)
        if kind is None:
            continue
        name, en, alt = _names(o.tags, langs)
        if not name:
            continue
        if o.is_node():
            if o.location.valid():
                items.append(Item(name, en, kind, o.location.lat,
                                  o.location.lon, alt, _population(o.tags)))
        elif o.is_relation():
            if o.tags.get("type") not in ("multipolygon", "site", None):
                continue
            members = [m.ref for m in o.members
                       if m.type == "w" and m.role in ("outer", "")]
            if members:
                rels.append((name, en, kind, alt, members[:20],
                             _population(o.tags)))
        else:
            refs = [n.ref for n in o.nodes]
            # A few points are enough for a centre.
            if len(refs) > 12:
                step = len(refs) // 12
                refs = refs[::step]
            ways.append((name, en, kind, alt, refs, _population(o.tags)))
            need.update(refs)

    if rels:
        # The member ways' points, a few per way.
        way_ids = {w for r in rels for w in r[4]}
        way_refs: dict[int, list[int]] = {}
        fp = (osmium.FileProcessor(str(path), osmium.osm.WAY)
              .with_filter(osmium.filter.IdFilter(way_ids)))
        for w in fp:
            refs = [n.ref for n in w.nodes]
            if len(refs) > 6:
                refs = refs[::len(refs) // 6]
            way_refs[w.id] = refs
        for name, en, kind, alt, members, pop in rels:
            refs = [n for w in members for n in way_refs.get(w, [])][:60]
            if refs:
                ways.append((name, en, kind, alt, refs, pop))
                need.update(refs)

    if ways:
        loc: dict[int, tuple[float, float]] = {}
        fp = (osmium.FileProcessor(str(path), osmium.osm.NODE)
              .with_filter(osmium.filter.IdFilter(need)))
        for n in fp:
            if n.location.valid():
                loc[n.id] = (n.location.lat, n.location.lon)
        for name, en, kind, alt, refs, pop in ways:
            pts = [loc[r] for r in refs if r in loc]
            if pts:
                lat = sum(p[0] for p in pts) / len(pts)
                lon = sum(p[1] for p in pts) / len(pts)
                items.append(Item(name, en, kind, lat, lon, alt, pop))
    return items


# ----------------------------------------------------------------------------
# Building a pack
# ----------------------------------------------------------------------------

def _xyz(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Points on a unit sphere, so KD-tree distance ~ real distance."""
    la, lo = np.radians(lat), np.radians(lon)
    return np.column_stack(
        (np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)))


EARTH_M = 6_371_000.0


def dedupe(items: list[Item]) -> list[Item]:
    """Drop repeats: both sides of a road, station node + station area..."""
    if not items:
        return items
    items.sort(key=lambda i: (IMPORTANCE[i.kind], i.kind))
    pts = _xyz(np.array([i.lat for i in items]), np.array([i.lon for i in items]))
    tree = cKDTree(pts)
    keep = np.ones(len(items), dtype=bool)
    norm = [" ".join(tokens(i.name)) for i in items]
    for idx, it in enumerate(items):
        if not keep[idx]:
            continue
        r = DEDUPE_M[it.kind] / EARTH_M
        for j in tree.query_ball_point(pts[idx], r):
            if j > idx and keep[j] and norm[j] == norm[idx] and (
                    items[j].kind == it.kind
                    or {items[j].kind, it.kind} <= {RAIL, METRO, TRAM}):
                keep[j] = False
                if not it.name_en and items[j].name_en:
                    it.name_en = items[j].name_en
    return [it for it, k in zip(items, keep) if k]


def _nearest(src: np.ndarray, targets: np.ndarray, max_m: float):
    if len(targets) == 0 or len(src) == 0:
        return np.full(len(src), -1), np.full(len(src), np.inf)
    d, i = cKDTree(targets).query(src, distance_upper_bound=max_m / EARTH_M)
    i = np.where(np.isfinite(d), i, -1)
    return i, d * EARTH_M


def write_db(path: Path, items: list[Item], meta: dict[str, str],
             cc_of: list[str | None] | None = None,
             searchable: list[bool] | None = None) -> None:
    """items must already be in final id order (id = index + 1)."""
    n = len(items)
    lat = np.array([i.lat for i in items])
    lon = np.array([i.lon for i in items])
    pts = _xyz(lat, lon)
    kinds = np.array([i.kind for i in items])

    # "near" = closest suburb/village (or town/city) within 5 km.
    # "city" = closest city within 30 km, else closest town within 15 km.
    loc_idx = np.where(np.isin(kinds, list(LOCALITY_KINDS - CITY_KINDS)))[0]
    city_idx = np.where(kinds == CITY)[0]
    town_idx = np.where(kinds == TOWN)[0]

    near_i, _ = _nearest(pts, pts[loc_idx], 5000)
    c_i, c_d = _nearest(pts, pts[city_idx], 30000)
    t_i, t_d = _nearest(pts, pts[town_idx], 15000)
    # Among cities within 30 km, name the biggest one: an area between
    # Chennai and Tambaram is "Chennai".
    if len(city_idx):
        pops = np.array([items[c].pop for c in city_idx])
        ctree = cKDTree(pts[city_idx])
        for k, cand in enumerate(
                ctree.query_ball_point(pts, 30000 / EARTH_M)):
            if len(cand) > 1 and c_d[k] > 3000:  # right next to a city: keep it
                best = max(cand, key=lambda j: pops[j])
                if pops[best] > 0 and best != c_i[k]:
                    c_i[k] = best
                    c_d[k] = float(np.linalg.norm(
                        pts[k] - pts[city_idx[best]])) * EARTH_M

    near = [None] * n
    city = [None] * n
    for k in range(n):
        kind = items[k].kind
        if kind in (CITY,):
            continue
        if kind not in LOCALITY_KINDS and near_i[k] >= 0:
            near[k] = int(loc_idx[near_i[k]]) + 1
        if kind == TOWN:
            continue  # a town is described well enough by its own name
        # Prefer the city ("Chennai"), unless a town is much closer (big
        # cities pull from four times as far away as towns).
        has_c = c_i[k] >= 0 and city_idx[c_i[k]] != k
        has_t = t_i[k] >= 0 and town_idx[t_i[k]] != k
        if has_c and (not has_t or c_d[k] / 4 <= t_d[k]):
            city[k] = int(city_idx[c_i[k]]) + 1
        elif has_t:
            city[k] = int(town_idx[t_i[k]]) + 1
        if near[k] is not None and near[k] == city[k]:
            near[k] = None

    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA page_size = 4096;
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
        CREATE TABLE places(
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          name_en TEXT,
          kind INTEGER NOT NULL,
          lat INTEGER NOT NULL,
          lon INTEGER NOT NULL,
          near INTEGER,
          city INTEGER,
          cc TEXT
        );
        CREATE TABLE tokens(
          tok TEXT NOT NULL,
          id INTEGER NOT NULL,
          PRIMARY KEY(tok, id)
        ) WITHOUT ROWID;
    """)
    db.executemany(
        "INSERT INTO places VALUES(?,?,?,?,?,?,?,?,?)",
        ((k + 1, it.name, it.name_en, it.kind,
          round(it.lat * COORD_SCALE), round(it.lon * COORD_SCALE),
          near[k], city[k], cc_of[k] if cc_of else None)
         for k, it in enumerate(items)))

    def tok_rows():
        for k, it in enumerate(items):
            if searchable is not None and not searchable[k]:
                continue
            words = set(tokens(it.name)) | set(tokens(it.name_en))
            for a in it.alt:
                words |= set(tokens(a))
            for w in words:
                yield (w, k + 1)
    db.executemany("INSERT OR IGNORE INTO tokens VALUES(?,?)", tok_rows())
    meta = dict(meta, schema=str(SCHEMA_VERSION), count=str(n))
    db.executemany("INSERT INTO meta VALUES(?,?)", meta.items())
    db.commit()
    db.execute("VACUUM")
    db.close()


def gzip_file(src: Path, dst: Path) -> None:
    with open(src, "rb") as f, gzip.open(dst, "wb", compresslevel=9) as g:
        shutil.copyfileobj(f, g)


def build_pack(pbf: Path, pack_id: str, cc: list[str], title: str,
               out_dir: Path, osm_date: str = "") -> Path:
    t0 = time.time()
    items = dedupe(read_pbf(pbf, cc))
    items.sort(key=lambda i: (IMPORTANCE[i.kind], i.name))
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / f"{pack_id}.db"
    write_db(db_path, items, {
        "id": pack_id, "cc": ",".join(cc), "title": title,
        "osm_date": osm_date, "built": time.strftime("%Y-%m-%d"),
    })
    gz = out_dir / f"{pack_id}.db.gz"
    gzip_file(db_path, gz)
    counts: dict[int, int] = {}
    for i in items:
        counts[i.kind] = counts.get(i.kind, 0) + 1
    print(f"[pack] {pack_id}: {len(items)} places "
          f"{ {k: counts[k] for k in sorted(counts)} } "
          f"db={db_path.stat().st_size >> 10} KB gz={gz.stat().st_size >> 10} KB "
          f"in {time.time() - t0:.0f}s", flush=True)
    return db_path


# ----------------------------------------------------------------------------
# World base file
# ----------------------------------------------------------------------------

def build_base(pack_dir: Path, out_gz: Path) -> None:
    items: list[Item] = []
    ccs: list[str | None] = []
    for db_path in sorted(pack_dir.glob("*.db")):
        if db_path.name == "base.db":  # left over from an earlier run
            continue
        db = sqlite3.connect(db_path)
        cc = dict(db.execute("SELECT key, value FROM meta"))["cc"].split(",")
        # Multi-country packs: tag rows with the first code; country
        # detection only needs to find a pack containing that code.
        rows = db.execute(
            "SELECT name, name_en, kind, lat, lon FROM places WHERE kind IN (%s)"
            % ",".join(str(k) for k in BASE_KINDS | {SUBURB, VILLAGE}))
        for name, en, kind, lat, lon in rows:
            items.append(Item(name, en, kind, lat / COORD_SCALE,
                              lon / COORD_SCALE))
            ccs.append(cc[0])
        db.close()
    # Neighbouring country files overlap at the borders.
    order = sorted(range(len(items)),
                   key=lambda k: (IMPORTANCE[items[k].kind], items[k].name))
    items = [items[k] for k in order]
    ccs = [ccs[k] for k in order]
    pairs = list(zip(items, ccs))
    kept = dedupe([p[0] for p in pairs])
    kept_ids = {id(x) for x in kept}
    pairs = [p for p in pairs if id(p[0]) in kept_ids]

    # Villages/suburbs are only kept to say "near X" for stations; they are
    # not searchable in the base file (the country pack has them).
    final = [p for p in pairs if p[0].kind in BASE_KINDS]
    extra = [p for p in pairs if p[0].kind not in BASE_KINDS]
    if extra:
        fpts = _xyz(np.array([p[0].lat for p in final]),
                    np.array([p[0].lon for p in final]))
        epts = _xyz(np.array([p[0].lat for p in extra]),
                    np.array([p[0].lon for p in extra]))
        # keep only localities that are the nearest one to some station
        stations = [k for k, p in enumerate(final)
                    if p[0].kind not in CITY_KINDS]
        idx, _ = _nearest(fpts[stations], epts, 5000)
        wanted = sorted({int(i) for i in idx if i >= 0})
        extra = [extra[i] for i in wanted]
    allp = final + extra
    searchable = [True] * len(final) + [False] * len(extra)
    tmp = out_gz.with_suffix("")
    if tmp.suffix != ".db":
        tmp = out_gz.parent / "base.db"
    write_db(tmp, [p[0] for p in allp],
             {"id": "base", "built": time.strftime("%Y-%m-%d")},
             cc_of=[p[1] for p in allp], searchable=searchable)
    gzip_file(tmp, out_gz)
    print(f"[base] {len(final)} searchable + {len(extra)} helper places, "
          f"gz={out_gz.stat().st_size >> 10} KB", flush=True)


# ----------------------------------------------------------------------------
# Whole world (GitHub Actions)
# ----------------------------------------------------------------------------

def _codes(p: dict) -> list[str]:
    """Country codes of a Geofabrik region (a list, or one code as text)."""
    v = p.get("iso3166-1:alpha2") or []
    if isinstance(v, str):
        v = v.split(",")
    return [c.strip() for c in v if c.strip()]


def country_regions(index: dict) -> list[dict]:
    """Geofabrik regions that cover whole countries (no double counting)."""
    feats = [f["properties"] for f in index["features"]]
    byid = {p["id"]: p for p in feats}
    out = []
    for p in feats:
        if "iso3166-1:alpha2" not in p:
            continue
        # Skip if a parent region already covers the same country.
        par, covered = p.get("parent"), False
        while par:
            pp = byid.get(par, {})
            if set(_codes(p)) <= set(_codes(pp)):
                covered = True
                break
            par = pp.get("parent")
        if not covered:
            out.append(p)
    return out


def _dated_urls(url: str, days: int = 7) -> list[str]:
    """Geofabrik's '-latest' link sometimes points at today's file before it
    is published (404). The dated files of the last few days are kept."""
    if not url.endswith("-latest.osm.pbf"):
        return []
    stem = url[: -len("latest.osm.pbf")]
    now = time.time()
    return [stem + time.strftime("%y%m%d", time.gmtime(now - d * 86400))
            + ".osm.pbf" for d in range(1, days + 1)]


def download(url: str, dst: Path) -> None:
    for attempt in range(5):
        try:
            _fetch(url, dst)
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print(f"  download failed ({e}), retrying", flush=True)
                time.sleep(10 * (attempt + 1))
                continue
            for older in _dated_urls(url):
                try:
                    _fetch(older, dst)
                    print(f"  latest not ready, used {older}", flush=True)
                    return
                except urllib.error.HTTPError:
                    continue
            break
        except Exception as e:  # noqa: BLE001
            print(f"  download failed ({e}), retrying", flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"could not download {url}")


def _fetch(url: str, dst: Path) -> None:
    req = urllib.request.Request(
        url, headers={"User-Agent": "waywaker-places-builder"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)


def build_countries(out_dir: Path, work: Path, only: set[str] | None,
                    shard: tuple[int, int] = (0, 1)) -> None:
    """Download and build country packs. With shard (i, n) only every n-th
    country is built, so several machines can share the work."""
    out_dir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(GEOFABRIK_INDEX) as r:
        index = json.load(r)
    regions = sorted(country_regions(index), key=lambda r: r["id"])
    if only:
        regions = [r for r in regions if r["id"] in only]
    i, n = shard
    regions = regions[i::n]
    for k, reg in enumerate(regions, 1):
        rid = reg["id"].replace("/", "-")
        print(f"[{k}/{len(regions)}] {rid}", flush=True)
        if (out_dir / f"{rid}.db.gz").exists():
            continue
        pbf = work / f"{rid}.osm.pbf"
        download(reg["urls"]["pbf"], pbf)
        try:
            build_pack(pbf, rid, _codes(reg), reg["name"],
                       out_dir)
        finally:
            pbf.unlink(missing_ok=True)  # never keep the big download


def finish(out_dir: Path) -> None:
    """Write manifest.json and base.db.gz from all packs in out_dir."""
    manifest = []
    for gz in sorted(out_dir.glob("*.db.gz")):
        if gz.name == "base.db.gz":
            continue
        db_path = gz.with_suffix("")
        if not db_path.exists():
            with gzip.open(gz, "rb") as g, open(db_path, "wb") as f:
                shutil.copyfileobj(g, f)
        con = sqlite3.connect(db_path)
        meta = dict(con.execute("SELECT key, value FROM meta"))
        con.close()
        manifest.append({
            "id": meta["id"],
            "title": meta["title"],
            "cc": meta["cc"].split(","),
            "built": meta["built"],
            "count": int(meta["count"]),
            "bytes": gz.stat().st_size,
            "db_bytes": db_path.stat().st_size,
        })
    build_base(out_dir, out_dir / "base.db.gz")
    (out_dir / "manifest.json").write_text(json.dumps({
        "schema": SCHEMA_VERSION,
        "built": time.strftime("%Y-%m-%d"),
        "attribution": "© OpenStreetMap contributors, ODbL 1.0",
        "packs": manifest,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] {len(manifest)} packs", flush=True)


# ----------------------------------------------------------------------------

def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("pack")
    a.add_argument("pbf", type=Path)
    a.add_argument("--id", required=True)
    a.add_argument("--cc", required=True, help="ISO codes, comma separated")
    a.add_argument("--title", required=True)
    a.add_argument("--out", type=Path, default=Path("packs"))

    b = sub.add_parser("base")
    b.add_argument("packs", type=Path)
    b.add_argument("--out", type=Path, default=Path("base.db.gz"))

    c = sub.add_parser("countries")
    c.add_argument("--out", type=Path, default=Path("packs"))
    c.add_argument("--work", type=Path, default=Path("work"))
    c.add_argument("--only", default="", help="e.g. india,goa")
    c.add_argument("--shard", default="0/1", help="i/n: build every n-th")

    e = sub.add_parser("finish")
    e.add_argument("packs", type=Path)

    d = sub.add_parser("dart-fold", help="print the Dart accent table")

    args = ap.parse_args(argv)
    if args.cmd == "pack":
        build_pack(args.pbf, args.id, args.cc.split(","), args.title, args.out)
    elif args.cmd == "base":
        build_base(args.packs, args.out)
    elif args.cmd == "countries":
        only = {x for x in args.only.split(",") if x} or None
        i, n = (int(x) for x in args.shard.split("/"))
        build_countries(args.out, args.work, only, (i, n))
    elif args.cmd == "finish":
        finish(args.packs)
    elif args.cmd == "dart-fold":
        body = ", ".join(
            f"0x{ord(k):04X}: '{v}'" for k, v in sorted(FOLD.items()))
        print(f"const Map<int, String> _fold = {{{body}}};")


if __name__ == "__main__":
    main(sys.argv[1:])
