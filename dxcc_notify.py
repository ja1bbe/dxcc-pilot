#!/usr/bin/env python3
"""
dxcc_notify.py
WSJT-X の UDP 出力を監視し、バンド別に未交信（または未コンファーム）の
DXCC エンティティがデコードされたらスマートフォンへプッシュ通知する。

構成: SDRconnect -> WSJT-X -> (UDPマルチキャスト) -> JTAlert
                                             +-> 本スクリプト -> Gmail (主) / ntfy (予備) -> スマホ

必要なもの:
  - settings.ini（自局のコールサイン・メールアドレスなど。同じフォルダに置く）
  - common\\cty_latest.dat と common\\cty_latest.csv
    (https://www.country-files.com/ の Big CTY を展開し、cty.dat と cty.csv の名前を変えて置く)
  - 交信ログ: HAMLOG の Hamlog.hdb（--hamlog）、または logs フォルダの ADIF（*.adi）
  - 送信用Gmail (2段階認証を有効化し、アプリパスワードを発行。環境変数 DXCC_MAIL_PASS に登録)
  - 予備にntfyを使う場合はスマホに ntfy アプリを入れトピックを購読

電離層の高さについて:
  NICT (https://wdc.nict.go.jp/Ionosphere/) のイオノゾンデ自動読取値(15分値、国分寺)の
  h'F2（求まらない時間帯はh'Fで代用）とfoF2を、通知に添える(--iono-station off で使わない)。
  NICTへは直接アクセスせず、作者のリポジトリが1時間に1回取得して公開している iono.json を読む。
  出典: 情報通信研究機構（NICT） 電離圏観測データ
  HF-STARTの伝搬経路・出力ガイドは、再配布に関する利用条件があるため未実装（要NICT確認）。
標準ライブラリのみで動作 (Python 3.8+)

HTML版の通知（2026年10月追加）:
  同じフォルダに notify_html.py と hamlog_reader.py を置くと、通知メールをHTML版にする
  （テキスト版も同梱）。グラフには matplotlib が必要（pip install matplotlib）。
  無い場合はグラフなしのHTML、HTMLの作成に失敗した場合は従来のテキスト版で送る。
  --hamlog に HAMLOG の Hamlog.hdb を指定すると、その交信も交信済みに反映する（読み取り専用）。
  --no-html で従来のテキスト版だけに戻せる。
"""
import argparse
import glob
import json
import os
import re
import socket
import struct
import time
import urllib.parse
import urllib.request
import urllib.error
import smtplib
import ssl
import threading
import queue
import select
import gzip
import calendar
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

# HTML版の通知とHAMLOGの読み込み（同じフォルダにあれば使う。無くても従来どおり動く）
try:
    import notify_html
except Exception as _e:
    notify_html = None
    print(f'[HTML] notify_html.py を読み込めないため、テキスト版で通知します: {_e}')
try:
    import hamlog_reader
except Exception:
    hamlog_reader = None
try:
    import dxcc_settings
except Exception:
    dxcc_settings = None

MY_CALL = ''        # 自局の代表のコールサイン（main で settings.ini から設定）


def _ua():
    """データ提供元に、どの局のスクリプトからの取得かを伝える User-Agent"""
    return f'dxcc_notify (amateur radio, {MY_CALL or "unknown station"})'

PILOT_VERSION = '1.0.0'   # dxcc-pilot（公開版）の版。お問い合わせの際にお知らせください

MAGIC = 0xADBCCBDA

# ---------------------------------------------------------------- UDP 解析
class Reader:
    """Qt QDataStream (ビッグエンディアン) の簡易リーダ"""
    def __init__(self, data):
        self.d, self.p = data, 0

    def _u(self, fmt):
        v = struct.unpack_from(fmt, self.d, self.p)[0]
        self.p += struct.calcsize(fmt)
        return v

    def u8(self):   return self._u('>B')
    def bool(self): return self._u('>?')
    def u32(self):  return self._u('>I')
    def i32(self):  return self._u('>i')
    def u64(self):  return self._u('>Q')
    def f64(self):  return self._u('>d')

    def qstr(self):
        n = self.u32()
        if n == 0xFFFFFFFF:
            return None
        s = self.d[self.p:self.p + n].decode('utf-8', 'replace')
        self.p += n
        return s


BANDS = [  # (下限Hz, 上限Hz, 名称)
    (1_800_000, 2_000_000, '160m'), (3_500_000, 4_000_000, '80m'),
    (5_250_000, 5_450_000, '60m'),  (7_000_000, 7_300_000, '40m'),
    (10_100_000, 10_150_000, '30m'), (14_000_000, 14_350_000, '20m'),
    (18_068_000, 18_168_000, '17m'), (21_000_000, 21_450_000, '15m'),
    (24_890_000, 24_990_000, '12m'), (28_000_000, 29_700_000, '10m'),
    (50_000_000, 54_000_000, '6m'),
]

def freq_to_band(hz):
    for lo, hi, name in BANDS:
        if lo <= hz <= hi:
            return name
    return None

# ---------------------------------------------------------------- DXCC 判定
class Cty:
    """cty.dat による最長一致プレフィックス判定。
    '*' 付きエンティティ (WAE専用, DXCC外) は除外するので、
    例えば IT9 は親の Italy に落ちる。"""
    STRIP = re.compile(r'\(.*?\)|\[.*?\]|<.*?>|\{.*?\}|~.*?~')
    SUFFIXES = {'P', 'M', 'QRP', 'A', 'AM', 'B', 'LH'}

    def __init__(self, path):
        self.prefix, self.exact, self.pos = {}, {}, {}
        self.names = set()      # 現存エンティティ（WAE専用を除く）
        with open(path, encoding='latin-1') as f:
            text = f.read()
        for block in text.split(';'):
            parts = block.strip().split(':')
            if len(parts) < 9:
                continue
            name, primary = parts[0].strip(), parts[7].strip()
            if primary.startswith('*'):
                continue
            try:   # 代表位置（cty.dat は西経を正で記載 → 東経に直す）
                self.pos[name] = (float(parts[4]), -float(parts[5]))
            except ValueError:
                pass
            for p in parts[8].split(','):
                p = self.STRIP.sub('', p).strip().upper()
                if not p:
                    continue
                if p.startswith('='):
                    self.exact[p[1:]] = name
                else:
                    self.prefix[p] = name
            self.names.add(name)

    @classmethod
    def normalize(cls, call):
        call = call.upper().strip('<>')
        if '/' not in call:
            return call
        parts = [x for x in call.split('/') if x]
        if 'MM' in parts:          # /MM は DXCC 対象外
            return None
        parts = [x for x in parts if x not in cls.SUFFIXES and not x.isdigit()]
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return min(parts, key=len)  # 短い側を運用地プレフィックスとみなす（簡易）

    def entity(self, call):
        raw = call.upper().strip('<>')
        if raw in self.exact:
            return self.exact[raw]
        c = self.normalize(raw)
        if not c:
            return None
        if c in self.exact:
            return self.exact[c]
        for i in range(len(c), 0, -1):
            e = self.prefix.get(c[:i])
            if e:
                return e
        return None

# ---------------------------------------------------------------- ログ読込
def load_worked(adif_path, cty, confirmed_only, skip=None, skipped=None):
    """skip(fld, band) が True を返す交信（ゲスト運用など）は数えない。skipped には除外件数を数える"""
    with open(adif_path, encoding='utf-8', errors='replace') as f:
        text = f.read()
    m = re.search(r'<eoh>', text, re.I)
    body = text[m.end():] if m else text
    worked = set()
    for rec in re.split(r'<eor>', body, flags=re.I):
        fld = {}
        for t in re.finditer(r'<(\w+):(\d+)(?::\w+)?>', rec):
            fld[t.group(1).upper()] = rec[t.end():t.end() + int(t.group(2))]
        call, band = fld.get('CALL'), fld.get('BAND', '').lower()
        if not call or not band:
            continue
        if confirmed_only:
            ok = (fld.get('QSL_RCVD', '').upper() in ('Y', 'V') or
                  fld.get('LOTW_QSL_RCVD', '').upper() in ('Y', 'V'))
            if not ok:
                continue
        if skip and skip(fld, band):
            if skipped is not None:      # 複数のADIFに同じ交信があっても1件と数える
                skipped.setdefault('keys', set()).add((call, band, fld.get('QSO_DATE', ''), fld.get('TIME_ON', '')[:4]))
            continue
        ent = cty.entity(call)
        if ent:
            worked.add((ent, band))
    return worked


def guest_matcher(hamlog_qsos, my_calls):
    """ADIFの交信がゲスト運用かどうかを判定する関数を返す。
    1) ADIFの STATION_CALLSIGN（送信したコールサイン）が自局でない → ゲスト運用
    2) HAMLOGのゲスト運用の交信と、コールサイン・バンド・日付(前後1日)が一致 → ゲスト運用
       （HAMLOGから書き出したADIFには、ゲスト運用の印が残っていないため）"""
    keys = set()
    for q in hamlog_qsos:
        if q.get('guest') and q.get('band') and q.get('time_utc'):
            d0 = q['time_utc'].date()
            for dd in (-1, 0, 1):
                keys.add((q['call'], q['band'], (d0 + timedelta(days=dd)).strftime('%Y%m%d')))

    def is_guest(fld, band):
        st = (fld.get('STATION_CALLSIGN') or '').upper().split('/')
        st = max(st, key=len) if st and st[0] else ''
        if st and st not in my_calls:
            return True
        return (fld.get('CALL', '').upper(), band, fld.get('QSO_DATE', '')) in keys
    return is_guest

def adif_qsos(adif_path, skip=None):
    """通知メールの「交信記録」欄用に、ADIFの交信を {call, band, mode, time_utc} の形で返す"""
    with open(adif_path, encoding='utf-8', errors='replace') as f:
        return adif_qsos_text(f.read(), skip)


def adif_qsos_text(text, skip=None):
    out = []
    for fld in adif_records(text):
        band = (fld.get('BAND') or '').lower()
        if not band and fld.get('FREQ'):
            try:
                band = freq_to_band(float(fld['FREQ']) * 1e6) or ''
            except ValueError:
                band = ''
        if not band or (skip and skip(fld, band)):
            continue
        ton = (fld.get('TIME_ON', '') + '0000')[:4]
        try:
            ts = datetime.strptime(fld.get('QSO_DATE', '') + ton, '%Y%m%d%H%M').replace(tzinfo=timezone.utc)
        except ValueError:
            ts = None
        out.append({'call': fld['CALL'].upper(), 'band': band, 'mode': fld.get('MODE', '').upper(),
                    'time_utc': ts, 'op_call': None, 'guest': False, 'source': 'ADIF'})
    return out


LOTW_REPORT = 'https://lotw.arrl.org/lotwuser/lotwreport.adi'


def _tls_context():
    """証明書の確認に、Windows と同じ仕組み（truststore）を使う。無ければ certifi、どちらも無ければ Python の既定。
    Windows では、まだ一度も使われていないルート証明書が Python から見えず、
    「CERTIFICATE_VERIFY_FAILED」になることがあるため（確認そのものを省くことはしない）"""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch_lotw_qsl(user, password, cache_path, max_age_h=24):
    """LoTW の確認済み（QSL）の一覧を ADIF で取得する。サーバーの負担を考え、24時間は保存したものを使う。
    取得に失敗したときは、前回保存したものを使う。どちらもなければ空文字"""
    if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < max_age_h * 3600:
        with open(cache_path, encoding='utf-8', errors='replace') as f:
            return f.read()
    if user and password:
        q = urllib.parse.urlencode({'login': user, 'password': password, 'qso_query': 1, 'qso_qsl': 'yes',
                                    'qso_qsldetail': 'yes', 'qso_qslsince': '1900-01-01'})
        try:
            req = urllib.request.Request(f'{LOTW_REPORT}?{q}', headers={'User-Agent': _ua()})
            with urllib.request.urlopen(req, timeout=120, context=_tls_context()) as r:
                text = r.read().decode('utf-8', errors='replace')
            if '<eoh>' not in text.lower():              # パスワード違いなどのときは ADIF ではない画面が返る
                raise ValueError('LoTW から ADIF が返りませんでした（ユーザー名・パスワードを確認してください）')
            with open(cache_path, 'w', encoding='utf-8') as f:
                f.write(text)
            return text
        except Exception as e:
            print(f'[LoTW] 確認状況を取得できませんでした: {e}')
    if os.path.exists(cache_path):
        with open(cache_path, encoding='utf-8', errors='replace') as f:
            return f.read()
    return ''


# 自局で受信した未交信局の通知の件名に付ける印（色で区別。iPhoneの通知やスマートウォッチでも見分けやすい）
SUBJECT_MARK = {'ATNO': '🔴全バンド初', 'BANDNEW': '🟢バンドニュー',
                'ATNO_PED': '🔴🟡全バンド初・ペディション', 'BANDNEW_PED': '🟡🟢バンドニュー・ペディション',
                'PED': '🟡ペディション'}


# ---------------------------------------------------------------- メッセージ解析
# コールサインの形: プレフィックス(英字を含む1〜3文字) + 数字 + サフィックス(英字1〜4文字)
# 「/P」などの付加や「VP2E/K1ABC」形式にも対応。FT8の偽デコード(例: Z64EGXM9SNU)を除外する
_BASE = re.compile(r'^([A-Z0-9]{1,3})([0-9])([A-Z]{1,4})$')


def _is_base_call(tok):
    m = _BASE.match(tok)
    return bool(m) and bool(re.search(r'[A-Z]', m.group(1)))


def _is_call(tok):
    tok = tok.strip('<>')
    if not tok or '...' in tok:
        return False
    parts = tok.split('/')
    if len(parts) > 3 or any(not p or not re.fullmatch(r'[A-Z0-9]{1,8}', p) for p in parts):
        return False
    return any(_is_base_call(p) for p in parts)


class _CallRE:
    """既存コードの CALL_RE.match(x) 呼び出しをそのまま使えるようにする"""
    @staticmethod
    def match(tok):
        return _is_call(tok)


CALL_RE = _CallRE()

def sender_of(msg, cq_only):
    t = msg.upper().split()
    if not t:
        return None
    if t[0] == 'CQ':
        for x in t[1:]:           # "CQ DX K1ABC FN42" の修飾語を読み飛ばす
            if CALL_RE.match(x):
                return None if '...' in x else x.strip('<>')
        return None
    if cq_only or len(t) < 2:
        return None
    x = t[1]                      # "JA1ABC K1ABC -10" の送信局側
    if CALL_RE.match(x) and '...' not in x:
        return x.strip('<>')
    return None

def called_of(msg):
    """「DXCALL JACALL ...」形式から (呼ばれている局, 呼んでいる局) を返す"""
    t = msg.upper().split()
    if len(t) < 2 or t[0] in ('CQ', 'QRZ', 'DE'):
        return None
    a, b = t[0], t[1]
    if not (CALL_RE.match(a) and CALL_RE.match(b)) or '...' in a or '...' in b:
        return None
    return a.strip('<>'), b.strip('<>')


GRID_RE = re.compile(r'^[A-R]{2}[0-9]{2}$')


def grid_of(msg):
    """メッセージ末尾の4桁グリッド（送信局の位置）。RR73 は除外"""
    t = msg.upper().split()
    if len(t) >= 3 and GRID_RE.match(t[-1]) and t[-1] != 'RR73':
        return t[-1]
    return None


def grid_center(g):
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2 + 1
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3]) + 0.5
    return lat, lon


def distance_km(p, q):
    import math
    la1, lo1, la2, lo2 = map(math.radians, (p[0], p[1], q[0], q[1]))
    c = (math.sin(la1) * math.sin(la2) + math.cos(la1) * math.cos(la2) * math.cos(lo1 - lo2))
    return 6371 * math.acos(max(-1.0, min(1.0, c)))


def bearing_deg(p, q):
    """pからqへの大圏方位（真北=0、真東=90、真南=180、真西=270）"""
    import math
    la1, lo1, la2, lo2 = map(math.radians, (p[0], p[1], q[0], q[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def utc_day(t):
    return time.strftime('%Y-%m-%d', time.gmtime(t))


def adif_records(text):
    """ADIFテキストからレコード（フィールド名を大文字にしたdict）の一覧を返す"""
    m = re.search(r'<eoh>', text, re.I)
    body = text[m.end():] if m else text
    out = []
    for rec in re.split(r'<eor>', body, flags=re.I):
        fld = {}
        for t in re.finditer(r'<(\w+):(\d+)(?::\w+)?>', rec):
            fld[t.group(1).upper()] = rec[t.end():t.end() + int(t.group(2))]
        if fld.get('CALL'):
            out.append(fld)
    return out


def load_dxcc_numbers(path):
    """cty.csv から エンティティ名 → ADIFのDXCC番号 の対応表を作る"""
    nums = {}
    with open(path, encoding='latin-1') as f:
        for line in f:
            p = line.split(',')
            if len(p) > 2 and not p[0].startswith('*') and p[2].strip().isdigit():
                nums[p[1].strip()] = p[2].strip()
    return nums


def append_qso(path, fld, band, dxcc):
    """受け取った交信を統合ログに追記する（同じ交信が既にあれば追記しない）"""
    call = fld['CALL'].upper()
    date, ton = fld.get('QSO_DATE', ''), fld.get('TIME_ON', '')[:4]
    try:
        with open(path, encoding='ascii', errors='replace') as f:
            for r in adif_records(f.read()):
                if (r.get('CALL', '').upper(), r.get('QSO_DATE', ''), r.get('TIME_ON', '')[:4],
                        r.get('BAND', '').lower()) == (call, date, ton, band):
                    return 'dup'
    except OSError:
        pass
    rec = {'CALL': call, 'QSO_DATE': date, 'TIME_ON': ton, 'BAND': band,
           'MODE': fld.get('MODE', ''), 'FREQ': fld.get('FREQ', ''),
           'GRIDSQUARE': fld.get('GRIDSQUARE', ''), 'DXCC': dxcc or ''}
    line = ''.join(f'<{k}:{len(v)}>{v}' for k, v in rec.items() if v and v.isascii()) + '<EOR>\r\n'
    for _ in range(5):                      # JTAlert が書き込み中なら少し待って再試行
        try:
            with open(path, 'a', encoding='ascii', newline='') as f:
                f.write(line)
            return 'ok'
        except PermissionError:
            time.sleep(1)
    return 'fail'


# ---------------------------------------------------------------- 宇宙天気（NOAA SWPC）
SWPC = 'https://services.swpc.noaa.gov'
SW_URLS = {
    'kp':     SWPC + '/products/noaa-planetary-k-index.json',
    'mag':    SWPC + '/json/rtsw/rtsw_mag_1m.json',
    'wind':   SWPC + '/json/rtsw/rtsw_wind_1m.json',
    'scales': SWPC + '/products/noaa-scales.json',
    'sfi':    SWPC + '/products/10cm-flux-30-day.json',
}


def _ts(s):
    """'2026-09-27T00:13:00' などをUTCの時刻（秒）に"""
    return datetime.fromisoformat(s.replace('Z', '')[:19]).replace(tzinfo=timezone.utc).timestamp()


def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={'Accept-Encoding': 'gzip',
                                               'User-Agent': _ua()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get('Content-Encoding', '').lower() == 'gzip' or data[:2] == b'\x1f\x8b':
            data = gzip.decompress(data)
    return json.loads(data.decode('utf-8'))


class SpaceWeather:
    """NOAAの宇宙天気を定期的に取得し、通知に添える1行と、好転・悪化の知らせを作る"""
    def __init__(self, a, notifier, site, fetch=fetch_json):
        self.a, self.notifier, self.site, self.fetch = a, notifier, site, fetch
        self.lock = threading.Lock()
        self.v = {}                      # 最新の値
        self.kp_hist = []                # [(時刻, Kp)]
        self.disturbed = False           # Kp 4以上の乱れを経験中か
        self.last_alert = {}             # 種類 -> 送った時刻
        self.last_s = 0
        self.errors = set()
        self.first = True

    # ---- 取得と解析
    def update(self):
        got = {}
        for key, url in SW_URLS.items():
            if key == 'sfi' and self.v.get('sfi_at', 0) > time.time() - 3 * 3600:
                continue                 # 1日1回の値なので、3時間に1回で十分
            try:
                got[key] = self.fetch(url)
                self.errors.discard(key)
            except Exception as e:
                if key not in self.errors:
                    print(f'[宇宙天気] {key} を取得できませんでした: {e}')
                self.errors.add(key)
        with self.lock:
            try:
                if 'kp' in got:
                    self._parse_kp(got['kp'])
                if 'mag' in got:
                    self._parse_rtsw(got['mag'], {'bz': 'bz_gsm', 'bt': 'bt'})
                if 'wind' in got:
                    self._parse_rtsw(got['wind'], {'speed': 'proton_speed', 'density': 'proton_density'})
                if 'scales' in got:
                    self._parse_scales(got['scales'])
                if 'sfi' in got:
                    rows = [r for r in got['sfi'] if isinstance(r, dict) and r.get('flux') is not None]
                    if rows:
                        self.v['sfi'] = float(rows[-1]['flux'])
                        self.v['sfi_at'] = time.time()
            except Exception as e:
                print(f'[宇宙天気] データの形式を読み取れませんでした: {e}')
        self._check_alerts()
        self.first = False

    def _parse_kp(self, data):
        rows = []
        if data and isinstance(data[0], list):             # 旧形式（1行目が見出し）にも対応
            head = data[0]
            data = [dict(zip(head, r)) for r in data[1:]]
        for r in data:
            kp = r.get('Kp', r.get('kp'))
            if kp is not None:
                rows.append((_ts(r['time_tag']), float(kp)))
        rows.sort()
        if rows:
            self.kp_hist = rows[-16:]                        # 直近48時間
            self.v['kp'], self.v['kp_at'] = rows[-1][1], rows[-1][0]

    def _parse_rtsw(self, data, fields):
        act = [r for r in data if isinstance(r, dict) and r.get('active') is True]
        if not act:
            return
        latest_t = max(_ts(r['time_tag']) for r in act)
        for name, col in fields.items():
            vals = [(_ts(r['time_tag']), r.get(col)) for r in act if r.get(col) is not None]
            if not vals:
                continue
            vals.sort()
            self.v[name] = float(vals[-1][1])
            win = [float(x) for t, x in vals if t > latest_t - 1800]
            if win:
                self.v[name + '30'] = sum(win) / len(win)
        self.v['rtsw_at'] = latest_t

    def _parse_scales(self, d):
        cur = d.get('0', {})
        for k in ('S', 'G', 'R'):
            sc = (cur.get(k) or {}).get('Scale')
            if sc not in (None, ''):
                self.v[k] = int(sc)
        prob = ((d.get('1') or {}).get('S') or {}).get('Prob')
        if prob not in (None, ''):
            self.v['S_prob'] = int(prob)

    # ---- 表示
    def line(self):
        with self.lock:
            v = dict(self.v)
        if not v:
            return '宇宙天気: 取得できていません'
        parts = []
        if 'kp' in v:
            parts.append(f"Kp {v['kp']:.1f}")
        if 'speed' in v:
            parts.append(f"太陽風 {v['speed']:.0f}km/s")
        if 'density' in v:
            parts.append(f"密度 {v['density']:.1f}")
        if 'bz' in v:
            parts.append(f"Bz {v['bz']:+.1f}nT（{'南向き' if v['bz'] < 0 else '北向き'}）")
        if 'bt' in v:
            parts.append(f"Bt {v['bt']:.1f}nT")
        if 'S' in v:
            parts.append(f"S{v['S']}")
        at = v.get('rtsw_at') or v.get('kp_at')
        tail = f" [{time.strftime('%H:%M', time.gmtime(at))} UTC]" if at else ''
        return '宇宙天気: ' + ' / '.join(parts) + tail

    def short(self):
        with self.lock:
            v = dict(self.v)
        out = []
        if 'kp' in v:
            out.append(f"Kp{v['kp']:.1f}")
        if 'bz' in v:
            out.append(f"Bz{v['bz']:+.0f}")
        if 'speed' in v:
            out.append(f"V{v['speed']:.0f}")
        return ' '.join(out)

    def start_text(self):
        with self.lock:
            v = dict(self.v)
        extra = []
        if 'sfi' in v:
            extra.append(f"SFI {v['sfi']:.0f}")
        if 'S_prob' in v:
            extra.append(f"プロトン現象の確率（今日） {v['S_prob']}%")
        if 'G' in v:
            extra.append(f"地磁気嵐 G{v['G']}")
        return self.line() + ('\n' + ' / '.join(extra) if extra else '')

    # ---- 好転・悪化の知らせ
    def _alert(self, kind, subject, body, min_interval=3 * 3600):
        now = time.time()
        if now - self.last_alert.get(kind, 0) < min_interval:
            return
        self.last_alert[kind] = now
        print(time.strftime('%H:%M:%S'), subject, '|', body.replace('\n', ' | '))
        self.notifier.send(subject, body)

    def _check_alerts(self):
        with self.lock:
            v = dict(self.v)
            hist = list(self.kp_hist)
        kp, bz30, sp30 = v.get('kp'), v.get('bz30'), v.get('speed30')
        if kp is None:
            return
        # 起動直後は、直近24時間に Kp 4以上があれば「乱れを経験中」とみなす（起動しただけでは知らせない）
        if self.first:
            self.disturbed = any(k >= 4 for t, k in hist if t > time.time() - 86400) and kp > 2
            self.last_s = v.get('S', 0)
            return
        if kp >= 4:
            self.disturbed = True
        # 好転: Kp 4以上 → 2以下、Bz 30分平均が北向き、太陽風 450km/s 未満
        if (self.disturbed and kp <= 2 and bz30 is not None and bz30 > 0
                and sp30 is not None and sp30 < 450):
            self.disturbed = False
            self._alert('better', f'[DXCC] 宇宙天気 好転 {self.site}',
                        '地磁気の乱れが収まり、ローバンドのコンディションが良くなる可能性があります。\n'
                        + self.line(), min_interval=0)
        # 悪化の予告: Bz 30分平均が -10nT より南向き、かつ太陽風 500km/s 以上
        if bz30 is not None and sp30 is not None and bz30 < -10 and sp30 >= 500:
            self._alert('worse', f'[DXCC] 宇宙天気 悪化の予告 {self.site}',
                        '強い南向きのBzと高速の太陽風が続いています。1時間ほどのうちに地磁気が乱れ、'
                        '北米・ヨーロッパ方面（極地方を通る経路）が悪くなる可能性があります。\n' + self.line())
        # プロトン現象: Sスケールが上がった
        s = v.get('S', 0)
        if s >= 1 and s > self.last_s:
            self._alert(f'proton{s}', f'[DXCC] 宇宙天気 プロトン現象 S{s} {self.site}',
                        '太陽プロトン現象が起きています。極地方で電波の吸収（PCA）が強まり、'
                        '北米・ヨーロッパ方面のローバンドは数日悪くなることがあります。\n' + self.line(),
                        min_interval=0)
        self.last_s = s

    def run(self):
        while True:
            time.sleep(self.a.sw_interval)
            try:
                self.update()
            except Exception as e:
                print(f'[宇宙天気] エラー: {e}')


# ---------------------------------------------------------------- 電離層の高さ（NICT イオノゾンデ）
# データ元: https://wdc.nict.go.jp/Ionosphere/ の自動読取15分値（作者のリポジトリ経由で取得）
IONO_JSON_URL = 'https://ja1bbe.github.io/ja1bbe-iono/iono.json'   # NICTの値を1時間に1回取得して公開
IONO_STALE_SEC = 90 * 60   # 観測からこれ以上経った値は、通知に「約○時間前の値」と書き添える
IONO_STATIONS = {   # 局コード: 表示名
    'to536': '国分寺',
}

class Ionosonde:
    """電離層の観測値（NICT 国分寺の15分値）を定期的に読み、電離層の高さ(h'F2)を通知に添える。
    NICTへは直接アクセスせず、作者のリポジトリ（ja1bbe-iono）が1時間に1回取得して
    GitHub Pagesで公開している iono.json を読む。NICTへのアクセスを1か所にまとめ、
    利用者が増えても観測データの提供元に負担をかけないため"""

    def __init__(self, a, notifier, site, station, fetch=None):
        self.a, self.notifier, self.site = a, notifier, site
        self.station = station.lower()
        self.fetch = fetch or self._http_full
        self.source = ''           # iono.json に書かれた出典
        self.lock = threading.Lock()
        self.v = {}
        self.errors = set()
        self.spread = False        # Spread-Fの兆候が出ている状態かどうか
        self.last_alert = 0
        self.first = True          # 起動直後は、既に乱れていても知らせない（起動時に急に鳴るのを防ぐ）

    def _http_full(self):
        req = urllib.request.Request(self.a.iono_url, headers={'User-Agent': _ua(), 'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()

    def update(self):
        """iono.json の最新の観測値を読む（NICTへは直接アクセスしない）"""
        try:
            d = json.loads(self.fetch().decode('utf-8'))
            recent = d.get('recent') or []
        except Exception as e:
            if 'fetch' not in self.errors:
                print(f'[電離層] iono.json を取得できませんでした: {e}\n  試したURL: {self.a.iono_url}')
            self.errors.add('fetch')
            return
        self.errors.discard('fetch')
        if (d.get('station') or '').lower() != self.station:
            if 'station' not in self.errors:
                print(f"[電離層] iono.json の観測局（{d.get('station')}）が、指定（{self.station}）と違います")
            self.errors.add('station')
            return
        self.errors.discard('station')
        self.source = d.get('source') or ''
        # h'F2/h'F/foF2 のどれかに値がある、一番新しい観測
        rec = next((r for r in reversed(recent)
                    if any(r.get(k) is not None for k in ('hF2', 'hF', 'foF2'))), None)
        if rec is None:
            if 'empty' not in self.errors:
                print('[電離層] iono.json に使える観測値がありません（直近6時間）')
            self.errors.add('empty')
            with self.lock:
                self.v = {}
            return
        self.errors.discard('empty')
        with self.lock:
            self.v = {}
            if rec.get('hF2') is not None:
                self.v['h'], self.v['h_kind'] = float(rec['hF2']), "h'F2"
            elif rec.get('hF') is not None:                # 夜間などh'F2が求まらない時間帯はh'Fで代用
                self.v['h'], self.v['h_kind'] = float(rec['hF']), "h'F"
            if rec.get('foF2') is not None:
                self.v['fof2'] = float(rec['foF2'])
            try:
                self.v['at'] = calendar.timegm(time.strptime(rec['time'], '%Y-%m-%dT%H:%M:%SZ'))
            except (KeyError, ValueError):
                self.v['at'] = None
        self._check_spread(bool(rec.get('spreadF')))

    def _check_spread(self, spread_now):
        if self.first:
            self.spread = spread_now
            self.first = False
            return
        name = IONO_STATIONS.get(self.station, self.station)
        if spread_now and not self.spread:
            self._alert(f'[DXCC] 電離層 乱れの兆候(Spread-F) {self.site}',
                        f'{name}上空の電離層観測で、周波数方向の乱れ（Spread-F）の兆候が見られます。\n'
                        '電離層が不安定になっている可能性があります（コンディションが悪化することも、'
                        '逆に飛びやすくなることもあります）。\n'
                        'これはあくまで観測点上空の状態で、交信したい方向の経路上の状態を'
                        '直接示すものではありません。')
        elif not spread_now and self.spread:
            self._alert(f'[DXCC] 電離層 乱れが収まった様子 {self.site}',
                        f'{name}上空の電離層観測で、周波数方向の乱れ（Spread-F）の兆候が収まりました。')
        self.spread = spread_now

    def _alert(self, subject, body, min_interval=1800):
        now = time.time()
        if now - self.last_alert < min_interval:
            return
        self.last_alert = now
        print(time.strftime('%H:%M:%S'), subject, '|', body.replace('\n', ' | '))
        self.notifier.send(subject, body)

    def line(self):
        with self.lock:
            v = dict(self.v)
        if 'h' not in v and 'fof2' not in v:
            return ''
        parts = []
        if 'h' in v:
            parts.append(f"{v['h_kind']} {v['h']:.0f}km")
        if 'fof2' in v:
            parts.append(f"foF2 {v['fof2']:.1f}MHz")
        at = v.get('at')
        tail = ''
        if at:
            g = time.gmtime(at)
            # NICTの公開ファイルは更新に時間差があるため、日付を付けて「いつの値か」を明示する
            tail = f" [{g.tm_mon}/{g.tm_mday} {time.strftime('%H:%M', g)} UTC]"
            age = time.time() - at
            if age >= IONO_STALE_SEC:
                tail += f'（約{age/60:.0f}分前の値）' if age < 3 * 3600 else f'（約{age/3600:.0f}時間前の値）'
        name = IONO_STATIONS.get(self.station, self.station)
        return f'電離層({name}): ' + ' / '.join(parts) + tail

    def short(self):
        """セッション記録の1行に添える簡潔な表記。電離層は15分毎の値なので、
        いつ時点の値かを示す測定日時（データ自体の時刻。取得・受信した時刻ではない）を必ず添える"""
        with self.lock:
            v = dict(self.v)
        if 'h' not in v and 'fof2' not in v:
            return ''
        parts = []
        if 'h' in v:
            parts.append(f"{v['h_kind']}{v['h']:.0f}km")
        if 'fof2' in v:
            parts.append(f"foF2{v['fof2']:.1f}")
        at = v.get('at')
        if at:
            g = time.gmtime(at)
            when = f"{g.tm_mon}/{g.tm_mday} {time.strftime('%H:%M', g)}"
        else:
            when = '--:--'
        return ' '.join(parts) + f'({when})'

    def run(self):
        while True:
            try:
                self.update()
            except Exception as e:
                print(f'[電離層] エラー: {e}')
            time.sleep(self.a.iono_interval)


# ---------------------------------------------------------------- 通知
def notify_ntfy(url, title, body, priority='high'):
    req = urllib.request.Request(url, data=body.encode('utf-8'), method='POST')
    req.add_header('Title', title)       # HTTPヘッダはASCIIのみ
    req.add_header('Priority', priority)
    req.add_header('Tags', 'satellite_antenna')
    urllib.request.urlopen(req, timeout=10).read()


def notify_gmail(user, password, to, subject, body, msg=None):
    if msg is None:                      # 従来のテキスト版
        msg = EmailMessage()
        msg['From'] = user
        msg['To'] = to
        msg['Subject'] = subject
        msg.set_content(body)
    # 回線が不安定なときや、Gmail が一時的に接続を切ったときに備え、
    # SSL（465番）と STARTTLS（587番）を順に、間をあけて試す。待ち時間は大きめ（画像入りのメールは大きいため）
    ctx = ssl.create_default_context()
    last = None
    for attempt in range(2):
        for name, port in (('SSL 465番', 465), ('STARTTLS 587番', 587)):
            try:
                if port == 465:
                    s = smtplib.SMTP_SSL('smtp.gmail.com', 465, context=ctx, timeout=60)
                else:
                    s = smtplib.SMTP('smtp.gmail.com', 587, timeout=60)
                    s.starttls(context=ctx)
                with s:
                    s.login(user, password)
                    s.send_message(msg)
                if attempt or port != 465:
                    print(f'[gmail] {name} で送信しました（{attempt + 1}回目）')
                return
            except smtplib.SMTPAuthenticationError:
                raise                         # パスワード違いは、何度試しても同じ
            except (smtplib.SMTPException, OSError) as e:
                last = e
                print(f'[gmail] {name} で送れませんでした: {type(e).__name__}: {e}')
                time.sleep(5)
    raise last


class Notifier:
    """送信は別スレッドで行う。SMTPの数秒の待ちでUDPパケットを取りこぼさないため。"""
    def __init__(self, a):
        self.a = a
        self.password = None
        if a.mail_to:
            self.password = os.environ.get('DXCC_MAIL_PASS')
            if not self.password and a.pass_file:
                with open(a.pass_file, encoding='utf-8') as f:
                    self.password = f.read().strip()
            if not self.password:
                raise SystemExit('Gmailのアプリパスワードが見つかりません '
                                 '(環境変数 DXCC_MAIL_PASS または --pass-file)')
        self.q = queue.Queue()
        self.sent = []          # 送信時刻（1時間あたりの上限管理）
        self.rich_builder = None  # HTML版を作る関数（main で設定）。None ならテキスト版
        threading.Thread(target=self._worker, daemon=True).start()

    def send(self, subject, body, rich=None):
        now = time.time()
        self.sent = [t for t in self.sent if now - t < 3600]
        if len(self.sent) >= self.a.max_per_hour:
            print('[notify] 1時間あたりの上限に達したため送信を見送り')
            return
        self.sent.append(now)
        self.q.put((subject, body, rich))

    def _worker(self):
        while True:
            subject, body, rich = self.q.get()
            if self.a.mail_to:
                msg = None
                if rich is not None and self.rich_builder:
                    try:                 # HTML版の作成（グラフの取得を含む）。失敗したらテキスト版
                        msg = self.rich_builder(subject, body, rich)
                    except Exception as e:
                        print(f'[HTML] HTML版を作れなかったため、テキスト版で送ります: {e}')
                        msg = None
                try:
                    notify_gmail(self.a.mail_from, self.password, self.a.mail_to, subject, body, msg)
                except Exception as e:
                    print(f'[gmail error] {e}')
                    if msg is not None:  # HTML版で送れなかった場合は、テキスト版でもう一度
                        try:
                            notify_gmail(self.a.mail_from, self.password, self.a.mail_to, subject, body)
                            print('[gmail] テキスト版で送り直しました')
                        except Exception as e2:
                            print(f'[gmail error] {e2}')
            if self.a.ntfy:
                try:
                    notify_ntfy(self.a.ntfy, subject.encode('ascii', 'ignore').decode() or 'DXCC alert', body)
                except Exception as e:
                    print(f'[ntfy error] {e}')


# ---------------------------------------------------------------- メイン
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--site', default=os.environ.get('COMPUTERNAME', 'PC'),
                    help='このPCのID（例: home）。メールの件名には settings.ini の [sites] で対応させた名前が入る')
    ap.add_argument('--cty', help='cty.dat のパス（既定: スクリプトと同じフォルダの common\\cty_latest.dat）')
    ap.add_argument('--adif', nargs='*',
                    help='交信ログADIF（既定: スクリプトと同じフォルダの logs の中の *.adi をすべて）')
    ap.add_argument('--my-call', help='自局のコールサイン（カンマ区切りで複数可）。既定は settings.ini の my_call')
    ap.add_argument('--mail-from', help='送信用Gmailアドレス（既定は settings.ini の mail_from）')
    ap.add_argument('--mail-to', help='通知の宛先（スマホで受信するアドレス）')
    ap.add_argument('--pass-file', help='アプリパスワードを1行で書いたファイル')
    ap.add_argument('--ntfy', help='予備: https://ntfy.sh/推測されにくいトピック名')
    ap.add_argument('--max-per-hour', type=int, default=20, help='通知の上限（件/時）')
    ap.add_argument('--group', default='239.255.0.1', help='マルチキャストアドレス')
    ap.add_argument('--port', type=int, default=2237)
    ap.add_argument('--bands', default='160m,80m,60m,40m,30m,20m,17m,15m,12m,10m,6m', help='通知対象バンド')
    ap.add_argument('--confirmed', action='store_true',
                    help='コンファーム済みのみ「済」扱い (Honor Roll向け)')
    ap.add_argument('--cq-only', action='store_true', help='CQ局のみ通知')
    ap.add_argument('--confirm', type=int, default=2,
                    help='同じ局をこの回数デコードしてから通知（偽デコード対策、既定2。1で即時通知）')
    ap.add_argument('--confirm-window', type=int, default=180, help='--confirm の回数を数える時間（秒）')
    ap.add_argument('--atno-confirm', type=int, default=1,
                    help='全バンド初（どのバンドでも未交信）のエンティティは、この回数のデコードで通知（既定1＝最初のデコードで通知）')
    ap.add_argument('--grid-km', type=int, default=6000,
                    help='グリッドとエンティティの位置がこの距離以上離れていたら「要確認」を付ける')
    ap.add_argument('--my-grid',
                    help='自局のグリッドロケータ（例: PM85。既定は settings.ini の my_grid）。指定すると、通知にローテーターの目安方位（真北=0、'
                         '真東=90、真南=180、真西=270）を添える')
    ap.add_argument('--log-port', type=int, default=0,
                    help='別PCのWSJT-X（セカンドUDPサーバー）から交信記録を受け取るポート。0で無効')
    ap.add_argument('--log-append', help='受け取った交信を追記するADIF（既定: --adif の最初のファイル）。none で追記しない')
    ap.add_argument('--cty-csv', help='DXCC番号の付与に使う cty.csv（既定: スクリプトと同じフォルダの cty_latest.csv）')
    ap.add_argument('--no-space-weather', action='store_true', help='宇宙天気（NOAA）を使わない')
    ap.add_argument('--hamlog', help='HAMLOGのデータファイル Hamlog.hdb のパス。指定するとその交信も交信済みに反映'
                                     '（読み取り専用。ゲスト運用の交信は交信済みに数えない）')
    ap.add_argument('--no-html', action='store_true', help='HTML版を使わず、従来のテキスト版だけで通知する')
    ap.add_argument('--lotw-user', help='LoTW の Web サイトのユーザー名（既定は settings.ini の lotw_user）。パスワードは環境変数 LOTW_PASS。'
                                        '指定すると確認済み（QSL）を1日1回取得し、交信数と確認数を分けて表示する')
    ap.add_argument('--sw-interval', type=int, default=900, help='宇宙天気を取りに行く間隔（秒、既定15分）')
    ap.add_argument('--iono-station', choices=['to536', 'off'], default='to536',
                    help='電離層の高さ(NICT)に使う観測局: to536=国分寺(既定) off=使わない')
    ap.add_argument('--iono-url', default=IONO_JSON_URL,
                    help='電離層の観測値（iono.json）の公開URL')
    ap.add_argument('--iono-interval', type=int, default=1800,
                    help='iono.json を読みに行く間隔（秒、既定30分。iono.json の更新は1時間に1回）')
    ap.add_argument('--called', choices=['off', 'ja', 'any'], default='ja',
                    help='他局が呼んでいる相手局も判定: ja=呼んでいるのが日本の局のときのみ(既定), any=誰でも, off=しない')
    ap.add_argument('--min-snr', type=int, default=-30)
    ap.add_argument('--debug-udp', action='store_true',
                    help='WSJT-Xからのデコードを受信するたびに画面に表示する（受信できているかの確認用）')
    ap.add_argument('--repeat', choices=['utcday', 'cooldown'], default='utcday',
                    help='同じ局の再通知: utcday=UTCの1日に1回(既定), cooldown=--cooldown 秒ごと')
    ap.add_argument('--cooldown', type=int, default=600, help='--repeat cooldown のときの再通知抑止秒')
    ap.add_argument('--state', help='通知済み記録ファイル（既定: sessions\\state_<PCの名前>.json）')
    ap.add_argument('--test', action='store_true', help='テスト通知を送って終了')
    # 局ごとの値は settings.ini から読み、コマンドラインで指定したものを優先する
    conf = dxcc_settings.load() if dxcc_settings else {'station': {}, 'sites': {}}
    st = conf['station']
    ap.set_defaults(**{k: st.get(k) or None for k in ('my_call', 'mail_from', 'mail_to', 'my_grid', 'lotw_user')})
    a = ap.parse_args()
    base = os.path.dirname(os.path.abspath(__file__))
    my_calls = {c.strip().upper() for c in (a.my_call or '').split(',') if c.strip()}
    if not my_calls or any('＜' in c for c in my_calls):
        raise SystemExit('自局のコールサインが設定されていません（settings.ini の my_call、または --my-call）')
    global MY_CALL
    MY_CALL = [c.strip().upper() for c in a.my_call.split(',') if c.strip()][0]   # 最初に書いたコール
    if hamlog_reader:
        hamlog_reader.set_my_calls(my_calls)
    if notify_html:
        notify_html.MY_CALL = MY_CALL
    if a.mail_to and (not a.mail_from or '＜' in a.mail_from or '＜' in a.mail_to):
        raise SystemExit('送信用・受信用のメールアドレスが設定されていません（settings.ini の mail_from, mail_to）')
    # batファイルに日本語を書くと文字化けしやすいので、--site には英数字のIDを書き、表示名は settings.ini で対応させる
    SITE_NAMES = conf['sites']
    print(f'dxcc-pilot 版 {PILOT_VERSION}')
    print(f'自局のコールサイン: {", ".join(sorted(my_calls))}')
    site_id = a.site
    a.site = SITE_NAMES.get(site_id.lower(), site_id)
    a.cty = a.cty or os.path.join(base, 'common', 'cty_latest.dat')
    my_pos = None
    if a.my_grid:
        g = a.my_grid.strip().upper()
        if re.fullmatch(r'[A-R]{2}\d{2}([A-X]{2})?', g):
            my_pos = grid_center(g)
        else:
            print(f'[注意] --my-grid の形式が不正なため無視します: {a.my_grid}')
    sess_dir = os.path.join(base, 'sessions')
    os.makedirs(sess_dir, exist_ok=True)

    def log_files():
        return a.adif if a.adif else sorted(glob.glob(os.path.join(base, 'logs', '*.adi')))

    if not a.mail_to and not a.ntfy:
        raise SystemExit('--mail-to か --ntfy の少なくとも一方を指定してください')
    notifier = Notifier(a)
    if a.test:
        notifier.send('[DXCC] テスト通知', 'dxcc_notify.py からのテスト通知です。')
        time.sleep(30)          # 送信スレッドの完了を待つ
        return

    targets = set(a.bands.lower().split(','))
    cty = Cty(a.cty)
    worked, mtimes = set(), None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', a.port))
    if ipaddr_is_multicast(a.group):
        # WSJT-X が送信に使うインターフェース（ループバック/LAN）のどちらでも受信できるよう両方で参加
        joined = 0
        for iface in ('127.0.0.1', '0.0.0.0'):
            try:
                mreq = struct.pack('4s4s', socket.inet_aton(a.group), socket.inet_aton(iface))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined += 1
            except OSError as e:
                print(f'[multicast] {iface} で参加できませんでした: {e}')
        if not joined:
            raise SystemExit('マルチキャストグループに参加できませんでした')
    print(f'listening {a.group}:{a.port}  bands={sorted(targets)}')

    # 別PCのWSJT-Xからの交信記録の受信口
    lsock, remote_worked = None, set()
    if a.log_port:
        lsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        lsock.bind(('', a.log_port))
        print(f'QSO log receiver on UDP port {a.log_port}')
    append_path = None if (a.log_append or '').lower() == 'none' else (a.log_append or (log_files() or [None])[0])
    csv_path = a.cty_csv or os.path.join(base, 'common', 'cty_latest.csv')
    dxcc_nums = {}
    if os.path.exists(csv_path):          # 交信の追記と、HTML版の「DXCC #番号」表示に使う
        dxcc_nums = load_dxcc_numbers(csv_path)
    elif lsock:
        print(f'[注意] {csv_path} がないため、追記する交信にDXCC番号を付けられません（JTAlertのRebuildに反映されません）')

    bands, last_sent, last_check, seen = {}, {}, 0, {}   # bands: WSJT-Xの名前(id)ごとのバンド

    # 通知済みの記録（UTCの日付ごと）。PCを再起動しても同じ日に再通知しないようファイルに保存
    state_path = a.state or os.path.join(sess_dir, f'state_{site_id}.json')
    sent_day = {}
    try:
        with open(state_path, encoding='utf-8') as f:
            today = utc_day(time.time())
            sent_day = {tuple(k.split('|')): v for k, v in json.load(f).items() if v == today}
    except (OSError, ValueError):
        pass

    def already_sent(key, now):
        if a.repeat == 'utcday':
            return sent_day.get(key) == utc_day(now)
        return now - last_sent.get(key, 0) < a.cooldown

    def mark_sent(key, now):
        last_sent[key] = now
        today = utc_day(now)
        sent_day[key] = today
        for k in [k for k, v in sent_day.items() if v != today]:   # 前日以前は削除
            del sent_day[k]
        try:
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump({'|'.join(k): v for k, v in sent_day.items()}, f)
        except OSError as e:
            print(f'[state] 保存できませんでした: {e}')
    # ---------------- ログの読み込み（起動時と、ファイルが更新されたとき）
    history = {}            # エンティティ -> {バンド: [交信], '_guest': [ゲスト運用の交信]}（HTML版の交信記録欄）
    remote_hist = []        # 別PCから受け取った交信

    confirmed = set()       # LoTW で確認済みの（エンティティ, バンド）
    lotw_state = {'n': 0, 't': 0}

    def load_confirmed():
        """LoTW の確認済みを読み込む（取得は1日1回。--lotw-user がなければ、保存済みのファイルがあれば使う）"""
        nonlocal confirmed
        cache = os.path.join(base, 'logs', 'lotw_qsl.adi.txt')   # logs の *.adi（交信ログ）と区別する名前
        if not a.lotw_user and not os.path.exists(cache):
            return
        if a.lotw_user and not os.environ.get('LOTW_PASS') and not os.path.exists(cache):
            print('[LoTW] 環境変数 LOTW_PASS（LoTW の Web サイトのパスワード）が設定されていないため、確認状況を取得できません')
            return
        text = fetch_lotw_qsl(a.lotw_user, os.environ.get('LOTW_PASS', ''), cache)
        conf, n = set(), 0
        for fld in adif_records(text):
            if (fld.get('QSL_RCVD') or '').upper() != 'Y':
                continue
            band = (fld.get('BAND') or '').lower()
            ent = cty.entity(fld.get('CALL', ''))
            if ent and band:
                conf.add((ent, band))
                n += 1
        confirmed, lotw_state['n'], lotw_state['t'] = conf, n, time.time()
        if n:
            print(f'LoTW: 確認済み（QSL） {n}件、エンティティ×バンド {len(conf)}組')
        else:
            print('[LoTW] 確認済み（QSL）を1件も読み取れませんでした')

    def reload_logs(force=False):
        nonlocal worked, mtimes, history
        files = log_files()
        watch = files + ([a.hamlog] if a.hamlog else [])
        m = tuple((p, os.path.getmtime(p)) for p in watch if os.path.exists(p))
        if not force and m == mtimes:
            return
        worked, mtimes = set(remote_worked), m
        info, adif_list = [], []
        # HAMLOGを先に読み、ゲスト運用の交信を把握する（ADIF側の同じ交信を除外するため）
        hl, skip = [], None
        if a.hamlog and hamlog_reader and os.path.exists(a.hamlog):
            try:
                hl, warn_h = hamlog_reader.read_hamlog(a.hamlog)
            except Exception as e:
                print(f'[HAMLOG] 読み込めませんでした: {e}')
                hl = []
        elif a.hamlog:
            print(f'[HAMLOG] 読み込めません（ファイルまたは hamlog_reader.py がありません）: {a.hamlog}')
        if not (hamlog_reader and hamlog_reader.COUNT_GUEST):
            skip = guest_matcher(hl, {c.split('/')[0] for c in my_calls})   # JA1ABC/1 も自局
        skipped = {}
        for p in files:
            if not os.path.exists(p):
                continue
            w = load_worked(p, cty, a.confirmed, skip, skipped)
            worked |= w
            info.append(f'{os.path.basename(p)}: {len(w)} pairs')
            print(f'log loaded: {info[-1]}')
            try:
                adif_list += adif_qsos(p, skip)
            except OSError as e:
                print(f'[交信記録] {p} を読めませんでした: {e}')
        if not files:
            print('[注意] 交信ログが見つかりません:', os.path.join(base, 'logs'))
        if skipped.get('keys'):
            info.append(f'ADIFのうちゲスト運用の交信 {len(skipped["keys"])}件（ファイル間の重複を除く）は交信済みに数えない')
            print(f'log loaded: {info[-1]}')
        if hl:
            own = [q for q in hl if hamlog_reader.is_own(q)]
            n_before = len(worked)
            if not a.confirmed:          # HAMLOGにはQSL受領の記録がないため、--confirmed では使わない
                for q in own:
                    ent = cty.entity(q['call'])
                    if ent and q['band']:
                        worked.add((ent, q['band']))
            info.append(f'HAMLOG: 自局{len(own)}件（ゲスト運用{len(hl) - len(own)}件は交信済みに数えない）'
                        f'、新たに加わった組み合わせ {len(worked) - n_before}')
            print(f'log loaded: {info[-1]}')
        # 交信記録欄: HAMLOGとADIFを合わせ、同じ交信は1件にする
        allq = (hamlog_reader.merge(hl, adif_list + remote_hist) if hamlog_reader
                else adif_list + remote_hist)
        hist = {}
        for q in allq:
            ent = cty.entity(q['call'])
            if not ent:
                continue
            guest = q.get('guest') and not (hamlog_reader and hamlog_reader.COUNT_GUEST)
            key = '_guest' if guest else q.get('band')
            if key:
                hist.setdefault(ent, {}).setdefault(key, []).append(q)
        early = datetime.min.replace(tzinfo=timezone.utc)
        for bands_ in hist.values():
            for lst in bands_.values():
                lst.sort(key=lambda q: q['time_utc'] or early)
        history = hist
        print(f'log total: {len(worked)} entity-band pairs')
        return info

    log_info = reload_logs(force=True) or []
    load_confirmed()
    if lotw_state['n']:
        log_info = log_info + [f"LoTW: 確認済み（QSL） {lotw_state['n']:,}件"]

    # 宇宙天気: 最初の1回はその場で取得し、以後は別スレッドで定期的に取得する
    sw = None
    if not a.no_space_weather:
        sw = SpaceWeather(a, notifier, a.site)
        print('宇宙天気を取得しています…')
        sw.update()
        print(sw.line())
        threading.Thread(target=sw.run, daemon=True).start()

    # 電離層の高さ（NICT）: 同様に、最初の1回はその場で取得し、以後は別スレッドで定期的に取得する
    iono = None
    if a.iono_station != 'off':
        iono = Ionosonde(a, notifier, a.site, a.iono_station)
        print('電離層データを取得しています…')
        iono.update()
        print(iono.line() or '（まだ値が得られていません）')
        threading.Thread(target=iono.run, daemon=True).start()

    def extra_lines():
        out = []
        if sw:
            out.append(sw.line())
        if iono and iono.line():
            out.append(iono.line())
        return ('\n' + '\n'.join(out)) if out else ''
    swline = extra_lines
    last_check = time.time()

    # ---------------- HTML版の通知
    html_on = bool(a.mail_to and not a.no_html and notify_html)
    if html_on:
        try:
            import matplotlib  # noqa: F401  グラフに使う（無ければグラフなしのHTML）
        except ImportError:
            print('[HTML] matplotlib が無いため、グラフなしのHTML版で通知します（pip install matplotlib）')

    def current_wx():
        """HTML版に載せる宇宙天気・電離層の値と、グラフ用の時系列"""
        series = None
        if sw:
            try:
                series = notify_html.fetch_series(fetch=fetch_json)
            except Exception as e:
                print(f'[HTML] グラフ用の宇宙天気データを取得できませんでした: {e}')
            with sw.lock:
                v, kh = dict(sw.v), list(sw.kp_hist)
        else:
            v, kh = {}, []
        wx = notify_html.wx_from_sw(v, kh, series)
        if iono:
            with iono.lock:
                iv = dict(iono.v)
            name = IONO_STATIONS.get(iono.station, iono.station)
            when = time.strftime('%H:%M UTC', time.gmtime(iv['at'])) if iv.get('at') else ''
            if iv.get('at') and time.time() - iv['at'] > 3 * 3600:     # 古い値は経過時間を添える
                when += f"（約{(time.time() - iv['at']) / 3600:.0f}時間前）"
            rows = []
            if 'h' in iv:
                rows.append((f"{iv['h_kind']}（{name}）", f"{iv['h']:.0f} km", when))
            if 'fof2' in iv:
                rows.append((f'foF2（{name}）', f"{iv['fof2']:.1f} MHz", when))
            wx['iono_rows'] = rows
        return wx, series, v

    def dx_ops():
        """DXペディションの予定（3か月以内）に、エンティティと交信済みバンドを付けて返す"""
        try:
            ops = notify_html.upcoming(notify_html.load_adxo())
        except Exception as e:
            print(f'[DXペディション] 予定を取得できませんでした: {e}')
            return []
        for o in ops:
            ent = cty.entity(o['call']) if o['call'] and o['call'] not in ('TBA', '?') else None
            o['_ent'] = ent
            wb = sorted({b for (e, b) in worked if e == ent}, key=lambda b: band_order.get(b, 99)) if ent else []
            o['worked_bands'] = ([b + (('(確)' if (ent, b) in confirmed else '(未)') if confirmed else '') for b in wb]
                                 if ent else None)
            # バンドごとの札: conf=LoTW確認済み、work=交信済み・未確認、plan=運用予定で未交信
            plan = [b for b in (o.get('bands') or notify_html.bands_of(o.get('info'))) if b in band_order and b not in wb]
            o['bands_status'] = ([(b, 'conf' if (ent, b) in confirmed else 'work') for b in wb]
                                 + [(b, 'plan') for b in plan]) if ent else None
        return ops

    ped_cache = {'t': 0, 'calls': set(), 'bands': {}}

    def active_ped_calls():
        """ADXOで今運用中のDXペディションのコールサイン（ADXOは12時間ごとに取得。一覧は10分ごとに作り直す）"""
        if not notify_html:
            return set()
        if time.time() - ped_cache['t'] > 600:
            try:
                now_ops = [o for o in notify_html.upcoming(notify_html.load_adxo())
                           if o.get('now') and o.get('call')]
                ped_cache['calls'] = {o['call'].upper() for o in now_ops}
                ped_cache['bands'] = {o['call'].upper(): (o.get('bands') or notify_html.bands_of(o.get('info'))) for o in now_ops}
            except Exception:
                ped_cache['calls'], ped_cache['bands'] = set(), {}
            ped_cache['t'] = time.time()
        return ped_cache['calls']

    def rich_builder(subject, body, ev):
        wx, series, _ = current_wx()
        ev = {**ev, 'dx_ops': [o for o in dx_ops() if o['_ent'] == ev['entity']],
              'confirmed_bands': sorted(b for (e, b) in confirmed if e == ev['entity']),
              'lotw': bool(confirmed)}
        label = ('HAMLOG＋交信ログ（ADIF）の記録' if a.hamlog else '交信ログ（ADIF）の記録')
        return notify_html.build_message(ev, wx, series, history.get(ev['entity'], {}),
                                         a.mail_from, a.mail_to, subject=subject,
                                         source_label=label, text=body)

    if html_on:
        notifier.rich_builder = rich_builder

    # ---------------- この運用で自局が直接受信した未交信局の記録
    t_start = time.time()
    start_worked = set(worked)                     # 開始時点の交信済み（終了時に「叶った交信」を求めるため）
    start_ents = {e for e, _ in start_worked}
    t_start_min = t_start // 60 * 60               # ログの時刻は分単位なので、開始した分の頭から数える

    def achieved():
        """この運用中に新たに交信できたエンティティ×バンドと、その交信（開始後の自局の交信があるものだけ）"""
        out = []
        for ent, band in sorted(worked - start_worked, key=lambda x: (band_order.get(x[1], 99), x[0])):
            qs = [q for q in history.get(ent, {}).get(band, [])
                  if q.get('time_utc') and q['time_utc'].timestamp() >= t_start_min]
            if not qs:
                continue                           # 交信ではなく、ログの修正などで増えたもの
            q = qs[-1]
            out.append({'ent': ent, 'band': band, 'call': q['call'], 'mode': q.get('mode', ''),
                        't': q['time_utc'].timestamp(), 'atno': ent not in start_ents})
        return out

    def session_qso_count():
        """この運用中の自局の交信の件数（ゲスト運用を除く）"""
        n = 0
        for bands_ in history.values():
            for b, lst in bands_.items():
                if b != '_guest':
                    n += sum(1 for q in lst if q.get('time_utc') and q['time_utc'].timestamp() >= t_start_min)
        return n
    stamp = time.strftime('%Y%m%d_%H%M', time.gmtime(t_start))
    sess_path = os.path.join(sess_dir, f'{site_id}_{stamp}UTC.txt')
    sess = {}                      # (call, band) -> 記録
    sess_dirty, sess_saved = False, 0
    n_notified = [0]
    band_order = {b: i for i, b in enumerate(['160m', '80m', '60m', '40m', '30m', '20m', '17m', '15m',
                                              '12m', '10m', '6m'])}

    def hm(t):
        return time.strftime('%H:%M', time.gmtime(t))

    def session_text(final=False):
        rows = [r for r in sess.values() if r['count'] >= a.confirm or r['notified']]
        rows.sort(key=lambda r: (band_order.get(r['band'], 99), r['first']))
        t_end = time.time()
        lines = []
        if final:
            got = achieved()
            if got:
                lines += [f'この運用中に、新たに{len(got)}件のDXCC（エンティティ×バンド）と交信できました。',
                          'バンド  コール        モード  交信(UTC)  エンティティ']
                for g in got:
                    lines.append(f"{g['band']:<6}  {g['call']:<12}  {g['mode']:<6}  {hm(g['t'])}      "
                                 f"{g['ent'][:26]}" + ('　★全バンド初' if g['atno'] else ''))
                lines += ['', '-' * 60, '']
            lines.append(f'この運用中の交信: {session_qso_count()}件')
        lines += [f'PC: {a.site}',
                 f'運用時間: {time.strftime("%Y-%m-%d %H:%M", time.gmtime(t_start))} 〜 '
                 f'{time.strftime("%Y-%m-%d %H:%M", time.gmtime(t_end))} UTC'
                 f'（{(t_end - t_start) / 3600:.1f}時間）' + ('' if final else '（運用中）'),
                 f'送った通知: {n_notified[0]}通',
                 '',
                 f'自局の設備で直接受信した未交信局: {len(rows)}局（{a.confirm}回以上デコードした局）',
                 'バンド  コール        エンティティ                受信(UTC)      回数  最大SNR  '
                 '宇宙天気(初受信時)    電離層(初受信時・測定日時はUTC)  備考']
        for r in rows:
            note = []
            if r['notified']:
                note.append('通知済み')
            if (r['ent'], r['band']) in worked:
                note.append('この運用中に交信')
            if r['warn']:
                note.append('要確認(グリッド)')
            lines.append(f"{r['band']:<6}  {r['call']:<12}  {r['ent'][:26]:<26}  "
                         f"{hm(r['first'])}-{hm(r['last'])}  {r['count']:>4}  {r['best']:>+5d}dB  "
                         f"{r.get('sw', ''):<18}  {r.get('iono', ''):<28}  {'、'.join(note)}")
        if not rows:
            lines.append('（なし）')
        if final:
            tail = []
            if sw:
                tail.append('終了時の' + sw.line())
            if iono and iono.line():
                tail.append(iono.line())
            if tail:
                lines += [''] + tail
        return '\n'.join(lines)

    def session_info(final=False):
        """終了のHTML版に載せる内容（session_text と同じ集計）"""
        rows = [r for r in sess.values() if r['count'] >= a.confirm or r['notified']]
        rows.sort(key=lambda r: (band_order.get(r['band'], 99), r['first']))
        out = []
        for r in rows:
            out.append({**r, 'worked_now': (r['ent'], r['band']) in worked})
        tail = []
        if final:
            if sw:
                tail.append('終了時の' + sw.line())
            if iono and iono.line():
                tail.append(iono.line())
        return {'site': a.site, 't_start': t_start, 't_end': time.time(), 'n_notified': n_notified[0],
                'confirm': a.confirm, 'rows': out,
                'achieved': achieved() if final else [], 'n_qso': session_qso_count(),
                'completed': [r for r in out if r['worked_now']] if final else [], 'tail': tail}

    def save_session(final=False):
        nonlocal sess_dirty, sess_saved
        try:
            with open(sess_path, 'w', encoding='utf-8') as f:
                f.write(session_text(final) + '\n')
            sess_dirty, sess_saved = False, time.time()
        except OSError as e:
            print(f'[session] 保存できませんでした: {e}')

    def send_now(subject, body, msg=None):
        """開始・終了のメールは上限に関係なく、その場で送る。msg（HTML版）で送れなければテキスト版で送り直す"""
        if a.mail_to:
            try:
                notify_gmail(a.mail_from, notifier.password, a.mail_to, subject, body, msg)
                return True
            except Exception as e:
                print(f'[gmail error] {e}')
                if msg is not None:
                    try:
                        notify_gmail(a.mail_from, notifier.password, a.mail_to, subject, body)
                        return True
                    except Exception as e2:
                        print(f'[gmail error] {e2}')
        return False

    stopped = [False]

    def stop(reason):
        if stopped[0]:
            return
        stopped[0] = True
        save_session(final=True)
        print(f'停止します（{reason}）。終了のメールを送ります…')
        try:
            reload_logs()                          # 終了の直前に記録した交信も反映する
        except Exception as e:
            print(f'[終了] 交信ログを読み直せませんでした: {e}')
        subject = f"[DXCC] STOP {a.site} {time.strftime('%m/%d %H:%M', time.gmtime(time.time() + 9 * 3600))}"
        body = session_text(final=True)
        msg = None
        if html_on:
            try:
                msg = notify_html.build_stop_message(session_info(final=True), a.mail_from, a.mail_to,
                                                     subject, body)
            except Exception as e:
                print(f'[HTML] 終了のHTML版を作れませんでした: {e}')
        ok = send_now(subject, body, msg)
        print('終了のメールを送りました。' if ok else '終了のメールを送れませんでした。', '記録:', sess_path)

    if os.name == 'nt':                 # 黒い画面を×で閉じたときも、できるだけ終了のメールを送る
        import ctypes
        HANDLER = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

        def _on_console(ev):
            if ev in (1, 2, 5, 6):      # Ctrl+Break、ウィンドウを閉じる、ログオフ、シャットダウン
                stop('画面を閉じた')
                return 1
            return 0                    # Ctrl+C は通常の処理（KeyboardInterrupt）に任せる
        _console_handler = HANDLER(_on_console)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)

    def main_loop():
        nonlocal last_check, sess_dirty
        while True:
            ready, _, _ = select.select([sock] + ([lsock] if lsock else []), [], [], 5.0)
            if lsock in ready:
                ldata, laddr = lsock.recvfrom(65535)
                try:
                    if len(ldata) > 12 and struct.unpack('>I', ldata[:4])[0] == MAGIC:
                        lr = Reader(ldata); lr.u32(); lr.u32()
                        if lr.u32() != 12:          # WSJT-X の「Logged ADIF」メッセージ以外は無視
                            continue
                        lr.qstr(); text = lr.qstr() or ''
                    else:
                        text = ldata.decode('utf-8', 'replace')
                    for fld in adif_records(text):
                        call = fld['CALL'].upper()
                        band = (fld.get('BAND') or '').lower()
                        if not band and fld.get('FREQ'):
                            try:
                                band = freq_to_band(float(fld['FREQ']) * 1e6) or ''
                            except ValueError:
                                band = ''
                        ent = cty.entity(call)
                        if not ent or not band:
                            print(f'[remote QSO] 判定できませんでした: {call} {band}')
                            continue
                        worked.add((ent, band)); remote_worked.add((ent, band))
                        remote_hist.extend({**q, 'band': band} for q in adif_qsos_text(text) if q['call'] == call)
                        res = append_qso(append_path, fld, band, dxcc_nums.get(ent)) if append_path else 'skip'
                        print(time.strftime('%H:%M:%S'), f'[remote QSO from {laddr[0]}] {call} ({ent}) {band} '
                              f'→ 交信済みに反映（ログ追記: {res}）')
                except (struct.error, IndexError, ValueError) as e:
                    print(f'[remote QSO] 読み取れませんでした: {e}')
                continue
            # ログの読み直し（交信直後に通知が止まるように）と、運用中の記録の保存
            if time.time() - last_check > 60:
                last_check = time.time()
                reload_logs()
                if lotw_state['t'] and time.time() - lotw_state['t'] > 6 * 3600:
                    load_confirmed()               # 取得そのものは1日1回（保存したものを6時間ごとに読み直す）
                if sess_dirty:
                    save_session()
            if sock not in ready:
                continue
            data, _ = sock.recvfrom(4096)

            try:
                r = Reader(data)
                if r.u32() != MAGIC:
                    continue
                r.u32()                      # schema
                mtype = r.u32()
                cid = r.qstr()               # WSJT-X の名前（--rig-name ごとに異なる）
                if mtype == 1:               # Status
                    bands[cid] = freq_to_band(r.u64())
                elif mtype == 2 and bands.get(cid) in targets:  # Decode
                    band = bands[cid]
                    is_new = r.bool(); r.u32()
                    snr = r.i32(); dt = r.f64(); df = r.u32()
                    mode = r.qstr(); msg = r.qstr() or ''
                    low_conf = r.bool()                 # WSJT-X が「?」を付ける低信頼デコード
                    if a.debug_udp:
                        print(f'[UDP受信] {cid} {band} SNR{snr:+d} "{msg}"'
                              + ('' if is_new else ' (再デコード)')
                              + (' (低信頼)' if low_conf else ''))
                    if not is_new or snr < a.min_snr or low_conf:
                        continue
                    cands = []                          # (コール, 種別)
                    call = sender_of(msg, a.cq_only)
                    if call:
                        cands.append((call, 'direct'))
                    if a.called != 'off':
                        res = called_of(msg)
                        if res:
                            target, caller = res
                            if a.called == 'any' or cty.entity(caller) == 'Japan':
                                cands.append((target, 'called'))
                    for call, kind in cands:
                        ent = cty.entity(call)
                        if not ent:
                            continue
                        ped_worked = False         # 運用中のDXペディションで、このバンドは交信済み
                        if (ent, band) in worked:
                            if kind == 'direct' and call.upper() in active_ped_calls():
                                ped_worked = True  # 他のバンドにもQRVしている可能性があるので知らせる
                            else:
                                continue
                        if kind == 'direct' and not ped_worked:   # 自局が直接受信した未交信局を、この運用の記録に残す
                            r0 = sess.get((call, band))
                            t0 = time.time()
                            if r0 is None:
                                r0 = sess[(call, band)] = {'call': call, 'band': band, 'ent': ent, 'first': t0,
                                                           'last': t0, 'count': 0, 'best': snr,
                                                           'notified': False, 'warn': False,
                                                           'sw': sw.short() if sw else '',
                                                           'iono': iono.short() if iono else ''}
                            r0['last'] = t0; r0['count'] += 1; r0['best'] = max(r0['best'], snr)
                            if r0['count'] == a.confirm:
                                sess_dirty = True
                        key = (call, band, 'ped' if ped_worked else kind)   # 直接受信は「呼ばれ中」と別に通知する
                        now = time.time()
                        # 偽デコード対策: 同じ局を一定時間内に所定回数デコードしてから通知
                        hist = [t for t in seen.get(key, []) if now - t < a.confirm_window] + [now]
                        seen[key] = hist
                        # 全バンド初（どのバンドでも未交信）のエンティティは、聞こえた一瞬を逃さないよう少ない回数で通知
                        atno_now = kind == 'direct' and not any(e == ent for (e, _) in worked)
                        need = min(a.confirm, a.atno_confirm) if atno_now else a.confirm
                        if len(hist) < need:
                            continue
                        if already_sent(key, now):
                            continue
                        dkey = (call, band, 'direct')
                        if kind == 'called':
                            # 自局を優先: 自局で受信済み（通知済み、またはデコード中）の局は他局情報を通知しない
                            if already_sent(dkey, now):
                                continue
                            if any(now - t < a.confirm_window for t in seen.get(dkey, [])):
                                continue
                        after_called = (kind == 'direct' and already_sent((call, band, 'called'), now))
                        mark_sent(key, now)
                        warn = ''
                        grid_warn = ''
                        g = grid_of(msg) if kind == 'direct' else None
                        if g and ent in cty.pos:
                            d = distance_km(grid_center(g), cty.pos[ent])
                            if d > a.grid_km:
                                warn = f'【要確認】グリッド{g}が{ent}から約{d:,.0f}km離れています（偽デコードの可能性）\n'
                                grid_warn = warn.strip()
                        bearing_line = ''
                        if my_pos:
                            tgt_pos = grid_center(g) if g else cty.pos.get(ent)
                            if tgt_pos:
                                src = 'グリッドより' if g else f'{ent}の目安位置より（概算）'
                                bearing_line = f"\nローテーター目安: {bearing_deg(my_pos, tgt_pos):.0f}°（{src}）"
                        if ped_worked:
                            left = [b for b in sorted(targets, key=lambda b: band_order.get(b, 99))
                                    if (ent, b) not in worked]
                            subject = f"[DXCC] {SUBJECT_MARK['PED']} {band} {call} ({ent}) 交信済みバンド"
                            pb = ped_cache['bands'].get(call.upper()) or []
                            target_left = [b for b in left if b in pb] if pb else []
                            warn = ('【DXペディション・このバンドは交信済み】\n'
                                    + (f"このペディションの予定で未交信のバンド: {' '.join(target_left)}\n" if target_left else '')
                                    + (f"未交信のバンド（すべて）: {' '.join(left)}\n" if left else '全バンドで交信済み\n')
                                    + warn)
                            body = (warn + f'{call} ({ent}) {band} SNR {snr:+d}dB DT {dt:+.1f}s DF {df}Hz\n{msg}'
                                    + bearing_line + swline())
                        elif kind == 'direct':
                            # 件名の先頭で、全バンド初（ATNO）かバンドニューかを一目で区別する
                            is_atno = not any(e == ent for (e, _) in worked)
                            is_ped = call.upper() in active_ped_calls()    # ADXOで今運用中のDXペディションか
                            mark = SUBJECT_MARK[('ATNO' if is_atno else 'BANDNEW') + ('_PED' if is_ped else '')]
                            subject = (f'[DXCC] {mark} {band} {call} ({ent})' + (' 要確認' if warn else '')
                                       + ('（1回デコード）' if len(hist) == 1 else ''))
                            warn = f"【{mark.lstrip('🔴🟡🟢')}】\n" + warn
                            if after_called:
                                subject += ' 自局でも受信'
                                warn += '先に「他局が呼び出し中」で通知した局を、自局の設備でも受信できました\n'
                            body = (warn + f'{call} ({ent}) {band} SNR {snr:+d}dB DT {dt:+.1f}s DF {df}Hz\n{msg}'
                                    + bearing_line + swline())
                        else:
                            subject = f'[DXCC] {band} {call} ({ent}) 他局が呼び出し中'
                            body = (f'{call} ({ent}) {band} を他局が呼んでいます（当局では未受信）\n'
                                    f'呼んでいる局の信号: SNR {snr:+d}dB DT {dt:+.1f}s DF {df}Hz\n{msg}'
                                    + bearing_line + swline())
                        print(time.strftime('%H:%M:%S'), body.replace('\n', ' | '))
                        rich = None
                        if html_on:
                            done = {b for (e, b) in worked if e == ent}
                            hk = ('PED' if ped_worked else 'CALLED' if kind == 'called'
                                  else ('BANDNEW' if done else 'ATNO'))
                            rich = {'kind': hk, 'call': call, 'entity': ent, 'dxcc': dxcc_nums.get(ent),
                                    'band': band, 'snr': snr, 'message': msg,
                                    'snr_label': '呼んでいる局のSNR' if kind == 'called' else 'SNR',
                                    'time_utc': datetime.now(timezone.utc), 'warn': grid_warn,
                                    'after_called': after_called, 'bearing': bearing_line.strip(),
                                    'ped': kind == 'direct' and call.upper() in active_ped_calls()}
                        notifier.send(subject, body, rich)
                        n_notified[0] += 1
                        if kind == 'direct' and not ped_worked and (call, band) in sess:
                            sess[(call, band)]['notified'] = True
                            sess[(call, band)]['warn'] = bool(warn and '要確認' in warn)
                            sess_dirty = True
            except (struct.error, IndexError):
                continue


    band_counts = {}
    for b in sorted(targets, key=lambda b: band_order.get(b, 99)):
        band_counts[b] = len({e for e, bb in worked if bb == b})
    conf_counts = ({b: len({e for e, bb in confirmed if bb == b}) for b in band_counts} if confirmed else None)
    if conf_counts:
        count_line = ('バンド別のDXCC（交信／LoTW確認）: '
                      + ' / '.join(f'{b} {n}/{conf_counts[b]}' for b, n in band_counts.items())
                      + f"（全バンド合計 {len({e for e, _ in worked})}/{len({e for e, _ in confirmed})} / {len(cty.names)}）")
    else:
        count_line = ('バンド別のDXCC（交信済み）: ' + ' / '.join(f'{b} {n}' for b, n in band_counts.items())
                      + f'（全バンド合計 {len({e for e, _ in worked})} / {len(cty.names)}）')
    # 件名に日時を入れて毎回違う件名にする（同じ件名だとGmailが1つのスレッドにまとめ、
    # 前回と同じ内容の部分を「…」で折りたたむため、DXペディションの一覧などが途切れて見える）
    start_subject = f"[DXCC] START {a.site} {time.strftime('%m/%d %H:%M', time.gmtime(t_start + 9 * 3600))}"
    start_body = (
             f'PC: {a.site}\n開始: {time.strftime("%Y-%m-%d %H:%M", time.gmtime(t_start))} UTC\n'
             f'対象バンド: {", ".join(sorted(targets, key=lambda b: band_order.get(b, 99)))}\n'
             f'交信ログ: ' + ('、'.join(log_info) if log_info else '見つかりません') +
             f'\n交信済み（エンティティ×バンド）: {len(worked)}\n'
             + count_line + '\n'
             + (sw.start_text() + '\n' if sw else '')
             + (iono.line() + '\n' if iono and iono.line() else '') +
             f'運用中の記録: {sess_path}')
    start_msg = None
    if html_on:
        try:
            wx, series, v = current_wx()
            extra = []
            if 'S_prob' in v:
                extra.append(f"プロトン現象の確率（今日） {v['S_prob']}%")
            if 'G' in v:
                extra.append(f"地磁気嵐 G{v['G']}")
            ops = dx_ops()
            info = {'dx_ops': ops, 'band_counts': band_counts, 'entities_worked': len({e for e, _ in worked}),
                    'conf_counts': conf_counts,
                    'entities_confirmed': len({e for e, _ in confirmed}) if confirmed else None,
                    'entities_total': len(cty.names),
                    'site': a.site, 't_start': t_start,
                    'bands': sorted(targets, key=lambda b: band_order.get(b, 99)),
                    'log_info': log_info or [], 'worked': len(worked), 'sess_path': sess_path, 'extra': extra}
            if ops:
                start_body += ('\n\nDXペディション予定（3か月以内、出典: NG3K ADXO）\n'
                               + '\n'.join(notify_html.ops_text(ops)))
            start_msg = notify_html.build_start_message(info, wx, series, a.mail_from, a.mail_to,
                                                        start_subject, start_body)
        except Exception as e:
            print(f'[HTML] 開始のHTML版を作れませんでした: {e}')
    ok = send_now(start_subject, start_body, start_msg)
    print('開始のメールを送りました' if ok else '[注意] 開始のメールを送れませんでした（上のエラーを確認してください）')
    try:
        main_loop()
    except KeyboardInterrupt:
        stop('Ctrl+C')




def ipaddr_is_multicast(addr):
    try:
        return 224 <= int(addr.split('.')[0]) <= 239
    except ValueError:
        return False


if __name__ == '__main__':
    main()
