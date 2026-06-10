#!/usr/bin/env python3
"""犬の幼稚園 送迎ルート自動作成ツール

Google カレンダーの予定から「🚗」マーク付きの園児を抽出し、
クレート(車載数)制限を考慮した送迎ルート表(HTML)を作成する。
各便にはGoogleマップで経路を開けるリンクを付与する。

カレンダー記法:
  タイトル: 🚗 犬の名前 [タグ]
    タグ省略時 = 往復
    [往復] / [迎えのみ] / [送りのみ] / [迎えのみ 9:00] のように時刻を併記可
  場所(location)欄: 自宅住所
"""

import argparse
import datetime
import os
import re
import sys

import yaml

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

CAR_MARK = "🚗"
TAG_PATTERN = re.compile(r"[\[(]([^\])]*)[\])]")
TIME_PATTERN = re.compile(r"(\d{1,2}):(\d{2})")


class Stop:
    def __init__(self, name, address, requested_time=None):
        self.name = name
        self.address = address
        self.requested_time = requested_time

    def __repr__(self):
        t = self.requested_time.strftime("%H:%M") if self.requested_time else "-"
        return f"{self.name} ({self.address}) [指定: {t}]"


DEFAULT_CONFIG = {
    "base_address": "大阪府大阪市北区天神橋6丁目 bonnepuppey天満店",
    "crate_capacity": 4,
    "morning_start_time": "08:30",
    "evening_start_time": "17:00",
}


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def sample_events(target_date):
    """--demo 用のサンプル予定 (Google Calendar API のレスポンス形式を模したもの)"""
    def dt(hour, minute):
        return datetime.datetime.combine(target_date, datetime.time(hour, minute)).isoformat()

    return [
        {"summary": "🚗 ポチ", "location": "大阪府大阪市北区天神橋1丁目1-1", "start": {"dateTime": dt(8, 30)}},
        {"summary": "🚗 タロウ [迎えのみ 8:50]", "location": "大阪府大阪市北区西天満2-2-2", "start": {"dateTime": dt(8, 50)}},
        {"summary": "🚗 ハナ [往復]", "location": "大阪府大阪市北区中崎西3-3-3", "start": {"dateTime": dt(9, 0)}},
        {"summary": "🚗 モモ [送りのみ 17:30]", "location": "大阪府大阪市北区天神橋4-4-4", "start": {"dateTime": dt(17, 30)}},
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
    has_pickup_tag = "迎え" in tag_text
    has_dropoff_tag = "送り" in tag_text
    is_roundtrip = "往復" in tag_text or (not has_pickup_tag and not has_dropoff_tag)

    tag_time_match = TIME_PATTERN.search(tag_text)
    tag_time = None
    if tag_time_match:
        tag_time = datetime.time(int(tag_time_match.group(1)), int(tag_time_match.group(2)))

    pickup_stop = None
    dropoff_stop = None

    if is_roundtrip or has_pickup_tag:
        t = tag_time if has_pickup_tag else event_time
        pickup_stop = Stop(name, location, t)
    if is_roundtrip or has_dropoff_tag:
        t = tag_time if has_dropoff_tag else event_time
        dropoff_stop = Stop(name, location, t)

    return pickup_stop, dropoff_stop


def split_into_trips(stops, capacity):
    """指定時刻があるものを優先しつつ、capacity頭ずつトリップに分割する"""
    sorted_stops = sorted(
        stops,
        key=lambda s: s.requested_time or datetime.time(23, 59),
    )
    return [sorted_stops[i : i + capacity] for i in range(0, len(sorted_stops), capacity)]


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

    base_address, capacity, trips_data = build_route(target_date, config, events)

    html = render_html(target_date, base_address, capacity, trips_data)
    output_path = args.output or f"送迎ルート_{target_date.isoformat()}.html"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"作成しました: {os.path.abspath(output_path)}")

    if not args.no_open:
        import webbrowser

        webbrowser.open(f"file://{os.path.abspath(output_path)}")


def build_route(target_date, config, events):
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
    capacity = config["crate_capacity"]

    morning_start = datetime.datetime.combine(
        target_date, datetime.time.fromisoformat(config["morning_start_time"])
    )
    evening_start = datetime.datetime.combine(
        target_date, datetime.time.fromisoformat(config["evening_start_time"])
    )

    trips_data = []
    for label, stops, departure in (
        ("朝のお迎え便", pickup_stops, morning_start),
        ("夕方の送り便", dropoff_stops, evening_start),
    ):
        if not stops:
            trips_data.append({"label": label, "rows": None})
            continue

        trips = split_into_trips(stops, capacity)
        for i, trip_stops in enumerate(trips, start=1):
            trip = {
                "label": label,
                "trip_no": i,
                "loaded": len(trip_stops),
                "departure": departure.strftime("%H:%M"),
                "rows": [],
                "maps_url": build_maps_url(base_address, [s.address for s in trip_stops]),
            }
            for stop in trip_stops:
                t = stop.requested_time.strftime("%H:%M") if stop.requested_time else "-"
                trip["rows"].append({"name": stop.name, "address": stop.address, "time": t})
            trips_data.append(trip)

    return base_address, capacity, trips_data


def render_html(target_date, base_address, capacity, trips_data):
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
    parts.append(f"<p class='meta'>拠点: {base_address}<br>クレート数(1便あたり最大): {capacity}</p>")

    for trip in trips_data:
        if trip["rows"] is None:
            parts.append(f"<h2>{trip['label']}</h2><p>対象なし</p>")
            continue

        parts.append(f"<h2>{trip['label']} 第{trip['trip_no']}便 (積載 {trip['loaded']}/{capacity})</h2>")
        parts.append("<table><tr><th>順番</th><th>名前</th><th>住所</th><th>希望時刻</th></tr>")
        parts.append(f"<tr><td>出発</td><td colspan='2'>{base_address}</td><td>{trip['departure']}</td></tr>")
        for i, row in enumerate(trip["rows"], start=1):
            parts.append(
                f"<tr><td>{i}</td><td>{row['name']}</td><td>{row['address']}</td><td>{row['time']}</td></tr>"
            )
        parts.append(f"<tr><td>帰着</td><td colspan='3'>{base_address}</td></tr>")
        parts.append("</table>")
        parts.append(f"<a class='maps-link' href='{trip['maps_url']}' target='_blank'>Googleマップでルートを開く</a>")

    parts.append("</body></html>")
    return "".join(parts)


if __name__ == "__main__":
    main()
