@echo off
rem ===== DXCC代行ワッチ 起動用ファイル（見本） =====
rem このファイルと同じフォルダにあるスクリプトを起動します。
rem 自局のコールサインやメールアドレスは settings.ini に書きます。
cd /d "%~dp0"

rem HAMLOGのデータファイルの場所（ご自分の環境に合わせて書き換えます）
set HAMLOG=C:\Hamlog\HAMLOG.hdb
set HLOPT=
if exist "%HAMLOG%" set HLOPT=--hamlog "%HAMLOG%"

rem --group と --port は、WSJT-Xの「設定」-「Reporting」のUDPサーバーと同じ値にします。
rem 2台目のWSJT-Xを同じPCで使う場合は、239.255.0.2 / 2238 のように分けます。
python dxcc_notify.py --site home --group 239.255.0.1 --port 2237 %HLOPT%
pause
