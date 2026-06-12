@echo off
set TASK_NAME=送迎ルート作成
set SCRIPT_PATH=%~dp0送迎ルートを作る.bat

schtasks /create /tn "%TASK_NAME%" /tr "\"%SCRIPT_PATH%\"" /sc daily /st 06:00 /f

echo.
echo 設定が完了しました。毎日 6:00 に自動でルート表が作成されます。
echo 時間を変えたい場合は、この画面を閉じてから「タスクスケジューラ」で
echo 「送迎ルート作成」というタスクの時刻を変更してください。
echo.
pause
