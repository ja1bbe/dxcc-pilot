# -*- coding: utf-8 -*-
"""
settings.ini（自局の設定）を読み込むモジュール。

自局のコールサインやメールアドレスなど、局ごとに違う値はすべて settings.ini に書きます。
スクリプト本体（dxcc_notify.py など）を書き換える必要はありません。
settings.ini はスクリプトと同じフォルダに置き、メモ帳で編集して「UTF-8」で保存してください。
コマンドラインで同じ項目を指定した場合は、そちらが優先されます。
"""
import configparser
import os

BASE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(BASE, "settings.ini")


def load(path=PATH):
    """{'station': {...}, 'sites': {...}} を返す。ファイルが無ければ空の辞書"""
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str                      # [sites] のIDの大文字小文字を保つ
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:   # メモ帳のBOM付きUTF-8にも対応
            cp.read_file(f)
    station = {k: v.strip() for k, v in cp["station"].items()} if cp.has_section("station") else {}
    sites = {k.lower(): v.strip() for k, v in cp["sites"].items()} if cp.has_section("sites") else {}
    return {"station": station, "sites": sites}


def my_calls(station):
    """my_call = JA1ABC, JA1ABC/1 のようなカンマ区切りを、大文字の集合にする"""
    return {c.strip().upper() for c in (station.get("my_call") or "").split(",") if c.strip()}


def main_call(station):
    """表示や LoTW の STATION_CALLSIGN に使う代表のコールサイン（my_call の最初）"""
    calls = [c.strip().upper() for c in (station.get("my_call") or "").split(",") if c.strip()]
    return calls[0] if calls else ""
