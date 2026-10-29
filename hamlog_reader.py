# -*- coding: utf-8 -*-
"""
Turbo HAMLOG のデータファイル（Hamlog.hdb, dBase III形式）を読み取り専用で読み込み、
交信記録（UTCにそろえたもの）を作るモジュール。

解読した格納ルール（作者の局の実データで確認, 2026年10月）
  ※ HAMLOGの公式な仕様ではありません。お使いのHAMLOGで正しく読めるかは、最初に
    「python hamlog_reader.py Hamlog.hdb」の自己点検で、件数・期間を確かめてください。
  CALLS : コールサイン先頭6文字（文字のまま）
  IGN   : 14バイト。先頭から左詰めの文字 = コールサインの7文字目以降（例 8J7AKI + TA → 8J7AKITA）
          末尾に右詰めの文字 = 「/」以降（例 IT9ACJ + I/BO → IT9ACJ/I/BO、9M6 + LA9DL → 9M6/LA9DL）
  DATE  : 4バイト = 世紀・年・月・日（例 14 16 0c 11 → 2022-12-17）
  TIME  : 2バイト = 時・分。分の最上位ビットが1ならUTC、0ならJST
  FREQ  : MHz（文字） / MODE : 文字 / HIS, MY : RST（FT8ではdB）
  RMK2  : $C=（送信したコールサイン）$、$D=（コールサイン）$、%GestOP:…% の記録
          ゲスト運用（他局の設備・コールサインでの運用）は、
          DXCCの交信済み判定には数えず、通知メールで別枠に表示する
  日付の明らかな誤り（1970年より前・未来・月日が0）は「日付不明」として扱い、交信自体は数える
元のファイルには一切書き込みません（一時フォルダにコピーしてから読みます）。
"""
import os, shutil, struct, tempfile, unicodedata, re
from datetime import datetime, timedelta, timezone

MY_CALLS = set()               # 自局のコールサイン。settings.ini の my_call から set_my_calls() で設定する
COUNT_GUEST = False            # Trueにすると、ゲスト運用の交信も交信済みに数える

def set_my_calls(calls):
    """自局のコールサイン（JA1ABC など）を設定する。read_hamlog() より前に呼ぶこと"""
    MY_CALLS.clear()
    MY_CALLS.update(c.strip().upper() for c in calls if c.strip())

BANDS = [  # (下限MHz, 上限MHz, バンド名)
    (1.8, 2.0, "160m"), (3.5, 3.805, "80m"), (5.3, 5.5, "60m"), (7.0, 7.3, "40m"),
    (10.1, 10.15, "30m"), (14.0, 14.35, "20m"), (18.068, 18.168, "17m"),
    (21.0, 21.45, "15m"), (24.89, 24.99, "12m"), (28.0, 29.7, "10m"),
    (50.0, 54.0, "6m"), (144.0, 148.0, "2m"), (430.0, 440.0, "70cm"),
]

# 「18」「24」のように整数だけで入力された周波数（MHz）を、バンドとして読む
WHOLE_MHZ = {1: "160m", 3: "80m", 7: "40m", 10: "30m", 14: "20m", 18: "17m", 21: "15m",
             24: "12m", 28: "10m", 29: "10m", 50: "6m", 144: "2m", 430: "70cm"}

def parse_freq(text):
    """'3,550' '3.5JA2' '18.14O' のような入力から周波数(MHz)とバンドを読む"""
    s = unicodedata.normalize("NFKC", text).strip().replace(",", ".")
    m = re.match(r"\d+(\.\d+)?", s)
    if not m:
        return 0.0, None
    f = float(m.group(0))
    for lo, hi, name in BANDS:
        if lo <= f <= hi:
            return f, name
    if m.group(1) is None and int(f) in WHOLE_MHZ:   # 小数点なしの整数だけの入力
        return f, WHOLE_MHZ[int(f)]
    t = m.group(0)
    if len(t) > 2 and t[0] == t[1]:   # 「33.571」→「3.571」のような先頭の数字の打ち重ね
        f2 = float(t[1:])
        for lo, hi, name in BANDS:
            if lo <= f2 <= hi:
                return f2, name
    return f, None

def _field_layout(f):
    h = f.read(32)
    if h[0] != 0x03:
        raise ValueError(f"dBase III形式ではありません（先頭バイト 0x{h[0]:02X}）")
    nrec = struct.unpack("<I", h[4:8])[0]
    hlen, rlen = struct.unpack("<HH", h[8:12])
    fields, pos = {}, 1
    while True:
        d = f.read(32)
        if d[0] == 0x0D:
            break
        name = d[:11].split(b"\x00")[0].decode("ascii")
        fields[name] = (pos, d[16])
        pos += d[16]
    return nrec, hlen, rlen, fields

def _text(raw):
    return raw.decode("cp932", "replace").strip()

def _operator_info(rmk2):
    """RMK2から、送信したコールサインとゲスト運用かどうかを読み取る"""
    s = unicodedata.normalize("NFKC", rmk2)   # 全角ＪＡ１ＡＢＣ → JA1ABC
    m = re.search(r"\$C=([^$]+)\$", s)
    station = m.group(1).strip().upper() if m else None
    g = re.search(r"%GestOP:([^%\s]*)", s, re.IGNORECASE)
    if station is None and g and len(g.group(1).split("/")[0]) >= 4:
        station = g.group(1).split("/")[0].upper()
    if station is None:
        m = re.search(r"\$D=([^$]+)\$", s)
        station = m.group(1).strip().upper() if m else None
    if station is None and g:                  # 備考の途中で切れた %GestOP:JP など
        station = (g.group(1).split("/")[0].upper() or "不明") + "（途中で切れた記録）"
    my_bases = {c.split("/")[0] for c in MY_CALLS}   # JA1ABC/1 のような移動運用も自局とみなす
    guest = bool(g) or (station is not None and station.split("/")[0] not in my_bases)
    return station, guest

def _callsign(calls, ign):
    """CALLS(6文字)とIGN(14バイト)から完全なコールサインを組み立てる"""
    base = _text(calls).upper()
    head = ign.decode("cp932", "replace")
    cont = head.split(" ")[0] if not head.startswith(" ") else ""     # 左詰め = 続きの文字
    suffix = head.rstrip().split(" ")[-1] if not head.endswith(" ") else ""   # 右詰め = /以降
    if cont and cont == head.strip() and suffix == cont:
        suffix = ""                                # 全体が1語なら続きの文字とみなす
    call = base + cont.strip().upper()
    if suffix.strip():
        call += "/" + suffix.strip().upper()
    return call

def read_hamlog(path):
    """HDBを読み、交信のリストと集計情報を返す"""
    if not MY_CALLS:
        raise ValueError("自局のコールサインが設定されていません（settings.ini の my_call）")
    tmp = os.path.join(tempfile.gettempdir(), "hamlog_reader_copy.hdb")
    shutil.copy2(path, tmp)
    qsos, warn = [], {"date_unknown": 0, "bad_band": 0, "deleted": 0}
    try:
        with open(tmp, "rb") as f:
            nrec, hlen, rlen, F = _field_layout(f)
            get = lambda rec, n: rec[F[n][0]:F[n][0] + F[n][1]]
            for i in range(nrec):
                f.seek(hlen + i * rlen)
                rec = f.read(rlen)
                if len(rec) < rlen:
                    break
                if rec[0:1] == b"*":
                    warn["deleted"] += 1
                    continue
                call = _callsign(get(rec, "CALLS"), get(rec, "IGN"))
                d, t = get(rec, "DATE"), get(rec, "TIME")
                is_utc = bool(t[1] & 0x80)
                utc = None
                try:
                    local = datetime(d[0] * 100 + d[1], d[2], d[3], t[0], t[1] & 0x7F)
                    if 1970 <= local.year and local <= datetime.now() + timedelta(days=1):
                        utc = (local if is_utc else local - timedelta(hours=9)).replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
                if utc is None:
                    warn["date_unknown"] += 1       # 交信自体は数え、日付は「不明」とする
                freq, band = parse_freq(_text(get(rec, "FREQ")))
                if band is None:
                    warn["bad_band"] += 1
                op_call, guest = _operator_info(_text(get(rec, "RMK2")))
                qsos.append({
                    "no": i + 1, "call": call,
                    "time_utc": utc,
                    "logged_in": "UTC" if is_utc else "JST",
                    "freq": freq, "band": band, "mode": _text(get(rec, "MODE")).upper(),
                    "his": _text(get(rec, "HIS")), "my": _text(get(rec, "MY")),
                    "op_call": op_call, "guest": guest, "source": "HAMLOG",
                })
    finally:
        os.remove(tmp)
    return qsos, warn

def is_own(q):
    """自局のコールサインでの交信か（COUNT_GUESTがFalseならゲスト運用は含めない）"""
    return COUNT_GUEST or not q["guest"]

def merge(primary, extra, tolerance_min=10):
    """2つの交信リストを合わせる。同じ局・バンド・モードで時刻が近いものは1件とみなす"""
    out = list(primary)
    index = {}
    for q in primary:
        if q["time_utc"] is None:
            continue
        index.setdefault((q["call"], q["band"], q["mode"]), []).append(q["time_utc"])
    tol = timedelta(minutes=tolerance_min)
    for q in extra:
        times = index.get((q["call"], q["band"], q["mode"]), [])
        if q["time_utc"] is None or not any(abs(q["time_utc"] - t) <= tol for t in times):
            out.append(q)
    return out

def worked_table(qsos, dxcc_of):
    """{DXCC番号: {バンド: [自局の交信, ...], "_guest": [ゲスト運用の交信, ...]}} を作る。
    dxcc_of はコールサイン→DXCC番号の関数（お手持ちのDXCC番号付けツールの判定関数を渡す）。
    ゲスト運用の交信は "_guest" に分けて入れ、交信済みの判定（バンドのキー）には含めない。"""
    table = {}
    key = lambda q: q["time_utc"] or datetime.min.replace(tzinfo=timezone.utc)
    for q in qsos:
        n = dxcc_of(q["call"])
        if n is None:
            continue
        if is_own(q):
            if q["band"] is None:
                continue
            table.setdefault(n, {}).setdefault(q["band"], []).append(q)
        else:
            table.setdefault(n, {}).setdefault("_guest", []).append(q)
    for bands in table.values():
        for lst in bands.values():
            lst.sort(key=key)
    return table

if __name__ == "__main__":
    # 自己点検: python hamlog_reader.py Hamlog.hdb（自局のコールサインは settings.ini から読む）
    # 交信の中身は表示せず、件数と点検結果だけを表示します。
    import sys
    from collections import Counter
    if len(sys.argv) < 2:
        raise SystemExit("使い方: python hamlog_reader.py Hamlog.hdbのパス")
    try:
        import dxcc_settings
        set_my_calls(dxcc_settings.my_calls(dxcc_settings.load()["station"]))
    except ImportError:
        pass
    if not MY_CALLS:
        raise SystemExit("settings.ini の my_call に自局のコールサインを書いてください")
    print("自局のコールサイン:", ", ".join(sorted(MY_CALLS)))
    qsos, warn = read_hamlog(sys.argv[1])
    own = [q for q in qsos if is_own(q)]
    print(f"読み込んだ交信: {len(qsos)}件（自局 {len(own)}件、ゲスト運用など {len(qsos) - len(own)}件）")
    if qsos:
        ts = sorted(q["time_utc"] for q in qsos if q["time_utc"])
        print(f"期間(UTC): {ts[0]:%Y-%m-%d} 〜 {ts[-1]:%Y-%m-%d}")
    print("記録の時刻:", dict(Counter(q["logged_in"] for q in qsos)))
    print("モード上位:", Counter(q["mode"] for q in qsos).most_common(6))
    print("バンド別:", Counter(q["band"] for q in qsos).most_common())
    print("送信したコールサイン:", Counter(q["op_call"] for q in qsos).most_common(6))
    print("長いコールサインの例:", [q["call"] for q in qsos if len(q["call"]) > 6][:12])
    print("点検:", warn)
    print("  date_unknown = 日付を「不明」とした件数 / bad_band = バンドを判定できなかった件数")
