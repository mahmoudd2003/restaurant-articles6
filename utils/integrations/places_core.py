# -*- coding: utf-8 -*-
"""
places_core.py
Reusable core functions for fetching Google Places (New) data and publishing to WordPress.
"""

import datetime
import html
from typing import Dict, List, Optional

import requests

# -----------------------------
# City presets
# -----------------------------
CITY_PRESETS = {
    "riyadh":   {"lat": 24.7136, "lng": 46.6753, "radius": 30000, "regionCode": "SA", "ar": "الرياض"},
    "jeddah":   {"lat": 21.4858, "lng": 39.1925, "radius": 30000, "regionCode": "SA", "ar": "جدة"},
    "dammam":   {"lat": 26.4207, "lng": 50.0888, "radius": 30000, "regionCode": "SA", "ar": "الدمام"},
    "dubai":    {"lat": 25.2048, "lng": 55.2708, "radius": 30000, "regionCode": "AE", "ar": "دبي"},
    "abudhabi": {"lat": 24.4539, "lng": 54.3773, "radius": 30000, "regionCode": "AE", "ar": "أبوظبي"},
    "sharjah":  {"lat": 25.3463, "lng": 55.4209, "radius": 25000, "regionCode": "AE", "ar": "الشارقة"},
}

# -----------------------------
# Helpers
# -----------------------------
def map_price_level_to_range(level: Optional[int]) -> str:
    if level is None or level == 0:  return "غير محدد"
    if level == 1:                   return "25 – 50 ر.س"
    if level == 2:                   return "50 – 75 ر.س"
    if level == 3:                   return "75 – 120 ر.س"
    if level >= 4:                   return "120+ ر.س"
    return "غير محدد"

def coerce_price_level(v) -> Optional[int]:
    """Places New قد يعود بـ enum مثل PRICE_LEVEL_MODERATE؛ نحوله لأرقام 1..4."""
    if v is None: return None
    if isinstance(v, int): return v
    s = str(v).upper()
    mapping = {
        "PRICE_LEVEL_INEXPENSIVE": 1,
        "PRICE_LEVEL_MODERATE": 2,
        "PRICE_LEVEL_EXPENSIVE": 3,
        "PRICE_LEVEL_VERY_EXPENSIVE": 4,
        "PRICE_LEVEL_UNSPECIFIED": None,
    }
    return mapping.get(s, None)

def safe_int(v, default=0):
    try: return int(v)
    except Exception:
        try: return int(str(v).replace(",", "").strip())
        except Exception: return default

def safe_float(v, default=None):
    try: return float(v)
    except Exception:
        try: return float(str(v).replace(",", "").strip())
        except Exception: return default

def normalize_time_string(s: str) -> str:
    if ":" in s:
        times = s.split(":", 1)[1].strip()
    else:
        times = s.strip()
    return times.replace("AM","ص").replace("PM","م").replace("am","ص").replace("pm","م").replace("-", "–")

def extract_thursday_times(weekday_desc: List[str]) -> str:
    """Return Thursday hours only (without the word Thursday)."""
    if not weekday_desc: return "—"
    for raw in weekday_desc:
        if "Thursday" in raw or "الخميس" in raw:
            return normalize_time_string(raw)
    return normalize_time_string(weekday_desc[0])

# -----------------------------
# HTML builders
# -----------------------------
def build_html_item(p: Dict) -> str:
    name = html.escape(p.get("name", "مطعم"))
    address = html.escape(p.get("address", "—"))
    phone = html.escape(p.get("phone", "—"))
    price_range = html.escape(p.get("price_range", "غير محدد"))
    website = p.get("website", "")
    maps_uri = p.get("maps_uri", "")
    thursday_hours = html.escape(p.get("thursday_hours", "—"))
    family = html.escape(p.get("family_friendly", "—"))
    signature = html.escape(p.get("signature_dish", "")) if p.get("signature_dish") else "—"
    crowd = html.escape(p.get("crowd_note", "—"))

    website_html = f'<a href="{website}" target="_blank" rel="nofollow noopener">زيارة الموقع</a>' if website else "—"
    maps_html = f'<a href="{maps_uri}" target="_blank" rel="nofollow noopener">فتح في خرائط Google</a>' if maps_uri else "—"

    return f"""
<div dir="rtl" style="padding:16px;margin-bottom:16px;">
  <h3 style="margin:0 0 8px 0;font-size:20px;">{name}</h3>
  <div style="line-height:1.9;">
    <div><strong>العنوان:</strong> {address}</div>
    <div><strong>الهاتف:</strong> <a href="tel:{phone}">{phone}</a></div>
    <div><strong>الأوقات:</strong> {thursday_hours}</div>
    <div><strong>مناسب للعوائل:</strong> {family}</div>
    <div><strong>السعر للشخص:</strong> {price_range}</div>
    <div><strong>الطبق المميز:</strong> {signature}</div>
    <div><strong>أوقات الزحمة:</strong> {crowd}</div>
    <div><strong>خرائط Google:</strong> {maps_html}</div>
    <div><strong>الموقع الإلكتروني:</strong> {website_html}</div>
  </div>
</div>
"""

def build_post_html(items: List[Dict], title: str) -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d")
    cards = "\n".join(build_html_item(p) for p in items)
    return f"""
<div dir="rtl">
  <p>آخر تحديث: {now}. المصدر: خرائط Google.</p>
  {cards}
</div>
"""

# -----------------------------
# Google Places (New)
# -----------------------------
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_DETAILS_URL_TMPL = "https://places.googleapis.com/v1/places/{place_id}"

def places_search_text(api_key: str, query: str, city_key: str, max_results: int = 15):
    preset = CITY_PRESETS.get(city_key.lower())
    if not preset: raise ValueError(f"Unsupported city '{city_key}'")
    payload = {
        "textQuery": query,
        "languageCode": "ar",
        "regionCode": preset["regionCode"],
        "maxResultCount": min(max_results, 20),
        "locationBias": {"circle": {"center": {"latitude": preset["lat"], "longitude": preset["lng"]}, "radius": preset["radius"]}},
        "includedType": "restaurant",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.id,places.displayName,places.rating,places.userRatingCount",
    }
    r = requests.post(PLACES_SEARCH_URL, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json().get("places", [])

def place_details(api_key: str, place_id: str, region_code: Optional[str] = None):
    field_mask = ",".join([
        "id","displayName","formattedAddress","nationalPhoneNumber",
        "websiteUri","googleMapsUri","priceLevel","rating","userRatingCount",
        "currentOpeningHours","regularOpeningHours"
    ])
    url = PLACES_DETAILS_URL_TMPL.format(place_id=place_id)
    headers = {"X-Goog-Api-Key": api_key,"X-Goog-FieldMask": field_mask}
    params = {"languageCode": "ar"}
    if region_code: params["regionCode"] = region_code
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

# -----------------------------
# WordPress REST
# -----------------------------
def create_or_update_draft_post(base_url, user, app_password, title, content_html, post_id=None):
    auth = (user, app_password)
    endpoint = f"{base_url.rstrip('/')}/wp-json/wp/v2/posts"
    payload = {"title": title, "content": content_html, "status": "draft"}
    url = f"{endpoint}/{post_id}" if post_id else endpoint
    r = requests.post(url, auth=auth, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()

# -----------------------------
# Orchestration
# -----------------------------
def make_items_from_places(api_key: str, places: List[Dict], min_reviews: int = 200, region_code: Optional[str] = None):
    items: List[Dict] = []
    for p in places:
        pid = p.get("id") or p.get("placeId")
        if not pid: continue

        det = place_details(api_key, pid, region_code=region_code)

        price_level = coerce_price_level(det.get("priceLevel"))
        rating = safe_float(det.get("rating"))
        rating_count = safe_int(det.get("userRatingCount"))

        ch = det.get("regularOpeningHours", {}) or det.get("currentOpeningHours", {}) or {}
        weekday_desc = ch.get("weekdayDescriptions", []) or []
        thursday_times = extract_thursday_times(weekday_desc)

        items.append({
            "name": det.get("displayName", {}).get("text", ""),
            "address": det.get("formattedAddress", ""),
            "phone": det.get("nationalPhoneNumber", ""),
            "website": det.get("websiteUri", ""),
            "maps_uri": det.get("googleMapsUri", ""),
            "price_range": map_price_level_to_range(price_level),
            "thursday_hours": thursday_times,
            "family_friendly": "نعم (تقديري)",
            "signature_dish": "",
            "crowd_note": "8:00 م – 11:00 م (تقديري)",
            "rating": rating,
            "rating_count": rating_count,
        })

    # فلترة وترتيب بقيم رقمية مؤكدة
    min_rev = safe_int(min_reviews)
    items = [it for it in items if safe_int(it.get("rating_count")) >= min_rev]
    items.sort(
        key=lambda x: (safe_float(x.get("rating"), -1.0), safe_int(x.get("rating_count"), -1)),
        reverse=True
    )
    return items
