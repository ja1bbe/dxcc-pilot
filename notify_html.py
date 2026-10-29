# -*- coding: utf-8 -*-
"""
DXCC通知メールのHTML版を作るモジュール（テキスト版を同梱し、グラフ画像はCID参照で添付）。

使い方（現行の通知スクリプトから）:
    from notify_html import build_message
    msg = build_message(event, wx_now, series, history, sender, to)
    smtp.send_message(msg)

HTMLやグラフの作成に失敗しても、例外は外に出さず、テキスト版だけのメールを返します。
（HTML化が原因で通知が届かなくなることを防ぐため）
動作確認: python notify_html.py --demo   → demo.eml と demo.html（試験用データ）を作成
"""
import io, json, os, time, urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid

# ---- 色と書体（見本と同じ） ----
INK, SUB, RULE = "#1C2430", "#5A6573", "#D5DAE1"
GOOD, CAU, BAD, NAVY = "#1F7A4D", "#B5651D", "#B3261E", "#24476B"
PAPER, TINT = "#FFFFFF", "#F3F5F8"
FONT = ("font-family:-apple-system,'Hiragino Sans','Hiragino Kaku Gothic ProN',"
        "'Noto Sans JP',Meiryo,sans-serif;")
MY_CALL = ""          # 自局のコールサイン（dxcc_notify.py が settings.ini の my_call から設定する）


def _ua():
    """データ提供元に、どの局のスクリプトからの取得かを伝える User-Agent"""
    return f"dxcc_notify (amateur radio, {MY_CALL or 'unknown station'})"


def _own():
    return f"{MY_CALL}の交信のみ" if MY_CALL else "自局の交信のみ"


GRID_BANDS = ["160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]
KIND_LABEL = {"ATNO": ("全バンド初のエンティティを自局で受信", BAD),
              "BANDNEW": ("バンドニューを自局で受信", GOOD),
              "CALLED": ("他局が呼び出し中（当局では未受信）", NAVY),
              "PED": ("DXペディションを受信（このバンドは交信済み）", "#9A6B00"),
              "JA_CALLING": ("他局が呼び出し中（当局では未受信）", NAVY)}

# ---- 宇宙天気の時系列（NOAA SWPC） ----
SWPC = "https://services.swpc.noaa.gov"
SOURCES = {   # 第1候補は dxcc_notify.py が普段使っているRTSWの1分値、だめなら1日分の製品
    "wind": [SWPC + "/json/rtsw/rtsw_wind_1m.json", SWPC + "/products/solar-wind/plasma-1-day.json"],
    "mag": [SWPC + "/json/rtsw/rtsw_mag_1m.json", SWPC + "/products/solar-wind/mag-1-day.json"],
    "kp": [SWPC + "/products/noaa-planetary-k-index.json"],
}
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spacewx_cache.json")

def _rows(data):
    """NOAAのJSON（先頭行が見出しの配列、または辞書の配列）を辞書の配列にそろえる"""
    if data and isinstance(data[0], list):
        head = data[0]
        return [dict(zip(head, r)) for r in data[1:]]
    return [r for r in data if isinstance(r, dict)]

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _t(s):
    return datetime.strptime(str(s)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")

def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _ua()})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def fetch_series(fetch=None, max_age_min=15):
    """過去1日分程度の太陽風速度・Bz・Kpを返す。15分以内に取得したものがあれば再利用する。
    fetch には dxcc_notify.py の fetch_json を渡せる（gzip対応）"""
    fetch = fetch or _get_json
    raw = None
    if os.path.exists(CACHE) and time.time() - os.path.getmtime(CACHE) < max_age_min * 60:
        try:
            with open(CACHE, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = None
    if raw is None:
        raw = {}
        for k, urls in SOURCES.items():
            for url in urls:
                try:
                    raw[k] = fetch(url)
                    break
                except Exception:
                    continue
        try:
            with open(CACHE, "w", encoding="utf-8") as f:
                json.dump(raw, f)
        except OSError:
            pass
    s = {"speed": [], "bz": [], "kp": []}
    def take(key, cols, out):
        for r in _rows(raw.get(key) or []):
            if r.get("active") is False:          # RTSWは使用中の衛星の値だけ
                continue
            for c in cols:
                v = _num(r.get(c))
                if v is not None:
                    try:
                        s[out].append((_t(r["time_tag"]), v))
                    except (KeyError, ValueError):
                        pass
                    break
    take("wind", ("proton_speed", "speed"), "speed")
    take("mag", ("bz_gsm",), "bz")
    take("kp", ("Kp", "kp"), "kp")
    for k in s:
        s[k] = sorted(set(s[k]))
    return s

def wx_from_series(s, sfi=None, s_scale=None):
    """時系列から、通知に載せる現在値と24時間の変化を求める"""
    last = lambda k: s[k][-1][1] if s[k] else None
    tail = lambda k, h: [v for t, v in s[k] if t >= s[k][-1][0] - timedelta(hours=h)] if s[k] else []
    return {"kp": last("kp"), "kp_max24": max(tail("kp", 24), default=None),
            "bz": last("bz"), "speed": last("speed"),
            "speed_max24": max(tail("speed", 24), default=None),
            "sfi": sfi, "s_scale": s_scale}

def wx_from_sw(v, kp_hist, series=None):
    """dxcc_notify.py の SpaceWeather.v と kp_hist から、通知に載せる値を作る（本文と同じ値になる）"""
    now = time.time()
    k24 = [k for t, k in kp_hist if t > now - 86400]
    wx = {"kp": v.get("kp"), "kp_max24": max(k24) if k24 else None, "bz": v.get("bz"),
          "speed": v.get("speed"), "speed_max24": None, "sfi": v.get("sfi"),
          "s_scale": f"S{v['S']}" if "S" in v else None}
    if series and series.get("speed"):
        last = series["speed"][-1][0]
        sp = [x for t, x in series["speed"] if t >= last - timedelta(hours=24)]
        wx["speed_max24"] = max(sp) if sp else None
    return wx

def judge(wx):
    """宇宙天気の一言まとめ（判定の境目は仮の値。経験に合わせて調整してください）"""
    kp, kmax, bz = wx.get("kp"), wx.get("kp_max24"), wx.get("bz")
    if kp is None:
        return "宇宙天気（データなし）", "宇宙天気のデータを取得できませんでした。", SUB
    if kp >= 5:
        return "宇宙天気は荒れています", f"Kpは{kp:.1f}です。高緯度を通る経路は特に厳しい状態です。", BAD
    if kmax is not None and kmax - kp >= 2:
        return "宇宙天気は回復傾向", f"過去24時間の最大{kmax:.1f}から、Kpは{kp:.1f}まで下がっています。", GOOD
    if bz is not None and bz <= -5:
        return "Bz南向きに注意", f"Bzが{bz:+.0f} nTの南向きです。この状態が続くと荒れる可能性があります。", CAU
    if kp < 3:      # Kpは1/3刻み（2+ = 2.33 まで静穏とする）
        return "宇宙天気は静穏", f"Kpは{kp:.1f}で、落ち着いた状態です。", GOOD
    return "宇宙天気はやや乱れ", f"Kpは{kp:.1f}です。", CAU

# ---- グラフ ----
def make_graph(s, t_rx, mark="受信時刻"):
    """Bz・太陽風速度・Kpの3段グラフ（PNGのバイト列）。t_rxは受信時刻（UTC, naive）"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as md
    from matplotlib import font_manager
    have = {f.name for f in font_manager.fontManager.ttflist}
    jp = [n for n in ("Yu Gothic", "Meiryo", "MS Gothic", "Hiragino Sans", "Noto Sans CJK JP") if n in have]
    plt.rcParams["font.family"] = jp[:1] + ["sans-serif"]   # 使える日本語フォントを1つ選ぶ
    plt.rcParams["font.size"] = 11
    t0 = t_rx - timedelta(hours=24)
    fig, ax = plt.subplots(3, 1, figsize=(3.6, 3.9), dpi=300, sharex=True,
                           gridspec_kw={"height_ratios": [1, 1, 1.05], "hspace": 0.32})
    for a in ax:
        for sp in ("top", "right"):
            a.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            a.spines[sp].set_color(RULE)
        a.tick_params(colors=INK, labelsize=8, length=2)
        a.grid(axis="y", color=RULE, lw=0.5)
        a.axvline(t_rx, color=INK, lw=1.2)
    sel = lambda k: [(t, v) for t, v in s[k] if t0 <= t <= t_rx]
    bz, sp = sel("bz"), sel("speed")
    if sp:   # L1から地球まで: 150万km ÷ 速度
        lead = timedelta(seconds=1.5e6 / max(sp[-1][1], 250))
        for a in ax[:2]:
            a.axvline(t_rx - lead, color=INK, lw=0.8, ls=(0, (2, 2)))
    if bz:
        t, v = zip(*bz)
        a = ax[0]
        a.fill_between(t, 0, v, where=[x < 0 for x in v], color=BAD, alpha=0.25, lw=0)
        a.fill_between(t, 0, v, where=[x >= 0 for x in v], color=GOOD, alpha=0.25, lw=0)
        a.plot(t, v, color=INK, lw=0.8)
        a.axhline(0, color=INK, lw=0.5)
        lim = max(10, max(abs(x) for x in v) * 1.15)   # 上下対称にして、値がはみ出さないようにする
        a.set_ylim(-lim, lim)
    ax[0].set_ylabel("Bz [nT]", fontsize=8, color=INK)
    ax[0].set_title("南向き（負）が続くと荒れやすい", loc="left", fontsize=7, color=BAD, pad=2)
    if sp:
        t, v = zip(*sp)
        ax[1].plot(t, v, color=NAVY, lw=1)
        ax[1].set_ylim(min(300, min(v) - 30), max(700, max(v) + 30))
        ax[1].set_title("破線＝L1観測時刻", loc="right", fontsize=6.5, color=INK, pad=2)
    ax[1].set_ylabel("太陽風\n[km/s]", fontsize=8, color=INK)
    for t, k in s["kp"]:
        if t0 - timedelta(hours=3) <= t <= t_rx:
            c = GOOD if k < 3 else (CAU if k < 5 else BAD)
            ax[2].bar(t, k, width=timedelta(hours=2.85), align="edge", color=c, alpha=0.85)
    ax[2].set_ylim(0, 9)
    ax[2].set_yticks([0, 3, 6, 9])
    ax[2].set_ylabel("Kp", fontsize=8, color=INK)
    ax[2].set_xlim(t0, t_rx + timedelta(minutes=40))
    ax[2].xaxis.set_major_locator(md.HourLocator(byhour=[0, 6, 12, 18]))
    ax[2].xaxis.set_major_formatter(md.DateFormatter("%H"))
    ax[2].set_xlabel(f"UTC（過去24時間）　実線＝{mark}", fontsize=7.5, color=INK)
    fig.align_ylabels(ax)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.06, facecolor="white")
    plt.close(fig)
    try:   # 64色に減らして約20KBにする（Pillowはmatplotlibと一緒に入っている）
        from PIL import Image
        im = Image.open(io.BytesIO(buf.getvalue())).convert("P", palette=Image.ADAPTIVE, colors=64)
        out = io.BytesIO()
        im.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return buf.getvalue()

# ---- 本文 ----
def _qtime(q):
    return f"{q['time_utc']:%Y-%m-%d %H:%M} UTC" if q.get("time_utc") else "日付不明"

def _fmt(v, f="{:g}"):
    return "－" if v is None else f.format(v)

def _wx_rows(wx):
    rows = []
    kp, km = wx.get("kp"), wx.get("kp_max24")
    note = (f"▼ {km:.1f}から回復", GOOD) if (kp is not None and km is not None and km - kp >= 2) else ("", SUB)
    rows.append(("Kp指数", _fmt(kp, "{:.1f}"), *note))
    bz = wx.get("bz")
    if bz is None:
        rows.append(("Bz", "－", "", SUB))
    else:
        rows.append(("Bz", f"{bz:+.1f} nT", "北向き" if bz >= 0 else "南向き", GOOD if bz >= 0 else BAD))
    v, vm = wx.get("speed"), wx.get("speed_max24")
    note = (f"▼ {vm:.0f}から低下", GOOD) if (v and vm and vm - v >= 100) else ("", SUB)
    rows.append(("太陽風速度", _fmt(v, "{:.0f} km/s"), *note))
    if wx.get("sfi") is not None:
        rows.append(("SFI", _fmt(wx["sfi"], "{:.0f}"), "", SUB))
    if wx.get("s_scale"):
        ok = wx["s_scale"] == "S0"
        rows.append(("Sスケール", wx["s_scale"], "プロトン現象なし" if ok else "D層吸収に注意", GOOD if ok else BAD))
    for label, val, note in wx.get("iono_rows", []):    # 電離層（NICT）の値
        rows.append((label, val, note, SUB))
    return rows

def render_text(ev, wx, history, source_label):
    jst = ev["time_utc"] + timedelta(hours=9)
    title, sentence, _ = judge(wx)
    lines = [f"{ev['band']} {KIND_LABEL[ev['kind']][0]}",
             f"{ev['call']}  {ev['entity']} (DXCC #{ev['dxcc']})",
             f"SNR {ev['snr']:+d} dB  {ev['time_utc']:%H:%M} UTC ({jst:%H:%M} JST)",
             f"受信メッセージ: {ev['message']}", "",
             f"{title}: {sentence}"]
    lines += [f"  {a}: {b} {c}".rstrip() for a, b, c, _ in _wx_rows(wx)]
    if history is not None:
        lines += ["", f"このエンティティの交信記録（{source_label}）"]
        worked = [b for b in GRID_BANDS if history.get(b)]
        lines.append("  交信済み: " + ("、".join(worked) if worked else "なし"))
        for b in worked:
            q = history[b][-1]
            lines.append(f"  {b} {q['mode']} {q['call']} {_qtime(q)}")
        for q in history.get("_guest", [])[-3:]:
            lines.append(f"  （ゲスト運用 {q.get('op_call') or ''}）{q.get('band') or ''} {q['mode']} {q['call']} {_qtime(q)}")
    lines += ["", f"QRZ.com: https://www.qrz.com/db/{ev['call']}",
              f"PSK Reporter: https://pskreporter.info/pskmap.html?callsign={ev['call']}"]
    return "\n".join(lines)

def render_html(ev, wx, history, source_label, img_src):
    F = FONT
    jst = ev["time_utc"] + timedelta(hours=9)
    label, kcol = KIND_LABEL[ev["kind"]]
    title, sentence, _ = judge(wx)
    others = history and any(history.get(b) for b in GRID_BANDS if b != ev["band"])
    sub = (f"DXCC #{ev['dxcc']}" if ev.get("dxcc") else "") + ("　他バンドで交信済み" if others else "")
    if ev.get("after_called"):
        label = "先に他局の呼び出しで通知した局を自局でも受信"
    # 件名と同じ色の印（全バンド初=赤、ペディション=黄、バンドニュー=緑。組み合わせは左右半分ずつ）
    RED, YEL, GRN = "#D93025", "#F4B400", "#1E8E3E"
    half = lambda c, side: (f'<span style="display:inline-block;width:7px;height:14px;background:{c};'
                            + ('border-radius:7px 0 0 7px;' if side == "L" else 'border-radius:0 7px 7px 0;')
                            + 'vertical-align:-2px;"></span>')
    dot, ped_txt = "", ""
    if ev["kind"] == "PED":
        dot = (f'<span style="display:inline-block;background:#FFFFFF;border-radius:10px;padding:2px 3px;'
               f'margin-right:8px;line-height:14px;">{half(YEL, "L")}{half(YEL, "R")}</span>')
    elif ev["kind"] in ("ATNO", "BANDNEW"):
        main = RED if ev["kind"] == "ATNO" else GRN
        if ev.get("ped"):
            l, r = (RED, YEL) if ev["kind"] == "ATNO" else (YEL, GRN)
            ped_txt = "・DXペディション"
        else:
            l = r = main
        dot = (f'<span style="display:inline-block;background:#FFFFFF;border-radius:10px;padding:2px 3px;'
               f'margin-right:8px;line-height:14px;">{half(l, "L")}{half(r, "R")}</span>')
    note = ""
    if ev.get("warn"):
        note += (f'<tr><td style="{F}padding:10px 16px 0 16px;"><div style="background:#FBEDE9;color:{BAD};'
                 f'font-size:13px;line-height:20px;font-weight:700;padding:8px 10px;border-radius:4px;">'
                 f'{ev["warn"]}</div></td></tr>')
    if ev.get("bearing"):
        note += (f'<tr><td style="{F}padding:8px 16px 0 16px;font-size:13px;color:{INK};">'
                 f'{ev["bearing"]}</td></tr>')
    wxrows = "".join(
        f'<tr><td style="{F}padding:7px 0;border-top:1px solid {RULE};font-size:13px;color:{SUB};">{a}</td>'
        f'<td style="{F}padding:7px 0;border-top:1px solid {RULE};font-size:15px;font-weight:700;color:{INK};text-align:right;white-space:nowrap;">{b}</td>'
        f'<td style="{F}padding:7px 0 7px 10px;border-top:1px solid {RULE};font-size:12px;font-weight:700;color:{col};white-space:nowrap;">{c}</td></tr>'
        for a, b, c, col in _wx_rows(wx))
    graph = ""
    if img_src:
        graph = (f'<img src="{img_src}" width="348" alt="過去24時間のBz、太陽風速度、Kp指数のグラフ" '
                 f'style="display:block;width:100%;max-width:348px;height:auto;border:0;">'
                 f'<div style="{F}font-size:11px;line-height:17px;color:{SUB};padding:6px 0 0 0;">'
                 '実線は受信時刻。太陽風とBzはL1点（地球から約150万km）での観測値で、地球に届くまで'
                 '約1時間かかるため、受信時刻に地球へ届いていたのは破線付近の値です。Kpは3時間ごとの値です。</div>')
    hist = ""
    if history is not None:
        def cell(b):
            if b == ev["band"]:
                return f'<td style="{F}font-size:15px;font-weight:800;color:#FFFFFF;background:{INK};text-align:center;padding:5px 0;border-radius:3px;">◆</td>'
            if history.get(b) and b in (ev.get("confirmed_bands") or []):
                return f'<td style="{F}font-size:15px;font-weight:800;color:#FFFFFF;background:{GOOD};text-align:center;padding:5px 0;border-radius:3px;">◎</td>'
            if history.get(b):
                return f'<td style="{F}font-size:15px;font-weight:800;color:{GOOD};background:#FFFFFF;border:1.5px solid {GOOD};text-align:center;padding:4px 0;border-radius:3px;">●</td>'
            return f'<td style="{F}font-size:15px;color:{RULE};background:{TINT};text-align:center;padding:5px 0;border-radius:3px;">－</td>'
        heads = "".join(f'<td style="{F}font-size:10px;color:{SUB};text-align:center;">{b[:-1]}</td>' for b in GRID_BANDS)
        cells = "".join(cell(b) for b in GRID_BANDS)
        rows = ""
        for b in GRID_BANDS:
            if not history.get(b):
                continue
            q = history[b][-1]   # そのバンドの最新の交信
            more = f"（ほか{len(history[b]) - 1}件）" if len(history[b]) > 1 else ""
            rows += (f'<tr><td style="{F}padding:7px 0;border-top:1px solid {RULE};vertical-align:top;width:58px;">'
                     f'<div style="font-size:15px;font-weight:800;color:{INK};">{b}</div>'
                     f'<div style="font-size:11px;color:{SUB};">{q["mode"]}</div></td>'
                     f'<td style="{F}padding:7px 0;border-top:1px solid {RULE};vertical-align:top;">'
                     f'<div style="font-size:15px;font-weight:700;color:{INK};">{q["call"]}</div>'
                     f'<div style="font-size:12px;color:{SUB};">{_qtime(q)}{more}</div></td></tr>')
        for q in history.get("_guest", [])[-3:]:   # ゲスト運用は灰色で別枠（最新3件）
            rows += (f'<tr><td style="{F}padding:7px 0;border-top:1px dashed {RULE};vertical-align:top;width:58px;">'
                     f'<div style="font-size:14px;font-weight:700;color:{SUB};">{q.get("band") or "－"}</div>'
                     f'<div style="font-size:11px;color:{SUB};">{q["mode"]}</div></td>'
                     f'<td style="{F}padding:7px 0;border-top:1px dashed {RULE};vertical-align:top;">'
                     f'<div style="font-size:14px;font-weight:700;color:{SUB};">{q["call"]}</div>'
                     f'<div style="font-size:12px;color:{SUB};">{_qtime(q)}　ゲスト運用（{q.get("op_call") or "他局"}）・交信済みに数えない</div></td></tr>')
        hist = (f'<tr><td style="padding:14px 16px 0 16px;">'
                f'<div style="{F}font-size:17px;font-weight:800;color:{INK};border-top:2px solid {INK};padding-top:10px;">このエンティティの交信記録</div>'
                f'<div style="{F}font-size:11px;color:{SUB};">{source_label}</div>'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="2" style="margin-top:8px;table-layout:fixed;"><tr>{heads}</tr><tr>{cells}</tr></table>'
                f'<div style="{F}font-size:11px;line-height:17px;color:{SUB};padding:4px 0 6px 0;">{"◎ LoTWで確認済み　● 交信済み（未確認）" if ev.get("lotw") else "● 交信済み"}　◆ 今回受信したバンド　－ 未交信</div>'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>')
    ops = ""
    if ev.get("dx_ops"):
        ops = _section("このエンティティの運用予定", _ops_rows(ev["dx_ops"]))
    btn = (f'display:block;text-align:center;font-size:15px;font-weight:700;color:{NAVY};'
           f'border:1.5px solid {NAVY};border-radius:6px;padding:11px 0;text-decoration:none;')
    return f'''<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light only"><meta name="supported-color-schemes" content="light"></head>
<body style="margin:0;padding:0;background:{TINT};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{TINT};"><tr><td align="center" style="padding:12px 8px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:380px;background:{PAPER};border-radius:6px;">
<tr><td style="{F}background:{kcol};color:#FFFFFF;font-size:13px;font-weight:700;padding:9px 16px;border-radius:6px 6px 0 0;">{dot}{ev["band"]}　{label}{ped_txt}</td></tr>
<tr><td style="padding:18px 16px 4px 16px;">
<div style="{F}font-size:40px;line-height:44px;font-weight:800;color:{INK};letter-spacing:1px;">{ev["call"]}</div>
<div style="{F}font-size:18px;line-height:26px;font-weight:700;color:{INK};">{ev["entity"]}</div>
<div style="{F}font-size:13px;line-height:20px;color:{SUB};padding-top:2px;">{sub}</div></td></tr>
<tr><td style="padding:10px 16px 0 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="50%" style="{F}padding:8px 0;border-top:2px solid {INK};"><div style="font-size:12px;color:{SUB};">SNR</div><div style="font-size:22px;font-weight:800;color:{INK};">{ev["snr"]:+d} dB</div></td>
<td width="50%" style="{F}padding:8px 0;border-top:2px solid {INK};"><div style="font-size:12px;color:{SUB};">受信時刻</div><div style="font-size:22px;font-weight:800;color:{INK};">{ev["time_utc"]:%H:%M} UTC</div><div style="font-size:12px;color:{SUB};">{jst:%H:%M} JST</div></td>
</tr></table>
<div style="{F}font-size:12px;color:{SUB};padding-top:6px;">受信メッセージ</div>
<div style="{F}font-size:16px;font-weight:700;color:{INK};padding:2px 0 4px 0;">{ev["message"]}</div></td></tr>
{note}
<tr><td style="padding:14px 16px 0 16px;">
<div style="{F}font-size:17px;font-weight:800;color:{INK};border-top:2px solid {INK};padding-top:10px;">{title}</div>
<div style="{F}font-size:14px;line-height:22px;color:{INK};padding:4px 0 8px 0;">{sentence}</div>{graph}</td></tr>
<tr><td style="padding:12px 16px 0 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{wxrows}</table></td></tr>
{hist}
{ops}
<tr><td style="padding:16px 16px 18px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="50%" style="padding-right:4px;"><a href="https://www.qrz.com/db/{ev["call"]}" style="{F}{btn}">QRZ.com</a></td>
<td width="50%" style="padding-left:4px;"><a href="https://pskreporter.info/pskmap.html?callsign={ev["call"]}" style="{F}{btn}">PSK Reporter</a></td>
</tr></table></td></tr>
</table></td></tr></table></body></html>'''

def default_subject(ev):
    return f"[DXCC] {ev['band']} {ev['call']} ({ev['entity']}) 自局で受信"

def build_message(ev, wx, series, history, sender, to, subject=None,
                  source_label="HAMLOG＋JTAlertの記録", text=None):
    """
    ev      : {"kind","call","entity","dxcc","band","snr","time_utc"(aware UTC),"message"}
    wx      : wx_from_series() の結果（SFIとSスケールは現行スクリプトの値を渡す）
    series  : fetch_series() の結果（Noneならグラフなし）
    history : worked_table()[DXCC番号]（{バンド: [交信]}）。Noneなら交信記録欄なし
    """
    msg = EmailMessage()
    msg["Subject"] = subject or default_subject(ev)
    msg["From"], msg["To"] = sender, to
    try:
        text = text or render_text(ev, wx, history, source_label)
        msg.set_content(text)
        png = None
        if series:
            try:
                png = make_graph(series, ev["time_utc"].replace(tzinfo=None))
            except Exception:
                png = None          # グラフだけ失敗した場合は、グラフなしのHTMLにする
        cid = make_msgid(domain="dxcc.notify")
        html = render_html(ev, wx, history, source_label, f"cid:{cid[1:-1]}" if png else None)
        msg.add_alternative(html, subtype="html")
        if png:
            msg.get_payload()[1].add_related(png, "image", "png", cid=cid,
                                             filename="spacewx.png")
        return msg
    except Exception as e:          # 何があってもテキスト版で送る
        plain = EmailMessage()
        plain["Subject"] = msg["Subject"]
        plain["From"], plain["To"] = sender, to
        plain.set_content((text or f"{ev.get('band')} {ev.get('call')} {ev.get('entity')}")
                          + f"\n\n（HTML版の作成に失敗: {e}）")
        return plain

# ======================================================================
# 開始・終了のメール（HTML版）
# ======================================================================
def _esc(x):
    import html as _h
    return _h.escape(str(x))

def _page(header, hcolor, inner):
    F = FONT
    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light only"><meta name="supported-color-schemes" content="light"></head>
<body style="margin:0;padding:0;background:{TINT};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{TINT};"><tr><td align="center" style="padding:12px 8px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:380px;background:{PAPER};border-radius:6px;">
<tr><td style="{F}background:{hcolor};color:#FFFFFF;font-size:13px;font-weight:700;padding:9px 16px;border-radius:6px 6px 0 0;">{header}</td></tr>
{inner}
<tr><td style="padding:0 0 16px 0;"></td></tr>
</table></td></tr></table></body></html>"""

def _section(title, body, first=False):
    F = FONT
    rule = "" if first else f"border-top:2px solid {INK};padding-top:10px;"
    return (f'<tr><td style="padding:14px 16px 0 16px;">'
            f'<div style="{F}font-size:17px;font-weight:800;color:{INK};{rule}">{title}</div>{body}</td></tr>')

def _kv_table(rows):
    F = FONT
    out = ""
    for k, v in rows:
        out += (f'<tr><td style="{F}padding:7px 10px 7px 0;border-top:1px solid {RULE};font-size:13px;color:{SUB};'
                f'vertical-align:top;white-space:nowrap;">{k}</td>'
                f'<td style="{F}padding:7px 0;border-top:1px solid {RULE};font-size:14px;color:{INK};">{v}</td></tr>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">{out}</table>'

def _wx_table(wx):
    F = FONT
    rows = "".join(
        f'<tr><td style="{F}padding:7px 0;border-top:1px solid {RULE};font-size:13px;color:{SUB};">{a}</td>'
        f'<td style="{F}padding:7px 0;border-top:1px solid {RULE};font-size:15px;font-weight:700;color:{INK};text-align:right;white-space:nowrap;">{b}</td>'
        f'<td style="{F}padding:7px 0 7px 10px;border-top:1px solid {RULE};font-size:12px;font-weight:700;color:{col};white-space:nowrap;">{c}</td></tr>'
        for a, b, c, col in _wx_rows(wx))
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">{rows}</table>'

def _utc_jst(t):
    g = time.gmtime(t)
    j = time.gmtime(t + 9 * 3600)
    return f'{time.strftime("%Y-%m-%d %H:%M", g)} UTC（{time.strftime("%H:%M", j)} JST）'

def render_start(info, wx, img_src):
    """info: site, t_start, bands, log_info(list), worked, sess_path, extra(list)"""
    F = FONT
    chips = "".join(f'<span style="display:inline-block;margin:0 4px 4px 0;padding:3px 7px;border:1px solid {RULE};'
                    f'border-radius:3px;font-size:12px;color:{INK};background:{TINT};">{_esc(b)}</span>'
                    for b in info["bands"])
    big = (f'<tr><td style="padding:18px 16px 0 16px;">'
           f'<div style="{F}font-size:13px;color:{SUB};">交信済み（エンティティ×バンド）</div>'
           f'<div style="{F}font-size:40px;line-height:46px;font-weight:800;color:{INK};">{info["worked"]:,}</div>'
           f'<div style="{F}font-size:12px;color:{SUB};">{_own()}（ゲスト運用は数えない）</div></td></tr>')
    basics = _kv_table([("開始", _utc_jst(info["t_start"])), ("対象バンド", chips)])
    counts = ""
    bc = info.get("band_counts")
    cc = info.get("conf_counts")          # LoTW で確認済みの数（無ければ交信数だけを表示）
    if bc:
        total, top = info.get("entities_total") or 0, max(bc.values()) or 1
        LIGHT = "#B7C7DA"                 # 交信済み（未確認の分）の淡い色
        if cc:
            counts += (f'<tr><td></td><td></td>'
                       f'<td style="{F}width:40px;font-size:11px;color:{SUB};text-align:right;">交信</td>'
                       f'<td style="{F}width:40px;font-size:11px;color:{GOOD};text-align:right;">確認</td></tr>')
        for b, n in bc.items():
            c = (cc or {}).get(b, 0)
            base_ = total or top
            wc = round(100 * c / base_) if c else 0
            ww = max(1, round(100 * n / base_)) if n else 0
            cells = ""
            if cc and wc:
                cells += (f'<td width="{wc}%" style="background:{NAVY};height:10px;font-size:1px;line-height:1px;'
                          f'border-radius:2px 0 0 2px;">&nbsp;</td>')
            rest = ww - wc if cc else ww
            if rest > 0:
                cells += (f'<td width="{rest}%" style="background:{LIGHT if cc else NAVY};height:10px;font-size:1px;'
                          f'line-height:1px;">&nbsp;</td>')
            counts += (f'<tr><td style="{F}width:44px;padding:4px 0;font-size:13px;font-weight:700;color:{INK};">{_esc(b)}</td>'
                       f'<td style="padding:4px 8px 4px 0;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                       f'{cells}'
                       f'<td style="background:{TINT};height:10px;font-size:1px;line-height:1px;">&nbsp;</td></tr></table></td>'
                       f'<td style="{F}width:40px;padding:4px 0;font-size:14px;font-weight:800;color:{INK};text-align:right;">{n}</td>'
                       + (f'<td style="{F}width:40px;padding:4px 0;font-size:14px;font-weight:800;color:{GOOD};text-align:right;">{c}</td>'
                          if cc else '') + '</tr>')
        allb, allc = info.get("entities_worked"), info.get("entities_confirmed")
        if cc and allc is not None:
            hr = (f'<div style="{F}font-size:13px;color:{INK};padding:6px 0 0 0;">全バンド合計　交信 '
                  f'<b style="font-size:20px;">{allb}</b>　／　<span style="color:{GOOD};">確認 '
                  f'<b style="font-size:20px;">{allc}</b></span>　／ {total} エンティティ</div>')
            note = (f'<div style="{F}font-size:11px;line-height:17px;color:{SUB};padding-top:6px;">'
                    f'棒の濃い部分がLoTWで確認済み、淡い部分が交信済み（未確認）。確認はLoTWのみで、紙のQSLカードは含みません。'
                    f'{_own()}。分母はcty.datの現存エンティティ数です。</div>')
        else:
            hr = (f'<div style="{F}font-size:13px;color:{INK};padding:6px 0 0 0;">全バンド合計 '
                  f'<b style="font-size:20px;">{allb}</b> / {total} エンティティ</div>') if allb is not None else ""
            note = (f'<div style="{F}font-size:11px;line-height:17px;color:{SUB};padding-top:6px;">'
                    f'交信済み（QSLの確認とは別）。{_own()}。分母はcty.datの現存エンティティ数です。</div>')
        counts = hr + (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">'
                       f'{counts}</table>') + note
    logs = "".join(f'<div style="{F}font-size:13px;line-height:19px;color:{INK};padding:7px 0;border-top:1px solid {RULE};">{_esc(x)}</div>'
                   for x in (info["log_info"] or ["見つかりません"]))
    logs = f'<div style="margin-top:6px;">{logs}</div>'
    title, sentence, _ = judge(wx)
    graph = ""
    if img_src:
        graph = (f'<img src="{img_src}" width="348" alt="過去24時間のBz、太陽風速度、Kp指数のグラフ" '
                 f'style="display:block;width:100%;max-width:348px;height:auto;border:0;margin-top:8px;">')
    extra = "".join(f'<div style="{F}font-size:12px;color:{SUB};padding-top:4px;">{_esc(x)}</div>' for x in info.get("extra", []))
    inner = (big
             + (_section("バンド別のDXCC（交信と確認）" if info.get("conf_counts") else "バンド別のDXCC（交信済み）", counts) if counts else "")
             + _section("運用の設定", basics)
             + _section("宇宙天気", f'<div style="{F}font-size:14px;line-height:22px;color:{INK};padding:4px 0 0 0;">'
                                    f'<b>{title}</b>　{sentence}</div>{graph}{_wx_table(wx)}{extra}')
             + _section("読み込んだ交信ログ", logs)
             + (_section(f'DXペディション予定（3か月以内・{info.get("dx_ops_total", len(info["dx_ops"]))}件）', _ops_rows(info["dx_ops"]))
                if info.get("dx_ops") else "")
             + f'<tr><td style="{F}padding:12px 16px 0 16px;font-size:11px;line-height:17px;color:{SUB};word-break:break-all;">'
               f'運用中の記録: {_esc(info["sess_path"])}</td></tr>')
    return _page(f'START　{_esc(info["site"])}', NAVY, inner)

def _badge(text, color, filled):
    style = (f"background:{color};color:#FFFFFF;" if filled else f"color:{color};border:1px solid {color};")
    return (f'<span style="display:inline-block;margin:4px 4px 0 0;padding:1px 6px;border-radius:3px;'
            f'font-size:11px;font-weight:700;{style}">{text}</span>')

def render_stop(info):
    """info: site, t_start, t_end, n_notified, confirm, rows(list of dict), completed(list), tail(list)"""
    F = FONT
    hours = (info["t_end"] - info["t_start"]) / 3600
    hm = lambda t: time.strftime("%H:%M", time.gmtime(t))
    head = (f'<tr><td style="padding:18px 16px 0 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td width="34%" style="{F}vertical-align:top;"><div style="font-size:12px;color:{SUB};">運用時間</div>'
            f'<div style="font-size:24px;font-weight:800;color:{INK};">{hours:.1f}<span style="font-size:13px;">時間</span></div></td>'
            f'<td width="33%" style="{F}vertical-align:top;"><div style="font-size:12px;color:{SUB};">未交信局</div>'
            f'<div style="font-size:24px;font-weight:800;color:{INK};">{len(info["rows"])}<span style="font-size:13px;">局</span></div></td>'
            f'<td width="33%" style="{F}vertical-align:top;"><div style="font-size:12px;color:{SUB};">送った通知</div>'
            f'<div style="font-size:24px;font-weight:800;color:{INK};">{info["n_notified"]}<span style="font-size:13px;">通</span></div></td>'
            f'</tr></table>'
            f'<div style="{F}font-size:12px;color:{SUB};padding-top:6px;">{_utc_jst(info["t_start"])} 〜 {hm(info["t_end"])} UTC'
            + (f'　交信 {info["n_qso"]}件' if info.get("n_qso") is not None else '') + '</div></td></tr>')
    done = ""
    got = info.get("achieved") or []
    if got:
        items = ""
        for g in got:
            star = _badge("全バンド初", BAD, True) if g.get("atno") else _badge("バンドニュー", GOOD, True)
            items += (f'<div style="{F}padding:6px 0;border-top:1px solid #CFE3D6;">'
                      f'<div style="font-size:16px;font-weight:800;color:{INK};">{_esc(g["band"])}　{_esc(g["call"])}'
                      f'<span style="font-size:13px;font-weight:400;color:{SUB};">　{_esc(g["ent"])}</span></div>'
                      f'<div style="font-size:12px;color:{SUB};">{_esc(g.get("mode", ""))}　{hm(g["t"])} UTC　{star}</div></div>')
        done = (f'<tr><td style="padding:14px 16px 0 16px;"><div style="background:#E8F3EC;border-left:4px solid {GOOD};'
                f'padding:10px 12px;border-radius:3px;"><div style="{F}font-size:15px;font-weight:800;color:{GOOD};padding-bottom:4px;">'
                f'この運用中に叶った交信（新しいDXCC {len(got)}件）</div>{items}</div></td></tr>')
    elif info.get("completed"):
        items = "".join(f'<div style="{F}font-size:15px;color:{INK};padding:3px 0;"><b>{_esc(r["band"])}　{_esc(r["call"])}</b>'
                        f'<span style="color:{SUB};font-size:13px;">　{_esc(r["ent"])}</span></div>' for r in info["completed"])
        done = (f'<tr><td style="padding:14px 16px 0 16px;"><div style="background:#E8F3EC;border-left:4px solid {GOOD};'
                f'padding:10px 12px;border-radius:3px;"><div style="{F}font-size:15px;font-weight:800;color:{GOOD};padding-bottom:4px;">'
                f'この運用中に新たに交信できました（{len(info["completed"])}）</div>{items}</div></td></tr>')
    cards = ""
    for r in info["rows"]:
        badges = ""
        if r.get("worked_now"):
            badges += _badge("この運用中に交信", GOOD, True)
        if r.get("notified"):
            badges += _badge("通知済み", NAVY, False)
        if r.get("warn"):
            badges += _badge("要確認（グリッド）", BAD, False)
        sub = ""
        if r.get("sw"):
            sub += f'<div>宇宙天気: {_esc(r["sw"])}</div>'
        if r.get("iono"):
            sub += f'<div>電離層: {_esc(r["iono"])}</div>'
        cards += (f'<tr><td style="padding:9px 0;border-top:1px solid {RULE};">'
                  f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                  f'<td style="{F}width:52px;vertical-align:top;font-size:14px;font-weight:800;color:{INK};">{_esc(r["band"])}</td>'
                  f'<td style="{F}vertical-align:top;">'
                  f'<div style="font-size:16px;font-weight:800;color:{INK};">{_esc(r["call"])}'
                  f'<span style="font-size:13px;font-weight:400;color:{SUB};">　{_esc(r["ent"])}</span></div>'
                  f'<div style="font-size:12px;color:{INK};padding-top:2px;">{hm(r["first"])}–{hm(r["last"])} UTC　{r["count"]}回　'
                  f'最大 {r["best"]:+d} dB</div>{badges}'
                  f'<div style="font-size:11px;line-height:16px;color:{SUB};padding-top:3px;">{sub}</div>'
                  f'</td></tr></table></td></tr>')
    if not cards:
        cards = f'<tr><td style="{F}padding:9px 0;border-top:1px solid {RULE};font-size:14px;color:{SUB};">（なし）</td></tr>'
    lst = (f'<div style="{F}font-size:12px;color:{SUB};">自局の設備で直接受信した局（{info["confirm"]}回以上デコード）</div>'
           f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">{cards}</table>')
    tail = "".join(f'<div style="{F}font-size:12px;line-height:18px;color:{SUB};padding-top:4px;">{_esc(x)}</div>'
                   for x in info.get("tail", []))
    inner = head + done + _section("受信した未交信局", lst) + (_section("終了時の状態", tail) if tail else "")
    return _page(f'STOP　{_esc(info["site"])}', INK, inner)

def _assemble(subject, text, html, png, sender, to, mark="graph"):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.set_content(text)
    if png:
        cid = make_msgid(domain="dxcc.notify")
        html = html.replace("cid:GRAPH", f"cid:{cid[1:-1]}")
        msg.add_alternative(html, subtype="html")
        msg.get_payload()[1].add_related(png, "image", "png", cid=cid, filename="spacewx.png")
    else:
        msg.add_alternative(html, subtype="html")
    return msg

def build_start_message(info, wx, series, sender, to, subject, text):
    """開始のHTMLメール。何か失敗したら None を返す（呼び出し側でテキスト版にする）"""
    try:
        png = None
        if series:
            try:
                png = make_graph(series, datetime.fromtimestamp(info["t_start"], timezone.utc).replace(tzinfo=None),
                                 mark="開始時刻")
            except Exception:
                png = None
        html = render_start(info, wx, "cid:GRAPH" if png else None)
        # GmailはHTMLが約102KBを超えると途中で切って表示するため、予定の件数を減らして収める
        ops_all = list(info.get("dx_ops") or [])
        n = len(ops_all)
        info = {**info, "dx_ops_total": n}
        while len(html.encode("utf-8")) > 95000 and n > 10:
            n -= 5
            shown = ops_all[:n]
            info = {**info, "dx_ops": shown}
            html = render_start(info, wx, "cid:GRAPH" if png else None)
            html = html.replace("（発表時点の予定。変更・中止があります）",
                                f"（発表時点の予定。変更・中止があります）。ほか{len(ops_all) - n}件は省略")
        return _assemble(subject, text, html, png, sender, to)
    except Exception as e:
        print(f"[HTML] 開始のHTML版を作れませんでした: {e}")
        return None

def build_stop_message(info, sender, to, subject, text):
    """終了のHTMLメール。何か失敗したら None を返す"""
    try:
        return _assemble(subject, text, render_stop(info), None, sender, to)
    except Exception as e:
        print(f"[HTML] 終了のHTML版を作れませんでした: {e}")
        return None


# ======================================================================
# DXペディションの予定（NG3K Announced DX Operations）
# NG3Kへは直接アクセスせず、作者のリポジトリ（dxpeditions）が1日2回取得して
# GitHub Pagesで公開している dxpeditions.json を読む。利用者が増えてもNG3Kへのアクセスは増えない。
# 出典: NG3K Announced DX Operations (Bill Feidt, NG3K) https://www.ng3k.com/Misc/adxo.html
# ======================================================================
ADXO_JSON_URL = "https://ja1bbe.github.io/dxpeditions/dxpeditions.json"
ADXO_JSON_VERSION = 1          # このプログラムが知っている形式の版
ADXO_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dxpeditions_cache.json")


def fetch_adxo_json(max_age_h=3):
    """dxpeditions.json を読む。3時間は保存したものを使い、取得できなければ前回の保存分を使う"""
    if os.path.exists(ADXO_CACHE) and time.time() - os.path.getmtime(ADXO_CACHE) < max_age_h * 3600:
        with open(ADXO_CACHE, encoding="utf-8") as f:
            return json.load(f)
    try:
        req = urllib.request.Request(ADXO_JSON_URL, headers={"User-Agent": _ua()})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        with open(ADXO_CACHE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        return d
    except Exception:
        if os.path.exists(ADXO_CACHE):
            with open(ADXO_CACHE, encoding="utf-8") as f:
                return json.load(f)
        raise


_warned_version = False


def parse_adxo(d):
    """dxpeditions.json の各運用を {start, end, entity, call, url, qrz, qsl, info, bands, modes} にする。
    「運用中か」は、JSONが作られた時点の値ではなく、upcoming() で今日の日付から判定し直す"""
    from datetime import date
    global _warned_version
    if d.get("version") != ADXO_JSON_VERSION and not _warned_version:
        print(f"[ADXO] dxpeditions.json の形式（版 {d.get('version')}）が、このプログラムの想定（版 "
              f"{ADXO_JSON_VERSION}）と違います。読める項目だけを使います。新しい版のプログラムがあれば更新してください")
        _warned_version = True
    out = []
    for it in d.get("items") or []:
        try:
            start, end = date.fromisoformat(it["start"]), date.fromisoformat(it["end"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"start": start, "end": end, "entity": it.get("entity") or "",
                    "call": it.get("call") or "", "url": it.get("web"), "qrz": it.get("qrz"),
                    "qsl": it.get("qsl") or "", "info": it.get("info") or "",
                    "bands": it.get("bands") or [], "modes": it.get("modes") or []})
    return out


def load_adxo():
    """DXペディションの予定の一覧（parse_adxo の形）"""
    return parse_adxo(fetch_adxo_json())

ADXO_BANDS = ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]


def bands_of(info):
    """ADXOの説明文（「80-10m」「160-6m」「HF + 6m」「incl 60m」など）から運用予定のバンドを推定する。
    範囲の表記に60mは含めず、明記されたときだけ含める。HFは3〜30MHz（80m〜10m）とする。わからなければ空"""
    import re
    s = (info or "").replace("–", "-")
    found = set()
    for a, b in re.findall(r"\b(160|80|60|40|30|20|17|15|12|10|6)\s*-\s*(160|80|60|40|30|20|17|15|12|10|6)\s*m\b", s):
        i, j = ADXO_BANDS.index(a + "m"), ADXO_BANDS.index(b + "m")
        found |= {x for x in ADXO_BANDS[min(i, j):max(i, j) + 1] if x != "60m"}
    for x in re.findall(r"\b(160|80|60|40|30|20|17|15|12|10|6)\s*m\b", s):
        found.add(x + "m")
    if re.search(r"\bHF\b", s):
        found |= {"80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m"}
    return [b for b in ADXO_BANDS if b in found]


def upcoming(ops, days=92, today=None):
    """今日から約3か月以内に運用中・運用予定のもの（運用中→開始日順）"""
    from datetime import date
    today = today or datetime.now(timezone.utc).date()
    lim = today + timedelta(days=days)
    sel = [o for o in ops if o["end"] >= today and o["start"] <= lim]
    for o in sel:
        o["now"] = o["start"] <= today <= o["end"]
    return sorted(sel, key=lambda o: (not o["now"], o["start"], o["end"]))

def _qsl(q):
    """ADXOのQSL欄。NG3Kの書き方: 「?」= QSLに応じない可能性、「TBA」= 後日発表"""
    q = (q or "").strip()
    if q == "?":
        return "?（応じない可能性）"
    if q.upper() == "TBA":
        return "TBA（後日発表）"
    return _esc(q)

def _ops_rows(ops, compact=False):
    F = FONT
    rows = ""
    for o in ops:
        d = (f'{o["start"]:%m/%d}–{o["end"]:%m/%d}' if o["start"].year == o["end"].year
             else f'{o["start"]:%Y/%m/%d}–{o["end"]:%Y/%m/%d}')
        badges = _badge("運用中", GOOD, True) if o.get("now") else ""
        wb = o.get("worked_bands")
        bs = o.get("bands_status")
        if wb is not None and not wb:
            badges += _badge("全バンド未交信", BAD, True)
        if bs:
            # 緑地に白「確」＝LoTW確認済み、オレンジ地に黒「未」＝交信済み・未確認、枠だけ＝運用予定で未交信
            chips = ""
            for b, st in bs:
                if st == "conf":
                    sty, mark = "background:#1E8E3E;color:#FFF;", " 確"
                elif st == "work":
                    sty, mark = "background:#E8710A;color:#000;", " 未"
                else:
                    sty, mark = f"color:{INK};border:1px solid {SUB};", ""
                chips += f'<span style="display:inline-block;margin:3px 3px 0 0;padding:1px 4px;font-size:11px;font-weight:700;{sty}">{_esc(b)}{mark}</span>'
            badges += f'<div>{chips}</div>'
        elif wb:
            badges += f'<span style="font-size:11px;color:{SUB};">　交信済み: {_esc(" ".join(wb))}</span>'
        call = _esc(o["call"])
        if o.get("url") and "qrz.com" not in o["url"]:
            call = f'<a href="{_esc(o["url"])}" style="color:{NAVY};">{call}</a>'
        info = o["info"] if len(o["info"]) <= 110 else o["info"][:108] + "…"
        rows += (f'<tr><td style="padding:8px 0;border-top:1px solid {RULE};">'
                 f'<div style="font-size:12px;color:{SUB};">{d}{"　QSL: " + _qsl(o["qsl"]) if o["qsl"] else ""}</div>'
                 f'<div style="font-size:15px;font-weight:800;color:{INK};">{_esc(o["entity"])}'
                 f'<span style="font-weight:700;color:{NAVY};">　{call}</span></div>'
                 f'<div>{badges}</div>'
                 f'<div style="font-size:11px;line-height:16px;color:{SUB};padding-top:2px;">{_esc(info)}</div>'
                 f'</td></tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="{F}margin-top:6px;">{rows}</table>'
            f'<div style="{FONT}font-size:11px;line-height:17px;color:{SUB};padding-top:6px;">'
            f'出典: NG3K Announced DX Operations（発表時点の予定。変更・中止があります）<br>'
            f'バンドの札：<span style="background:#1E8E3E;color:#FFFFFF;font-weight:700;padding:0 4px;border-radius:2px;">確</span> LoTWで確認済み　'
            f'<span style="background:#E8710A;color:#000000;font-weight:700;padding:0 4px;border-radius:2px;">未</span> 交信済み・未確認　'
            f'枠だけ＝運用予定で未交信（説明文から推定）</div>')

def ops_text(ops):
    lines = []
    for o in ops:
        tag = "［運用中］" if o.get("now") else ""
        wb = o.get("worked_bands")
        w = "" if wb is None else ("（全バンド未交信）" if not wb else f"（交信済み: {' '.join(wb)}）")
        lines.append(f"{o['start']:%m/%d}-{o['end']:%m/%d} {o['entity']} {o['call']} {tag}{w}")
    return lines


# ---- 試験用データでの動作確認 ----
if __name__ == "__main__":
    import base64, random, sys
    random.seed(7)
    t_rx = datetime(2026, 10, 16, 21, 32, tzinfo=timezone.utc)
    t0 = t_rx.replace(tzinfo=None) - timedelta(hours=24)
    import math
    series = {"speed": [], "bz": [], "kp": []}
    for m in range(0, 24 * 60 + 1, 5):
        h = m / 60
        t = t0 + timedelta(minutes=m)
        series["speed"].append((t, 620 - 190 / (1 + math.exp(-(h - 11) / 2.5)) + random.gauss(0, 8)))
        series["bz"].append((t, -7 * math.exp(-((h - 5) / 4) ** 2) + 3.2 / (1 + math.exp(-(h - 15) / 1.5)) - 0.5 + random.gauss(0, 0.9)))
    for i, k in enumerate([5, 5, 4, 4, 3, 3, 2, 2]):
        series["kp"].append((datetime(2026, 10, 15, 21) + timedelta(hours=3 * i), k))
    wx = wx_from_series(series, sfi=128, s_scale="S0")
    U = timezone.utc
    history = {"40m": [{"call": "VP2MDX", "mode": "FT8", "time_utc": datetime(2023, 3, 12, 22, 5, tzinfo=U)}],
               "20m": [{"call": "VP2MGG", "mode": "CW", "time_utc": datetime(2019, 11, 2, 12, 41, tzinfo=U)}],
               "_guest": [{"call": "VP2MZZ", "mode": "SSB", "band": "15m", "op_call": "JA1ZZZ",
                           "time_utc": datetime(2024, 5, 3, 11, 20, tzinfo=U)}]}
    ev = {"kind": "BANDNEW", "call": "VP2MAA", "entity": "Montserrat", "dxcc": 96, "band": "80m",
          "snr": -22, "time_utc": t_rx, "message": "CQ VP2MAA FK86"}
    msg = build_message(ev, wx, series, history, "notify@example.com", "me@example.com",
                        subject="[試験用データ] " + default_subject(ev))
    with open("demo.eml", "wb") as f:
        f.write(bytes(msg))
    png = make_graph(series, t_rx.replace(tzinfo=None))
    with open("demo.html", "w", encoding="utf-8") as f:
        f.write(render_html(ev, wx, history, "試験用データ",
                            "data:image/png;base64," + base64.b64encode(png).decode()))
    print(f"demo.eml {os.path.getsize('demo.eml')} バイト / demo.html を作成しました")
