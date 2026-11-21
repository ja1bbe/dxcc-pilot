# -*- coding: utf-8 -*-
"""
cluster_watch.py  -  DXクラスター通知（JA局のスポットから未交信を知らせる）

DXCC代行ワッチ（dxcc-pilot）の追加機能です。日本の局がDXクラスターに上げたスポット
（DXSCAPE の JA25 と、DX Summit のうちスポッターが日本の局のもの）を定期的に取得し、
自局が未交信のエンティティが見つかったら、スマートフォンにメールで知らせます。

  - モード（FT8・FT4・CW・SSB）とバンドを settings.ini の [cluster] で選べます。
  - 「全バンド初」だけ、または「バンドニュー」も、を選べます。
  - 交信済みの判定は dxcc_notify.py と同じです（HAMLOG の自局の交信と logs\\*.adi。ゲスト運用は数えない）。
  - スポットにはモードの欄がないため、モードはコメントと周波数から推定します。

起動:  python cluster_watch.py --site home --hamlog "C:\\Hamlog\\HAMLOG.hdb"
試験:  python cluster_watch.py --hamlog "…" --dry-run     （メールを送らず画面に表示）
       python cluster_watch.py --test                     （テストメールを1通送って終了）
"""
import argparse, configparser, glob, html, json, os, re, subprocess, time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import dxcc_notify as dn          # 部品として読み込むだけ（WSJT-Xの監視は始まらない）
import dxcc_settings
import hamlog_reader as hr

CLUSTER_VERSION = "0.1.0"
URL_DXSCAPE = "http://www.dxscape.com/ja25.html"
URL_DXSUMMIT = "https://www.dxsummit.fi/api/v1/spots"
UA = f"dxcc-pilot-cluster_watch/{CLUSTER_VERSION} (amateur radio, personal, non-commercial)"
SOURCE_LABEL = {"DXSCAPE": "DXSCAPE JA25（日本からのスポット）",
                "DXSummit": "DX Summit（スポッターが日本の局のもの）"}
ALL_BANDS = ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]
ALL_MODES = ["FT8", "FT4", "CW", "SSB"]

# ---------------------------------------------------------------- モードの推定
# デジタルモードの標準周波数（kHz, ダイヤル）。ここから +0〜+3 kHz をそのモードとみなす
DIGITAL_DIALS = {
    "FT8": [1840, 1908, 3531, 3573, 7041, 7074, 10136, 14074, 18100, 21074, 24915, 28074, 50313, 50323],
    "FT4": [3575.5, 7047.5, 10140, 14080, 18104, 21140, 24919, 28180, 50318],
}
# CW と SSB のおおよその区分（kHz）。国や地域で異なるので、あくまで目安
CW_SEGMENTS = [(1800, 1838), (3500, 3570), (7000, 7040), (10100, 10130), (14000, 14070),
               (18068, 18095), (21000, 21070), (24890, 24915), (28000, 28070), (50000, 50100)]
SSB_SEGMENTS = [(1843, 2000), (3600, 4000), (7050, 7300), (14100, 14350), (18110, 18168),
                (21150, 21450), (24930, 24990), (28300, 29700), (50100, 50300)]
KEYWORDS = [("FT4", r"\bFT4\b"), ("FT8", r"\bFT8\b"), ("RTTY", r"\bRTTY\b"),
            ("CW", r"\bCW\b"), ("SSB", r"\b(SSB|USB|LSB)\b")]


def guess_mode(freq_khz, comment):
    """(モード, 根拠) を返す。根拠は「コメント」か「周波数」。わからなければ (None, None)"""
    c = (comment or "").upper()
    for mode, pat in KEYWORDS:
        if re.search(pat, c):
            return mode, "コメント"
    for mode, dials in DIGITAL_DIALS.items():
        if any(0 <= freq_khz - d <= 3.0 for d in dials):
            return mode, "周波数"
    if any(lo <= freq_khz <= hi for lo, hi in CW_SEGMENTS):
        return "CW", "周波数"
    if any(lo <= freq_khz <= hi for lo, hi in SSB_SEGMENTS):
        return "SSB", "周波数"
    return None, None


# ---------------------------------------------------------------- スポットの取得
def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    for enc in ("utf-8", "shift_jis"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1", errors="replace")


# 例: "6W/I2YSB  0851Z  14240.0 5-10up   JA3USA-9"（コメントは空のこともある）
LINE_RE = re.compile(r"^(\S+)\s+(\d{4})Z\s+(\d+\.\d)\s*(.*?)\s+(\S+)\s*$")


def parse_dxscape(page, now_utc):
    text = html.unescape(re.sub(r"<[^>]+>", "", page))
    spots = []
    for line in text.splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        call, hhmm, freq, comment, spotter = m.groups()
        t = now_utc.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0, microsecond=0)
        if t > now_utc + timedelta(minutes=5):        # 日付をまたいだスポット
            t -= timedelta(days=1)
        spots.append(dict(call=call.upper(), time=t, freq=float(freq),
                          comment=comment.strip(), spotter=spotter, source="DXSCAPE"))
    return spots


def parse_dxsummit(text, cty):
    """スポッターが日本（Big CTY で Japan）の局のスポットだけを取り出す。(一覧, idの範囲) を返す"""
    spots, ids = [], []
    for it in json.loads(text):
        if isinstance(it.get("id"), int):
            ids.append(it["id"])
        de, dx = (it.get("de_call") or "").upper(), (it.get("dx_call") or "").upper()
        if not de or not dx or it.get("frequency") is None:
            continue
        if cty.entity(de.split("-")[0]) != "Japan":   # JA1ABC-@ / -# → JA1ABC
            continue
        try:
            t = datetime.strptime(it["time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            f = float(it["frequency"])
        except (KeyError, TypeError, ValueError):
            continue
        spots.append(dict(call=dx, time=t, freq=f, comment=(it.get("info") or "").strip(),
                          spotter=de, source="DXSummit"))
    return spots, ((min(ids), max(ids)) if ids else None)


# ---------------------------------------------------------------- 交信済み（dxcc_notify.py と同じ判定）
def load_worked(a, cty, my_calls):
    """{(エンティティ, バンド)} を返す。HAMLOG を指定していて読めないときは None
    （HAMLOGを読めないと、ADIFのうちゲスト運用の交信を見分けられないため）"""
    hl = []
    if a.hamlog:
        if not os.path.exists(a.hamlog):
            print(f"[HAMLOG] ファイルがありません: {a.hamlog}")
            return None
        try:
            hl, _ = hr.read_hamlog(a.hamlog)
        except Exception as e:
            print(time.strftime("%H:%M:%S"), f"[HAMLOG] 読み込めませんでした: {e}")
            return None
    skip = None if hr.COUNT_GUEST else dn.guest_matcher(hl, {c.split("/")[0] for c in my_calls})
    pairs = set()
    for p in log_files(a):
        if os.path.exists(p):
            pairs |= dn.load_worked(p, cty, False, skip)
    for q in hl:
        if hr.is_own(q) and q["band"]:
            ent = cty.entity(q["call"])
            if ent:
                pairs.add((ent, q["band"]))
    return pairs


def log_files(a):
    return a.adif if a.adif else sorted(glob.glob(os.path.join(a.base, "logs", "*.adi")))


def watched_mtimes(a):
    files = log_files(a) + ([a.hamlog] if a.hamlog else [])
    return tuple((p, os.path.getmtime(p)) for p in files if os.path.exists(p))


# ---------------------------------------------------------------- その他
def wsjtx_running():
    if os.name != "nt":
        return True
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq wsjtx.exe"], capture_output=True,
                             text=True, creationflags=0x08000000).stdout
        return "wsjtx.exe" in out.lower()
    except Exception:
        return True


def acquire_lock(path):
    if os.name != "nt":
        return object()
    import msvcrt
    f = open(path, "a+")
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return f
    except OSError:
        return None


def split_list(s):
    return [x.strip() for x in re.split(r"[,\s]+", s or "") if x.strip()]


def make_mail(s, kind, ent, band, mode, basis, dxcc_no, site, sender, to):
    mark = dn.SUBJECT_MARK["ATNO" if kind == "ATNO" else "BANDNEW"]
    title = "全バンド初" if kind == "ATNO" else "バンドニュー"
    subject = f"[クラスター] {mark} {band} {mode} {s['call']} ({ent})"
    jst = (s["time"] + timedelta(hours=9)).strftime("%H:%M")
    rows = [("コール", s["call"]),
            ("エンティティ", f"{ent} DXCC #{dxcc_no}" if dxcc_no else ent),
            ("周波数", f"{s['freq']:.1f} kHz（{band}）"),
            ("モード", f"{mode}（{basis}から推定）"),
            ("スポット時刻", f"{s['time']:%H:%M} UTC（{jst} JST）"),
            ("コメント", s["comment"] or "-"),
            ("スポッター", s["spotter"]),
            ("出典", SOURCE_LABEL[s["source"]]),
            ("通知したPC", site)]
    note = "日本の局によるスポットです。誤ったコールサインや、すでにQRTしている場合があります。"
    text = "\n".join(f"{k}: {v}" for k, v in rows) + "\n\n" + note
    color = "#c0392b" if kind == "ATNO" else "#27864a"
    trs = "".join(f'<tr><td style="padding:3px 10px 3px 0;color:#666">{html.escape(k)}</td>'
                  f'<td style="padding:3px 0"><b>{html.escape(str(v))}</b></td></tr>' for k, v in rows)
    body = (f'<html><body style="font-family:sans-serif">'
            f'<div style="background:{color};color:#fff;padding:8px 12px;font-weight:bold">'
            f'{title}（DXクラスター） {html.escape(band)} {html.escape(mode)} {html.escape(s["call"])}</div>'
            f'<table style="border-collapse:collapse;margin:8px 0">{trs}</table>'
            f'<p style="font-size:small;color:#666">{html.escape(note)}</p></body></html>')
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    return subject, text, msg


# ---------------------------------------------------------------- メイン
def main():
    base = os.path.dirname(os.path.abspath(__file__))
    conf = dxcc_settings.load()
    st = conf["station"]
    cp = configparser.ConfigParser(interpolation=None)
    if os.path.exists(dxcc_settings.PATH):
        with open(dxcc_settings.PATH, encoding="utf-8-sig") as f:
            cp.read_file(f)
    cl = {k: v.strip() for k, v in cp["cluster"].items()} if cp.has_section("cluster") else {}

    ap = argparse.ArgumentParser(description="DXクラスター通知（dxcc-pilot の追加機能）")
    ap.add_argument("--site", default="home", help="PCの名前のID（settings.ini の [sites]）")
    ap.add_argument("--hamlog", help="HAMLOG のデータファイル（Hamlog.hdb）")
    ap.add_argument("--adif", nargs="*", help="交信ログのADIF（既定: logs\\*.adi）")
    ap.add_argument("--cty", help="Big CTY（既定: common\\cty_latest.dat）")
    ap.add_argument("--cty-csv", help="DXCC番号の表示用（既定: common\\cty_latest.csv）")
    ap.add_argument("--modes", default=cl.get("modes", "FT8, CW, SSB"))
    ap.add_argument("--bands", default=cl.get("bands", "all"))
    ap.add_argument("--notify", default=cl.get("notify", "atno"), help="atno または atno, bandnew")
    ap.add_argument("--sources", default=cl.get("sources", "dxscape, dxsummit"))
    ap.add_argument("--interval", type=int, default=int(cl.get("interval_sec", 120)))
    ap.add_argument("--max-age", type=int, default=int(cl.get("max_age_min", 30)))
    ap.add_argument("--renotify", type=float, default=float(cl.get("renotify_hours", 6)))
    ap.add_argument("--max-per-hour", type=int, default=int(cl.get("max_per_hour", 10)))
    ap.add_argument("--pass-file", help="アプリパスワードのファイル（ふつうは環境変数 DXCC_MAIL_PASS）")
    ap.add_argument("--stop-with-wsjtx", action="store_true", help="WSJT-Xが終了したら、このスクリプトも終了する")
    ap.add_argument("--dry-run", action="store_true", help="メールを送らず画面に表示する")
    ap.add_argument("--test", action="store_true", help="テストメールを1通送って終了する")
    a = ap.parse_args()
    a.base = base
    a.interval = max(60, a.interval)

    print(f"cluster_watch 版 {CLUSTER_VERSION}（dxcc-pilot の追加機能）")
    my_calls = dxcc_settings.my_calls(st)
    if not my_calls or any("＜" in c for c in my_calls):
        raise SystemExit("自局のコールサインが設定されていません（settings.ini の my_call）")
    hr.set_my_calls(my_calls)
    mail_from, mail_to = st.get("mail_from", ""), st.get("mail_to", "")
    if not mail_from or not mail_to or "＜" in mail_from + mail_to:
        raise SystemExit("送信用・受信用のメールアドレスが設定されていません（settings.ini の mail_from, mail_to）")
    site = conf["sites"].get(a.site.lower(), a.site)

    modes = {m.upper() for m in split_list(a.modes)}
    bands = set(ALL_BANDS) if a.bands.strip().lower() in ("all", "全バンド", "") else \
        {b.lower() for b in split_list(a.bands)}
    kinds = {k.lower() for k in split_list(a.notify)}
    sources = {s.lower() for s in split_list(a.sources)}
    bad = (modes - set(ALL_MODES)) | (bands - set(ALL_BANDS)) | (kinds - {"atno", "bandnew"}) | \
        (sources - {"dxscape", "dxsummit"})
    if bad:
        raise SystemExit(f"settings.ini の [cluster] に分からない値があります: {', '.join(sorted(bad))}")
    print(f"自局: {', '.join(sorted(my_calls))} / モード: {', '.join(m for m in ALL_MODES if m in modes)}"
          f" / バンド: {', '.join(b for b in ALL_BANDS if b in bands)}"
          f" / 通知: {'全バンド初＋バンドニュー' if 'bandnew' in kinds else '全バンド初のみ'}"
          f" / 取得先: {', '.join(sorted(sources))}")

    password = os.environ.get("DXCC_MAIL_PASS")
    if not password and a.pass_file:
        with open(a.pass_file, encoding="utf-8") as f:
            password = f.read().strip()
    if not password and not a.dry_run:
        raise SystemExit("Gmailのアプリパスワードが見つかりません（環境変数 DXCC_MAIL_PASS）")
    if a.test:
        dn.notify_gmail(mail_from, password, mail_to, "[クラスター] テスト通知",
                        "cluster_watch.py からのテスト通知です。")
        print("テストメールを送りました")
        return

    lock = acquire_lock(os.path.join(os.environ.get("TEMP", base), f"cluster_watch_{a.site}.lock"))
    if lock is None:
        print("このPCでは既に起動しています。終了します。")
        return

    cty = dn.Cty(a.cty or os.path.join(base, "common", "cty_latest.dat"))
    csv_path = a.cty_csv or os.path.join(base, "common", "cty_latest.csv")
    dxcc_nums = dn.load_dxcc_numbers(csv_path) if os.path.exists(csv_path) else {}

    worked = None
    for _ in range(20):                         # HAMLOG を読めるまで最大10分待つ
        worked = load_worked(a, cty, my_calls)
        if worked is not None:
            break
        print("HAMLOG を読めないため、30秒後に読み直します")
        time.sleep(30)
    if worked is None:
        raise SystemExit("HAMLOG を読めなかったため終了します（ゲスト運用の交信を除外できないため）")
    mtimes = watched_mtimes(a)
    print(f"交信済み: {len(worked)} 組（エンティティ×バンド）、エンティティ {len({e for e, b in worked})}")

    sess = os.path.join(base, "sessions")
    os.makedirs(sess, exist_ok=True)
    state_path = os.path.join(sess, f"cluster_{a.site}.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    sent_times, missing, last_id = [], 0, None

    while True:
        if a.stop_with_wsjtx:
            missing = 0 if wsjtx_running() else missing + 1
            if missing >= 2:
                print("WSJT-X の終了を確認しました。終了します。")
                break

        m = watched_mtimes(a)
        if m != mtimes:
            w = load_worked(a, cty, my_calls)
            if w is None:
                print("前回の交信済みの表をそのまま使います")
            else:
                worked, mtimes = w, m
                print(time.strftime("%H:%M:%S"), f"交信記録を再読込しました（{len(worked)} 組）")
        worked_ents = {e for e, b in worked}

        now = datetime.now(timezone.utc)
        spots = []
        if "dxscape" in sources:
            try:
                spots += parse_dxscape(fetch(URL_DXSCAPE), now)
            except Exception as e:
                print(time.strftime("%H:%M:%S"), "DXSCAPE 取得エラー:", e)
        if "dxsummit" in sources:
            try:
                ss, idr = parse_dxsummit(fetch(URL_DXSUMMIT), cty)
                spots += ss
                if idr and last_id and idr[0] > last_id + 1:
                    print(time.strftime("%H:%M:%S"),
                          f"[DX Summit] 前回から {idr[0] - last_id - 1} 件以上のスポットを取得できていない可能性があります")
                if idr:
                    last_id = max(last_id or 0, idr[1])
            except Exception as e:
                print(time.strftime("%H:%M:%S"), "DX Summit 取得エラー:", e)
        spots.sort(key=lambda s: s["time"])        # 古い順。両方に載った局は先に見つけた側で通知

        for s in spots:
            if now - s["time"] > timedelta(minutes=a.max_age):
                continue
            band = dn.freq_to_band(s["freq"] * 1000)
            if band not in bands:
                continue
            mode, basis = guess_mode(s["freq"], s["comment"])
            if mode not in modes:
                continue
            ent = cty.entity(s["call"])
            if not ent:
                continue
            if ent not in worked_ents:
                kind = "ATNO"
            elif (ent, band) not in worked and "bandnew" in kinds:
                kind = "BANDNEW"
            else:
                continue
            if kind == "ATNO" and "atno" not in kinds:
                continue
            key = f"{s['call']}|{band}|{mode}"
            if now.timestamp() - state.get(key, 0) < a.renotify * 3600:
                continue
            sent_times = [t for t in sent_times if now.timestamp() - t < 3600]
            if len(sent_times) >= a.max_per_hour:
                print("[通知] 1時間あたりの上限に達したため送信を見送りました")
                break
            subject, text, msg = make_mail(s, kind, ent, band, mode, basis, dxcc_nums.get(ent),
                                           site, mail_from, mail_to)
            try:
                if a.dry_run:
                    print("[DRY]", subject)
                else:
                    dn.notify_gmail(mail_from, password, mail_to, subject, text, msg)
                    print(time.strftime("%H:%M:%S"), "通知:", subject)
            except Exception as e:
                print("[Gmail エラー]", e)
                continue                       # 送れなかったものは次の回にもう一度試す
            sent_times.append(now.timestamp())
            state[key] = now.timestamp()
            state = {k: v for k, v in state.items() if now.timestamp() - v < 48 * 3600}
            try:
                with open(state_path, "w", encoding="utf-8") as f:
                    json.dump(state, f)
            except OSError as e:
                print("[記録] 保存できませんでした:", e)

        time.sleep(a.interval)


if __name__ == "__main__":
    main()
