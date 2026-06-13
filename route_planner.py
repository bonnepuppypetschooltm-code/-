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
import unicodedata
import urllib.parse
import urllib.request

import yaml

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# カレンダーは日本時間(JST)で運用されているため、その日の0:00-23:59 (JST) を取得する
JST = datetime.timezone(datetime.timedelta(hours=9))

CAR_MARK = "🚗"
# 半角 [] () と 全角 ［］（） の両方に対応
TAG_PATTERN = re.compile(r"[\[(［（]([^\])］）]*)[\])］）]")
TIME_PATTERN = re.compile(r"(朝|夕)?(\d{1,2})[:：](\d{2})")

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
        self.coords = None

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


POSTAL_CODE_PATTERN = re.compile(r"〒?\d{3}-?\d{4}\s*")


def geocode(address, cache):
    """住所から (緯度, 経度) を取得する (国土地理院 住所検索API、無料・APIキー不要)"""
    if address in cache and cache[address] is not None:
        return cache[address]
    # 「Mebius西天満Bldg.、日本、〒530-0047大阪府...」のように、
    # 郵便番号より前にビル名などが付いている住所は検索APIがうまく認識できないため、
    # 郵便番号より前の部分は取り除き、郵便番号より後ろの部分だけで検索する
    m = POSTAL_CODE_PATTERN.search(address)
    query = address[m.end():].strip() if m else address.strip()
    try:
        url = GEOCODE_URL + urllib.parse.quote(query)
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


def estimate_leg_minutes(base_coords, stops, avg_speed_kmh, route_distance_factor=1.0, travel_time_overrides=None, stop_minutes=0):
    """拠点 -> 各お宅 -> 拠点 を1区間ずつ移動した場合の所要時間(分)のリストを返す。
    結果は (len(stops) + 1) 件で、先頭が「拠点 -> 最初のお宅」、
    末尾が「最後のお宅 -> 拠点」。座標が取得できない区間は None になる。

    実際の道路距離は直線距離より長くなるため、route_distance_factor を
    かけて補正する(例: 1.3 なら直線距離の1.3倍を走行距離とみなす)。

    travel_time_overrides は { 住所: {"from_store": 分, "to_store": 分} }
    の形式で、拠点との往復にかかる実際の時間がわかっている場合に
    計算結果を上書きするための設定(値は純粋な移動時間)。

    stop_minutes は1軒あたりの乗せ降ろし時間。最初の区間(拠点 -> 最初のお宅)
    以外の各区間には、出発前の乗せ降ろし時間としてこの分数を加算する。
    """
    travel_time_overrides = travel_time_overrides or {}
    # 全角/半角や前後の空白の違いで一致しないことがあるため、正規化してから比較する
    normalized_overrides = {
        normalize_address(addr): override for addr, override in travel_time_overrides.items()
    }

    points = [base_coords] + [s.coords for s in stops] + [base_coords]
    legs = []
    for i in range(len(points) - 1):
        km = haversine_km(points[i], points[i + 1])
        if km is None:
            legs.append(None)
        else:
            legs.append(km * route_distance_factor / avg_speed_kmh * 60)

    if stops:
        first_override = normalized_overrides.get(normalize_address(stops[0].address), {})
        if "from_store" in first_override:
            legs[0] = first_override["from_store"]
        last_override = normalized_overrides.get(normalize_address(stops[-1].address), {})
        if "to_store" in last_override:
            legs[-1] = last_override["to_store"]

    for i in range(1, len(legs)):
        if legs[i] is not None:
            legs[i] += stop_minutes

    return legs


def normalize_address(address):
    """全角/半角や空白の違いを無視して住所を比較するための正規化"""
    normalized = unicodedata.normalize("NFKC", address)
    return re.sub(r"\s+", "", normalized)


def estimate_trip_minutes(leg_minutes):
    """leg_minutes(estimate_leg_minutesの結果、乗せ降ろし時間込み)から、
    拠点に戻ってくるまでの所要時間(目安、分)を概算する。
    区間が1つでも不明なら None を返す。
    """
    if any(m is None for m in leg_minutes):
        return None
    return sum(leg_minutes)


DEFAULT_CONFIG = {
    "base_address": "大阪府大阪市北区天神橋4-6-17 上谷ビル1F.2F",
    "crate_capacity": {"特大": 2, "大": 4, "中": 9, "小": 12},
    "default_crate_size": "中",
    "morning_start_time": "08:30",
    "evening_start_time": "17:00",
    "departure_buffer_minutes": 15,
    "avg_speed_kmh": 20,
    "route_distance_factor": 1.3,
    "stop_minutes": 5,
    "time_gap_split_minutes": 60,
}


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def set_travel_time_override(config_path, config, events, target_date, name, from_store, to_store):
    """指定した名前の子のカレンダーの「場所」欄から住所を取得し、
    config.yaml の travel_time_overrides に所要時間を保存する。
    """
    if from_store is None and to_store is None:
        print("--from-store または --to-store のどちらかを指定してください。", file=sys.stderr)
        return

    address = None
    for event in events:
        title = event.get("summary", "")
        if CAR_MARK in title and name in title:
            address = event.get("location", "").strip()
            break

    if address is None:
        print(
            f"{target_date.isoformat()} のカレンダーに「{name}」を含む🚗予定が見つかりませんでした。",
            file=sys.stderr,
        )
        return

    overrides = config.get("travel_time_overrides") or {}
    # 既存の登録(全角/半角や空白の違いを含む)があれば上書きする
    normalized = normalize_address(address)
    for existing_addr in list(overrides):
        if normalize_address(existing_addr) == normalized:
            del overrides[existing_addr]

    entry = overrides.get(address, {})
    if from_store is not None:
        entry["from_store"] = from_store
    if to_store is not None:
        entry["to_store"] = to_store
    overrides[address] = entry

    # 既存の travel_time_overrides セクション(末尾にあるはず)を取り除き、
    # 残りの内容(コメント等)はそのまま保持して、新しい内容を末尾に追加する
    with open(config_path, encoding="utf-8") as f:
        text = f.read()
    text = re.split(r"^travel_time_overrides:", text, maxsplit=1, flags=re.MULTILINE)[0]
    text = text.rstrip() + "\n\n"
    text += yaml.dump({"travel_time_overrides": overrides}, allow_unicode=True, default_flow_style=False, sort_keys=False)

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"「{name}」({address}) の所要時間を保存しました: {entry}")


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
        {"summary": "🚗 ロイ [往復 8:10 17:45 大]", "location": "大阪府大阪市北区中之島7-7-7", "start": {"dateTime": dt(8, 10)}},
        {"summary": "🚗 桐野ロイ [往復 朝8:00 大]", "location": "大阪府大阪市淀川区木川西2丁目1-4-5 ラ・メゾン・ポナール", "start": {"dateTime": dt(8, 0)}},
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
    start = datetime.datetime.combine(target_date, datetime.time.min, tzinfo=JST).isoformat()
    end = datetime.datetime.combine(target_date, datetime.time.max, tzinfo=JST).isoformat()
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
    # 「[往復 朝8:00] [大]」のように複数の[]に分けて書いた場合も
    # まとめて1つのタグとして扱う
    tag_text = " ".join(m.group(1) for m in TAG_PATTERN.finditer(after_mark))
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
    has_pickup_tag = bool(re.search(r"(迎え|朝)のみ", tag_text))
    has_dropoff_tag = bool(re.search(r"(送り|夕)のみ", tag_text))
    is_roundtrip = "往復" in tag_text or (not has_pickup_tag and not has_dropoff_tag)

    pickup_time = None
    dropoff_time = None
    unlabeled_times = []
    for prefix, h, m in TIME_PATTERN.findall(tag_text):
        t = datetime.time(int(h), int(m))
        if prefix == "朝":
            pickup_time = t
        elif prefix == "夕":
            dropoff_time = t
        else:
            unlabeled_times.append(t)

    if len(unlabeled_times) >= 2:
        if pickup_time is None:
            pickup_time = unlabeled_times[0]
        if dropoff_time is None:
            dropoff_time = unlabeled_times[1]
    elif len(unlabeled_times) == 1:
        if pickup_time is None and dropoff_time is None:
            pickup_time = dropoff_time = unlabeled_times[0]
        elif pickup_time is None:
            pickup_time = unlabeled_times[0]
        elif dropoff_time is None:
            dropoff_time = unlabeled_times[0]

    crate_size = None
    for size in CRATE_SIZE_ORDER:
        if size in tag_text:
            crate_size = size
            break

    pickup_stop = None
    dropoff_stop = None

    if is_roundtrip or has_pickup_tag:
        t = pickup_time if pickup_time else event_time
        pickup_stop = Stop(name, location, t, crate_size)
    if is_roundtrip or has_dropoff_tag:
        # 往復で夕方の希望時刻が指定されていない場合、カレンダーの予定時刻(朝の時刻)を
        # そのまま夕方の希望時刻として使わない (出発時刻の計算がおかしくなるため)
        dropoff_stop = Stop(name, location, dropoff_time, crate_size)

    return pickup_stop, dropoff_stop


def split_into_trips(stops, capacity_units, crate_weights, default_crate_size, time_gap_split_minutes=None):
    """クレートサイズごとの占有units数をもとに、車に収まる範囲でトリップに分割する。

    時刻指定があるお宅はその時刻順を優先する。時刻指定がないお宅は、
    店舗から遠い順 (遠方を先に回り、店舗近くで締めくくる) に並べる。

    時刻指定のあるお宅同士の希望時刻が time_gap_split_minutes 以上離れている場合は、
    その間で一旦店舗に戻ることにして便を分割する
    (待ち時間が長くなりすぎるのを避けるため)。
    """
    def sort_key(s):
        if s.requested_time:
            return (0, s.requested_time, 0.0)
        return (1, datetime.time(0, 0), -(s.distance_from_base or 0.0))

    sorted_stops = sorted(stops, key=sort_key)

    trips = []
    current = []
    current_units = 0
    last_requested_time = None
    for stop in sorted_stops:
        size = stop.crate_size or default_crate_size
        units = crate_weights.get(size, crate_weights[default_crate_size])
        split_for_capacity = current and current_units + units > capacity_units
        split_for_time_gap = (
            time_gap_split_minutes is not None
            and current
            and last_requested_time is not None
            and stop.requested_time is not None
            and minutes_between(last_requested_time, stop.requested_time) >= time_gap_split_minutes
        )
        if split_for_capacity or split_for_time_gap:
            trips.append(current)
            current = []
            current_units = 0
            last_requested_time = None
        current.append(stop)
        current_units += units
        if stop.requested_time is not None:
            last_requested_time = stop.requested_time
    if current:
        trips.append(current)
    return trips


def minutes_between(time1, time2):
    """datetime.time同士の差(分)を返す (time2 - time1)"""
    return (time2.hour * 60 + time2.minute) - (time1.hour * 60 + time1.minute)


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


def build_embed_url(base_address, stop_addresses):
    """APIキー不要で画面に埋め込める Google マップの経路表示用URLを作る"""
    points = [base_address] + stop_addresses + [base_address]
    encoded = [urllib.parse.quote_plus(p) for p in points]
    saddr = encoded[0]
    daddr = "+to:".join(encoded[1:])
    return f"https://maps.google.com/maps?saddr={saddr}&daddr={daddr}&output=embed"


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
    parser.add_argument(
        "--set-travel-time",
        metavar="名前",
        default=None,
        help="指定した名前の子について、店舗との所要時間をconfig.yamlに保存する"
        "(--from-store / --to-store とあわせて指定。--date の日のカレンダーから住所を自動取得)",
    )
    parser.add_argument(
        "--from-store",
        type=int,
        default=None,
        help="店舗 → そのお宅 までの所要時間(分) (--set-travel-time と併用)",
    )
    parser.add_argument(
        "--to-store",
        type=int,
        default=None,
        help="そのお宅 → 店舗 までの所要時間(分) (--set-travel-time と併用)",
    )
    parser.add_argument(
        "--list-events",
        action="store_true",
        help="--date で指定した日のカレンダーの予定を、すべて(🚗マーク以外も含めて)一覧表示して終了する"
        "(🚗の予定が反映されない場合の確認用)",
    )
    args = parser.parse_args()

    target_date = (
        datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
    )

    if args.list_events:
        config = load_config(args.config)
        service = get_calendar_service(config)
        events = fetch_events(service, config["calendar_id"], target_date)
        if not events:
            print(f"{target_date.isoformat()} の予定は見つかりませんでした。")
        else:
            print(f"{target_date.isoformat()} の予定一覧:")
            for event in events:
                title = event.get("summary", "(タイトルなし)")
                start_info = event.get("start", {})
                start = start_info.get("dateTime") or start_info.get("date") or "?"
                location = event.get("location", "").strip() or "(場所未設定)"
                print(f"- {start} {title}  場所: {location}")
        return

    if args.set_travel_time:
        config = load_config(args.config)
        service = get_calendar_service(config)
        events = fetch_events(service, config["calendar_id"], target_date)
        set_travel_time_override(
            args.config, config, events, target_date, args.set_travel_time, args.from_store, args.to_store
        )
        return

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

    base_coords = None
    if geocode_enabled:
        cache = load_geocode_cache()
        base_coords = geocode(base_address, cache)
        for stop in pickup_stops + dropoff_stops:
            stop.coords = geocode(stop.address, cache)
            stop.distance_from_base = haversine_km(base_coords, stop.coords)
        save_geocode_cache(cache)

    default_morning_start = datetime.datetime.combine(
        target_date, datetime.time.fromisoformat(config["morning_start_time"])
    )
    default_evening_start = datetime.datetime.combine(
        target_date, datetime.time.fromisoformat(config["evening_start_time"])
    )
    buffer_minutes = config.get("departure_buffer_minutes", 15)

    trips_data = []
    for label, stops, default_start, default_is_arrival_target in (
        ("朝のお迎え便", pickup_stops, default_morning_start, False),
        ("夕方の送り便", dropoff_stops, default_evening_start, True),
    ):
        if not stops:
            trips_data.append({"label": label, "rows": None})
            continue

        trips = split_into_trips(
            stops, capacity_units, crate_weights, default_crate_size,
            config.get("time_gap_split_minutes", 60),
        )
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
            elif default_is_arrival_target:
                # 時刻指定がない場合、default_start (例: 17:00) に最初のお宅へ
                # 到着する目安になるよう、出発時刻を前倒しする
                departure = default_start - datetime.timedelta(minutes=buffer_minutes)
            else:
                departure = default_start

            remaining_units = capacity_units - loaded_units
            remaining_counts = {
                size: remaining_units // weight for size, weight in crate_weights.items()
            }

            leg_minutes = estimate_leg_minutes(
                base_coords,
                trip_stops,
                config.get("avg_speed_kmh", 20),
                config.get("route_distance_factor", 1.3),
                config.get("travel_time_overrides"),
                config.get("stop_minutes", 5),
            )
            trip_minutes = estimate_trip_minutes(leg_minutes)
            if trip_minutes is not None:
                arrival = departure + datetime.timedelta(minutes=trip_minutes)
                arrival_text = arrival.strftime("%H:%M") + " 頃 (目安)"
            else:
                arrival_text = "-"

            # 出発時刻からの累計移動時間(分)。出発時刻を画面上で変更したときに
            # 各お宅への到着予定・帰着予定を再計算するために使う
            cumulative_minutes = []
            running = 0.0
            broken = False
            for leg in leg_minutes[:len(trip_stops)]:
                if leg is None:
                    broken = True
                if broken:
                    cumulative_minutes.append(None)
                else:
                    running += leg
                    cumulative_minutes.append(running)

            trip = {
                "label": label,
                "trip_no": i,
                "size_counts": size_counts,
                "remaining_counts": remaining_counts,
                "departure": departure.strftime("%H:%M"),
                "arrival": arrival_text,
                "trip_minutes": trip_minutes,
                "rows": [],
                "maps_url": build_maps_url(base_address, [s.address for s in trip_stops]),
                "embed_url": build_embed_url(base_address, [s.address for s in trip_stops]),
            }
            for idx, stop in enumerate(trip_stops):
                t = stop.requested_time.strftime("%H:%M") if stop.requested_time else "-"
                size = stop.crate_size or f"{default_crate_size}(既定)"
                prev_label = trip_stops[idx - 1].name if idx > 0 else "店舗"
                next_label = trip_stops[idx + 1].name if idx + 1 < len(trip_stops) else "店舗"
                from_minutes = leg_minutes[idx]
                to_minutes = leg_minutes[idx + 1]
                from_text = f"{prev_label}から約{round(from_minutes)}分" if from_minutes is not None else "-"
                to_text = f"{next_label}まで約{round(to_minutes)}分" if to_minutes is not None else "-"
                arrival_minutes = cumulative_minutes[idx]
                if arrival_minutes is not None:
                    arrival_time_text = (departure + datetime.timedelta(minutes=arrival_minutes)).strftime("%H:%M")
                else:
                    arrival_time_text = "-"
                trip["rows"].append(
                    {
                        "name": stop.name,
                        "address": stop.address,
                        "time": t,
                        "crate": size,
                        "from": from_text,
                        "next": to_text,
                        "arrival_minutes": arrival_minutes,
                        "arrival_time": arrival_time_text,
                    }
                )
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
        ".map-embed{width:100%;height:400px;border:0;margin-top:8px;}"
        ".departure{font-size:1.1em;font-weight:bold;color:#1a73e8;margin:4px 0;}"
        ".departure input{font-size:1em;font-weight:bold;color:#1a73e8;"
        "border:1px solid #1a73e8;border-radius:4px;padding:2px 4px;}"
        ".capacity-note{color:#555;margin:4px 0;}"
        "</style></head><body>"
    )
    parts.append(f"<h1>送迎ルート {target_date.isoformat()}</h1>")
    parts.append(f"<p class='meta'>拠点: {base_address}</p>")
    parts.append(
        "<p class='meta'>出発時刻は下の入力欄で変更できます。"
        "変更すると、到着予定・帰着予定が自動で再計算されます(あくまで目安です)。</p>"
    )

    trip_idx = 0
    for trip in trips_data:
        if trip["rows"] is None:
            parts.append(f"<h2>{trip['label']}</h2><p>対象なし</p>")
            continue

        trip_idx += 1
        size_text = "・".join(
            f"{size}{trip['size_counts'][size]}個"
            for size in CRATE_SIZE_ORDER
            if trip["size_counts"].get(size)
        )
        remaining_text = "・".join(
            f"{size}{trip['remaining_counts'][size]}個"
            for size in CRATE_SIZE_ORDER
            if trip["remaining_counts"].get(size)
        )
        parts.append(f"<h2>{trip['label']} 第{trip['trip_no']}便</h2>")
        parts.append(
            "<p class='departure'>出発時刻: "
            f"<input type='time' id='dep-{trip_idx}' value='{trip['departure']}' "
            f"oninput='recalcTrip({trip_idx})'></p>"
        )
        parts.append(
            f"<p class='departure'>帰着予定: <span data-trip='{trip_idx}' "
            f"data-min='{trip['trip_minutes'] if trip['trip_minutes'] is not None else ''}' "
            f"data-suffix=' 頃 (目安)'>{trip['arrival']}</span></p>"
        )
        parts.append(f"<p class='capacity-note'>積載: {size_text}</p>")
        if remaining_text:
            parts.append(f"<p class='capacity-note'>あとまだ積めます: {remaining_text}</p>")
        else:
            parts.append("<p class='capacity-note'>満載です</p>")
        parts.append("<table><tr><th>順番</th><th>名前</th><th>住所</th><th>希望時刻</th><th>クレート</th><th>到着予定</th><th>ここまで</th><th>次まで</th></tr>")
        parts.append(f"<tr><td>出発</td><td colspan='2'>{base_address}</td><td>{trip['departure']}</td><td>-</td><td>-</td><td>-</td><td>-</td></tr>")
        for i, row in enumerate(trip["rows"], start=1):
            arrival_min = row["arrival_minutes"]
            data_min = f"{arrival_min}" if arrival_min is not None else ""
            parts.append(
                f"<tr><td>{i}</td><td>{row['name']}</td><td>{row['address']}</td>"
                f"<td>{row['time']}</td><td>{row['crate']}</td>"
                f"<td data-trip='{trip_idx}' data-min='{data_min}' data-suffix=''>{row['arrival_time']}</td>"
                f"<td>{row['from']}</td><td>{row['next']}</td></tr>"
            )
        parts.append(
            f"<tr><td>帰着</td><td colspan='2'>{base_address}</td><td>{trip['arrival']}</td>"
            f"<td data-trip='{trip_idx}' data-min='{trip['trip_minutes'] if trip['trip_minutes'] is not None else ''}' "
            f"data-suffix=' 頃 (目安)'>{trip['arrival']}</td><td>-</td><td>-</td></tr>"
        )
        parts.append("</table>")
        parts.append(f"<a class='maps-link' href='{trip['maps_url']}' target='_blank'>Googleマップでルートを開く</a>")
        parts.append(f"<iframe class='map-embed' src='{trip['embed_url']}' loading='lazy'></iframe>")

    parts.append(
        "<script>"
        "function recalcTrip(tripIdx){"
        "var inp=document.getElementById('dep-'+tripIdx);"
        "var p=inp.value.split(':');"
        "var base=parseInt(p[0],10)*60+parseInt(p[1],10);"
        "var cells=document.querySelectorAll('[data-trip=\"'+tripIdx+'\"]');"
        "cells.forEach(function(cell){"
        "var min=cell.getAttribute('data-min');"
        "if(min===null||min===''){cell.textContent='-';return;}"
        "var total=Math.round(base+parseFloat(min));"
        "total=((total%1440)+1440)%1440;"
        "var h=Math.floor(total/60);var m=total%60;"
        "var hh=(h<10?'0':'')+h;var mm=(m<10?'0':'')+m;"
        "var suffix=cell.getAttribute('data-suffix')||'';"
        "cell.textContent=hh+':'+mm+suffix;"
        "});"
        "}"
        "</script>"
    )

    parts.append("</body></html>")
    return "".join(parts)


if __name__ == "__main__":
    main()
