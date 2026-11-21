@echo off
rem ===== DXクラスター通知 起動用ファイル（見本） =====
rem このファイルと同じフォルダの cluster_watch.py を起動します。
rem 通知の条件（モード・バンドなど）は settings.ini の [cluster] に書きます。
cd /d "%~dp0"

rem HAMLOGのデータファイルの場所（start_dxcc.bat と同じにします）
set HAMLOG=C:\Hamlog\HAMLOG.hdb
set HLOPT=
if exist "%HAMLOG%" set HLOPT=--hamlog "%HAMLOG%"

python cluster_watch.py --site home %HLOPT%
pause
