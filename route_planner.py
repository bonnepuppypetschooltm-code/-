#!/usr/bin/env python3
"""犬の幼稚園 送迎ルート自動作成ツール

Google カレンダーの予定から「🚗」マーク付きの園児を抽出し、
Google Maps API でクレート(車載数)制限を考慮した送迎ルートを最適化して表示する。

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


def optimize_trip(gmaps, base_address, stops, departure_time):
    if not stops:
        return None

    waypoints = [s.address for s in stops]
    directions = gmaps.directions(
        origin=base_address,
        destination=base_address,
        waypoints=waypoints,
        optimize_waypoints=True,
        mode="driving",
        departure_time=departure_time,
    )
    if not directions:
        return None

    route = directions[0]
    order = route["waypoint_order"]
    ordered_stops = [stops[i] for i in order]
    legs = route["legs"]
    return ordered_stops, legs


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
    args = parser.parse_args()

    target_date = (
        datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
    )

    if args.demo:
        config = DEFAULT_CONFIG
        if os.path.exists(args.config):
            config = {**DEFAULT_CONFIG, **load_config(args.config)}
        gmaps = None
        events = sample_events(target_date)
        print("*** デモモード: サンプルデータで動作確認中 (実際のカレンダー/Mapsは使用しません) ***\n")
    else:
        config = load_config(args.config)

        import googlemaps

        api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
        if not api_key:
            print("エラー: 環境変数 GOOGLE_MAPS_API_KEY を設定してください。", file=sys.stderr)
            sys.exit(1)
        gmaps = googlemaps.Client(key=api_key)

        service = get_calendar_service(config)
        events = fetch_events(service, config["calendar_id"], target_date)

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

    print(f"=== {target_date.isoformat()} 送迎ルート ===")
    print(f"拠点: {base_address}")
    print(f"クレート数(1便あたり最大): {capacity}")

    morning_start = datetime.datetime.combine(
        target_date, datetime.time.fromisoformat(config["morning_start_time"])
    )
    evening_start = datetime.datetime.combine(
        target_date, datetime.time.fromisoformat(config["evening_start_time"])
    )

    for label, stops, departure in (
        ("朝のお迎え便", pickup_stops, morning_start),
        ("夕方の送り便", dropoff_stops, evening_start),
    ):
        if not stops:
            print(f"\n--- {label} ---\n対象なし")
            continue

        trips = split_into_trips(stops, capacity)
        for i, trip_stops in enumerate(trips, start=1):
            print(f"\n--- {label} 第{i}便 (積載 {len(trip_stops)}/{capacity}) ---")

            if gmaps is None:
                # デモモード: 最適化はせず、希望時刻順に表示するのみ
                print(f"{departure.strftime('%H:%M')} 出発: {base_address}")
                for stop in trip_stops:
                    print(f"  -> {stop.name} 様宅 ({stop.address})")
                print(f"  -> {base_address} 帰着")
                print("  ※ 訪問順・移動時間はGoogle Maps API設定後に自動最適化されます")
                continue

            result = optimize_trip(gmaps, base_address, trip_stops, departure)
            if not result:
                print("ルート計算に失敗しました")
                continue
            ordered_stops, legs = result

            current_time = departure
            print(f"{current_time.strftime('%H:%M')} 出発: bonnepuppey天満店")
            for stop, leg in zip(ordered_stops, legs[:-1]):
                current_time += datetime.timedelta(seconds=leg["duration"]["value"])
                print(
                    f"  -> {current_time.strftime('%H:%M')} {stop.name} 様宅 "
                    f"({stop.address}) [移動 {leg['distance']['text']} / {leg['duration']['text']}]"
                )
            return_leg = legs[-1]
            current_time += datetime.timedelta(seconds=return_leg["duration"]["value"])
            print(
                f"  -> {current_time.strftime('%H:%M')} bonnepuppey天満店 帰着 "
                f"[移動 {return_leg['distance']['text']} / {return_leg['duration']['text']}]"
            )


if __name__ == "__main__":
    main()
