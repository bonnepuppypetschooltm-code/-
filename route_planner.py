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
import base64
import datetime
import itertools
import json
import math
import os
import re
import smtplib
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
# 「8:00」「8：00」「8時」「8時30分」のいずれの書き方にも対応する
TIME_PATTERN = re.compile(r"(朝|夕方|夕)?(\d{1,2})(?:[:：](\d{2})|時(?:(\d{1,2})分)?)(まで|以降)?")
# 連泊ホテルの「(1/2日目)」のような表記
DAY_PATTERN = re.compile(r"\((\d+)/(\d+)日目\)")

# 「特大」は「大」の部分文字列を含むため、長い名前から先に判定する
CRATE_SIZE_ORDER = ["特大", "大", "中", "小"]


def parse_crate_sizes(tag_text):
    """タグ内のクレートサイズ表記をすべて取り出す。

    「中」「中×2」「中2」「中 中」のように、1頭ずつ複数のクレートを
    指定した場合もそれぞれ1つずつのサイズとしてリストで返す。
    """
    sizes = []
    remaining = tag_text
    for size in CRATE_SIZE_ORDER:
        pattern = re.compile(re.escape(size) + r"\s*[×xX]?\s*(\d*)")
        for m in pattern.finditer(remaining):
            count = int(m.group(1)) if m.group(1) else 1
            sizes.extend([size] * count)
        remaining = pattern.sub("", remaining)
    return sizes


def format_crate_sizes(sizes, default_crate_size):
    """クレートサイズのリストを「中×2」「大+中」のような表示用文字列にする。"""
    if not sizes:
        return f"{default_crate_size}(既定)"
    counts = {}
    order = []
    for size in sizes:
        if size not in counts:
            order.append(size)
        counts[size] = counts.get(size, 0) + 1
    return "+".join(
        f"{size}×{counts[size]}" if counts[size] > 1 else size for size in order
    )


def build_static_map_url(map_points, api_key):
    """実際の地図(Google Maps)上に、番号付きのマーカーとルート線を描いた
    画像のURLを作成する(Google Maps Static APIを利用、APIキーが必要)。

    お宅が増えても、どのマーカーがどのお宅かを番号で区別できるようにする。
    """
    if not map_points or not api_key:
        return None

    label_chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    path_points = "|".join(f"{p['lat']},{p['lon']}" for p in map_points)
    params = [
        ("size", "600x400"),
        ("path", f"color:0x1a73e8cc|weight:3|{path_points}"),
    ]
    for p in map_points:
        if p["label"] in ("店", "P"):
            label = "P" if p["label"] == "P" else "S"
            color = "0x4285f4"
        else:
            try:
                num = int(p["label"])
            except ValueError:
                num = 1
            label = label_chars[num % len(label_chars)]
            color = "0xea4335"
        params.append(("markers", f"color:{color}|label:{label}|{p['lat']},{p['lon']}"))
    params.append(("key", api_key))
    query = "&".join(f"{k}={urllib.parse.quote(str(v), safe=':,|')}" for k, v in params)
    return f"https://maps.googleapis.com/maps/api/staticmap?{query}"


def fetch_static_map_data_uri(map_points, api_key, timeout=10, retries=2):
    """地図画像をルート作成時にダウンロードし、HTMLに直接埋め込めるbase64形式にする。

    メールで届いたHTMLをスマホで開く時点ではネット接続やJavaScriptの実行が
    制限されていることが多いため、<img src="https://...">のような外部URLでは
    画像が表示されない。あらかじめパソコン側(ルート作成時)で画像データを
    取得し、HTMLファイルの中に埋め込んでおくことで、スマホ側はネット接続なしで
    画像を表示できるようにする。
    """
    url = build_static_map_url(map_points, api_key)
    if not url:
        return None
    last_error = None
    for _ in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "route-planner/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                content_type = resp.headers.get_content_type() or "image/png"
            if len(data) < 1000:
                last_error = "image too small (APIキーやエラー画像の可能性)"
                continue
            encoded = base64.b64encode(data).decode("ascii")
            return f"data:{content_type};base64,{encoded}"
        except Exception as exc:
            last_error = exc
            continue
    print(f"警告: 地図画像の取得に失敗しました ({last_error})。Googleマップの埋め込みを表示します。", file=sys.stderr)
    return None



GEOCODE_CACHE_FILE = ".geocode_cache.json"
GEOCODE_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch?q="


class Stop:
    def __init__(self, name, address, requested_time=None, crate_sizes=None, requested_time_type="by"):
        self.name = name
        self.address = address
        self.requested_time = requested_time
        # "by": その時刻までに到着 (まで、または指定なし)
        # "after": その時刻以降に到着 (以降)
        self.requested_time_type = requested_time_type
        self.crate_sizes = crate_sizes or []
        self.distance_from_base = None
        self.coords = None

    def __repr__(self):
        t = self.requested_time.strftime("%H:%M") if self.requested_time else "-"
        suffix = {"by": "まで", "after": "以降"}.get(self.requested_time_type, "")
        return f"{self.name} ({self.address}) [指定: {t}{suffix if self.requested_time else ''}, クレート: {self.crate_sizes}]"


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


def _geocode_request(query):
    """国土地理院 住所検索APIへ1回問い合わせる。失敗時は None"""
    try:
        url = GEOCODE_URL + urllib.parse.quote(query)
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.load(resp)
        if data:
            lon, lat = data[0]["geometry"]["coordinates"]
            return [lat, lon]
    except Exception:
        pass
    return None


def geocode(address, cache):
    """住所から (緯度, 経度) を取得する (国土地理院 住所検索API、無料・APIキー不要)"""
    if address in cache and cache[address] is not None:
        return cache[address]
    # 「Mebius西天満Bldg.、日本、〒530-0047大阪府...」のように、
    # 郵便番号より前にビル名などが付いている住所は検索APIがうまく認識できないため、
    # 郵便番号より前の部分は取り除き、郵便番号より後ろの部分だけで検索する
    m = POSTAL_CODE_PATTERN.search(address)
    query = address[m.end():].strip() if m else address.strip()
    result = _geocode_request(query)
    if result is None:
        # 「大阪府大阪市北区天神橋4-6-17 上谷ビル1F.2F」のように、番地の後ろに
        # スペース区切りでビル名・階数が付いている場合は、番地までの部分だけで再検索する
        first_part = query.split()[0] if query.split() else query
        if first_part != query:
            result = _geocode_request(first_part)
    if result is not None:
        # 失敗(None)はキャッシュしない: 一時的な通信エラーなどで失敗した場合に、
        # 次回以降もずっと失敗扱いになってしまうのを防ぐ
        cache[address] = result
    return result


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


def estimate_leg_minutes(start_coords, end_coords, stops, avg_speed_kmh, route_distance_factor=1.0, travel_time_overrides=None, stop_minutes=0):
    """出発地点 -> 各お宅 -> 帰着地点 を1区間ずつ移動した場合の所要時間(分)のリストを返す。
    結果は (len(stops) + 1) 件で、先頭が「出発地点 -> 最初のお宅」、
    末尾が「最後のお宅 -> 帰着地点」。座標が取得できない区間は None になる。

    実際の道路距離は直線距離より長くなるため、route_distance_factor を
    かけて補正する(例: 1.3 なら直線距離の1.3倍を走行距離とみなす)。

    travel_time_overrides は { 住所: {"from_store": 分, "to_store": 分} }
    の形式で、出発・帰着地点との間にかかる実際の時間がわかっている場合に
    計算結果を上書きするための設定(値は純粋な移動時間)。

    stop_minutes は1軒あたりの乗せ降ろし時間。最初の区間(出発地点 -> 最初のお宅)
    以外の各区間には、出発前の乗せ降ろし時間としてこの分数を加算する。
    """
    travel_time_overrides = travel_time_overrides or {}
    # 全角/半角や前後の空白の違いで一致しないことがあるため、正規化してから比較する
    normalized_overrides = {
        normalize_address(addr): override for addr, override in travel_time_overrides.items()
    }

    points = [start_coords] + [s.coords for s in stops] + [end_coords]
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


def cumulative_arrival_minutes(leg_minutes, n):
    """leg_minutes(店舗 -> 1件目 -> ... -> 店舗)から、店舗出発を起点とした
    各お宅への累計移動時間(分)のリスト(長さ n)を返す。
    途中で区間が不明(None)になったら、それ以降は None になる。
    """
    cumulative = []
    running = 0.0
    broken = False
    for leg in leg_minutes[:n]:
        if leg is None:
            broken = True
        if broken:
            cumulative.append(None)
        else:
            running += leg
            cumulative.append(running)
    return cumulative


def evaluate_departure(trip_stops, leg_minutes, target_date, buffer_minutes):
    """trip_stopsの並び順での累計移動時間(cumulative)をもとに、
    時刻指定のあるお宅すべてに合わせた出発時刻(departure)と、
    その並び順の良さを表す margin(分、大きいほど余裕がある)を計算する。

    - 「まで」指定のお宅: 出発時刻 + 累計移動時間 <= 希望時刻 - buffer_minutes
      を満たす最も遅い出発時刻を求める
    - 「以降」指定のお宅: 出発時刻 + 累計移動時間 >= 希望時刻
      を満たす最も早い出発時刻(=ちょうど希望時刻に到着)を求める

    両方が混在する場合、margin = (まで制約の上限) - (以降制約の下限) で、
    margin >= 0 ならすべて満たせる。departure は「まで」制約があれば
    その上限(最も遅い時刻)、なければ「以降」制約の下限を使う。

    時刻指定のあるお宅が無い場合は (None, None, cumulative) を返す。
    """
    cumulative = cumulative_arrival_minutes(leg_minutes, len(trip_stops))

    by_candidates = []
    after_candidates = []
    for stop, cum in zip(trip_stops, cumulative):
        if stop.requested_time is None or cum is None:
            continue
        requested_dt = datetime.datetime.combine(target_date, stop.requested_time)
        if stop.requested_time_type == "after":
            after_candidates.append(requested_dt - datetime.timedelta(minutes=cum))
        else:
            by_candidates.append(requested_dt - datetime.timedelta(minutes=cum + buffer_minutes))

    if not by_candidates and not after_candidates:
        return None, None, cumulative

    upper = min(by_candidates) if by_candidates else None
    lower = max(after_candidates) if after_candidates else None

    if upper is not None and lower is not None:
        margin = (upper - lower).total_seconds() / 60
        departure = upper
    elif upper is not None:
        margin = float("inf")
        departure = upper
    else:
        margin = float("inf")
        departure = lower

    return departure, margin, cumulative


def order_stops_for_schedule(trip_stops, start_coords, end_coords, avg_speed_kmh, route_distance_factor,
                              travel_time_overrides, stop_minutes, target_date, buffer_minutes,
                              is_dropoff, max_anchors=7):
    """時刻指定のあるお宅(アンカー)の順番を入れ替えて、すべての希望時刻に
    なるべく合うような訪問順を探す。

    時刻指定のないお宅は、朝のお迎え便ならアンカーの後ろ、夕方の送り便なら
    アンカーの前に固定したまま、アンカー同士の順番だけ並べ替えて試す
    (アンカーが多い場合は計算量を抑えるため並べ替えを行わない)。

    戻り値: (trip_stops, leg_minutes, departure, cumulative)
    departure は時刻指定のあるお宅が無い場合は None。
    """
    anchors = [s for s in trip_stops if s.requested_time is not None]
    others = [s for s in trip_stops if s.requested_time is None]

    if len(anchors) <= 1 or len(anchors) > max_anchors:
        candidates = [trip_stops]
    else:
        candidates = []
        for perm in itertools.permutations(anchors):
            if is_dropoff:
                candidates.append(list(others) + list(perm))
            else:
                candidates.append(list(perm) + list(others))

    best = None
    for candidate_stops in candidates:
        leg_minutes = estimate_leg_minutes(
            start_coords, end_coords, candidate_stops, avg_speed_kmh, route_distance_factor,
            travel_time_overrides, stop_minutes,
        )
        departure, margin, cumulative = evaluate_departure(
            candidate_stops, leg_minutes, target_date, buffer_minutes
        )
        if margin is None:
            margin = float("inf")
        # 同じ margin (例: すべて「以降」指定で制約が無い) の場合は、
        # 全体の所要時間が短い順を優先する
        trip_minutes = estimate_trip_minutes(leg_minutes)
        tie_breaker = -trip_minutes if trip_minutes is not None else float("-inf")
        score = (margin, tie_breaker)
        if best is None or score > best[4]:
            best = (candidate_stops, leg_minutes, departure, cumulative, score)

    candidate_stops, leg_minutes, departure, cumulative, _ = best
    return candidate_stops, leg_minutes, departure, cumulative


DEFAULT_CONFIG = {
    "base_address": "大阪府大阪市北区天神橋4-6-17 上谷ビル1F.2F",
    "base_name": "天満店",
    "parking_address": None,
    "parking_name": "駐車場",
    "google_maps_api_key": None,
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

    # 既存の travel_time_overrides セクションを取り除き、
    # それ以外の内容(travel_time_overridesより後にある設定やコメントも含む)はそのまま保持して、
    # 新しい内容を末尾に追加する
    with open(config_path, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(
        r"^travel_time_overrides:.*?(?=^\S.*:|\Z)", "", text, count=1, flags=re.MULTILINE | re.DOTALL
    )
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
    """🚗マーク付きイベントを (name, tag_text, address, day_info) にパースする。
    対象外なら None を返す。
    day_info は連泊ホテルの「(1/2日目)」のような表記があれば (日目, 全体日数) の
    タプル、なければ None。
    """
    title = event.get("summary", "")
    if CAR_MARK not in title:
        return None

    location = event.get("location", "").strip()
    if not location:
        print(f"警告: 「{title}」に場所(住所)が設定されていません。スキップします。", file=sys.stderr)
        return None

    after_mark = title.split(CAR_MARK, 1)[1].strip()

    # 「(1/2日目)」のような連泊表記を先に取り除く (TAG_PATTERNが半角()も
    # タグとして扱ってしまうため、先に処理する)
    day_info = None
    day_match = DAY_PATTERN.search(after_mark)
    if day_match:
        day_info = (int(day_match.group(1)), int(day_match.group(2)))
        after_mark = DAY_PATTERN.sub("", after_mark).strip()

    # 「[往復 朝8:00] [大]」のように複数の[]に分けて書いた場合も
    # まとめて1つのタグとして扱う
    tag_text = " ".join(m.group(1) for m in TAG_PATTERN.finditer(after_mark))
    name = TAG_PATTERN.sub("", after_mark).strip()
    if not name:
        name = "(名前未設定)"

    return name, tag_text, location, day_info


def classify_stop(name, tag_text, location, day_info=None):
    """(pickup_stop_or_None, dropoff_stop_or_None) を返す"""
    has_pickup_tag = bool(re.search(r"(迎え|朝)のみ", tag_text)) or "チェックインのみ" in tag_text
    has_dropoff_tag = bool(re.search(r"(送り|夕)のみ", tag_text)) or "チェックアウトのみ" in tag_text
    is_roundtrip = "往復" in tag_text or (not has_pickup_tag and not has_dropoff_tag)

    pickup_time = None
    pickup_time_type = "by"
    dropoff_time = None
    dropoff_time_type = "by"
    unlabeled_times = []
    for prefix, h, m_colon, m_kanji, suffix in TIME_PATTERN.findall(tag_text):
        minute = m_colon or m_kanji or "0"
        t = datetime.time(int(h), int(minute))
        time_type = "after" if suffix == "以降" else "by"
        if prefix == "朝":
            pickup_time, pickup_time_type = t, time_type
        elif prefix in ("夕", "夕方"):
            dropoff_time, dropoff_time_type = t, time_type
        else:
            unlabeled_times.append((t, time_type))

    if len(unlabeled_times) >= 2:
        if pickup_time is None:
            pickup_time, pickup_time_type = unlabeled_times[0]
        if dropoff_time is None:
            dropoff_time, dropoff_time_type = unlabeled_times[1]
    elif len(unlabeled_times) == 1:
        if pickup_time is None and dropoff_time is None:
            (pickup_time, pickup_time_type) = (dropoff_time, dropoff_time_type) = unlabeled_times[0]
        elif pickup_time is None:
            pickup_time, pickup_time_type = unlabeled_times[0]
        elif dropoff_time is None:
            dropoff_time, dropoff_time_type = unlabeled_times[0]

    crate_sizes = parse_crate_sizes(tag_text)

    pickup_stop = None
    dropoff_stop = None

    # 連泊ホテル「(X/Y日目)」表記がある場合: 中日は送迎なし、
    # 1日目はお迎え(チェックイン)のみ、最終日は送り(チェックアウト)のみ
    if day_info is not None:
        day_num, total_days = day_info
        if total_days > 1:
            if 1 < day_num < total_days:
                return None, None
            if day_num == 1:
                if is_roundtrip or has_pickup_tag:
                    pickup_stop = Stop(name, location, pickup_time, crate_sizes, pickup_time_type)
                return pickup_stop, None
            if day_num == total_days:
                if is_roundtrip or has_dropoff_tag:
                    dropoff_stop = Stop(name, location, dropoff_time, crate_sizes, dropoff_time_type)
                return None, dropoff_stop

    if is_roundtrip or has_pickup_tag:
        pickup_stop = Stop(name, location, pickup_time, crate_sizes, pickup_time_type)
    if is_roundtrip or has_dropoff_tag:
        dropoff_stop = Stop(name, location, dropoff_time, crate_sizes, dropoff_time_type)

    return pickup_stop, dropoff_stop


def split_into_trips(stops, capacity_units, crate_weights, default_crate_size, time_gap_split_minutes=None, is_dropoff=False):
    """クレートサイズごとの占有units数をもとに、車に収まる範囲でトリップに分割する。

    朝のお迎え便 (is_dropoff=False): 時刻指定があるお宅はその時刻順を優先する。
    時刻指定がないお宅は、店舗から遠い順 (遠方を先に回り、店舗近くで締めくくる) に並べる。

    夕方の送り便 (is_dropoff=True): 時刻指定がないお宅を先に (店舗から遠い順)、
    時刻指定があるお宅を最後にその時刻順で並べる。最後に訪問するお宅の希望時刻に
    間に合うよう、出発時刻を逆算するため (build_route側で計算)。

    時刻指定のあるお宅同士の希望時刻が time_gap_split_minutes 以上離れている場合は、
    その間で一旦店舗に戻ることにして便を分割する
    (待ち時間が長くなりすぎるのを避けるため)。
    """
    if is_dropoff:
        def sort_key(s):
            if s.requested_time:
                return (1, s.requested_time, 0.0)
            return (0, datetime.time(0, 0), -(s.distance_from_base or 0.0))
    else:
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
        sizes = stop.crate_sizes or [default_crate_size]
        units = sum(crate_weights.get(size, crate_weights[default_crate_size]) for size in sizes)
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


def build_maps_url(start_address, end_address, stop_addresses):
    """APIキー不要の Google Maps 経路リンクを作る (出発地点 -> 各お宅 -> 帰着地点)"""
    points = [start_address] + stop_addresses + [end_address]
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


def build_embed_url(start_address, end_address, stop_addresses):
    """APIキー不要で画面に埋め込める Google マップの経路表示用URLを作る"""
    points = [start_address] + stop_addresses + [end_address]
    encoded = [urllib.parse.quote_plus(p) for p in points]
    saddr = encoded[0]
    daddr = "+to:".join(encoded[1:])
    return f"https://maps.google.com/maps?saddr={saddr}&daddr={daddr}&output=embed"


def send_route_email(config, target_date, html, output_path):
    """作成したルートHTMLを、メールに添付して送信する (Gmailのアプリパスワードを使用)"""
    import mimetypes
    from email.message import EmailMessage

    gmail_address = config["email_from"]
    app_password = config["email_app_password"]
    to_addrs = config["email_to"]
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]

    msg = EmailMessage()
    msg["Subject"] = f"【送迎ルート】{target_date.isoformat()}"
    msg["From"] = gmail_address
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(
        f"{target_date.isoformat()} の送迎ルートです。\n"
        "添付のファイルを開いて確認してください。"
    )
    msg.add_attachment(
        html.encode("utf-8"),
        maintype="text",
        subtype="html",
        filename=os.path.basename(output_path),
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_address, app_password)
        smtp.send_message(msg)


def main():
    parser = argparse.ArgumentParser(description="犬の幼稚園 送迎ルート自動作成")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス")
    parser.add_argument(
        "--date",
        default=None,
        help="対象日 (YYYY-MM-DD)。未指定なら今日",
    )
    parser.add_argument(
        "--tomorrow",
        action="store_true",
        help="対象日を「明日」にする(--dateと併用した場合は--dateが優先されます)",
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
        "--no-email",
        action="store_true",
        help="config.yamlにメール設定があってもメールを送らない",
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

    if args.date:
        target_date = datetime.date.fromisoformat(args.date)
    elif args.tomorrow:
        target_date = datetime.date.today() + datetime.timedelta(days=1)
    else:
        target_date = datetime.date.today()

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

    locations, trips_data = build_route(
        target_date, config, events, geocode_enabled=not args.demo
    )

    html = render_html(target_date, locations, trips_data, config.get("google_maps_api_key"))
    output_path = args.output or f"送迎ルート_{target_date.isoformat()}.html"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"作成しました: {os.path.abspath(output_path)}")

    if config.get("email_to") and not args.no_email:
        try:
            send_route_email(config, target_date, html, output_path)
            print(f"メールを送信しました: {config['email_to']}")
        except Exception as e:
            print(f"警告: メールの送信に失敗しました: {e}", file=sys.stderr)

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
        name, tag_text, location, day_info = parsed
        pickup, dropoff = classify_stop(name, tag_text, location, day_info)
        if pickup:
            pickup_stops.append(pickup)
        if dropoff:
            dropoff_stops.append(dropoff)

    base_address = config["base_address"]
    base_name = config.get("base_name") or "天満店"
    parking_address = config.get("parking_address")
    parking_name = config.get("parking_name") or "駐車場"
    crate_capacity = config["crate_capacity"]
    default_crate_size = config["default_crate_size"]
    capacity_units, crate_weights = crate_units(crate_capacity)

    base_coords = None
    parking_coords = None
    if geocode_enabled:
        cache = load_geocode_cache()
        base_coords = geocode(base_address, cache)
        if parking_address:
            parking_coords = geocode(parking_address, cache)
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

    base_info = (base_address, base_name, base_coords, "店")
    parking_info = (parking_address or base_address, parking_name if parking_address else base_name,
                    parking_coords if parking_address else base_coords, "P" if parking_address else "店")

    trips_data = []
    for label, stops, default_start, default_is_arrival_target, start_info, end_info in (
        ("朝のお迎え便", pickup_stops, default_morning_start, False, parking_info, base_info),
        ("夕方の送り便", dropoff_stops, default_evening_start, True, parking_info, parking_info),
    ):
        start_address, start_name, start_coords, start_short = start_info
        end_address, end_name, end_coords, end_short = end_info

        if not stops:
            trips_data.append({"label": label, "rows": None})
            continue

        trips = split_into_trips(
            stops, capacity_units, crate_weights, default_crate_size,
            config.get("time_gap_split_minutes", 60) if default_is_arrival_target else None,
            is_dropoff=default_is_arrival_target,
        )
        for i, trip_stops in enumerate(trips, start=1):
            size_counts = {}
            loaded_units = 0
            for stop in trip_stops:
                sizes = stop.crate_sizes or [default_crate_size]
                for size in sizes:
                    size_counts[size] = size_counts.get(size, 0) + 1
                    loaded_units += crate_weights.get(size, crate_weights[default_crate_size])

            remaining_units = capacity_units - loaded_units
            remaining_counts = {
                size: remaining_units // weight for size, weight in crate_weights.items()
            }

            trip_stops, leg_minutes, departure, cumulative_minutes = order_stops_for_schedule(
                trip_stops,
                start_coords,
                end_coords,
                config.get("avg_speed_kmh", 20),
                config.get("route_distance_factor", 1.3),
                config.get("travel_time_overrides"),
                config.get("stop_minutes", 5),
                target_date,
                buffer_minutes,
                is_dropoff=default_is_arrival_target,
            )

            if departure is None:
                if default_is_arrival_target:
                    # 時刻指定がない場合、default_start (例: 17:00) に最初のお宅へ
                    # 到着する目安になるよう、出発時刻を前倒しする
                    departure = default_start - datetime.timedelta(minutes=buffer_minutes)
                else:
                    departure = default_start

            # 表示する「○分」(四捨五入後)と実際の到着時刻がずれて見えるのを防ぐため、
            # 四捨五入した分数を使って到着時刻・帰着予定も計算する
            rounded_leg_minutes = [round(m) if m is not None else None for m in leg_minutes]
            rounded_cumulative_minutes = cumulative_arrival_minutes(rounded_leg_minutes, len(trip_stops))

            trip_minutes = estimate_trip_minutes(rounded_leg_minutes)
            if trip_minutes is not None:
                arrival = departure + datetime.timedelta(minutes=trip_minutes)
                arrival_text = arrival.strftime("%H:%M") + " 頃 (目安)"
            else:
                arrival_text = "-"

            map_points = []
            if start_coords:
                map_points.append({"lat": start_coords[0], "lon": start_coords[1], "label": start_short, "title": start_name})
            for idx, stop in enumerate(trip_stops, start=1):
                if stop.coords:
                    map_points.append({"lat": stop.coords[0], "lon": stop.coords[1], "label": str(idx), "title": stop.name})
            if end_coords:
                map_points.append({"lat": end_coords[0], "lon": end_coords[1], "label": end_short, "title": end_name})

            trip = {
                "label": label,
                "trip_no": i,
                "size_counts": size_counts,
                "remaining_counts": remaining_counts,
                "departure": departure.strftime("%H:%M"),
                "arrival": arrival_text,
                "trip_minutes": trip_minutes,
                "map_points": map_points,
                "rows": [],
                "start_name": start_name,
                "end_name": end_name,
                "maps_url": build_maps_url(start_address, end_address, [s.address for s in trip_stops]),
                "embed_url": build_embed_url(start_address, end_address, [s.address for s in trip_stops]),
                "start_coords": start_coords,
                "end_coords": end_coords,
                "base_name": base_name,
                "base_coords": base_coords,
                "avg_speed_kmh": config.get("avg_speed_kmh", 20),
                "route_distance_factor": config.get("route_distance_factor", 1.3),
                "stop_minutes": config.get("stop_minutes", 5),
            }
            for idx, stop in enumerate(trip_stops):
                if stop.requested_time:
                    suffix = "以降" if stop.requested_time_type == "after" else "まで"
                    t = stop.requested_time.strftime("%H:%M") + suffix
                else:
                    t = "-"
                size = format_crate_sizes(stop.crate_sizes, default_crate_size)
                prev_label = trip_stops[idx - 1].name if idx > 0 else start_name
                next_label = trip_stops[idx + 1].name if idx + 1 < len(trip_stops) else end_name
                from_minutes = rounded_leg_minutes[idx]
                to_minutes = rounded_leg_minutes[idx + 1]
                from_text = f"{prev_label}から約{from_minutes}分" if from_minutes is not None else "-"
                to_text = f"{next_label}まで約{to_minutes}分" if to_minutes is not None else "-"
                arrival_minutes = rounded_cumulative_minutes[idx]
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
                        "lat": stop.coords[0] if stop.coords else None,
                        "lon": stop.coords[1] if stop.coords else None,
                    }
                )
            trips_data.append(trip)

    locations = {
        "base_name": base_name,
        "base_address": base_address,
        "parking_name": parking_name if parking_address else None,
        "parking_address": parking_address,
    }
    return locations, trips_data


def render_html(target_date, locations, trips_data, google_maps_api_key=None):
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
        ".route-map{width:100%;height:auto;margin-top:8px;border:1px solid #ccc;background:#eef3f8;}"
        ".departure{font-size:1.1em;font-weight:bold;color:#1a73e8;margin:4px 0;}"
        ".departure input{font-size:1em;font-weight:bold;color:#1a73e8;"
        "border:1px solid #1a73e8;border-radius:4px;padding:2px 4px;}"
        ".capacity-note{color:#555;margin:4px 0;}"
        ".arrival-input{font-size:1em;border:1px solid #ccc;border-radius:4px;padding:2px 4px;width:100%;}"
        ".move-btn{margin-left:4px;cursor:pointer;}"
        "</style></head><body>"
    )
    parts.append(f"<h1>送迎ルート {target_date.isoformat()}</h1>")
    parts.append(f"<p class='meta'>{locations['base_name']}: {locations['base_address']}</p>")
    if locations.get("parking_address"):
        parts.append(f"<p class='meta'>{locations['parking_name']}: {locations['parking_address']}</p>")
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
        trip_minutes = trip["trip_minutes"]
        trip_minutes_attr = trip_minutes if trip_minutes is not None else ""
        if trip_minutes is not None:
            departure_dt = datetime.datetime.strptime(trip["departure"], "%H:%M")
            arrival_value = (departure_dt + datetime.timedelta(minutes=trip_minutes)).strftime("%H:%M")
            arrival_input = (
                f"<input type='time' id='arr-{trip_idx}' value='{arrival_value}' "
                f"data-trip-minutes='{trip_minutes_attr}' data-order='{len(trip['rows']) + 1}' "
                f"oninput='recalcFromArrival({trip_idx})' "
                f"onchange='recalcFromArrival({trip_idx})'>"
            )
        else:
            arrival_input = (
                f"<input type='time' id='arr-{trip_idx}' data-trip-minutes='' disabled> (目安計算不可)"
            )

        parts.append(f"<h2>{trip['label']} 第{trip['trip_no']}便</h2>")
        parts.append(
            "<p class='departure'>出発時刻: "
            f"<input type='time' id='dep-{trip_idx}' value='{trip['departure']}' "
            f"oninput='recalcFromDeparture({trip_idx})' onchange='recalcFromDeparture({trip_idx})'></p>"
        )
        parts.append(f"<p class='departure'>帰着予定: {arrival_input}</p>")
        parts.append(f"<p class='capacity-note'>積載: {size_text}</p>")
        if remaining_text:
            parts.append(f"<p class='capacity-note'>あとまだ積めます: {remaining_text}</p>")
        else:
            parts.append("<p class='capacity-note'>満載です</p>")
        start_lat, start_lon = trip["start_coords"] if trip["start_coords"] else ("", "")
        end_lat, end_lon = trip["end_coords"] if trip["end_coords"] else ("", "")
        base_lat, base_lon = trip["base_coords"] if trip["base_coords"] else ("", "")
        parts.append(
            f"<table data-route-trip='{trip_idx}' data-start-lat='{start_lat}' data-start-lon='{start_lon}' "
            f"data-end-lat='{end_lat}' data-end-lon='{end_lon}' data-speed='{trip['avg_speed_kmh']}' "
            f"data-factor='{trip['route_distance_factor']}' data-stopmin='{trip['stop_minutes']}' "
            f"data-start-name='{trip['start_name']}' data-end-name='{trip['end_name']}' "
            f"data-base-lat='{base_lat}' data-base-lon='{base_lon}' data-base-name='{trip['base_name']}'>"
            "<tr><th>順番</th><th>名前</th><th>住所</th><th>希望時刻</th><th>クレート</th><th>到着予定</th><th>ここまで</th><th>次まで</th></tr>"
        )
        final_order = len(trip["rows"]) + 1
        parts.append(
            f"<tr><td>出発</td><td colspan='2'>{trip['start_name']}</td><td>-</td><td>-</td>"
            f"<td data-trip='{trip_idx}' data-order='0' data-min='0' data-suffix=''>{trip['departure']}</td><td>-</td><td>-</td></tr>"
        )
        stop_count = len(trip["rows"])
        for i, row in enumerate(trip["rows"], start=1):
            arrival_min = row["arrival_minutes"]
            data_min = f"{arrival_min}" if arrival_min is not None else ""
            arrival_value = row["arrival_time"] if row["arrival_time"] != "-" else ""
            arrival_cell = (
                f"<input type='time' class='arrival-input' data-trip='{trip_idx}' data-order='{i}' "
                f"data-min='{data_min}' data-suffix='' value='{arrival_value}' "
                f"oninput='recalcFromArrivalEdit(this)' onchange='recalcFromArrivalEdit(this)'>"
            )
            lat = row["lat"] if row["lat"] is not None else ""
            lon = row["lon"] if row["lon"] is not None else ""
            order_options = "".join(
                f"<option value='{n}'{' selected' if n == i else ''}>{n}</option>"
                for n in range(1, stop_count + 1)
            )
            order_cell = (
                f"<select class='order-select' onchange='moveStopRowTo(this)'>{order_options}</select> "
                "<button type='button' class='move-btn' onclick='moveStopRow(this,-1)'>↑</button>"
                "<button type='button' class='move-btn' onclick='moveStopRow(this,1)'>↓</button>"
                "<button type='button' class='move-btn' onclick='insertShopAfter(this)'>+店舗</button>"
            )
            parts.append(
                f"<tr class='stop-row' data-lat='{lat}' data-lon='{lon}' data-name='{row['name']}'>"
                f"<td class='order-cell'>{order_cell}</td><td>{row['name']}</td><td>{row['address']}</td>"
                f"<td>{row['time']}</td><td>{row['crate']}</td>"
                f"<td>{arrival_cell}</td>"
                f"<td class='from-cell'>{row['from']}</td><td class='next-cell'>{row['next']}</td></tr>"
            )
        parts.append(
            f"<tr><td>帰着</td><td colspan='2'>{trip['end_name']}</td><td>-</td><td>-</td>"
            f"<td class='final-cell' data-trip='{trip_idx}' data-order='{final_order}' data-min='{trip_minutes_attr}' "
            f"data-suffix=' 頃 (目安)'>{trip['arrival']}</td><td>-</td><td>-</td></tr>"
        )
        parts.append("</table>")
        parts.append(f"<a class='maps-link' href='{trip['maps_url']}' target='_blank'>Googleマップでルートを開く</a>")
        if trip["map_points"] and google_maps_api_key:
            data_uri = fetch_static_map_data_uri(trip["map_points"], google_maps_api_key)
        else:
            data_uri = None
        if data_uri:
            print("→ 地図画像の取得に成功しました。", file=sys.stderr)
            parts.append(f"<img class='route-map' src='{data_uri}' alt='ルート地図(番号は表の順番と対応)'>")
        else:
            parts.append(f"<iframe class='map-embed' src='{trip['embed_url']}' loading='lazy'></iframe>")

    parts.append(
        "<script>"
        "function timeToMinutes(value){"
        "var p=value.split(':');return parseInt(p[0],10)*60+parseInt(p[1],10);"
        "}"
        "function minutesToTime(total){"
        "total=Math.round(total);total=((total%1440)+1440)%1440;"
        "var h=Math.floor(total/60);var m=total%60;"
        "return (h<10?'0':'')+h+':'+(m<10?'0':'')+m;"
        "}"
        "function recalcFromDeparture(tripIdx){"
        "var dep=document.getElementById('dep-'+tripIdx);"
        "if(!dep.value)return;"
        "var base=timeToMinutes(dep.value);"
        "var cells=document.querySelectorAll('[data-trip=\"'+tripIdx+'\"]');"
        "cells.forEach(function(cell){"
        "var min=cell.getAttribute('data-min');"
        "var isInput=(cell.tagName==='INPUT');"
        "if(min===null||min===''){if(isInput){cell.value='';}else{cell.textContent='-';}return;}"
        "var suffix=cell.getAttribute('data-suffix')||'';"
        "var text=minutesToTime(base+parseFloat(min))+suffix;"
        "if(isInput){cell.value=text;}else{cell.textContent=text;}"
        "});"
        "var arr=document.getElementById('arr-'+tripIdx);"
        "var tripMin=arr.getAttribute('data-trip-minutes');"
        "if(tripMin!==null&&tripMin!==''){"
        "arr.value=minutesToTime(base+parseFloat(tripMin));"
        "}"
        "}"
        "function recalcFromArrival(tripIdx){"
        "var arr=document.getElementById('arr-'+tripIdx);"
        "if(!arr.value)return;"
        "var tripMin=arr.getAttribute('data-trip-minutes');"
        "if(tripMin===null||tripMin==='')return;"
        "var dep=document.getElementById('dep-'+tripIdx);"
        "dep.value=minutesToTime(timeToMinutes(arr.value)-parseFloat(tripMin));"
        "recalcFromDeparture(tripIdx);"
        "}"
        "function recalcFromArrivalEdit(input){"
        "var tripIdx=input.getAttribute('data-trip');"
        "var dep=document.getElementById('dep-'+tripIdx);"
        "if(!dep||!dep.value||!input.value)return;"
        "var base=timeToMinutes(dep.value);"
        "var oldMin=parseFloat(input.getAttribute('data-min'));"
        "if(isNaN(oldMin))return;"
        "var newMin=timeToMinutes(input.value)-base;"
        "var delta=newMin-oldMin;"
        "if(delta===0)return;"
        "dep.value=minutesToTime(base+delta);"
        "recalcFromDeparture(tripIdx);"
        "}"
        "function haversineKm(lat1,lon1,lat2,lon2){"
        "var R=6371.0;"
        "var toRad=function(d){return d*Math.PI/180;};"
        "var dLat=toRad(lat2-lat1);var dLon=toRad(lon2-lon1);"
        "var a=Math.sin(dLat/2)*Math.sin(dLat/2)+"
        "Math.cos(toRad(lat1))*Math.cos(toRad(lat2))*Math.sin(dLon/2)*Math.sin(dLon/2);"
        "var c=2*Math.atan2(Math.sqrt(a),Math.sqrt(1-a));"
        "return R*c;"
        "}"
        "function moveStopRow(button,direction){"
        "var row=button.closest('tr');"
        "var target=direction<0?row.previousElementSibling:row.nextElementSibling;"
        "if(!target||!target.classList.contains('stop-row'))return;"
        "if(direction<0){row.parentNode.insertBefore(row,target);}"
        "else{row.parentNode.insertBefore(target,row);}"
        "var table=row.closest('table');"
        "renumberStopRows(table);"
        "recalcOrder(table);"
        "}"
        "function renumberStopRows(table){"
        "var rows=table.querySelectorAll('tr.stop-row');"
        "var total=rows.length;"
        "rows.forEach(function(r,idx){"
        "var cell=r.querySelector('.order-cell');"
        "var buttons=Array.prototype.slice.call(cell.querySelectorAll('.move-btn'));"
        "var oldSelect=cell.querySelector('.order-select');"
        "if(oldSelect){cell.removeChild(oldSelect);}"
        "buttons.forEach(function(b){if(b.parentNode===cell){cell.removeChild(b);}});"
        "var select=document.createElement('select');"
        "select.className='order-select';"
        "select.setAttribute('onchange','moveStopRowTo(this)');"
        "for(var n=1;n<=total;n++){"
        "var opt=document.createElement('option');"
        "opt.value=n;opt.textContent=n;"
        "if(n===idx+1){opt.selected=true;}"
        "select.appendChild(opt);"
        "}"
        "cell.appendChild(select);"
        "cell.appendChild(document.createTextNode(' '));"
        "buttons.forEach(function(b){cell.appendChild(b);});"
        "var input=r.querySelector('.arrival-input');"
        "if(input&&!r.classList.contains('shop-row')){input.setAttribute('data-order',idx+1);}"
        "});"
        "}"
        "function moveStopRowTo(select){"
        "var row=select.closest('tr');"
        "var table=row.closest('table');"
        "var rows=Array.prototype.slice.call(table.querySelectorAll('tr.stop-row'));"
        "var targetIdx=parseInt(select.value,10)-1;"
        "var currentIdx=rows.indexOf(row);"
        "if(isNaN(targetIdx)||targetIdx===currentIdx)return;"
        "var others=rows.filter(function(r){return r!==row;});"
        "var refNode=(targetIdx>=others.length)?"
        "(others.length?others[others.length-1].nextElementSibling:row.nextElementSibling):others[targetIdx];"
        "var parent=row.parentNode;"
        "parent.removeChild(row);"
        "parent.insertBefore(row,refNode||null);"
        "renumberStopRows(table);"
        "recalcOrder(table);"
        "}"
        "function insertShopAfter(button){"
        "var row=button.closest('tr');"
        "var table=row.closest('table');"
        "var startLat=table.getAttribute('data-base-lat');"
        "var startLon=table.getAttribute('data-base-lon');"
        "var startName=table.getAttribute('data-base-name')||'店舗';"
        "var newRow=document.createElement('tr');"
        "newRow.className='stop-row shop-row';"
        "newRow.setAttribute('data-lat',startLat);"
        "newRow.setAttribute('data-lon',startLon);"
        "newRow.setAttribute('data-name',startName);"
        "newRow.innerHTML="
        "\"<td class='order-cell'></td>\"+"
        "\"<td>\"+startName+\"(経由)</td>\"+"
        "\"<td>-</td><td>-</td><td>-</td>\"+"
        "\"<td><input type='time' class='arrival-input' readonly></td>\"+"
        "\"<td class='from-cell'>-</td><td class='next-cell'>-</td>\";"
        "var orderCell=newRow.querySelector('.order-cell');"
        "var upBtn=document.createElement('button');"
        "upBtn.type='button';upBtn.className='move-btn';upBtn.textContent='↑';"
        "upBtn.setAttribute('onclick','moveStopRow(this,-1)');"
        "var downBtn=document.createElement('button');"
        "downBtn.type='button';downBtn.className='move-btn';downBtn.textContent='↓';"
        "downBtn.setAttribute('onclick','moveStopRow(this,1)');"
        "var delBtn=document.createElement('button');"
        "delBtn.type='button';delBtn.className='move-btn';delBtn.textContent='✕削除';"
        "delBtn.setAttribute('onclick','removeShopRow(this)');"
        "orderCell.appendChild(upBtn);orderCell.appendChild(downBtn);orderCell.appendChild(delBtn);"
        "row.parentNode.insertBefore(newRow,row.nextElementSibling);"
        "renumberStopRows(table);"
        "recalcOrder(table);"
        "}"
        "function removeShopRow(button){"
        "var row=button.closest('tr');"
        "var table=row.closest('table');"
        "row.parentNode.removeChild(row);"
        "renumberStopRows(table);"
        "recalcOrder(table);"
        "}"
        "function recalcOrder(table){"
        "var tripIdx=table.getAttribute('data-route-trip');"
        "var startLat=parseFloat(table.getAttribute('data-start-lat'));"
        "var startLon=parseFloat(table.getAttribute('data-start-lon'));"
        "var endLat=parseFloat(table.getAttribute('data-end-lat'));"
        "var endLon=parseFloat(table.getAttribute('data-end-lon'));"
        "var speed=parseFloat(table.getAttribute('data-speed'));"
        "var factor=parseFloat(table.getAttribute('data-factor'));"
        "var stopMin=parseFloat(table.getAttribute('data-stopmin'));"
        "var startName=table.getAttribute('data-start-name');"
        "var endName=table.getAttribute('data-end-name');"
        "var rows=Array.prototype.slice.call(table.querySelectorAll('tr.stop-row'));"
        "var points=[{lat:startLat,lon:startLon,name:startName}];"
        "rows.forEach(function(r){"
        "points.push({lat:parseFloat(r.getAttribute('data-lat')),lon:parseFloat(r.getAttribute('data-lon')),name:r.getAttribute('data-name')});"
        "});"
        "points.push({lat:endLat,lon:endLon,name:endName});"
        "var legs=[];"
        "for(var i=0;i<points.length-1;i++){"
        "var p1=points[i],p2=points[i+1];"
        "if(isNaN(p1.lat)||isNaN(p1.lon)||isNaN(p2.lat)||isNaN(p2.lon)){legs.push(null);continue;}"
        "var km=haversineKm(p1.lat,p1.lon,p2.lat,p2.lon);"
        "var min=km*factor/speed*60;"
        "if(i>0){min+=stopMin;}"
        "legs.push(min);"
        "}"
        "var dep=document.getElementById('dep-'+tripIdx);"
        "var base=dep&&dep.value?timeToMinutes(dep.value):null;"
        "var cum=0;var broken=false;"
        "rows.forEach(function(r,idx){"
        "var input=r.querySelector('.arrival-input');"
        "var fromCell=r.querySelector('.from-cell');"
        "var nextCell=r.querySelector('.next-cell');"
        "var fromLeg=legs[idx];var nextLeg=legs[idx+1];"
        "fromCell.textContent=fromLeg!==null?(points[idx].name+'から約'+Math.round(fromLeg)+'分'):'-';"
        "nextCell.textContent=nextLeg!==null?(points[idx+2].name+'まで約'+Math.round(nextLeg)+'分'):'-';"
        "if(broken||fromLeg===null){broken=true;input.setAttribute('data-min','');input.value='';return;}"
        "cum+=fromLeg;"
        "input.setAttribute('data-min',cum);"
        "input.value=base!==null?minutesToTime(base+cum):'';"
        "});"
        "var lastLeg=legs[legs.length-1];"
        "var tripTotal=null;"
        "if(!broken&&lastLeg!==null){tripTotal=cum+lastLeg;}"
        "var arrCell=table.querySelector('.final-cell');"
        "if(arrCell){"
        "arrCell.setAttribute('data-min',tripTotal!==null?tripTotal:'');"
        "arrCell.textContent=(tripTotal!==null&&base!==null)?(minutesToTime(base+tripTotal)+' 頃 (目安)'):'-';"
        "}"
        "var arr=document.getElementById('arr-'+tripIdx);"
        "if(arr){"
        "arr.setAttribute('data-trip-minutes',tripTotal!==null?tripTotal:'');"
        "if(tripTotal!==null&&base!==null){arr.value=minutesToTime(base+tripTotal);}"
        "}"
        "}"
        "</script>"
    )

    parts.append("</body></html>")
    return "".join(parts)


if __name__ == "__main__":
    main()
