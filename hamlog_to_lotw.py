# -*- coding: utf-8 -*-
"""
HAMLOG の Hamlog.hdb から、LoTW に提出するための ADIF ファイルを作る（読み取り専用）

使い方（DXCCフォルダで）:
    python hamlog_to_lotw.py "C:\\...\\HAMLOG.hdb"
自局のコールサインと証明書の開始日は settings.ini（my_call, lotw_cert_start）から読みます。
作られるもの（同じフォルダの lotw フォルダの中）:
    lotw_test.adi   最近の20件だけ（まず試しに提出して、LoTWで受け付けられるか確かめる用）
    lotw_all.adi    提出できる全件
    lotw_excluded.csv  提出から外した交信と、その理由（確認用）

方針:
  ・settings.ini の my_call に書いたコールサインの交信だけ（ゲスト運用は含めない）
  ・時刻はすべて UTC（HAMLOG の JST の記録は 9 時間引く）
  ・日付が不明・バンドが不明・LoTWの証明書の開始日（lotw_cert_start）より前の交信は外す
  ・モードは LoTW（ADIF）の表記にそろえる。対応がわからないモードは外して一覧に残す
  ・STATION_CALLSIGN には my_call の最初のコールサインを入れる。my_call に複数のコールを書いた場合、
    LoTW の証明書はコールサインごとに必要なので、提出前に lotw_all.adi を確認すること
LoTW に一度提出した交信は削除できないため、必ず lotw_test.adi で試してから lotw_all.adi を提出すること。
"""
import csv, os, sys
from datetime import datetime, timezone
import hamlog_reader as h
import dxcc_settings

TEST_COUNT = 20
# HAMLOG のモード → ADIF の (MODE, SUBMODE)。ここにないモードは外して一覧に残す
MODE_MAP = {
    "SSB": ("SSB", ""), "LSB": ("SSB", "LSB"), "USB": ("SSB", "USB"),
    "CW": ("CW", ""), "AM": ("AM", ""), "FM": ("FM", ""),
    "FT8": ("FT8", ""), "FT4": ("MFSK", "FT4"), "JT65": ("JT65", ""), "JT9": ("JT9", ""),
    "RTTY": ("RTTY", ""), "PSK": ("PSK", "PSK31"), "PSK31": ("PSK", "PSK31"),
    "SSTV": ("SSTV", ""), "FREE": ("DIGITALVOICE", "FREEDV"), "FREEDV": ("DIGITALVOICE", "FREEDV"),
}
# バンドの端（MHz）。周波数がこの範囲外（「18」のような概数の入力など）のときは FREQ を付けない
EDGES = {"160m": (1.8, 2.0), "80m": (3.5, 4.0), "60m": (5.06, 5.45), "40m": (7.0, 7.3),
         "30m": (10.1, 10.15), "20m": (14.0, 14.35), "17m": (18.068, 18.168), "15m": (21.0, 21.45),
         "12m": (24.89, 24.99), "10m": (28.0, 29.7), "6m": (50.0, 54.0), "2m": (144.0, 148.0),
         "70cm": (420.0, 450.0)}


def f(tag, val):
    val = str(val)
    return f"<{tag}:{len(val)}>{val}"


def main():
    if len(sys.argv) < 2:
        raise SystemExit("使い方: python hamlog_to_lotw.py Hamlog.hdbのパス")
    hdb = sys.argv[1]
    st = dxcc_settings.load()["station"]
    h.set_my_calls(dxcc_settings.my_calls(st))
    station_call = dxcc_settings.main_call(st)
    if not station_call:
        raise SystemExit("settings.ini の my_call に自局のコールサインを書いてください")
    cert_start = None                           # 証明書の「QSOs not before」
    if st.get("lotw_cert_start"):
        try:
            cert_start = datetime.strptime(st["lotw_cert_start"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise SystemExit(f"settings.ini の lotw_cert_start は 2003-09-06 の形で書いてください: {st['lotw_cert_start']}")
    print(f"自局のコールサイン: {station_call}（判定に使うコール: {', '.join(sorted(h.MY_CALLS))}）")
    base = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(base, "lotw")
    os.makedirs(out, exist_ok=True)
    qsos, warn = h.read_hamlog(hdb)
    ok, ng = [], []
    for q in qsos:
        reason = ""
        if not h.is_own(q):
            reason = f"ゲスト運用（{q.get('op_call') or ''}）"
        elif q["time_utc"] is None:
            reason = "日付が不明"
        elif cert_start and q["time_utc"] < cert_start:
            reason = "証明書の開始日より前"
        elif not q["band"]:
            reason = "バンドが不明"
        elif q["mode"] not in MODE_MAP:
            reason = f"モード「{q['mode']}」の対応が未設定"
        (ng if reason else ok).append((q, reason))
    ok = [q for q, _ in ok]
    ok.sort(key=lambda q: q["time_utc"])

    def rec(q):
        mode, sub = MODE_MAP[q["mode"]]
        parts = [f("CALL", q["call"]), f("QSO_DATE", q["time_utc"].strftime("%Y%m%d")),
                 f("TIME_ON", q["time_utc"].strftime("%H%M")), f("BAND", q["band"]), f("MODE", mode)]
        if sub:
            parts.append(f("SUBMODE", sub))
        lo, hi = EDGES.get(q["band"], (0, 0))
        if lo <= q["freq"] <= hi:
            parts.append(f("FREQ", f"{q['freq']:.4f}".rstrip("0").rstrip(".")))
        parts.append(f("STATION_CALLSIGN", station_call))
        return " ".join(parts) + " <EOR>\n"

    head = (f"LoTW upload file made from HAMLOG by hamlog_to_lotw.py ({datetime.now():%Y-%m-%d %H:%M})\n"
            + f("ADIF_VER", "3.1.4") + " " + f("PROGRAMID", "hamlog_to_lotw") + " <EOH>\n")
    for name, lst in (("lotw_test.adi", ok[-TEST_COUNT:]), ("lotw_all.adi", ok)):
        with open(os.path.join(out, name), "w", encoding="ascii", errors="replace") as fp:
            fp.write(head + "".join(rec(q) for q in lst))
    with open(os.path.join(out, "lotw_excluded.csv"), "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["HAMLOGの番号", "コールサイン", "日時(UTC)", "バンド", "モード", "外した理由"])
        for q, r in ng:
            w.writerow([q["no"], q["call"], q["time_utc"].strftime("%Y-%m-%d %H:%M") if q["time_utc"] else "",
                        q["band"] or "", q["mode"], r])

    from collections import Counter
    print(f"提出できる交信: {len(ok)}件（{ok[0]['time_utc']:%Y-%m-%d} 〜 {ok[-1]['time_utc']:%Y-%m-%d}）" if ok else "提出できる交信: 0件")
    print("  モード別:", dict(Counter(q["mode"] for q in ok)))
    print(f"提出から外した交信: {len(ng)}件")
    for r, c in Counter(r.split("（")[0] if r.startswith("ゲスト") else r for _, r in ng).most_common():
        print(f"  {r}: {c}件")
    print(f"作成しました（{out}）: lotw_test.adi（最近の{min(TEST_COUNT, len(ok))}件） / lotw_all.adi（全件） / lotw_excluded.csv")


if __name__ == "__main__":
    main()
