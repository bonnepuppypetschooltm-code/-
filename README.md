# 犬の幼稚園 送迎ルート自動化ツール (bonnepuppey天満店)

Google カレンダーの予定から送迎対象の園児を抽出し、Google Maps API で
クレート(車載数)制限を考慮した最適な送迎ルートを自動計算します。

## カレンダーの記法ルール

送迎が必要な園児の予定タイトルの先頭に `🚗` を付けてください。

```
🚗 ポチ
🚗 ポチ [往復]
🚗 ポチ [迎えのみ]
🚗 ポチ [送りのみ]
🚗 ポチ [迎えのみ 9:00]
```

- タグ省略 / `[往復]` : 朝の「お迎え便」と夕方の「送り便」の両方の対象になります
- `[迎えのみ]` : 朝の「お迎え便」のみ対象
- `[送りのみ]` : 夕方の「送り便」のみ対象
- タグ内に `9:00` のように時刻を書くと、その時刻を希望時刻として優先的に並び替えます
  (時刻指定がない場合は予定の開始時刻が使われます)

「場所」欄には、その子の**自宅住所**を入力してください。

## まずは試してみる(API設定不要)

サンプルデータを使って、出力イメージをすぐに確認できます。
Google CalendarやMaps APIの設定は一切不要です。

```bash
pip install pyyaml
python3 route_planner.py --demo
```

「やりたいこと」のイメージと合っているか確認してから、本番セットアップ(下記)に進んでください。

## セットアップ

1. 依存パッケージをインストール

   ```bash
   pip install -r requirements.txt
   ```

2. Google Calendar API の認証情報を取得

   - [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成
   - 「Google Calendar API」を有効化
   - 「OAuth クライアント ID」(デスクトップアプリ)を作成し、`credentials.json` としてこのディレクトリに保存

3. Google Maps Platform の API キーを取得

   - 「Directions API」を有効化し、APIキーを発行
   - 環境変数として設定

     ```bash
     export GOOGLE_MAPS_API_KEY="あなたのAPIキー"
     ```

4. `config.example.yaml` を `config.yaml` としてコピーし、拠点住所・クレート数・出発時刻を編集

   ```bash
   cp config.example.yaml config.yaml
   ```

## 実行方法

```bash
python route_planner.py
```

特定の日付を指定する場合:

```bash
python route_planner.py --date 2026-06-15
```

初回実行時はブラウザが開き、Google アカウントでの認証を求められます。
認証後は `token.json` に保存され、以後は自動で再利用されます。

## 出力例

```
=== 2026-06-10 送迎ルート ===
拠点: 大阪府大阪市北区天神橋6丁目 bonnepuppey天満店
クレート数(1便あたり最大): 4

--- 朝のお迎え便 第1便 (積載 4/4) ---
08:30 出発: bonnepuppey天満店
  -> 08:42 ポチ 様宅 (大阪府...) [移動 3.2km / 12分]
  -> 08:55 タロウ 様宅 (大阪府...) [移動 4.1km / 13分]
  ...
  -> 09:10 bonnepuppey天満店 帰着 [移動 5.0km / 15分]
```

## 今後の拡張案

- 複数台の車・複数ドライバーへの自動振り分け
- 渋滞状況を考慮したリアルタイム再計算
- LINE / Slack への自動通知
