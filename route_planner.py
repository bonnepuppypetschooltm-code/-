#!/usr/bin/env python3
"""犬の幼稚園 送迎ルート自動作成ツール

Google カレンダーの予定から「🚗」マーク付きの園児を抽出し、
クレート(車載数)制限を考慮した送迎ルート表(HTML)を作成する。
各便にはGoogleマップで経路を開けるリンクを付与する。

カレンダー記法:
  タイトル: 🚗 犬の名前 [タグ]
    タグ省略時 = 往復
    [往復] / [迎えのみ] / [送りのみ] / [朝のみ] / [夕のみ] /
    [朝のみ 9:00] のように時刻を併記可
  場所(location)欄: 自宅住所
"""

import argparse
import datetime
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request

import yaml

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

CAR_MARK = "🚗"
TAG_PATTERN = re.compile(r"[\[(]([^\])]*)[\])]")
TIME_PATTERN = re.compile(r"(\d{1,2}):(\d{2})")

# 「特大」は「大」の部分文字列を含むため、長い名前から先に判定する
CRATE_SIZE_ORDER = ["特大", "大", "中", "小"]

GEOCODE_CACHE_FILE = ".geocode_cache.json"
GEOCODE_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch?q="


class Stop:
    def __init__(self, name, address, requested_time=None, crate_size=None):
        self.name = name
        self.address = address
        self.requested_time = requested_time
        self.crate_size = crate_size
        self.distance_from_base = None

    def __repr__(self):
        t = self.requested_time.strftime("%H:%M") if self.requested_time else "-"
        return f"{self.name} ({self.address}) [指定: {t}, クレート: {self.crate_size}]"


def crate_units(crate_capacity):
    """crate_capacity: {サイズ名: そのサイズだけで車に積める最大数} から
    車の総容量(units)と、サイズごとの占有units数を計算する。
    """
    counts = list(crate_capacity.values())
    capacity_units = counts[0]
    for c in counts[1:]:
        capacity_units = capacity_units * c // math.gcd(capacity_units, c)
    weights = {size: capacity_units // count for size, count in crate_capacity.items()}
    return capacity_units, weights


def load_geocode_cache():
    if os.path.exists(GEOCODE_CACHE_FILE):
        with open(GEOCODE_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_geocode_cache(cache):
    with open(GEOCODE_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def geocode(address, cache):
    """住所から (緯度, 経度) を取得する (国土地理院 住所検索API、無料・APIキー不要)"""
    if address in cache:
        return cache[address]
    try:
        url = GEOCODE_URL + urllib.parse.quote(address)
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.load(resp)
        if data:
            lon, lat = data[0]["geometry"]["coordinates"]
            cache[address] = [lat, lon]
        else:
            cache[address] = None
    except Exception:
        cache[address] = None
    return cache[address]


def haversine_km(p1, p2):
    if p1 is None or p2 is None:
        return None
    lat1, lon1 = p1
    lat2, lon2 = p2
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


DEFAULT_CONFIG = {
    "base_address": "大阪府大阪市北区天神橋4-6-17 上谷ビル1F.2F",
    "crate_capacity": {"特大": 2, "大": 7, "中": 9, "小": 12},
    "default_crate_size": "中",
    "morning_start_time": "08:30",
    "evening_start_time": "17:00",
    "departure_buffer_minutes": 15,
}


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def sample_events(target_date):
    """--demo 用のサンプル予定 (Google Calendar API のレスポンス形式を模したもの)"""
    def dt(hour, minute):
        return datetime.datetime.combine(target_date, datetime.time(hour, minute)).isoformat()

    return [
        {"summary": "🚗 ポチ [大]", "location": "大阪府大阪市北区天神橋1丁目1-1", "start": {"dateTime": dt(8, 30)}},
        {"summary": "🚗 タロウ [迎えのみ 8:50 中]", "location": "大阪府大阪市北区西天満2-2-2", "start": {"dateTime": dt(8, 50)}},
        {"summary": "🚗 ハナ [往復 小]", "location": "大阪府大阪市北区中崎西3-3-3", "start": {"dateTime": dt(9, 0)}},
        {"summary": "🚗 モモ [送りのみ 17:30 大]", "location": "大阪府大阪市北区天神橋4-4-4", "start": {"dateTime": dt(17, 30)}},
        {"summary": "🚗 中前大地 [朝のみ 8:00 大]", "location": "大阪府大阪市北区錦町6-6-6", "start": {"dateTime": dt(8, 0)}},
        {"summary": "トリミング 来店 (送迎なし)", "location": "大阪府大阪市北区南森町5-5-5", "start": {"dateTime": dt(10, 0)}},
    ]


def get_calendar_service(config):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    token_file = config["google_oauth_token_file"]
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, CALENDAR_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config["google_oauth_client_secret_file"], CALENDAR_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def fetch_events(service, calendar_id, target_date):
    start = datetime.datetime.combine(target_date, datetime.time.min).isoformat() + "Z"
    end = datetime.datetime.combine(target_date, datetime.time.max).isoformat() + "Z"
    result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=start,
            timeMax=end,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return result.get("items", [])


def parse_event(event, target_date):
    """🚗マーク付きイベントを (name, tag_text, address, event_time) にパースする。
    対象外なら None を返す。
    """
    title = event.get("summary", "")
    if CAR_MARK not in title:
        return None

    location = event.get("location", "").strip()
    if not location:
        print(f"警告: 「{title}」に場所(住所)が設定されていません。スキップします。", file=sys.stderr)
        return None

    after_mark = title.split(CAR_MARK, 1)[1].strip()
    tag_match = TAG_PATTERN.search(after_mark)
    tag_text = tag_match.group(1) if tag_match else ""
    name = TAG_PATTERN.sub("", after_mark).strip()
    if not name:
        name = "(名前未設定)"

    event_time = None
    start_info = event.get("start", {})
    if "dateTime" in start_info:
        dt = datetime.datetime.fromisoformat(start_info["dateTime"])
        event_time = dt.time()

    return name, tag_text, location, event_time


def classify_stop(name, tag_text, location, event_time):
    """(pickup_stop_or_None, dropoff_stop_or_None) を返す"""
    has_pickup_tag = "迎え" in tag_text or "朝" in tag_text
    has_dropoff_tag = "送り" in tag_text or "夕" in tag_text
    is_roundtrip = "往復" in tag_text or (not has_pickup_tag and not has_dropoff_tag)

    tag_time_match = TIME_PATTERN.search(tag_text)
    tag_time = None
    if tag_time_match:
        tag_time = datetime.time(int(tag_time_match.group(1)), int(tag_time_match.group(2)))

    crate_size = None
    for size in CRATE_SIZE_ORDER:
        if size in tag_text:
            crate_size = size
            break

    pickup_stop = None
    dropoff_stop = None

    if is_roundtrip or has_pickup_tag:
        t = tag_time if has_pickup_tag else event_time
        pickup_stop = Stop(name, location, t, crate_size)
    if is_roundtrip or has_dropoff_tag:
        t = tag_time if has_dropoff_tag else event_time
        dropoff_stop = Stop(name, location, t, crate_size)

    return pickup_stop, dropoff_stop


def split_into_trips(stops, capacity_units, crate_weights, default_crate_size):
    """クレートサイズごとの占有units数をもとに、車に収まる範囲でトリップに分割する。

    時刻指定があるお宅はその時刻順を優先する。時刻指定がないお宅は、
    店舗から遠い順 (遠方を先に回り、店舗近くで締めくくる) に並べる。
    """
    def sort_key(s):
        if s.requested_time:
            return (0, s.requested_time, 0.0)
        return (1, datetime.time(0, 0), -(s.distance_from_base or 0.0))

    sorted_stops = sorted(stops, key=sort_key)

    trips = []
    current = []
    current_units = 0
    for stop in sorted_stops:
        size = stop.crate_size or default_crate_size
        units = crate_weights.get(size, crate_weights[default_crate_size])
        if current and current_units + units > capacity_units:
            trips.append(current)
            current = []
            current_units = 0
        current.append(stop)
        current_units += units
    if current:
        trips.append(current)
    return trips


def build_maps_url(base_address, stop_addresses):
    """APIキー不要の Google Maps 経路リンクを作る (拠点 -> 各お宅 -> 拠点)"""
    import urllib.parse

    points = [base_address] + stop_addresses + [base_address]
    encoded = [urllib.parse.quote(p, safe="") for p in points]
    origin = encoded[0]
    destination = encoded[-1]
    waypoints = "%7C".join(encoded[1:-1])

    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={origin}&destination={destination}&travelmode=driving"
    )
    if waypoints:
        url += f"&waypoints={waypoints}"
    return url


def main():
    parser = argparse.ArgumentParser(description="犬の幼稚園 送迎ルート自動作成")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス")
    parser.add_argument(
        "--date",
        default=None,
        help="対象日 (YYYY-MM-DD)。未指定なら今日",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="サンプルデータを使い、Google Calendar/Maps APIなしで動作確認する",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="出力するHTMLファイルのパス(未指定なら 送迎ルート_YYYY-MM-DD.html)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="作成後にブラウザで自動的に開かない",
    )
    args = parser.parse_args()

    target_date = (
        datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
    )

    if args.demo:
        config = DEFAULT_CONFIG
        if os.path.exists(args.config):
            config = {**DEFAULT_CONFIG, **load_config(args.config)}
        events = sample_events(target_date)
        print("*** デモモード: サンプルデータで動作確認中 (実際のカレンダーは使用しません) ***\n")
    else:
        config = load_config(args.config)
        service = get_calendar_service(config)
        events = fetch_events(service, config["calendar_id"], target_date)

    base_address, trips_data = build_route(
        target_date, config, events, geocode_enabled=not args.demo
    )

    html = render_html(target_date, base_address, trips_data)
    output_path = args.output or f"送迎ルート_{target_date.isoformat()}.html"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"作成しました: {os.path.abspath(output_path)}")

    if not args.no_open:
        import webbrowser

        webbrowser.open(f"file://{os.path.abspath(output_path)}")


def build_route(target_date, config, events, geocode_enabled=True):
    pickup_stops = []
    dropoff_stops = []
    for event in events:
        parsed = parse_event(event, target_date)
        if not parsed:
            continue
        name, tag_text, location, event_time = parsed
        pickup, dropoff = classify_stop(name, tag_text, location, event_time)
        if pickup:
            pickup_stops.append(pickup)
        if dropoff:
            dropoff_stops.append(dropoff)

    base_address = config["base_address"]
    crate_capacity = config["crate_capacity"]
    default_crate_size = config["default_crate_size"]
    capacity_units, crate_weights = crate_units(crate_capacity)

    if geocode_enabled:
        cache = load_geocode_cache()
        base_coords = geocode(base_address, cache)
        for stop in pickup_stops + dropoff_stops:
            stop.distance_from_base = haversine_km(base_coords, geocode(stop.address, cache))
        save_geocode_cache(cache)

    default_morning_start = datetime.datetime.combine(
        target_date, datetime.time.fromisoformat(config["morning_start_time"])
    )
    default_evening_start = datetime.datetime.combine(
        target_date, datetime.time.fromisoformat(config["evening_start_time"])
    )
    buffer_minutes = config.get("departure_buffer_minutes", 15)

    trips_data = []
    for label, stops, default_start in (
        ("朝のお迎え便", pickup_stops, default_morning_start),
        ("夕方の送り便", dropoff_stops, default_evening_start),
    ):
        if not stops:
            trips_data.append({"label": label, "rows": None})
            continue

        trips = split_into_trips(stops, capacity_units, crate_weights, default_crate_size)
        for i, trip_stops in enumerate(trips, start=1):
            size_counts = {}
            loaded_units = 0
            for stop in trip_stops:
                size = stop.crate_size or default_crate_size
                size_counts[size] = size_counts.get(size, 0) + 1
                loaded_units += crate_weights.get(size, crate_weights[default_crate_size])

            requested_times = [s.requested_time for s in trip_stops if s.requested_time]
            if requested_times:
                earliest = datetime.datetime.combine(target_date, min(requested_times))
                departure = earliest - datetime.timedelta(minutes=buffer_minutes)
            else:
                departure = default_start

            trip = {
                "label": label,
                "trip_no": i,
                "size_counts": size_counts,
                "loaded_units": loaded_units,
                "capacity_units": capacity_units,
                "departure": departure.strftime("%H:%M"),
                "rows": [],
                "maps_url": build_maps_url(base_address, [s.address for s in trip_stops]),
            }
            for stop in trip_stops:
                t = stop.requested_time.strftime("%H:%M") if stop.requested_time else "-"
                size = stop.crate_size or f"{default_crate_size}(既定)"
                trip["rows"].append({"name": stop.name, "address": stop.address, "time": t, "crate": size})
            trips_data.append(trip)

    return base_address, trips_data


def render_html(target_date, base_address, trips_data):
    parts = []
    parts.append("<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>")
    parts.append(f"<title>送迎ルート {target_date.isoformat()}</title>")
    parts.append(
        "<style>"
        "body{font-family:sans-serif;margin:20px;}"
        "h1{font-size:1.4em;} h2{margin-top:2em;border-bottom:2px solid #888;padding-bottom:4px;}"
        "table{border-collapse:collapse;width:100%;margin-top:8px;}"
        "th,td{border:1px solid #ccc;padding:8px;text-align:left;}"
        "th{background:#f0f0f0;}"
        ".meta{color:#555;}"
        ".maps-link{display:inline-block;margin-top:8px;padding:6px 12px;"
        "background:#1a73e8;color:#fff;text-decoration:none;border-radius:4px;}"
        "</style></head><body>"
    )
    parts.append(f"<h1>送迎ルート {target_date.isoformat()}</h1>")
    parts.append(f"<p class='meta'>拠点: {base_address}</p>")

    for trip in trips_data:
        if trip["rows"] is None:
            parts.append(f"<h2>{trip['label']}</h2><p>対象なし</p>")
            continue

        size_text = ", ".join(f"{size}{count}" for size, count in trip["size_counts"].items())
        parts.append(
            f"<h2>{trip['label']} 第{trip['trip_no']}便 "
            f"(積載: {size_text} / 容量 {trip['loaded_units']}/{trip['capacity_units']})</h2>"
        )
        parts.append("<table><tr><th>順番</th><th>名前</th><th>住所</th><th>希望時刻</th><th>クレート</th></tr>")
        parts.append(f"<tr><td>出発</td><td colspan='2'>{base_address}</td><td>{trip['departure']}</td><td>-</td></tr>")
        for i, row in enumerate(trip["rows"], start=1):
            parts.append(
                f"<tr><td>{i}</td><td>{row['name']}</td><td>{row['address']}</td>"
                f"<td>{row['time']}</td><td>{row['crate']}</td></tr>"
            )
        parts.append(f"<tr><td>帰着</td><td colspan='4'>{base_address}</td></tr>")
        parts.append("</table>")
        parts.append(f"<a class='maps-link' href='{trip['maps_url']}' target='_blank'>Googleマップでルートを開く</a>")

    parts.append("</body></html>")
    return "".join(parts)


if __name__ == "__main__":
    main()
