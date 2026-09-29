# -*- coding: utf-8 -*-
"""Standalone local core copied from the Colab notebook; no Colab runtime or Google Docs API."""

# -*- coding: utf-8 -*-
# ============================================================
# Dankoba Helper Ver.1.19.1  [3/10] 共通基盤（import / ログ / 実行レポート / 通信）
# ------------------------------------------------------------
# Konwenga Helper Ver.9.0.0 の [2/8] をそのまま転用している。
# 直す場合は両方そろえること。
# ============================================================
from __future__ import annotations

import asyncio
import base64
import calendar
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Final, List, Optional, Sequence, Tuple, Union
from urllib import robotparser
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup, NavigableString

try:
    import nest_asyncio
    nest_asyncio.apply()
except Exception:
    pass

_IN_COLAB = False

VERSION: Final[str] = "1.19.1"

logger = logging.getLogger("dankoba")

_QUIET_LOGGERS: Final[Tuple[str, ...]] = (
    "googleapiclient.discovery_cache",
    "google_auth_httplib2",
    "urllib3",
)


class _NotebookLogHandler(logging.StreamHandler):
    """Colab はセルごとに sys.stdout を差し替えるため、書き出し時に解決する。"""

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, value):
        # StreamHandler.__init__ からの代入は無視する
        pass


# ハンドラの見分けに使う名前。isinstance で見分けてはいけない。
# このセルを実行し直すと _NotebookLogHandler クラス自体が作り直され、
# 前回のハンドラは「別のクラスのインスタンス」になる。isinstance が False に
# なって毎回ハンドラが増え、実行回数ぶんログが重複する。
LOG_HANDLER_NAME: Final[str] = "dankoba-notebook"


def setup_logging(level: str = "INFO") -> None:
    """root のハンドラは触らない。Colab 側のハンドラを外しに行くと
    出力待ちのロックを掴んでセルが固まることがある。"""
    resolved_level = getattr(logging, str(level).strip().upper(), logging.INFO)
    # このロガーは他の誰も使わないので、付いているハンドラは全部外してから付け直す。
    # 名前で絞ると、修正前のバージョンで作られた無名のハンドラが残ってしまう。
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = _NotebookLogHandler()
    handler.set_name(LOG_HANDLER_NAME)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(handler)
    logger.setLevel(resolved_level)
    logger.propagate = False
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


setup_logging("INFO")


def normalize_club_key(value: str) -> str:
    """NFKC正規化で全角と半角の差を吸収し、大文字小文字も無視する。
    「ＦＣ東京」と「FC東京」、「Ｖ・ファーレン長崎」と「V・ファーレン長崎」が
    同じキーになる。クラブ情報 [5/10] より先の [4/10] でも使うのでここに置く。"""
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


# 旧名（アップロード版モジュールからの移植互換）
_normalise = normalize_club_key


def display_width(text: str) -> int:
    """全角を2、半角を1として数える。ログの列を目視で揃えるため。"""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in str(text))


def pad_display(text: str, width: int, align: str = "left") -> str:
    padding = " " * max(0, width - display_width(text))
    return padding + str(text) if align == "right" else str(text) + padding


# ------------------------------------------------------------
# 実行結果サマリ
# ------------------------------------------------------------
class StepStatus(str, Enum):
    OK = "OK"
    NG = "NG"
    SKIP = "SKIP"


@dataclass
class StepResult:
    name: str
    status: StepStatus
    detail: str = ""


class RunReport:
    def __init__(self) -> None:
        self.steps: List[StepResult] = []
        self.warnings: List[str] = []
        self.document_url: str = ""

    def record(self, name: str, status: StepStatus, detail: str = "") -> None:
        self.steps.append(StepResult(name=name, status=status, detail=detail))

    def ok(self, name: str, detail: str = "") -> None:
        self.record(name, StepStatus.OK, detail)

    def ng(self, name: str, detail: str = "") -> None:
        self.record(name, StepStatus.NG, detail)

    def skip(self, name: str, detail: str = "") -> None:
        self.record(name, StepStatus.SKIP, detail)

    def warn(self, message: str) -> None:
        logger.warning(message)
        self.warnings.append(message)

    @property
    def failed_steps(self) -> List[StepResult]:
        return [step for step in self.steps if step.status is StepStatus.NG]

    def render(self) -> str:
        name_width = max([display_width(step.name) for step in self.steps] + [16])
        lines = ["", "=== 実行結果サマリ ==="]
        for step in self.steps:
            detail = f"  {step.detail}" if step.detail else ""
            lines.append(f"{pad_display(step.name, name_width)}  {step.status.value:<4}{detail}")
        if self.document_url:
            lines.append(f"{pad_display('Docs URL', name_width)}  {'':<4}  {self.document_url}")
        if self.warnings:
            lines.append("")
            lines.append(f"警告 {len(self.warnings)}件:")
            lines.extend(f"  - {message}" for message in self.warnings)
        else:
            lines.append("")
            lines.append("警告なし")
        failed = self.failed_steps
        lines.append("")
        if failed:
            lines.append(f"失敗 {len(failed)}件: {', '.join(step.name for step in failed)}")
        elif self.warnings:
            lines.append("失敗はありませんが、警告の内容を確認してください。")
        else:
            lines.append("テンプレートを生成しました。")
        lines.append("=" * 22)
        return "\n".join(lines)

    def emit(self) -> None:
        logger.info(self.render())


def emit_warning(report: Optional[RunReport], message: str, *args: Any) -> None:
    text = message % args if args else message
    if report is not None:
        report.warn(text)
    else:
        logger.warning(text)


# ------------------------------------------------------------
# 通信基盤
# ------------------------------------------------------------
@dataclass(frozen=True)
class NetworkConfig:
    timeout_sec: int = 20
    playwright_timeout_sec: int = 30
    max_workers: int = 4
    # ブラウザ以外の User-Agent を弾くクラブサイトがあるため、ブラウザの形に
    # 寄せつつ末尾に出自を残す。何かあったときに問い合わせ先が分かる形は保つ。
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 Dankoba-Helper/1.9 (+https://grapo.net/)"
    )
    # robots.txt の User-agent 行との突き合わせに使う名前
    robots_agent: str = "Dankoba-Helper"
    default_encoding: str = "utf-8"
    # 同じホストへ連続で投げるときの最短間隔。相手は個人運営に近いクラブもある。
    min_interval_sec: float = 1.0
    respect_robots: bool = True
    # ヘッドレスのままだと自動操作と見分けられて弾かれるサイトがある。
    # False にすると画面ありで起動する（ローカル画面、Colabでは仮想ディスプレイ）。
    playwright_headless: bool = True
    # "chrome" を指定すると Playwright 同梱の Chromium ではなく実 Chrome を使う。
    playwright_channel: str = ""


def build_network_config() -> NetworkConfig:
    """ローカル設定から通信設定を作る。"""
    return NetworkConfig(
        respect_robots=bool(RESPECT_ROBOTS_TXT),
        min_interval_sec=float(REQUEST_MIN_INTERVAL_SEC or 0),
        playwright_headless=bool(PLAYWRIGHT_HEADLESS),
        playwright_channel=str(PLAYWRIGHT_CHANNEL or "").strip(),
    )

def build_session(network: NetworkConfig) -> requests.Session:
    """一時エラーだけ再試行するセッション。Konwenga [3/9] からの移植。"""
    session = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3, status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=max(4, network.max_workers),
        pool_maxsize=max(8, network.max_workers * 2),
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    # User-Agent だけのリクエストを弾く WAF があるので、ブラウザが送る
    # 基本的なヘッダも添える。
    session.headers.update({
        "User-Agent": network.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    })
    return session


def decode_response_text(response: requests.Response, network: NetworkConfig) -> str:
    """apparent_encoding はレスポンス全文を chardet で走査するので使わない。
    ただしクラブ公式サイトは Football LAB と違って文字コードがまちまちなので、
    サーバが宣言していればそれを優先し、無い場合だけ既定値に落とす。"""
    declared = (response.encoding or "").strip().lower()
    if not declared or declared == "iso-8859-1":
        # requests は Content-Type に charset が無いと ISO-8859-1 を入れてくる
        meta_encoding = requests.utils.get_encodings_from_content(response.text[:2048])
        response.encoding = meta_encoding[0] if meta_encoding else network.default_encoding
    return response.text


def run_in_notebook(coro):
    """nest_asyncio.apply() 済みなので asyncio.run で足りる。
    get_event_loop() は Python 3.12 以降で非推奨。"""
    return asyncio.run(coro)


_VIRTUAL_DISPLAY: Any = None


def ensure_display() -> bool:
    """画面ありでブラウザを起動するための下ごしらえ。

    Colab に画面は無いので Xvfb で仮想ディスプレイを立てる。
    LoanWatch の --headed に相当する動きを Colab で作るための部分。"""
    global _VIRTUAL_DISPLAY
    if os.environ.get("DISPLAY"):
        return True
    # Windows / macOS のローカルJupyterはデスクトップ画面をそのまま使える。
    if not sys.platform.startswith("linux"):
        return True
    if _VIRTUAL_DISPLAY is not None:
        return True
    try:
        from pyvirtualdisplay import Display
    except ImportError:
        logger.warning(
            "pyvirtualdisplay が入っていないため画面ありで起動できません。"
            "セル[1/10]の INSTALL_REAL_CHROME を True にして流し直してください"
        )
        return False
    try:
        _VIRTUAL_DISPLAY = Display(visible=False, size=(1440, 1000))
        _VIRTUAL_DISPLAY.start()
        logger.info("仮想ディスプレイを起動しました（DISPLAY=%s）", os.environ.get("DISPLAY"))
        return True
    except Exception:
        logger.exception("仮想ディスプレイの起動に失敗しました")
        return False


# page.content() は shadow root の中身を返さない。Jリーグの試合ページは
# ハイライト動画を <youtube-video><template shadowrootmode="open">…</template>
# の形で持っているため、そのままでは iframe が読めない。
# 取り出す前に、各 shadow root の中身を <template> として本体へ写し込む。
SHADOW_DOM_SERIALIZER: Final[str] = """
() => {
  const walk = (root, depth) => {
    if (depth > 6) return;
    root.querySelectorAll('*').forEach((el) => {
      if (el.shadowRoot) {
        const holder = document.createElement('template');
        holder.setAttribute('data-shadow-root', '');
        holder.innerHTML = el.shadowRoot.innerHTML;
        el.appendChild(holder);
        walk(el.shadowRoot, depth + 1);
      }
    });
  };
  try { walk(document, 0); } catch (e) {}
  return document.documentElement.outerHTML;
}
"""


class HostThrottle:
    """ホストごとに最短間隔を空ける。並列取得でも1ホストに集中させない。"""

    def __init__(self, min_interval_sec: float):
        self.min_interval_sec = max(0.0, float(min_interval_sec))
        self._last_access: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        if self.min_interval_sec <= 0:
            return
        host = urlparse(url).netloc.lower()
        with self._lock:
            previous = self._last_access.get(host, 0.0)
            elapsed = time.monotonic() - previous
            sleep_sec = self.min_interval_sec - elapsed
            if sleep_sec > 0:
                logger.info(
                    "同じサイトへのアクセス間隔を空けるため %.1f秒待機します: %s",
                    sleep_sec, host,
                )
                time.sleep(sleep_sec)
            self._last_access[host] = time.monotonic()


class RobotsPolicy:
    """robots.txt をホストごとに1回だけ読んで判定を覚える。

    取得できなかった場合は許可として扱う。robots.txt が無いサイトを
    一律で拒否すると、実質すべて読めなくなるため。"""

    def __init__(
        self,
        session: requests.Session,
        network: NetworkConfig,
        report: Optional[RunReport] = None,
    ):
        self.session = session
        self.network = network
        self.report = report
        self._parsers: Dict[str, Optional[robotparser.RobotFileParser]] = {}
        self._lock = threading.Lock()

    def _parser_for(self, url: str) -> Optional[robotparser.RobotFileParser]:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            if origin in self._parsers:
                return self._parsers[origin]
        parser: Optional[robotparser.RobotFileParser] = None
        try:
            robots_url = urljoin(origin, "/robots.txt")
            logger.info("robots.txt を確認します: %s", robots_url)
            response = self.session.get(robots_url, timeout=self.network.timeout_sec)
            if response.status_code == 200:
                parser = robotparser.RobotFileParser()
                parser.parse(decode_response_text(response, self.network).splitlines())
                logger.info("robots.txt を読み込みました: %s", origin)
            else:
                logger.debug("robots.txt が見つかりません (HTTP %s): %s", response.status_code, origin)
        except Exception:
            logger.debug("robots.txt の取得に失敗しました: %s", origin, exc_info=True)
        with self._lock:
            self._parsers[origin] = parser
        return parser

    def is_allowed(self, url: str) -> bool:
        if not self.network.respect_robots:
            return True
        parser = self._parser_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.network.robots_agent, url)


@dataclass
class FetchResult:
    url: str
    html: str
    source: str  # "静的HTML" または "Playwright"
    status_code: Optional[int] = None
    soup: Optional[BeautifulSoup] = None

    def links(self, keywords: Sequence[str], limit: int = 5) -> List[Tuple[str, str]]:
        return extract_links(self.soup, self.url, keywords, limit=limit)


def parse_html(html: str) -> BeautifulSoup:
    """lxml が使えればそちらを使う。入れ子の壊れたHTMLの補正が素直なため。"""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        logger.debug("lxmlが使えないためhtml.parserで解析します", exc_info=True)
        return BeautifulSoup(html, "html.parser")


def extract_links(
    soup: Optional[BeautifulSoup],
    base_url: str,
    keywords: Sequence[str],
    limit: int = 5,
) -> List[Tuple[str, str]]:
    """アンカーの文字列かURLにキーワードを含むリンクを拾い、絶対URLにして返す。
    同じURLは1回だけ。順序はページ上の並びを保つ。"""
    if soup is None:
        return []
    lowered = [str(keyword).lower() for keyword in keywords if str(keyword).strip()]
    found: List[Tuple[str, str]] = []
    seen: set = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href")).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        text = anchor.get_text(" ", strip=True)
        haystack = f"{text} {href}".lower()
        if not any(keyword in haystack for keyword in lowered):
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        found.append((text or absolute, absolute))
        if len(found) >= limit:
            break
    return found


class PageFetcher:
    """静的HTMLで取り、だめなら Playwright に落とす。

    Konwenga の FootballLabShotsSvgExporter が取っていた形をそのまま一般化した。
    シュートチャートのSVGがJavaScriptで描かれていたのと同じ事情が、
    クラブ公式サイトの試合情報ページでも起きる。"""

    def __init__(
        self,
        session: requests.Session,
        network: Optional[NetworkConfig] = None,
        report: Optional[RunReport] = None,
    ):
        self.session = session
        self.network = network or NetworkConfig()
        self.report = report
        self.throttle = HostThrottle(self.network.min_interval_sec)
        self.robots = RobotsPolicy(session, self.network, report)
        # 同じページを二度取らないための、この実行のあいだだけのキャッシュ。
        # 試合情報の一覧を「候補として開く」「探索対象に足す」で2回取っていた。
        self._cache: Dict[str, Optional[FetchResult]] = {}
        # 静的な取得を拒否されたホスト。次からは最初からブラウザで開く。
        # 同じサイトのページごとに「拒否された→読み直します」を繰り返さない。
        self._render_hosts: set = set()

    def _warn(self, message: str, *args: Any) -> None:
        emit_warning(self.report, message, *args)

    @staticmethod
    def _prefer_https(url: str) -> str:
        """http は https に寄せる。クラブ情報に http で登録されているサイトがあり、
        転送を1回減らせるほか、平文のリクエストを拒否する設定にも当たらずに済む。"""
        parsed = urlparse(url)
        if parsed.scheme != "http":
            return url
        return urlunparse(parsed._replace(scheme="https"))

    def fetch(
        self,
        url: str,
        *,
        use_playwright_fallback: bool = False,
        wait_selector: str = "",
        min_html_length: int = 0,
        force_render: bool = False,
    ) -> Optional[FetchResult]:
        """1ページ取る。取れなければ None を返して警告する。

        min_html_length を指定すると、本文が短すぎる（=JavaScriptで組み立てる
        ページを空のまま受け取った）場合に Playwright へ回す。
        force_render を立てると、静的HTMLを試さず最初からブラウザで描画する。
        静的HTMLは取れているのに一部の要素だけ入っていない、というときに使う。"""
        if not str(url or "").strip():
            return None
        url, original_url = self._prefer_https(url), url
        cache_key = f"{url}|{wait_selector}"
        if cache_key in self._cache and not force_render:
            logger.info("取得済みのページを再利用します: %s", url)
            return self._cache[cache_key]
        if not self.robots.is_allowed(url):
            self._warn("robots.txt で許可されていないため取得しません: %s", url)
            return None
        logger.info("ページ取得を開始します: %s", url)
        host = urlparse(url).netloc.lower()
        if not force_render and use_playwright_fallback and host in self._render_hosts:
            logger.info("このサイトは静的に取れないので、最初からブラウザで開きます: %s", url)
            force_render = True
        if force_render:
            self.throttle.wait(url)
            result = self._fetch_rendered(url, wait_selector)
            self._cache[cache_key] = result
            return result

        result: Optional[FetchResult] = None
        refused = False
        refused_status: Any = "不明"
        self.throttle.wait(url)
        try:
            response = self.session.get(url, timeout=self.network.timeout_sec)
            response.raise_for_status()
            html = decode_response_text(response, self.network)
            result = FetchResult(url=url, html=html, source="静的HTML",
                                 status_code=response.status_code, soup=parse_html(html))
            logger.info(
                "静的HTMLを取得しました: %s (HTTP %s、%s文字)",
                url, response.status_code, len(html),
            )
        except requests.HTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", "不明")
            if status in (401, 403, 406, 429):
                # このあとブラウザで読み直して成功することがあるので、
                # ここでは警告にしない。だめだったときだけ警告する。
                logger.info(
                    "静的な取得を拒否されました (HTTP %s): %s。ブラウザで読み直します",
                    status, url,
                )
                refused = True
                refused_status = status
                self._render_hosts.add(host)
            else:
                self._warn("ページを取得できませんでした (HTTP %s): %s", status, url)
        except requests.RequestException:
            if url != original_url:
                logger.info("https で繋がらないため、元のURLで試します: %s", original_url)
                try:
                    response = self.session.get(original_url, timeout=self.network.timeout_sec)
                    response.raise_for_status()
                    html = decode_response_text(response, self.network)
                    result = FetchResult(url=original_url, html=html, source="静的HTML",
                                         status_code=response.status_code, soup=parse_html(html))
                except Exception:
                    logger.exception("ページ取得に失敗しました: %s", original_url)
                    self._warn("ページを取得できませんでした: %s", original_url)
            else:
                logger.exception("ページ取得に失敗しました（接続またはタイムアウト）: %s", url)
                self._warn("ページを取得できませんでした（接続またはタイムアウト）: %s", url)
        except Exception:
            logger.exception("ページ解析に失敗しました: %s", url)
            self._warn("ページを解析できませんでした: %s", url)

        needs_fallback = use_playwright_fallback and (
            refused or result is None
            or (min_html_length and len(result.html) < min_html_length)
        )
        if not needs_fallback:
            if refused:
                self._warn(
                    "サーバに拒否されました (HTTP %s): %s。"
                    "[4/10] の USE_PLAYWRIGHT_FALLBACK を True にすると読める場合があります",
                    refused_status, url,
                )
            self._cache[cache_key] = result
            return result

        logger.info("静的HTMLでは内容が取れないため Playwright に切り替えます: %s", url)
        rendered = self._fetch_rendered(url, wait_selector)
        final = rendered or result
        if rendered is None and refused:
            self._warn("ブラウザでも読めませんでした: %s", url)
        self._cache[cache_key] = final
        return final

    def _fetch_rendered(self, url: str, wait_selector: str = "") -> Optional[FetchResult]:
        # ブラウザの起動は失敗することがある（Colab の資源不足など）。
        # 1回だけやり直す。それでもだめなら、何が起きたかを警告に載せる。
        last_error: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                logger.info("Playwrightでページを開きます (%s/2): %s", attempt, url)
                html = run_in_notebook(self._render_async(url, wait_selector))
            except ModuleNotFoundError:
                self._warn(
                    "Playwright のPythonパッケージが見つかりません。"
                    "依存パッケージをインストールしてから実行してください"
                )
                return None
            except Exception as error:
                last_error = error
                logger.exception("ブラウザでの取得に失敗しました (%s回目): %s", attempt, url)
                if attempt == 1:
                    time.sleep(2)
                continue
            if not html:
                logger.warning("Playwrightから本文を取得できませんでした: %s", url)
                return None
            logger.info("Playwrightでページを取得しました: %s (%s文字)", url, len(html))
            return FetchResult(url=url, html=html, source="Playwright", soup=parse_html(html))
        self._warn(
            "ブラウザでもページを取得できませんでした: %s（%s: %s）",
            url, type(last_error).__name__, last_error,
        )
        return None

    # Cookie同意バナーが本文を覆うことがあるので、出ていれば閉じる
    CONSENT_SELECTORS: Final[Tuple[str, ...]] = (
        "button:has-text('同意')", "#onetrust-accept-btn-handler",
        "button:has-text('I Accept')", "button:has-text('Accept')",
    )

    async def _render_async(self, url: str, wait_selector: str = "") -> str:
        """ブラウザで開いてHTMLを返す。

        User-Agent は偽装せず、ブラウザ自身の値をそのまま使う。
        代わりに、自動操作だと見分けられる要素を減らす:
          - locale / timezone / viewport を実機並みに設定する
          - AutomationControlled と --enable-automation を外す
          - navigator.webdriver を隠す
        それでも通らない場合は、残るのは接続元IPの問題になる。"""
        # ブラウザ本体を入れていないと ModuleNotFoundError / Error になるので、
        # import はここで行い、呼び出し側で受け止める。
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright

        timeout_ms = int(self.network.playwright_timeout_sec * 1000)
        headless = self.network.playwright_headless
        if not headless and not ensure_display():
            logger.info("画面を用意できなかったため、ヘッドレスで起動します")
            headless = True

        launch_kwargs: Dict[str, Any] = {
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled", "--lang=ja-JP"],
            "ignore_default_args": ["--enable-automation"],
        }
        if self.network.playwright_channel:
            launch_kwargs["channel"] = self.network.playwright_channel

        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(**launch_kwargs)
            except PlaywrightError:
                if not self.network.playwright_channel:
                    raise
                logger.warning(
                    "実Chrome（channel=%s）を起動できないため、同梱のChromiumで試します",
                    self.network.playwright_channel,
                )
                launch_kwargs.pop("channel")
                browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(
                    locale="ja-JP",
                    timezone_id="Asia/Tokyo",
                    viewport={"width": 1440, "height": 1000},
                    extra_http_headers={"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"},
                )
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page = await context.new_page()
                try:
                    response = await page.goto(url, wait_until="load", timeout=timeout_ms)
                    # goto はエラー応答でも例外にならない。ステータスを見ないと、
                    # 403のエラーページを「取得できた」と誤認する。
                    status = response.status if response is not None else 0
                    if status >= 400:
                        self._warn(
                            "ブラウザで開いても拒否されました (HTTP %s): %s。"
                            "接続元IPで遮断されている可能性があります",
                            status, url,
                        )
                        return ""
                    for selector in self.CONSENT_SELECTORS:
                        try:
                            await page.click(selector, timeout=1500)
                            logger.debug("Cookie同意を閉じました: %s", selector)
                            break
                        except Exception:
                            continue
                    if wait_selector:
                        # 待つものが決まっているならそれを待つ。JSで数秒後に
                        # 差し込まれる要素は、load だけでは間に合わない。
                        try:
                            await page.wait_for_selector(wait_selector, timeout=timeout_ms)
                        except PlaywrightTimeoutError:
                            logger.info(
                                "指定した要素は現れませんでしたが、読めるところまで読みます: %s（%s）",
                                wait_selector, url,
                            )
                            try:
                                await page.wait_for_load_state("networkidle", timeout=5000)
                            except PlaywrightTimeoutError:
                                pass
                            await page.wait_for_timeout(2000)
                    else:
                        # 何を待てばよいか分からない場合は、通信が落ち着くまで少し待つ。
                        try:
                            await page.wait_for_load_state("networkidle", timeout=5000)
                        except PlaywrightTimeoutError:
                            logger.debug("networkidle にならなかったので先へ進みます: %s", url)
                except PlaywrightTimeoutError:
                    logger.warning("Playwright の待機がタイムアウトしました: %s", url)
                except PlaywrightError:
                    logger.exception("Playwright の遷移に失敗しました: %s", url)
                    return ""
                return await page.evaluate(SHADOW_DOM_SERIALIZER)
            finally:
                await browser.close()

    def fetch_many(self, urls: Sequence[str], **kwargs: Any) -> Dict[str, Optional[FetchResult]]:
        """複数ページをまとめて取る。Konwenga の CBP 7ページ取得と同じ形。
        ホスト単位の間隔は HostThrottle が見るので、並列でも1サイトに集中しない。"""
        unique_urls = list(dict.fromkeys(url for url in urls if str(url or "").strip()))
        results: Dict[str, Optional[FetchResult]] = {}
        if not unique_urls:
            return results
        max_workers = min(self.network.max_workers, len(unique_urls))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.fetch, url, **kwargs): url for url in unique_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    results[url] = future.result()
                except Exception:
                    logger.exception("ページ取得中に想定外のエラー: %s", url)
                    results[url] = None
        return results

# -*- coding: utf-8 -*-
# ============================================================
# Dankoba Helper Ver.1.19.1  [4/10] 設定② 試合情報と記事設定
# ------------------------------------------------------------
# このセルを実行すると、Jリーグ公式の日程検索から [2/10] で指定した
# 2クラブの試合を探し、試合ページの内容を入力欄に入れて表示する。
#
# 入力欄の下に「入力チェック」が出る。いま何が埋まっていて、
# 何を自分で書く必要があるかが一覧で分かる。直したら
# 「もう一度チェック」ボタンを押せば更新される。
#
#   ✅ 自動   Jリーグ公式から入った
#   ✅ 入力済 自分で入れた
#   ✏️ 要入力 ここを埋めないと記事が成り立たない
#   －  任意   空欄のままでもよい
#
# 記事番号と両チームの布陣は、毎回変わるのに自動では決められない。
# 前回の値が残っていると直し忘れるので、空欄から始まるようにしてある。
#
# 日程一覧はカテゴリ別（J1/J2/J3）に引く。自チームの所属から自動で選ぶ。
# ただし一覧に出るのは今週ぶん（おおよそ 前2日〜後5日）だけ。
# もっと先の試合を扱うときは、試合ページのURLを MATCH_PAGE_URL に
# 直接貼れば、日程検索を飛ばしてそこだけ読みに行く。
# ============================================================

# --- 自動取得 ---------------------------------------------------
AUTO_FILL_FROM_JLEAGUE = True  # @param {type:"boolean"}
# 会場と住所が取れたら、tenki.jp から試合時間の天気も入れる。
AUTO_FILL_WEATHER = True  # @param {type:"boolean"}
# 対戦相手の直近試合のハイライト動画を、Jリーグ公式の試合ページから拾う。
SEARCH_HIGHLIGHT_VIDEO = True  # @param {type:"boolean"}
# 空欄なら下の JLEAGUE_SCHEDULE_URL から探す。
# 例: https://www.jleague.jp/match/j1/2026/091902/
MATCH_PAGE_URL = ""  # @param {type:"string"}

# --- 記事の識別 -------------------------------------------------
# 毎回同じ値を使うものだけ @param に残している。
SEASON = "2026/27"  # @param {type:"string"}
# 空欄なら自チームの所属カテゴリから「明治安田J1リーグ」を組み立てる。
COMPETITION = ""  # @param {type:"string"}

# --- 対戦カードの体裁 -------------------------------------------
USE_OFFICIAL_CLUB_NAME = True  # @param {type:"boolean"}
# 空欄ならクラブ情報のハッシュタグから自動で選ぶ。
OPPONENT_HASHTAG = ""  # @param {type:"string"}

# 記事番号・布陣・節・ホーム/アウェイ・キックオフ・会場・中継・天気は、
# このセルの下に出る入力欄で編集する。Colab の @param はセルのソースと
# 結びついていて、実行時に取得した値を表示へ反映できないため。

# --- 章立ての細かさ ---------------------------------------------
ATTACK_POINT_COUNT = 2  # @param {type:"slider", min:1, max:5, step:1}
DEFENSE_POINT_COUNT = 2  # @param {type:"slider", min:1, max:5, step:1}
INCLUDE_REFERENCE_SECTION = True  # @param {type:"boolean"}

# --- クラブ公式サイトの収集 ---------------------------------------
# 両クラブの公式サイトから、試合情報・アクセス・スタジアムグルメ・
# グッズ販売のリンクを探して参考リンクに足す。見に行くのは対戦する
# 2クラブのサイトだけ（4〜6リクエスト）。拾えた候補を人が選ぶ前提で、
# 本文には自動で差し込まない。
SCRAPE_CLUB_SITE = True  # @param {type:"boolean"}
# 静的HTMLで中身が取れないときに Playwright で描画してから読む。
# セル[1/10]の INSTALL_PLAYWRIGHT_BROWSER が False だと動かない。
USE_PLAYWRIGHT_FALLBACK = True  # @param {type:"boolean"}
RESPECT_ROBOTS_TXT = True  # @param {type:"boolean"}
# 同じホストへ連続で投げるときの最短間隔（秒）。
REQUEST_MIN_INTERVAL_SEC = 1.0  # @param {type:"number"}
# ヘッドレスのままだと自動操作と見分けられて弾かれるサイトがあるため、
# 既定では画面あり（VS CodeローカルではPC画面、ColabではXvfb）で起動する。
# 画面を用意できない場合は自動でヘッドレスに落ちる。
PLAYWRIGHT_HEADLESS = False  # @param {type:"boolean"}
# 実物の Google Chrome を使う。起動できなければ同梱の Chromium に落ちる。
# どちらもセル[1/10]の INSTALL_REAL_CHROME が前提。
PLAYWRIGHT_CHANNEL = "chrome"  # @param ["", "chrome"]

# --- 出力先 ----------------------------------------------------
DRIVE_FOLDER_NAME = "グラぽ マッチプレビュー"  # @param {type:"string"}

# --- ログ ------------------------------------------------------
LOG_LEVEL = "INFO"  # @param ["INFO", "DEBUG", "WARNING"]

setup_logging(LOG_LEVEL)

# Jリーグ公式の日程検索。大会ごとにパスとカテゴリが変わる。
# リーグ戦はカテゴリと同じパス、カップ戦は j1 のパスに category を載せる。
#   https://www.jleague.jp/j1/match/search-list/
#     ?startdate=2026-09-20&enddate=2026-10-04&period=custom&category=j1&sort_desc=true
# club= にクラブ識別子を入れると、そのクラブの試合だけに絞れる。
JLEAGUE_SEARCH_URL_TEMPLATE: Final[str] = (
    "https://www.jleague.jp/{path}/match/search-list/"
    "?startdate={startdate}&enddate={enddate}&period=custom&category={category}"
)
JLEAGUE_SCHEDULE_LEAGUES: Final[Tuple[str, ...]] = ("j1", "j2", "j3")


@dataclass(frozen=True)
class CompetitionType:
    name: str          # [2/10] で選ぶ名前
    path: str          # URLのパス部分
    category: str      # category= の値
    label: str         # 記事に出す大会名
    is_league: bool    # 順位表や「第N節」があるか


COMPETITION_TYPES: Final[Tuple[CompetitionType, ...]] = (
    CompetitionType("J1", "j1", "j1", "明治安田J1リーグ", True),
    CompetitionType("J2", "j2", "j2", "明治安田J2リーグ", True),
    CompetitionType("J3", "j3", "j3", "明治安田J3リーグ", True),
    CompetitionType("天皇杯", "j1", "emperor", "天皇杯 JFA 全日本サッカー選手権大会", False),
    CompetitionType("ルヴァンカップ", "j1", "leaguecup", "JリーグYBCルヴァンカップ", False),
)
# 対象試合を探す期間（実行日から何日先まで）
TARGET_MATCH_WINDOW_DAYS: Final[int] = 14


def competition_type(name: str) -> CompetitionType:
    for item in COMPETITION_TYPES:
        if item.name == str(name or "").strip():
            return item
    logger.warning("知らない大会です: %r。J1として扱います", name)
    return COMPETITION_TYPES[0]


def jleague_search_url(
    competition: CompetitionType,
    startdate: date,
    enddate: date,
    club_params: Sequence[str] = (),
    sort_desc: bool = False,
) -> str:
    """日程検索のURLを組み立てる。club_params を渡すとそのクラブに絞る。"""
    url = JLEAGUE_SEARCH_URL_TEMPLATE.format(
        path=competition.path, category=competition.category,
        startdate=startdate.isoformat(), enddate=enddate.isoformat(),
    )
    clubs = [param for param in club_params if param]
    if clubs:
        url += "&club=" + "%2C".join(clubs)
    if sort_desc:
        url += "&sort_desc=true"
    return url


def club_param_of(team: str) -> str:
    """[6/10] のクラブ解決が読み込まれると、識別子を返す版で上書きされる。"""
    return ""


def search_league_hint(team: str) -> Optional[str]:
    """自チームの所属カテゴリ。日程検索のURLを決めるために使う。

    このセルはクラブ情報 [5/10] より前に走るので、ここでは分からないと返す。
    [6/10] が読み込まれると、クラブ情報から答える版で上書きされる。
    分からない場合は J1 → J2 → J3 の順に当たる。"""
    return None


# ------------------------------------------------------------
# 固定の文言とスタイル
# ------------------------------------------------------------
WEEKDAY_JA: Final[Tuple[str, ...]] = ("月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日")


class SectionLabel:
    """記事の骨格に出てくる絵文字つき見出し。表記ゆれを避けるため定数化する。"""

    SPEAKER_DANKOBA: Final[str] = "【🎙 ダンコバ】"
    SPEAKER_ATTACK: Final[str] = "【⚔️ 攻撃担当】"
    SPEAKER_DEFENSE: Final[str] = "【🛡 守備担当】"
    GUIDE: Final[str] = "📌 試合観戦ガイド＆インフォメーション"
    GOODS: Final[str] = "🍡 グッズ・イベント・アクセス情報"
    SITUATION: Final[str] = "⚽️ 両チームの状況と先発予想"
    WIN_PATH: Final[str] = "🔥 {my_team}の勝ち筋"
    ATTACK: Final[str] = "⚔️ 攻撃のポイント"
    DEFENSE: Final[str] = "🛡 守備のポイント"
    CLOSING: Final[str] = "📝 おわりに"
    KICKOFF: Final[str] = "⏰ キックオフ"
    VENUE: Final[str] = "🏟 試合会場"
    BROADCAST: Final[str] = "📺 試合中継"
    WEATHER: Final[str] = "☀️ 天気予報"
    REFERENCE: Final[str] = "🔗 参考リンク（公開前に削除してください）"


@dataclass(frozen=True)
class DocsStyleConfig:
    google_api_max_retries: int = 6
    google_api_initial_wait_sec: float = 1.0
    google_api_max_wait_sec: float = 16.0
    google_api_request_interval_sec: float = 0.05

    # 表のレイアウト
    table_font_pt: float = 11.0
    table_cell_padding_pt: float = 12.0
    table_min_column_width_pt: float = 26.0
    table_max_total_width_pt: float = 450.0
    table_align_center: bool = True

    # 表のチーム名セル
    my_team_cell_bg: str = "#d80c18"
    my_team_cell_text: str = "#ffffff"
    opponent_team_cell_bg: str = "#cfe8fa"

    # 後から書き換えるプレースホルダの見た目
    placeholder_color: str = "#808080"
    placeholder_italic: bool = True


def normalize_serial_number(value: Any) -> str:
    """"286" / "D286" / "d 286" を "286" に揃える。"""
    text = str(value or "").strip().upper()
    text = re.sub(r"^D\s*", "", text)
    digits = re.sub(r"\D", "", text)
    if not digits:
        logger.warning("通し番号 %r から数字を読み取れませんでした。タイトルは 'D' のみになります", value)
    return digits


def parse_kickoff_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        logger.warning("キックオフ日が空です。タイトルの日付はプレースホルダになります")
        return None
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    logger.warning("キックオフ日 %r を解釈できませんでした。YYYY-MM-DD で入力してください", value)
    return None


# ============================================================
# Jリーグ公式の日程・試合ページから初期値を拾う
# ============================================================
@dataclass(frozen=True)
class JLeagueMatchInfo:
    """試合ページから読み取れた値。読めなかった項目は空のまま。"""

    url: str = ""
    kickoff_date: str = ""      # YYYY-MM-DD
    kickoff_time: str = ""      # HH:MM
    round_label: str = ""       # 「第8節」「3回戦」「準決勝」など
    home_team: str = ""
    away_team: str = ""
    venue_name: str = ""
    venue_address: str = ""
    venue_map_url: str = ""
    broadcast: str = ""

    def opponent_of(self, my_team: str) -> str:
        """ホーム・アウェイのうち、自チームでないほうを返す。
        [2/10] で OPPONENT_TEAM を空欄にしたときに使う。"""
        if not my_team:
            return ""
        my_key = resolve_club_canonical(my_team) or normalize_club_key(my_team)
        for name in (self.home_team, self.away_team):
            if not name:
                continue
            key = resolve_club_canonical(name) or normalize_club_key(name)
            if key != my_key:
                return name
        return ""

    def as_rows(self) -> List[Tuple[str, str]]:
        return [
            ("試合ページ", self.url),
            ("キックオフ日", self.kickoff_date),
            ("キックオフ時刻", self.kickoff_time),
            ("節/ラウンド", self.round_label),
            ("ホーム", self.home_team),
            ("アウェイ", self.away_team),
            ("会場", self.venue_name),
            ("住所", self.venue_address),
            ("地図", self.venue_map_url),
            ("中継", self.broadcast),
        ]


def resolve_club_canonical(name: str) -> Optional[str]:
    """クラブ名を正式名称に解決する。分からなければ None。

    このセルはクラブ情報 [5/10] より前に走るので、ここでは分からないと返す。
    [6/10] が読み込まれると、クラブ情報から答える版で上書きされる。"""
    return None


def search_name_variants(name: str) -> List[str]:
    """日程一覧との突き合わせに使うクラブ名の候補。

    このセルはクラブ情報 [5/10] より前に走るので、ここでは入力をそのまま返す。
    [6/10] が読み込まれると、正式名称と短縮名も足す版で上書きされる。
    「グランパス」のような愛称で入力した場合は [10/10] が取り直す。"""
    text = str(name or "").strip()
    return [text] if text else []


class JLeagueScheduleLookup:
    """日程検索から該当カードの試合ページを探し、その中身を読む。

    試合ページはサーバ側で組み立てられているので静的HTMLで読める。
    日程検索のほうはJavaScript描画やマークアップ差で候補が取れないことがあるため、
    条件に合う試合が見つからなければ Playwright で再取得する。"""

    # /match/j1/2026/091902/ の形
    MATCH_URL_PATTERN = re.compile(
        r"/match/(j1|j2|j3|leaguecup|emperor|acle|acl2|acl)/(20\d{2})/(\d{6})/?"
    )
    DATE_PATTERN = re.compile(r"(20\d{2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})")
    # リーグは「第8節」、カップは「3回戦」「準々決勝」などになる。
    # 節番号を数値で持つとカップ戦を表せないので、文字列のまま扱う。
    ROUND_PATTERNS: Tuple[Any, ...] = (
        re.compile(r"第\s*\d{1,3}\s*節"),
        re.compile(r"\d{1,2}\s*回戦"),
        re.compile(r"準々決勝|準決勝|決勝|プレーオフ\S{0,6}|グループステージ\S{0,6}"),
    )
    KICKOFF_PATTERN = re.compile(r"KICK\s*OFF\s*(\d{1,2})\s*[:：]\s*(\d{2})", re.IGNORECASE)
    ADDRESS_PATTERN = re.compile(r"住所\s*([^\s].{2,60}?)\s*(?:地図|https?://)")
    # 中継はページヘッダの o-page-header__broadcast の中だけを見る。
    # 全文から探すと、フッタの「J.LEAGUE OFFICIAL BROADCASTING PARTNER」の
    # DAZNロゴを毎回拾ってしまう。
    BROADCAST_CONTAINER_CLASS: Final[str] = "o-page-header__broadcast"
    BROADCAST_ITEM_CLASS: Final[str] = "o-page-header__broadcast-item"
    # クラス名が変わっても拾えるよう、部分一致でも探す
    BROADCAST_CLASS_HINT: Final[str] = "broadcast"
    BROADCAST_ITEM_CLASS_HINT: Final[str] = "broadcast-item"
    DAZN_HOST: Final[str] = "dazn.com"

    def __init__(
        self,
        fetcher: PageFetcher,
        report: Optional[RunReport] = None,
        render_on_miss: bool = False,
    ):
        self.fetcher = fetcher
        self.report = report
        # 静的HTMLに中継欄が無かったとき、ブラウザで描画し直すか
        self.render_on_miss = render_on_miss
        # 日程一覧で当たった行。試合ページから中継が取れなかったときの控え
        self.last_schedule_row: Optional[Any] = None

    def _warn(self, message: str, *args: Any) -> None:
        emit_warning(self.report, message, *args)

    # ---- 日程検索から試合ページを探す ----------------------------
    @staticmethod
    def _loose_match(listed_name: str, wanted_name: str) -> bool:
        """一覧の表記（「名古屋」「ＦＣ東京」）と入力（「名古屋グランパス」）を
        ゆるく突き合わせる。短いほうが長いほうに含まれていれば同じとみなす。"""
        listed = normalize_club_key(listed_name)
        wanted = normalize_club_key(wanted_name)
        if not listed or not wanted:
            return False
        return listed in wanted or wanted in listed

    def _container_element(self, anchor, max_levels: int = 6):
        """_container_text と同じたどり方で、テキストではなく要素を返す。
        日程一覧の行には中継の表示も入っているので、控えとして使う。"""
        node = anchor
        best = anchor
        for _ in range(max_levels):
            node = node.parent
            if node is None or node.name in ("body", "html", "[document]"):
                break
            urls = {
                match.group(0) for link in node.find_all("a", href=True)
                if (match := self.MATCH_URL_PATTERN.search(str(link.get("href"))))
            }
            if len(urls) != 1:
                break
            best = node
        return best

    def _container_text(self, anchor, max_levels: int = 6) -> str:
        """リンク1本ぶんの「行」のテキスト。

        文字数で打ち切ると、一覧全体を1つの塊として拾ってしまい
        どの候補も両チーム名を含むことになる。そこで、祖先に含まれる
        試合リンクが1本のままである限り上へたどり、2本目が現れる
        直前で止める。マークアップの形に依存しない。"""
        def element_text(element) -> str:
            parts = [element.get_text(" ", strip=True)]
            for child in element.find_all(True):
                for attribute in ("alt", "title", "aria-label", "data-club-name", "data-team-name"):
                    value = child.get(attribute)
                    if value:
                        parts.append(str(value).strip())
            return " ".join(part for part in parts if part)

        best = element_text(anchor)
        node = anchor
        for _ in range(max_levels):
            node = node.parent
            if node is None or node.name in ("body", "html", "[document]"):
                break
            # 1つの行に「対戦データ」「空リンク」など同じ試合への
            # リンクが複数あるので、本数ではなく URL の種類で数える。
            urls = {
                match.group(0) for link in node.find_all("a", href=True)
                if (match := self.MATCH_URL_PATTERN.search(str(link.get("href"))))
            }
            if len(urls) != 1:
                break
            best = element_text(node)
        return best

    ROW_DATE_PATTERN = re.compile(r"(20\d{2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})")

    def _row_date(self, text: str, match_url: str = "") -> Optional[date]:
        found = self.ROW_DATE_PATTERN.search(str(text or ""))
        if found:
            try:
                return date(*(int(value) for value in found.groups()))
            except ValueError:
                pass

        # /match/leaguecup/2026/092908/ の末尾は MMDD + 試合番号。
        # 一覧の表示形式が変わっても、日付は試合ページURLから補える。
        url_match = self.MATCH_URL_PATTERN.search(str(match_url or ""))
        if url_match:
            year = int(url_match.group(2))
            date_code = url_match.group(3)
            try:
                return date(year, int(date_code[:2]), int(date_code[2:4]))
            except ValueError:
                return None
        return None

    def find_target_match(
        self,
        my_team: str,
        opponent_team: str,
        competition: CompetitionType,
    ) -> Tuple[str, str]:
        """実行日から2週間以内で、いちばん近い日の試合を返す。(URL, 行テキスト)

        club= で自チーム（相手を指定していればその2クラブ）に絞るので、
        読み込む行が少なくなる。"""
        today = date.today()
        club_params = [param for param in (club_param_of(my_team), club_param_of(opponent_team))
                       if param]
        url = jleague_search_url(
            competition, today, today + timedelta(days=TARGET_MATCH_WINDOW_DAYS), club_params
        )
        logger.info("%s の日程から対象試合を探します: %s", competition.name, url)

        page = self.fetcher.fetch(
            url, use_playwright_fallback=bool(USE_PLAYWRIGHT_FALLBACK),
            wait_selector="a[href*='/match/']",
        )
        if page is None or page.soup is None:
            self._warn("Jリーグ公式の日程を取得できませんでした: %s", url)
            return "", ""

        def extract_candidates(current_page: FetchResult) -> List[Tuple[date, str, str]]:
            found: List[Tuple[date, str, str]] = []
            seen: set = set()
            date_matched = 0
            teams_matched = 0
            for anchor in current_page.soup.find_all("a", href=True):
                href = str(anchor.get("href"))
                if not self.MATCH_URL_PATTERN.search(href):
                    continue
                match_url = urljoin(current_page.url, href)
                if match_url in seen:
                    continue
                seen.add(match_url)
                surrounding = self._container_text(anchor)
                kickoff = self._row_date(surrounding, match_url)
                if kickoff is None or kickoff < today or kickoff > today + timedelta(days=TARGET_MATCH_WINDOW_DAYS):
                    continue
                date_matched += 1
                if not self._mentions(surrounding, my_team):
                    continue
                if opponent_team and not self._mentions(surrounding, opponent_team):
                    continue
                teams_matched += 1
                found.append((kickoff, match_url, surrounding))
            logger.info(
                "日程HTMLの判定結果: 試合ページリンク=%s件、期間内=%s件、両クラブ一致=%s件",
                len(seen), date_matched, teams_matched,
            )
            return found

        candidates = extract_candidates(page)
        if not candidates and self.render_on_miss and page.source != "Playwright":
            logger.info("静的HTMLから対象試合を特定できないため、Playwrightで日程を再取得します")
            rendered = self.fetcher.fetch(
                url,
                use_playwright_fallback=True,
                wait_selector="a[href*='/match/']",
                force_render=True,
            )
            if rendered is not None and rendered.soup is not None:
                candidates = extract_candidates(rendered)

        if not candidates:
            self._warn(
                "%s の日程に、%s の試合が%s日以内に見つかりませんでした。"
                "大会の選択を確認するか、MATCH_PAGE_URL に試合ページのURLを貼ってください",
                competition.name, my_team, TARGET_MATCH_WINDOW_DAYS,
            )
            return "", ""

        candidates.sort(key=lambda item: item[0])
        kickoff, match_url, surrounding = candidates[0]
        logger.info("対象試合: %s（%s）", kickoff.isoformat(), match_url)
        return match_url, surrounding

    def _mentions(self, text: str, team: str) -> bool:
        if not team:
            return True
        normalized = normalize_club_key(text)
        fragments = self._name_fragments(text)
        for variant in search_name_variants(team):
            variant_key = normalize_club_key(variant)
            if not variant_key:
                continue
            if variant_key in normalized:
                return True
            if any(self._loose_match(fragment, variant) for fragment in fragments):
                return True
        return False

    def _loose_match_pair(self, text: str, my_team: str, opponent_team: str) -> bool:
        """1行ぶんのテキストに両チームが出ているか。
        入力が正式名称でも短縮名でも通るよう、部分一致の両方向で見る。"""
        normalized = normalize_club_key(text)
        fragments = self._name_fragments(text)

        def is_present(team: str) -> bool:
            for variant in search_name_variants(team):
                variant_key = normalize_club_key(variant)
                if not variant_key:
                    continue
                if variant_key in normalized:
                    return True
                if any(self._loose_match(fragment, variant) for fragment in fragments):
                    return True
            return False

        return is_present(my_team) and is_present(opponent_team)

    @staticmethod
    def _name_fragments(text: str) -> List[str]:
        """空白区切りの断片。一覧の「名古屋」「ＦＣ東京」を個別に取り出す。"""
        return [fragment for fragment in re.split(r"[\s　]+", text) if len(fragment) >= 2]

    @classmethod
    def extract_round_label(cls, *texts: str) -> str:
        """「第8節」「3回戦」「準決勝」など、そのまま記事に出せる形で返す。
        節番号を数値で持つとカップ戦で表せなくなるので、文字列のまま扱う。"""
        for text in texts:
            for pattern in cls.ROUND_PATTERNS:
                if found := pattern.search(str(text or "")):
                    return re.sub(r"\s+", "", found.group(0))
        return ""

    # ---- 試合ページを読む --------------------------------------
    def read_match_page(self, match_url: str) -> Optional[JLeagueMatchInfo]:
        page = self.fetcher.fetch(match_url)
        if page is None or page.soup is None:
            self._warn("試合ページを取得できませんでした: %s", match_url)
            return None

        soup = page.soup
        text = soup.get_text(" ", strip=True)

        kickoff_date = ""
        if date_match := self.DATE_PATTERN.search(text):
            year, month, day = (int(value) for value in date_match.groups())
            try:
                kickoff_date = date(year, month, day).isoformat()
            except ValueError:
                logger.debug("日付として解釈できませんでした: %s", date_match.group(0))

        kickoff_time = ""
        if time_match := self.KICKOFF_PATTERN.search(text):
            kickoff_time = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"

        round_label = self.extract_round_label(text)

        home_team, away_team = self._extract_teams(soup, text)
        venue_name, venue_address, venue_map_url = self._extract_venue(soup, text)

        info = JLeagueMatchInfo(
            url=page.url,
            kickoff_date=kickoff_date,
            kickoff_time=kickoff_time,
            round_label=round_label,
            home_team=home_team,
            away_team=away_team,
            venue_name=venue_name,
            venue_address=venue_address,
            venue_map_url=venue_map_url,
            broadcast=self._resolve_broadcast(soup, page.url),
        )
        missing = [label for label, value in info.as_rows() if not value]
        if missing:
            logger.info("試合ページから読めなかった項目: %s", missing)
        return info

    @staticmethod
    def _dedupe_repeated_name(name: str) -> str:
        """クラブ名は「ＦＣ東京FC東京」「ＦＣ町田ゼルビア町田」のように、
        正式名称のうしろに短縮名がくっついた形で出てくる。

        クラブ情報が使えるなら、前半と後半が同じクラブに解決できる切れ目を
        探す。これなら「ファジアーノ岡山岡山」のように短縮名が先頭に来ない
        クラブでも正しくほどける。
        クラブ情報より前（[4/10] の実行時）は、後半が前半の接頭辞になる
        切れ目を探す簡易な方法で代用する。"""
        for split_at in range(1, len(name)):
            head, tail = name[:split_at], name[split_at:]
            head_club = resolve_club_canonical(head)
            if head_club and head_club == resolve_club_canonical(tail):
                return head
        for split_at in range(len(name) - 1, 0, -1):
            head, tail = name[:split_at], name[split_at:]
            head_key, tail_key = normalize_club_key(head), normalize_club_key(tail)
            if tail_key and head_key.startswith(tail_key):
                return head
        return name

    @classmethod
    def _extract_teams(cls, soup: BeautifulSoup, text: str) -> Tuple[str, str]:
        """ホームが先、アウェイが後。クラブページへのリンクの並びで判断する。
        タイトルの「A vs B」より、リンクのほうが表記が安定している。"""
        club_names: List[str] = []
        for anchor in soup.find_all("a", href=True):
            if not re.search(r"/club/[a-z0-9]+/?$", str(anchor.get("href"))):
                continue
            name = anchor.get_text(" ", strip=True)
            # 「5位(4勝2分1敗) ＦＣ東京FC東京5位(4勝2分1敗)」のように順位が混ざる
            name = re.sub(r"\d+位\s*[（(][^）)]*[）)]", " ", name).strip()
            fragments = [part for part in re.split(r"[\s　]+", name) if part]
            if fragments:
                club_names.append(cls._dedupe_repeated_name(fragments[0]))
            if len(club_names) >= 2:
                break
        if len(club_names) >= 2:
            return club_names[0], club_names[1]
        if title_match := re.search(r"([^\s|【】]+)\s*vs\s*([^\s|【】]+)", text):
            return title_match.group(1), title_match.group(2)
        return "", ""

    @classmethod
    def _extract_venue(cls, soup: BeautifulSoup, text: str) -> Tuple[str, str, str]:
        """会場名と住所は、ページに貼られているGoogleマップのリンクから取る。
        query が「<会場名> 日本 <住所>」の形になっているので、そこを割る。"""
        venue_name = ""
        venue_address = ""
        venue_map_url = ""
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href"))
            if "google.com/maps" not in href or "query=" not in href:
                continue
            query = parse_qs(urlparse(href).query).get("query", [""])[0]
            if not query:
                continue
            venue_map_url = href
            parts = re.split(r"\s+日本\s+", query, maxsplit=1)
            venue_name = parts[0].strip()
            if len(parts) > 1:
                venue_address = parts[1].strip()
            break
        if not venue_address and (address_match := cls.ADDRESS_PATTERN.search(text)):
            venue_address = address_match.group(1).strip()
        return venue_name, venue_address, venue_map_url

    @staticmethod
    def _class_list(node) -> List[str]:
        return [str(name) for name in (node.get("class") or [])]

    @classmethod
    def _broadcast_container(cls, root) -> Optional[Any]:
        """中継欄の入れ物を探す。まず指定のクラス、だめならクラス名に
        broadcast を含む要素。フッタの放送パートナー欄は除く。"""
        container = root.find(class_=cls.BROADCAST_CONTAINER_CLASS)
        if container is not None:
            return container
        for node in root.find_all(attrs={"class": True}):
            classes = cls._class_list(node)
            if not any(cls.BROADCAST_CLASS_HINT in name.lower() for name in classes):
                continue
            if any(cls.BROADCAST_ITEM_CLASS_HINT in name.lower() for name in classes):
                node = node.parent if node.parent is not None else node
            if node.find_parent("footer") is not None:
                continue
            if any("partner" in name.lower() for name in cls._class_list(node)):
                continue
            logger.info("中継欄をクラス名の部分一致で見つけました: %s", " ".join(cls._class_list(node)))
            return node
        return None

    @classmethod
    def _read_broadcast_items(cls, root) -> str:
        """入れ物の中から放送局名を並べる。
          DAZN            … <a> の中の <img alt="DAZN">
          NHK BS など     … <span> のテキスト
        日程一覧では「スカチャン5・スカパー！動画ストア」のように
        1つの要素へ「・」で連結されることがあるので割る。"""
        items = root.find_all(class_=cls.BROADCAST_ITEM_CLASS)
        if not items:
            items = [
                node for node in root.find_all(attrs={"class": True})
                if any(cls.BROADCAST_ITEM_CLASS_HINT in name.lower() for name in cls._class_list(node))
            ]
        names: List[str] = []
        for item in items:
            image = item.find("img")
            name = str(image.get("alt") or "").strip() if image is not None else ""
            if not name:
                name = item.get_text(" ", strip=True)
            names.extend(part.strip() for part in re.split(r"[・/／]", name) if part.strip())
        if not names:
            # 項目要素が無くても、DAZNへのリンクがあればDAZN中継とみなす
            for anchor in root.find_all("a", href=True):
                if cls.DAZN_HOST not in str(anchor.get("href")).lower():
                    continue
                image = anchor.find("img")
                names.append(str(image.get("alt") or "").strip() if image is not None else "DAZN")
        return " / ".join(dict.fromkeys(name for name in names if name))

    @classmethod
    def _extract_broadcast(cls, soup: BeautifulSoup) -> str:
        container = cls._broadcast_container(soup)
        if container is None:
            cls._log_broadcast_diagnostics(soup)
            return ""
        return cls._read_broadcast_items(container)

    @classmethod
    def _log_broadcast_diagnostics(cls, soup: BeautifulSoup) -> None:
        """なぜ取れなかったのかを切り分けられるように残す。
        クラス名が変わったのか、そもそもHTMLに入っていないのかで対処が違う。"""
        found = sorted({
            " ".join(cls._class_list(node)) for node in soup.find_all(attrs={"class": True})
            if any(cls.BROADCAST_CLASS_HINT in name.lower() for name in cls._class_list(node))
        })
        if found:
            logger.warning(
                "中継欄のクラス名が想定と違います。ページにあったクラス: %s", found[:5]
            )
        else:
            logger.warning(
                "中継欄が静的HTMLに含まれていません（JavaScriptで描画されている可能性）。"
                "[1/10] の INSTALL_PLAYWRIGHT_BROWSER と [4/10] の USE_PLAYWRIGHT_FALLBACK を"
                " True にして実行し直すと取れることがあります"
            )

    def _resolve_broadcast(self, soup: BeautifulSoup, match_url: str) -> str:
        """試合ページ → 日程一覧の行 → ブラウザ描画 の順に中継を探す。"""
        broadcast = self._extract_broadcast(soup)
        if broadcast:
            return broadcast

        if self.last_schedule_row is not None:
            broadcast = self._read_broadcast_items(self.last_schedule_row)
            if broadcast:
                logger.info("中継は日程一覧の行から拾いました: %s", broadcast)
                return broadcast

        if self.render_on_miss:
            logger.info("中継欄を探すため、試合ページをブラウザで描画し直します")
            rendered = self.fetcher.fetch(
                match_url, force_render=True,
                wait_selector=f".{self.BROADCAST_ITEM_CLASS}",
            )
            if rendered is not None and rendered.soup is not None:
                broadcast = self._extract_broadcast(rendered.soup)
                if broadcast:
                    logger.info("中継はブラウザ描画で拾えました: %s", broadcast)
                    return broadcast
        return ""

    # ---- まとめ ------------------------------------------------
    def resolve(
        self,
        my_team: str,
        opponent_team: str,
        competition: CompetitionType,
        match_page_url: str = "",
    ) -> Optional[JLeagueMatchInfo]:
        url = str(match_page_url or "").strip()
        row_text = ""
        if url:
            logger.info("指定された試合ページを読みます: %s", url)
        else:
            url, row_text = self.find_target_match(my_team, opponent_team, competition)
            if not url:
                return None
        info = self.read_match_page(url)
        if info is not None and not info.round_label and row_text:
            # 試合ページから読めなければ、一覧の行から拾う
            info = replace(info, round_label=self.extract_round_label(row_text))
        return info


# ============================================================
# 前節のハイライト動画を探す
# ------------------------------------------------------------
# YouTube の robots.txt は検索結果(/results)を禁じているので、
# YouTube 側は検索しない。Jリーグ公式の日程・結果から辿る。
#
#   1. シーズン開幕〜実行日の日程・結果を新しい順で開く
#        https://www.jleague.jp/j1/match/search-list/
#          ?startdate=2026-08-01&enddate=2026-09-18&sort_desc=true
#   2. 上から見て、対戦相手が出てくる最初の行 = その相手の直近試合
#   3. その試合ページを開き、#review にある youtube-video / iframe から
#      YouTube のURLを取り出す
#
# 大会を絞っていないので、直近試合がカップ戦のこともある。
# 動画が無ければ次に新しい試合へ進む。
# ============================================================
# シーズンは8月開幕。"2026/27" なら 2026-08-01 から。
YOUTUBE_ID_PATTERN = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
YOUTUBE_WATCH_URL: Final[str] = "https://www.youtube.com/watch?v={video_id}"
# 直近から何試合までさかのぼって動画を探すか
HIGHLIGHT_MAX_MATCHES: Final[int] = 4


@dataclass(frozen=True)
class HighlightVideo:
    url: str
    title: str = ""
    round_label: str = ""
    match_url: str = ""


class JLeagueHighlightLookup:
    """対戦相手の直近試合のハイライト動画を1本だけ返す。"""

    def __init__(self, fetcher: PageFetcher, report: Optional[RunReport] = None):
        self.fetcher = fetcher
        self.report = report
        # 行の切り出しは日程検索と同じやり方を使う
        self._schedule = JLeagueScheduleLookup(fetcher, report, render_on_miss=True)

    def _warn(self, message: str, *args: Any) -> None:
        emit_warning(self.report, message, *args)

    @staticmethod
    def lookback_start(today: date, months: int = 2) -> date:
        """暦上の指定月数前。同じ日がない月は月末に合わせる。"""
        month_index = today.year * 12 + (today.month - 1) - months
        year, month_index = divmod(month_index, 12)
        month = month_index + 1
        day = min(today.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    def search_url(self, league: str, season: str, club_param: str = "") -> str:
        """実行日の2カ月前から今日まで、新しい順。club= でそのクラブに絞る。
        全大会を見たいので category はリーグのコードを入れつつ、
        カップ戦も拾えるよう同じパスの一覧を使う。"""
        code = str(league or "j1").strip().lower()
        if code not in JLEAGUE_SCHEDULE_LEAGUES:
            code = "j1"
        competition = CompetitionType(code.upper(), code, code, "", True)
        today = date.today()
        start = self.lookback_start(today)
        logger.info("ハイライト検索期間: %s から %s", start.isoformat(), today.isoformat())
        return jleague_search_url(
            competition, start, today,
            club_params=[club_param] if club_param else (), sort_desc=True,
        )

    # ---- 直近試合を探す ----------------------------------------
    def _mentions(self, text: str, team: str) -> bool:
        normalized = normalize_club_key(text)
        return any(normalize_club_key(name) in normalized
                   for name in search_name_variants(team) if name)

    def recent_match_urls(self, opponent_team: str, league: str, season: str) -> List[Tuple[str, str]]:
        """新しい順に、対戦相手が出てくる試合のURLと行テキストを返す。"""
        url = self.search_url(league, season, club_param_of(opponent_team))
        logger.info("日程・結果から直近試合を探します: %s", url)
        page = self.fetcher.fetch(
            url, use_playwright_fallback=bool(USE_PLAYWRIGHT_FALLBACK),
            wait_selector="a[href*='/match/']",
        )
        if page is None or page.soup is None:
            self._warn("Jリーグ公式の日程・結果を取得できませんでした: %s", url)
            return []

        today = date.today()

        def extract_candidates(current_page: FetchResult) -> List[Tuple[date, str, str]]:
            candidates: List[Tuple[date, str, str]] = []
            seen: set = set()
            for anchor in current_page.soup.find_all("a", href=True):
                href = str(anchor.get("href"))
                if not self._schedule.MATCH_URL_PATTERN.search(href):
                    continue
                match_url = urljoin(current_page.url, href)
                if match_url in seen:
                    continue
                seen.add(match_url)
                surrounding = self._schedule._container_text(anchor)
                if not self._mentions(surrounding, opponent_team):
                    continue
                match_date = self._schedule._row_date(surrounding, match_url)
                if match_date is None or match_date > today:
                    continue
                candidates.append((match_date, match_url, surrounding))
            return candidates

        candidates = extract_candidates(page)
        if (not candidates and page.source != "Playwright"
                and USE_PLAYWRIGHT_FALLBACK):
            logger.info("静的HTMLから過去試合を特定できないため、Playwrightで日程を再取得します")
            rendered = self.fetcher.fetch(
                url, use_playwright_fallback=True,
                wait_selector="a[href*='/match/']", force_render=True,
            )
            if rendered is not None and rendered.soup is not None:
                candidates = extract_candidates(rendered)
        candidates.sort(key=lambda item: item[0], reverse=True)
        found = [(match_url, row_text) for _, match_url, row_text in
                 candidates[:HIGHLIGHT_MAX_MATCHES]]
        if not found:
            self._warn("%s の直近試合が日程・結果に見つかりませんでした", opponent_team)
        return found

    # ---- 試合ページから動画を取り出す ---------------------------
    @classmethod
    def extract_video(cls, page: FetchResult) -> Tuple[str, str]:
        """#review に置かれた動画のURLとタイトルを返す。"""
        for node in page.soup.find_all("youtube-video"):
            if video_id := cls._video_id(str(node.get("src") or "")):
                title = ""
                if iframe := node.find("iframe"):
                    title = str(iframe.get("title") or "")
                return YOUTUBE_WATCH_URL.format(video_id=video_id), title
        for iframe in page.soup.find_all("iframe", src=True):
            if video_id := cls._video_id(str(iframe.get("src"))):
                return (YOUTUBE_WATCH_URL.format(video_id=video_id),
                        str(iframe.get("title") or ""))
        # 埋め込みが見つからないときの控え。本文中のリンクを拾う
        for anchor in page.soup.find_all("a", href=True):
            if video_id := cls._video_id(str(anchor.get("href"))):
                return (YOUTUBE_WATCH_URL.format(video_id=video_id),
                        anchor.get_text(" ", strip=True))
        return "", ""

    @staticmethod
    def _video_id(text: str) -> str:
        found = YOUTUBE_ID_PATTERN.search(text)
        return found.group(1) if found else ""

    VIDEO_WAIT_SELECTOR: Final[str] = "youtube-video, iframe[src*='youtube'], a[href*='youtu']"

    def _read_video(self, match_url: str) -> Tuple[str, str]:
        """試合ページから動画を取り出す。

        ハイライトは #review のタブに置かれていて、JavaScript で組み立てられる。
        しかも <youtube-video> は中身を shadow root に持つ。静的HTMLには
        入ってこないので、最初からブラウザで、URLに #review を付けて開く。"""
        review_url = f"{match_url.rstrip('/')}/#review"
        if not USE_PLAYWRIGHT_FALLBACK:
            page = self.fetcher.fetch(match_url)
            return self.extract_video(page) if page and page.soup else ("", "")
        page = self.fetcher.fetch(
            review_url, force_render=True, wait_selector=self.VIDEO_WAIT_SELECTOR
        )
        if page is None or page.soup is None:
            return "", ""
        return self.extract_video(page)

    # ---- まとめ ------------------------------------------------
    def find(
        self,
        opponent_team: str,
        league: str,
        season: str,
        exclude_urls: Sequence[str] = (),
    ) -> Optional[HighlightVideo]:
        skip = {str(url).rstrip("/") for url in exclude_urls if url}
        for match_url, row_text in self.recent_match_urls(opponent_team, league, season):
            if match_url.rstrip("/") in skip:
                logger.info("いま書いている試合なので飛ばします: %s", match_url)
                continue
            video_url, title = self._read_video(match_url)
            if not video_url:
                logger.info("この試合ページには動画がありませんでした: %s", match_url)
                continue
            section = JLeagueScheduleLookup.extract_round_label(title, row_text)
            logger.info("ハイライト動画: %s", title or video_url)
            return HighlightVideo(
                url=video_url, title=title, round_label=section,
                match_url=f"{match_url.rstrip('/')}/#review",
            )
        self._warn(
            "%s の直近試合のハイライト動画が見つかりませんでした。手で貼ってください",
            opponent_team,
        )
        return None


def fetch_previous_highlight(
    opponent_team: str,
    season: str,
    report: Optional[RunReport] = None,
) -> Optional[HighlightVideo]:
    """対戦相手の直近試合のハイライトを探す。

    いま書いている試合そのものは除く。実行日が試合日より後だと、
    その試合が一覧の先頭に来てしまうため。"""
    if not SEARCH_HIGHLIGHT_VIDEO:
        logger.info("SEARCH_HIGHLIGHT_VIDEO が False のため、ハイライトは探しません")
        return None
    league = search_league_hint(opponent_team) or "j1"
    network = build_network_config()
    lookup = JLeagueHighlightLookup(PageFetcher(build_session(network), network, report), report)
    exclude: List[str] = []
    # Colab版ではグローバルに試合情報を持つが、ローカルGUIではその名前自体が
    # 定義されない。globals().get で両方の実行方式を扱い、NameErrorを避ける。
    current_info = globals().get("JLEAGUE_MATCH_INFO")
    current_url = str(getattr(current_info, "url", "") or "").strip()
    if current_url:
        exclude.append(current_url)
    configured_url = str(globals().get("MATCH_PAGE_URL", "") or "").strip()
    if configured_url:
        exclude.append(configured_url)
    return lookup.find(opponent_team, league, season, exclude_urls=exclude)


# ============================================================
# Jリーグ公式の順位表
# ------------------------------------------------------------
#   https://www.jleague.jp/j1/standings/  （j2 / j3 も同じ形）
# 表はサーバ側で組み立てられているので静的HTMLで読める。
# 列は 順位 / クラブ / 勝点 / 試合 / 勝 / 分 / 負 / 得点 / 失点 / 得失 / 直近5試合。
# 記事には「直近5試合」以外を載せる。
# ============================================================
STANDINGS_URL_TEMPLATE: Final[str] = "https://www.jleague.jp/{league}/standings/"
STANDINGS_COLUMNS: Final[Tuple[str, ...]] = (
    "順位", "クラブ", "勝点", "試合数", "勝", "分", "負", "得点", "失点", "得失点",
)
# ページの見出しと、記事に載せる列名の対応。ページ側は「試合」「得失」と短い
STANDINGS_HEADER_ALIASES: Final[Dict[str, str]] = {
    "順位": "順位", "クラブ": "クラブ", "勝点": "勝点",
    "試合": "試合数", "試合数": "試合数",
    "勝": "勝", "分": "分", "負": "負",
    "得点": "得点", "失点": "失点", "得失": "得失点", "得失点": "得失点",
}


@dataclass(frozen=True)
class StandingsTable:
    url: str
    league: str
    rows: List[List[str]] = field(default_factory=list)

    @property
    def clubs(self) -> List[str]:
        return [row[1] for row in self.rows[1:] if len(row) > 1]


class JLeagueStandings:
    RANK_PATTERN = re.compile(r"(\d{1,2})")

    def __init__(self, fetcher: PageFetcher, report: Optional[RunReport] = None):
        self.fetcher = fetcher
        self.report = report

    def _warn(self, message: str, *args: Any) -> None:
        emit_warning(self.report, message, *args)

    @staticmethod
    def url_for(league: str) -> str:
        code = str(league or "j1").strip().lower()
        if code not in JLEAGUE_SCHEDULE_LEAGUES:
            code = "j1"
        return STANDINGS_URL_TEMPLATE.format(league=code)

    @classmethod
    def _header_map(cls, row) -> Dict[int, str]:
        """見出し行から「何列目が何か」を作る。列の並びが変わっても追従する。"""
        mapping: Dict[int, str] = {}
        for index, cell in enumerate(row.find_all(["th", "td"])):
            # 見出しは「順位 順位」のように2度書かれていることがある
            text = cell.get_text(" ", strip=True).split()
            head = text[0] if text else ""
            if name := STANDINGS_HEADER_ALIASES.get(head):
                mapping.setdefault(index, name)
        return mapping

    @classmethod
    def _find_header_row(cls, rows) -> Tuple[int, Dict[int, str]]:
        """見出し行は必ずしも1行目とは限らないので、先頭数行から探す。"""
        for index, row in enumerate(rows[:4]):
            mapping = cls._header_map(row)
            names = set(mapping.values())
            if "順位" in names and "勝点" in names:
                return index, mapping
        return -1, {}

    @staticmethod
    def _club_column(rows) -> int:
        """クラブ名の列は、クラブページへのリンクがあるセルで見分ける。

        見出しの文字列に頼ると、ページ側が「クラブ」と書かない作りに
        変わったとたんに表ごと落とすことになる。リンクの有無なら
        見出しの文言が変わっても効く。"""
        for row in rows:
            for index, cell in enumerate(row.find_all(["th", "td"])):
                anchor = cell.find("a", href=True)
                if anchor and re.search(r"/club/[a-z0-9\-]+/?$", str(anchor.get("href"))):
                    return index
        return -1

    def _club_name(self, cell) -> str:
        """「ＦＣ町田ゼルビア町田」のように正式名称と短縮名が続くのでほどく。"""
        anchor = cell.find("a")
        text = (anchor or cell).get_text(" ", strip=True)
        fragments = [part for part in re.split(r"[\s　]+", text) if part]
        return JLeagueScheduleLookup._dedupe_repeated_name(fragments[0]) if fragments else ""

    def fetch(self, league: str) -> Optional[StandingsTable]:
        url = self.url_for(league)
        page = self.fetcher.fetch(url, use_playwright_fallback=bool(USE_PLAYWRIGHT_FALLBACK),
                                  wait_selector="table")
        if page is None or page.soup is None:
            self._warn("Jリーグ公式の順位表を取得できませんでした: %s", url)
            return None

        for table in page.soup.find_all("table"):
            table_rows = table.find_all("tr")
            if not table_rows:
                continue
            header_index, header = self._find_header_row(table_rows)
            if header_index < 0:
                continue
            data_rows = table_rows[header_index + 1:]

            wanted = {name: index for index, name in header.items()}
            club_column = wanted.get("クラブ", self._club_column(data_rows))
            if club_column < 0:
                logger.debug("クラブ名の列を特定できませんでした: %s", url)
                continue
            wanted["クラブ"] = club_column

            # 得失点はページに無くても得点と失点から出せる
            computed_goal_diff = "得失点" not in wanted
            missing = [name for name in STANDINGS_COLUMNS
                       if name not in wanted and not (name == "得失点" and computed_goal_diff)]
            if missing:
                found = [cell.get_text(" ", strip=True)
                         for cell in table_rows[header_index].find_all(["th", "td"])]
                self._warn("順位表に見つからない列があります: %s（見出し: %s）", missing, found)
                continue

            rows: List[List[str]] = [list(STANDINGS_COLUMNS)]
            for table_row in data_rows:
                cells = table_row.find_all(["th", "td"])
                if len(cells) <= max(wanted.values()):
                    continue
                rank_text = cells[wanted["順位"]].get_text(" ", strip=True)
                # 順位のセルには昇降の矢印アイコンが入るので数字だけ取る
                rank = self.RANK_PATTERN.search(rank_text)
                club = self._club_name(cells[club_column])
                if not rank or not club:
                    continue
                values = [rank.group(1), club]
                for name in STANDINGS_COLUMNS[2:]:
                    if name == "得失点" and computed_goal_diff:
                        values.append(self._goal_difference(values))
                        continue
                    values.append(cells[wanted[name]].get_text(" ", strip=True))
                rows.append(values)

            if len(rows) <= 1:
                continue
            logger.info("順位表を取得しました: %s（%sクラブ）", url, len(rows) - 1)
            return StandingsTable(url=url, league=str(league or "j1").upper(), rows=rows)

        self._warn("順位表の表を見つけられませんでした: %s", url)
        return None

    @staticmethod
    def _goal_difference(values: Sequence[str]) -> str:
        """得失点がページに無い場合に、得点と失点から出す。"""
        try:
            scored = int(values[STANDINGS_COLUMNS.index("得点")])
            conceded = int(values[STANDINGS_COLUMNS.index("失点")])
        except (ValueError, IndexError):
            return ""
        return str(scored - conceded)


def fetch_standings(my_team: str, report: Optional[RunReport] = None) -> Optional[StandingsTable]:
    """自チームのカテゴリの順位表を取る。"""
    league = search_league_hint(my_team) or "j1"
    lookup = JLeagueStandings(
        PageFetcher(build_session(build_network_config()), build_network_config(), report), report
    )
    return lookup.fetch(league)


# ============================================================
# tenki.jp から試合時間の天気を拾う
# ------------------------------------------------------------
#   1. 住所の都道府県から、サッカー場一覧の都道府県ページへ
#   2. その中から会場名でスタジアムのページを探す
#   3. 1hour.html の該当日・該当時刻の列を読む
#
# 1hour.html は今日・明日・明後日の3日ぶん。それより先の試合は
# 同じページの「10日間天気」から日単位で拾う（時刻別にはならない）。
# ============================================================
TENKI_SOCCER_INDEX_URL: Final[str] = "https://tenki.jp/leisure/soccer/"
# 「曇り / 気温 22.7℃前後 / 降水確率 30%」の形に整える
WEATHER_TEXT_FORMAT: Final[str] = "{weather} / 気温 {temperature}℃前後 / 降水確率 {precipitation}%の予想です。"
WEATHER_TEXT_FORMAT_DAILY: Final[str] = "{weather} / 最高 {high}℃ 最低 {low}℃ / 降水確率 {precipitation}%の予想です。"
PREFECTURE_PATTERN = re.compile(r"^\s*(北海道|東京都|京都府|大阪府|.{2,3}県)")


@dataclass(frozen=True)
class WeatherForecast:
    url: str = ""
    text: str = ""
    hourly: bool = True


class TenkiJpForecast:
    """サッカー場の天気を tenki.jp から拾う。"""

    HOUR_COUNT: Final[int] = 24
    DATE_IN_TABLE = re.compile(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日")
    DATE_MONTH_DAY = re.compile(r"(\d{1,2})月\s*(\d{1,2})日")

    def __init__(self, fetcher: PageFetcher, report: Optional[RunReport] = None):
        self.fetcher = fetcher
        self.report = report
        self._prefecture_links: Dict[str, str] = {}

    def _warn(self, message: str, *args: Any) -> None:
        emit_warning(self.report, message, *args)

    # ---- 会場ページを探す --------------------------------------
    @staticmethod
    def prefecture_of(address: str) -> str:
        match = PREFECTURE_PATTERN.match(str(address or ""))
        return match.group(1) if match else ""

    def prefecture_links(self) -> Dict[str, str]:
        """サッカー場一覧の都道府県リンク。北海道は道北・道央などに分かれている。"""
        if self._prefecture_links:
            return self._prefecture_links
        page = self.fetcher.fetch(TENKI_SOCCER_INDEX_URL)
        if page is None or page.soup is None:
            self._warn("tenki.jp のサッカー場一覧を取得できませんでした")
            return {}
        links: Dict[str, str] = {}
        for anchor in page.soup.find_all("a", href=True):
            href = str(anchor.get("href"))
            if not re.search(r"/leisure/soccer/\d+/\d+/?$", href):
                continue
            name = anchor.get_text(" ", strip=True)
            if name:
                links[name] = urljoin(page.url, href)
        self._prefecture_links = links
        return links

    def prefecture_page_urls(self, prefecture: str) -> List[str]:
        links = self.prefecture_links()
        if not links:
            return []
        if prefecture in links:
            return [links[prefecture]]
        if prefecture == "北海道":
            # 一覧では 道北・道東・道央・道南 に分かれているので順に当たる
            return [url for name, url in links.items() if name.startswith("道")]
        self._warn("tenki.jp のサッカー場一覧に %r がありません", prefecture)
        return []

    @staticmethod
    def _name_matches(listed: str, wanted: str) -> bool:
        """「ＭＵＦＧスタジアム」と「MUFGスタジアム（国立競技場）」を同じとみなす。"""
        listed_key = normalize_club_key(re.sub(r"[（(].*?[）)]", "", listed))
        wanted_key = normalize_club_key(re.sub(r"[（(].*?[）)]", "", wanted))
        if not listed_key or not wanted_key:
            return False
        if listed_key in wanted_key or wanted_key in listed_key:
            return True
        # 「長崎スタジアムシティ」と「長崎スタジアム」のように途中まで一致する場合
        common = 0
        for left, right in zip(listed_key, wanted_key):
            if left != right:
                break
            common += 1
        return common >= 4

    def find_stadium_url(self, venue_name: str, address: str) -> str:
        prefecture = self.prefecture_of(address)
        if not prefecture:
            self._warn("住所から都道府県を読み取れませんでした: %r", address)
            return ""
        for page_url in self.prefecture_page_urls(prefecture):
            page = self.fetcher.fetch(page_url)
            if page is None or page.soup is None:
                continue
            for anchor in page.soup.find_all("a", href=True):
                href = str(anchor.get("href"))
                if not re.search(r"/leisure/soccer/\d+/\d+/\d+/?$", href):
                    continue
                # リンク文字列には「曇25℃ / 20℃」のような天気が続くので落とす
                listed = re.split(r"[晴曇雨雪]", anchor.get_text(" ", strip=True))[0].strip()
                if self._name_matches(listed, venue_name):
                    logger.info("tenki.jp の会場ページ: %s（%s）", listed, urljoin(page.url, href))
                    return urljoin(page.url, href)
        self._warn(
            "tenki.jp の%s一覧に「%s」が見つかりませんでした。天気は手で入れてください",
            prefecture, venue_name,
        )
        return ""

    # ---- 表を読む ----------------------------------------------
    @classmethod
    def _tail_cells(cls, row, count: int) -> List[str]:
        cells = row.find_all(["td", "th"])
        if len(cells) < count:
            return []
        return [cell.get_text(" ", strip=True) for cell in cells[-count:]]

    @classmethod
    def _find_hour_row(cls, rows) -> Tuple[int, List[int]]:
        """01〜24 が並ぶ行を探す。行頭の見出しセルの有無に左右されないよう、
        末尾24セルだけを見る。"""
        for index, row in enumerate(rows):
            values = cls._tail_cells(row, cls.HOUR_COUNT)
            if not values:
                continue
            try:
                hours = [int(value) for value in values]
            except ValueError:
                continue
            if hours == list(range(1, cls.HOUR_COUNT + 1)):
                return index, hours
        return -1, []

    @classmethod
    def _values_for_label(cls, rows, label: str) -> List[str]:
        """見出しの行、または見出しの直後にある24セルの行を返す。
        気温は見出し行と値の行が分かれている。"""
        for index, row in enumerate(rows):
            head = row.find(["th", "td"])
            if head is None or label not in head.get_text(" ", strip=True):
                continue
            for candidate in rows[index:index + 3]:
                values = cls._tail_cells(candidate, cls.HOUR_COUNT)
                if values and any(value for value in values):
                    return values
        return []

    def read_hourly(self, page: FetchResult, target_date: date, hour: int) -> Optional[Dict[str, str]]:
        for table in page.soup.find_all("table"):
            match = self.DATE_IN_TABLE.search(table.get_text(" ", strip=True))
            if not match:
                continue
            year, month, day = (int(value) for value in match.groups())
            if (year, month, day) != (target_date.year, target_date.month, target_date.day):
                continue
            rows = table.find_all("tr")
            hour_index, hours = self._find_hour_row(rows)
            if hour_index < 0:
                continue
            if hour not in hours:
                continue
            column = hours.index(hour)
            weather = self._values_for_label(rows, "天気")
            temperature = self._values_for_label(rows, "気温")
            precipitation = self._values_for_label(rows, "降水確率")
            if not (weather and temperature and precipitation):
                continue
            return {
                "weather": weather[column],
                "temperature": temperature[column],
                "precipitation": precipitation[column].replace("%", "").strip(),
            }
        return None

    def read_daily(self, page: FetchResult, target_date: date) -> Optional[Dict[str, str]]:
        """10日間天気の表から日単位で拾う。時刻別は取れない。"""
        for table in page.soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            header_cells = rows[0].find_all(["th", "td"])
            columns: List[Optional[Tuple[int, int]]] = []
            for cell in header_cells:
                found = self.DATE_MONTH_DAY.search(cell.get_text(" ", strip=True))
                columns.append((int(found.group(1)), int(found.group(2))) if found else None)
            if (target_date.month, target_date.day) not in [c for c in columns if c]:
                continue
            column = columns.index((target_date.month, target_date.day))
            count = len(header_cells)

            def row_values(label: str) -> List[str]:
                for row in rows:
                    head = row.find(["th", "td"])
                    if head is None or label not in head.get_text(" ", strip=True):
                        continue
                    cells = row.find_all(["td", "th"])
                    if len(cells) >= count:
                        return [cell.get_text(" ", strip=True) for cell in cells[-count:]]
                return []

            weather = row_values("天気")
            temperature = row_values("気温")
            precipitation = row_values("降水")
            if not (weather and temperature and precipitation):
                continue
            # 気温セルは「27 22」のように最高・最低が続く
            numbers = re.findall(r"-?\d+", temperature[column])
            return {
                "weather": weather[column],
                "high": numbers[0] if numbers else "",
                "low": numbers[1] if len(numbers) > 1 else "",
                "precipitation": precipitation[column].replace("%", "").strip(),
            }
        return None

    # ---- まとめ ------------------------------------------------
    def resolve(
        self,
        venue_name: str,
        address: str,
        kickoff_date: str,
        kickoff_time: str,
    ) -> Optional[WeatherForecast]:
        if not (venue_name and address and kickoff_date):
            logger.info("会場・住所・キックオフ日が揃っていないため天気は取得しません")
            return None
        try:
            target_date = datetime.strptime(kickoff_date, "%Y-%m-%d").date()
        except ValueError:
            self._warn("キックオフ日 %r を解釈できないため天気を取得できません", kickoff_date)
            return None
        hour_match = re.match(r"\s*(\d{1,2})", str(kickoff_time or ""))
        hour = int(hour_match.group(1)) if hour_match else 0

        stadium_url = self.find_stadium_url(venue_name, address)
        if not stadium_url:
            return None
        hourly_url = urljoin(stadium_url, "1hour.html")
        page = self.fetcher.fetch(hourly_url)
        if page is None or page.soup is None:
            self._warn("tenki.jp の1時間天気を取得できませんでした: %s", hourly_url)
            return None

        if hour and (values := self.read_hourly(page, target_date, hour)):
            return WeatherForecast(
                url=hourly_url,
                text=WEATHER_TEXT_FORMAT.format(**values),
                hourly=True,
            )

        if values := self.read_daily(page, target_date):
            logger.info("試合日が1時間天気の範囲外のため、10日間天気から日単位で拾いました")
            return WeatherForecast(
                url=hourly_url,
                text=WEATHER_TEXT_FORMAT_DAILY.format(**values),
                hourly=False,
            )

        self._warn(
            "%s の予報がまだ出ていません（tenki.jp は11日先まで）。試合が近づいてから取り直してください",
            target_date.isoformat(),
        )
        return WeatherForecast(url=hourly_url, text="", hourly=False)


# ============================================================
# 入力欄と入力チェック
# ------------------------------------------------------------
# Colab の @param は、表示している値がセルのソースコードと結びついている。
# 実行時に Python 側で代入しても表示は変わらないので、
# 「検索結果が入った状態で見える」必要がある項目はここに集めた。
#
# あわせて、何を自分で書く必要があるかを一覧で出す。
# 埋まっているのか、これから書くのかがフォームを見て分からない、
# というのが @param のいちばん困るところだった。
# ============================================================
# The Colab-only ipywidgets form is intentionally not part of this local module.


# -*- coding: utf-8 -*-
# ============================================================
# Dankoba Helper Ver.1.19.1  [5/10] Jリーグクラブ情報
# ------------------------------------------------------------
# J1・J2・J3 全60クラブの正式名称をキーに、次を持つ。
#   league                … 所属カテゴリ（J1 / J2 / J3）
#   official_site_url     … クラブ公式サイト
#   jleague_profile_url   … Jリーグ公式のクラブプロフィール
#   club_param            … 日程検索の club= に入れる識別子（nagoya など）
#   jleague_club_slug     … 日程検索の club= に渡す識別子
#   football_lab_url      … Football LAB のチームデータ
#   official_x_account    … 公式Xアカウント（URL と handle）
#   official_x_hashtags   … 公式Xでクラブを表すハッシュタグ
#   aliases               … 短縮名・略称・愛称・独自の呼び方
#
# 昇降格やURL変更のときに触るのはこのセル。
# 呼び方を足したいだけなら、次のセルの GRAPO_EXTRA_ALIASES のほうが安全。
# ============================================================
from typing import Literal, TypedDict

League = Literal["J1", "J2", "J3"]
AliasCategory = Literal["short_name", "abbreviation", "nickname", "custom"]


class AliasNode(TypedDict):
    short_name: List[str]
    abbreviation: List[str]
    nickname: List[str]
    custom: List[str]


class OfficialXAccountNode(TypedDict):
    url: str
    handle: str


class XHashtagsNode(TypedDict):
    club: List[str]


class ClubNode(TypedDict):
    league: League
    official_site_url: str
    jleague_profile_url: str
    # 日程検索の club= に入れる識別子。jleague_profile_url の末尾と同じ
    club_param: str
    # 日程検索の club= に渡す識別子（Jリーグ公式のクラブページと同じ）
    jleague_club_slug: str
    football_lab_url: str
    official_x_account: OfficialXAccountNode
    official_x_hashtags: XHashtagsNode
    aliases: AliasNode


# データを収集した日付。シーズン途中の昇降格やURL変更を追う目印にする。
DATA_AS_OF = "2026-09-17"

SOURCE_URLS = {
    "jleague_j1_clubs": "https://www.jleague.jp/j1/club/",
    "jleague_j2_clubs": "https://www.jleague.jp/j2/club/",
    "jleague_j3_clubs": "https://www.jleague.jp/j3/club/",
    "football_lab_team_select": "https://www.football-lab.jp/",
    "x": "https://x.com/",
}

# 正式名称（Jリーグ公式表記）をキーにしたクラブ情報。
# 各カテゴリ内はJリーグ公式クラブ一覧の並び順。
J_LEAGUE_CLUBS: Dict[str, ClubNode] = {
    # ------------------------------ J1 ------------------------------
    '鹿島アントラーズ': {
        'league': 'J1',
        'official_site_url': 'https://www.antlers.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/kashima/',
        'club_param': 'kashima',
        'jleague_club_slug': 'kashima',
        'football_lab_url': 'https://www.football-lab.jp/kasm/',
        'official_x_account': {'url': 'https://x.com/atlrs_official', 'handle': '@atlrs_official'},
        'official_x_hashtags': {'club': ['#antlers', '#kashima', '#鹿島アントラーズ']},
        'aliases': {'short_name': ['鹿島'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '水戸ホーリーホック': {
        'league': 'J1',
        'official_site_url': 'http://www.mito-hollyhock.net/',
        'jleague_profile_url': 'https://www.jleague.jp/club/mito/',
        'club_param': 'mito',
        'jleague_club_slug': 'mito',
        'football_lab_url': 'https://www.football-lab.jp/mito/',
        'official_x_account': {'url': 'https://x.com/hollyhock_staff', 'handle': '@hollyhock_staff'},
        'official_x_hashtags': {'club': ['#水戸ホーリーホック', '#hollyhock']},
        'aliases': {'short_name': ['水戸'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '浦和レッズ': {
        'league': 'J1',
        'official_site_url': 'http://www.urawa-reds.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/urawa/',
        'club_param': 'urawa',
        'jleague_club_slug': 'urawa',
        'football_lab_url': 'https://www.football-lab.jp/uraw/',
        'official_x_account': {'url': 'https://x.com/REDSOFFICIAL', 'handle': '@REDSOFFICIAL'},
        'official_x_hashtags': {'club': ['#浦和レッズ', '#urawareds', '#WeareREDS']},
        'aliases': {'short_name': ['浦和'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ジェフユナイテッド千葉': {
        'league': 'J1',
        'official_site_url': 'https://jefunited.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/chiba/',
        'club_param': 'chiba',
        'jleague_club_slug': 'chiba',
        'football_lab_url': 'https://www.football-lab.jp/chib/',
        'official_x_account': {'url': 'https://x.com/jef_united', 'handle': '@jef_united'},
        'official_x_hashtags': {'club': ['#jefunited', '#ジェフ千葉']},
        'aliases': {'short_name': ['千葉'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '柏レイソル': {
        'league': 'J1',
        'official_site_url': 'https://www.reysol.co.jp/index.php/',
        'jleague_profile_url': 'https://www.jleague.jp/club/kashiwa/',
        'club_param': 'kashiwa',
        'jleague_club_slug': 'kashiwa',
        'football_lab_url': 'https://www.football-lab.jp/kasw/',
        'official_x_account': {'url': 'https://x.com/REYSOL_Official', 'handle': '@REYSOL_Official'},
        'official_x_hashtags': {'club': ['#柏レイソル', '#reysol', '#NoREYSOLNoLIFE']},
        'aliases': {'short_name': ['柏'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ＦＣ東京': {
        'league': 'J1',
        'official_site_url': 'https://www.fctokyo.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/ftokyo/',
        'club_param': 'ftokyo',
        'jleague_club_slug': 'ftokyo',
        'football_lab_url': 'https://www.football-lab.jp/fctk/',
        'official_x_account': {'url': 'https://x.com/fctokyoofficial', 'handle': '@fctokyoofficial'},
        'official_x_hashtags': {'club': ['#fctokyo', '#tokyo', '#FC東京']},
        'aliases': {'short_name': ['FC東京'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '東京ヴェルディ': {
        'league': 'J1',
        'official_site_url': 'https://www.verdy.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/tokyov/',
        'club_param': 'tokyov',
        'jleague_club_slug': 'tokyov',
        'football_lab_url': 'https://www.football-lab.jp/tk-v/',
        'official_x_account': {'url': 'https://x.com/TokyoVerdySTAFF', 'handle': '@TokyoVerdySTAFF'},
        'official_x_hashtags': {'club': ['#verdy', '#東京ヴェルディ', '#東京V']},
        'aliases': {'short_name': ['東京Ｖ'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ＦＣ町田ゼルビア': {
        'league': 'J1',
        'official_site_url': 'http://www.zelvia.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/machida/',
        'club_param': 'machida',
        'jleague_club_slug': 'machida',
        'football_lab_url': 'https://www.football-lab.jp/mcd/',
        'official_x_account': {'url': 'https://x.com/FCMachidaZelvia', 'handle': '@FCMachidaZelvia'},
        'official_x_hashtags': {'club': ['#FC町田ゼルビア', '#zelvia']},
        'aliases': {'short_name': ['町田'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '川崎フロンターレ': {
        'league': 'J1',
        'official_site_url': 'https://www.frontale.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/kawasakif/',
        'club_param': 'kawasakif',
        'jleague_club_slug': 'kawasakif',
        'football_lab_url': 'https://www.football-lab.jp/ka-f/',
        'official_x_account': {'url': 'https://x.com/frontale_staff', 'handle': '@frontale_staff'},
        'official_x_hashtags': {'club': ['#frontale', '#川崎フロンターレ']},
        'aliases': {'short_name': ['川崎Ｆ'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '横浜Ｆ・マリノス': {
        'league': 'J1',
        'official_site_url': 'https://www.f-marinos.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/yokohamafm/',
        'club_param': 'yokohamafm',
        'jleague_club_slug': 'yokohamafm',
        'football_lab_url': 'https://www.football-lab.jp/y-fm/',
        'official_x_account': {'url': 'https://x.com/prompt_fmarinos', 'handle': '@prompt_fmarinos'},
        'official_x_hashtags': {'club': ['#fmarinos', '#マリノスファミリー']},
        'aliases': {'short_name': ['横浜FM'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '清水エスパルス': {
        'league': 'J1',
        'official_site_url': 'https://www.s-pulse.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/shimizu/',
        'club_param': 'shimizu',
        'jleague_club_slug': 'shimizu',
        'football_lab_url': 'https://www.football-lab.jp/shim/',
        'official_x_account': {'url': 'https://x.com/spulse_official', 'handle': '@spulse_official'},
        'official_x_hashtags': {'club': ['#spulse', '#エスパルス']},
        'aliases': {'short_name': ['清水'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '名古屋グランパス': {
        'league': 'J1',
        'official_site_url': 'http://nagoya-grampus.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/nagoya/',
        'club_param': 'nagoya',
        'jleague_club_slug': 'nagoya',
        'football_lab_url': 'https://www.football-lab.jp/nago/',
        'official_x_account': {'url': 'https://x.com/nge_official', 'handle': '@nge_official'},
        'official_x_hashtags': {'club': ['#grampus', '#グランパス']},
        'aliases': {'short_name': ['名古屋'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '京都サンガF.C.': {
        'league': 'J1',
        'official_site_url': 'http://www.sanga-fc.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/kyoto/',
        'club_param': 'kyoto',
        'jleague_club_slug': 'kyoto',
        'football_lab_url': 'https://www.football-lab.jp/kyot/',
        'official_x_account': {'url': 'https://x.com/sangafc', 'handle': '@sangafc'},
        'official_x_hashtags': {'club': ['#京都サンガ', '#sanga', '#京都サンガFC']},
        'aliases': {'short_name': ['京都'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ガンバ大阪': {
        'league': 'J1',
        'official_site_url': 'http://www.gamba-osaka.net/',
        'jleague_profile_url': 'https://www.jleague.jp/club/gosaka/',
        'club_param': 'gosaka',
        'jleague_club_slug': 'gosaka',
        'football_lab_url': 'https://www.football-lab.jp/g-os/',
        'official_x_account': {'url': 'https://x.com/GAMBA_OFFICIAL', 'handle': '@GAMBA_OFFICIAL'},
        'official_x_hashtags': {'club': ['#ガンバ大阪', '#GAMBAOSAKA']},
        'aliases': {'short_name': ['Ｇ大阪'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'セレッソ大阪': {
        'league': 'J1',
        'official_site_url': 'https://www.cerezo.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/cosaka/',
        'club_param': 'cosaka',
        'jleague_club_slug': 'cosaka',
        'football_lab_url': 'https://www.football-lab.jp/c-os/',
        'official_x_account': {'url': 'https://x.com/crz_official', 'handle': '@crz_official'},
        'official_x_hashtags': {'club': ['#セレッソ大阪', '#cerezo']},
        'aliases': {'short_name': ['Ｃ大阪'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ヴィッセル神戸': {
        'league': 'J1',
        'official_site_url': 'https://www.vissel-kobe.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/kobe/',
        'club_param': 'kobe',
        'jleague_club_slug': 'kobe',
        'football_lab_url': 'https://www.football-lab.jp/kobe/',
        'official_x_account': {'url': 'https://x.com/visselkobe', 'handle': '@visselkobe'},
        'official_x_hashtags': {'club': ['#visselkobe', '#ヴィッセル神戸']},
        'aliases': {'short_name': ['神戸'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ファジアーノ岡山': {
        'league': 'J1',
        'official_site_url': 'http://www.fagiano-okayama.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/okayama/',
        'club_param': 'okayama',
        'jleague_club_slug': 'okayama',
        'football_lab_url': 'https://www.football-lab.jp/okay/',
        'official_x_account': {'url': 'https://x.com/fagiano_koho', 'handle': '@fagiano_koho'},
        'official_x_hashtags': {'club': ['#ファジアーノ岡山']},
        'aliases': {'short_name': ['岡山'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'サンフレッチェ広島': {
        'league': 'J1',
        'official_site_url': 'http://www.sanfrecce.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/hiroshima/',
        'club_param': 'hiroshima',
        'jleague_club_slug': 'hiroshima',
        'football_lab_url': 'https://www.football-lab.jp/hiro/',
        'official_x_account': {'url': 'https://x.com/sanfrecce_SFC', 'handle': '@sanfrecce_SFC'},
        'official_x_hashtags': {'club': ['#sanfrecce', '#サンフレッチェ広島']},
        'aliases': {'short_name': ['広島'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'アビスパ福岡': {
        'league': 'J1',
        'official_site_url': 'http://www.avispa.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/fukuoka/',
        'club_param': 'fukuoka',
        'jleague_club_slug': 'fukuoka',
        'football_lab_url': 'https://www.football-lab.jp/fuku/',
        'official_x_account': {'url': 'https://x.com/AvispaF', 'handle': '@AvispaF'},
        'official_x_hashtags': {'club': ['#アビスパ福岡', '#avispa']},
        'aliases': {'short_name': ['福岡'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'Ｖ・ファーレン長崎': {
        'league': 'J1',
        'official_site_url': 'https://www.v-varen.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/nagasaki/',
        'club_param': 'nagasaki',
        'jleague_club_slug': 'nagasaki',
        'football_lab_url': 'https://www.football-lab.jp/ngsk/',
        'official_x_account': {'url': 'https://x.com/v_varenstaff', 'handle': '@v_varenstaff'},
        'official_x_hashtags': {'club': ['#vvaren', '#Ｖ・ファーレン長崎']},
        'aliases': {'short_name': ['長崎'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    # ------------------------------ J2 ------------------------------
    '北海道コンサドーレ札幌': {
        'league': 'J2',
        'official_site_url': 'https://www.consadole-sapporo.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/sapporo/',
        'club_param': 'sapporo',
        'jleague_club_slug': 'sapporo',
        'football_lab_url': 'https://www.football-lab.jp/sapp/',
        'official_x_account': {'url': 'https://x.com/consaofficial', 'handle': '@consaofficial'},
        'official_x_hashtags': {'club': ['#consadole', '#コンサドーレ', '#北海道コンサドーレ札幌']},
        'aliases': {'short_name': ['札幌'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ヴァンラーレ八戸': {
        'league': 'J2',
        'official_site_url': 'http://www.vanraure.net/',
        'jleague_profile_url': 'https://www.jleague.jp/club/hachinohe/',
        'club_param': 'hachinohe',
        'jleague_club_slug': 'hachinohe',
        'football_lab_url': 'https://www.football-lab.jp/hach/',
        'official_x_account': {'url': 'https://x.com/vanraure', 'handle': '@vanraure'},
        'official_x_hashtags': {'club': ['#ヴァンラーレ八戸']},
        'aliases': {'short_name': ['八戸'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ベガルタ仙台': {
        'league': 'J2',
        'official_site_url': 'https://www.vegalta.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/sendai/',
        'club_param': 'sendai',
        'jleague_club_slug': 'sendai',
        'football_lab_url': 'https://www.football-lab.jp/send/',
        'official_x_account': {'url': 'https://x.com/vega_official_', 'handle': '@vega_official_'},
        'official_x_hashtags': {'club': ['#VEGALTA', '#vegalta', '#ベガルタ仙台']},
        'aliases': {'short_name': ['仙台'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ブラウブリッツ秋田': {
        'league': 'J2',
        'official_site_url': 'http://blaublitz.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/akita/',
        'club_param': 'akita',
        'jleague_club_slug': 'akita',
        'football_lab_url': 'https://www.football-lab.jp/aki/',
        'official_x_account': {'url': 'https://x.com/blaublitz_akita', 'handle': '@blaublitz_akita'},
        'official_x_hashtags': {'club': ['#ブラウブリッツ秋田', '#bbakita']},
        'aliases': {'short_name': ['秋田'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'モンテディオ山形': {
        'league': 'J2',
        'official_site_url': 'https://www.montedioyamagata.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/yamagata/',
        'club_param': 'yamagata',
        'jleague_club_slug': 'yamagata',
        'football_lab_url': 'https://www.football-lab.jp/yama/',
        'official_x_account': {'url': 'https://x.com/monte_prstaff', 'handle': '@monte_prstaff'},
        'official_x_hashtags': {'club': ['#montedio', '#yamagataichigan']},
        'aliases': {'short_name': ['山形'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'いわきＦＣ': {
        'league': 'J2',
        'official_site_url': 'https://iwakifc.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/iwaki/',
        'club_param': 'iwaki',
        'jleague_club_slug': 'iwaki',
        'football_lab_url': 'https://www.football-lab.jp/ifc/',
        'official_x_account': {'url': 'https://x.com/IwakiFcOfficial', 'handle': '@IwakiFcOfficial'},
        'official_x_hashtags': {'club': ['#iwakifc', '#いわきFC']},
        'aliases': {'short_name': ['いわき'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '栃木シティ': {
        'league': 'J2',
        'official_site_url': 'https://tochigi-city.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/tochigic/',
        'club_param': 'tochigic',
        'jleague_club_slug': 'tochigic',
        'football_lab_url': 'https://www.football-lab.jp/to-c/',
        'official_x_account': {'url': 'https://x.com/tochigi_city_', 'handle': '@tochigi_city_'},
        'official_x_hashtags': {'club': ['#栃木シティ']},
        'aliases': {'short_name': ['栃木Ｃ'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ＲＢ大宮アルディージャ': {
        'league': 'J2',
        'official_site_url': 'https://www.ardija.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/omiya/',
        'club_param': 'omiya',
        'jleague_club_slug': 'omiya',
        'football_lab_url': 'https://www.football-lab.jp/omiy/',
        'official_x_account': {'url': 'https://x.com/Ardija_Official', 'handle': '@Ardija_Official'},
        'official_x_hashtags': {'club': ['#RB大宮アルディージャ', '#ardija']},
        'aliases': {'short_name': ['大宮'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '横浜ＦＣ': {
        'league': 'J2',
        'official_site_url': 'https://www.yokohamafc.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/yokohamafc/',
        'club_param': 'yokohamafc',
        'jleague_club_slug': 'yokohamafc',
        'football_lab_url': 'https://www.football-lab.jp/y-fc/',
        'official_x_account': {'url': 'https://x.com/yokohama_fc', 'handle': '@yokohama_fc'},
        'official_x_hashtags': {'club': ['#yokohamafc', '#横浜FC']},
        'aliases': {'short_name': ['横浜FC'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '湘南ベルマーレ': {
        'league': 'J2',
        'official_site_url': 'http://www.bellmare.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/shonan/',
        'club_param': 'shonan',
        'jleague_club_slug': 'shonan',
        'football_lab_url': 'https://www.football-lab.jp/shon/',
        'official_x_account': {'url': 'https://x.com/bellmare_staff', 'handle': '@bellmare_staff'},
        'official_x_hashtags': {'club': ['#bellmare', '#ベルマーレ']},
        'aliases': {'short_name': ['湘南'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ヴァンフォーレ甲府': {
        'league': 'J2',
        'official_site_url': 'http://www.ventforet.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/kofu/',
        'club_param': 'kofu',
        'jleague_club_slug': 'kofu',
        'football_lab_url': 'https://www.football-lab.jp/kofu/',
        'official_x_account': {'url': 'https://x.com/vfk_official', 'handle': '@vfk_official'},
        'official_x_hashtags': {'club': ['#vfk', '#ヴァンフォーレ甲府']},
        'aliases': {'short_name': ['甲府'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'アルビレックス新潟': {
        'league': 'J2',
        'official_site_url': 'http://www.albirex.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/niigata/',
        'club_param': 'niigata',
        'jleague_club_slug': 'niigata',
        'football_lab_url': 'https://www.football-lab.jp/niig/',
        'official_x_account': {'url': 'https://x.com/albirex_pr', 'handle': '@albirex_pr'},
        'official_x_hashtags': {'club': ['#albirex', '#アルビレックス新潟']},
        'aliases': {'short_name': ['新潟'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'カターレ富山': {
        'league': 'J2',
        'official_site_url': 'http://www.kataller.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/toyama/',
        'club_param': 'toyama',
        'jleague_club_slug': 'toyama',
        'football_lab_url': 'https://www.football-lab.jp/toya/',
        'official_x_account': {'url': 'https://x.com/katallertoyama', 'handle': '@katallertoyama'},
        'official_x_hashtags': {'club': ['#カターレ富山', '#kataller']},
        'aliases': {'short_name': ['富山'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ジュビロ磐田': {
        'league': 'J2',
        'official_site_url': 'http://www.jubilo-iwata.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/iwata/',
        'club_param': 'iwata',
        'jleague_club_slug': 'iwata',
        'football_lab_url': 'https://www.football-lab.jp/iwat/',
        'official_x_account': {'url': 'https://x.com/Jubiloiwata_YFC', 'handle': '@Jubiloiwata_YFC'},
        'official_x_hashtags': {'club': ['#ジュビロ磐田', '#jubilo']},
        'aliases': {'short_name': ['磐田'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '藤枝ＭＹＦＣ': {
        'league': 'J2',
        'official_site_url': 'http://myfc.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/fujieda/',
        'club_param': 'fujieda',
        'jleague_club_slug': 'fujieda',
        'football_lab_url': 'https://www.football-lab.jp/fuji/',
        'official_x_account': {'url': 'https://x.com/fujiedamyfc_pr', 'handle': '@fujiedamyfc_pr'},
        'official_x_hashtags': {'club': ['#藤枝MYFC', '#fujiedamyfc']},
        'aliases': {'short_name': ['藤枝'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '徳島ヴォルティス': {
        'league': 'J2',
        'official_site_url': 'http://www.vortis.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/tokushima/',
        'club_param': 'tokushima',
        'jleague_club_slug': 'tokushima',
        'football_lab_url': 'https://www.football-lab.jp/toku/',
        'official_x_account': {'url': 'https://x.com/vortis_pr', 'handle': '@vortis_pr'},
        'official_x_hashtags': {'club': ['#徳島ヴォルティス', '#vortis']},
        'aliases': {'short_name': ['徳島'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ＦＣ今治': {
        'league': 'J2',
        'official_site_url': 'http://www.fcimabari.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/imabari/',
        'club_param': 'imabari',
        'jleague_club_slug': 'imabari',
        'football_lab_url': 'https://www.football-lab.jp/imab/',
        'official_x_account': {'url': 'https://x.com/FCimabari', 'handle': '@FCimabari'},
        'official_x_hashtags': {'club': ['#FC今治', '#fcimabari']},
        'aliases': {'short_name': ['今治'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'サガン鳥栖': {
        'league': 'J2',
        'official_site_url': 'http://www.sagan-tosu.net/',
        'jleague_profile_url': 'https://www.jleague.jp/club/tosu/',
        'club_param': 'tosu',
        'jleague_club_slug': 'tosu',
        'football_lab_url': 'https://www.football-lab.jp/tosu/',
        'official_x_account': {'url': 'https://x.com/saganofficial17', 'handle': '@saganofficial17'},
        'official_x_hashtags': {'club': ['#サガン鳥栖', '#SAGANTOSU', '#sagantosu']},
        'aliases': {'short_name': ['鳥栖'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '大分トリニータ': {
        'league': 'J2',
        'official_site_url': 'https://www.oita-trinita.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/oita/',
        'club_param': 'oita',
        'jleague_club_slug': 'oita',
        'football_lab_url': 'https://www.football-lab.jp/oita/',
        'official_x_account': {'url': 'https://x.com/TRINITAofficial', 'handle': '@TRINITAofficial'},
        'official_x_hashtags': {'club': ['#大分トリニータ', '#trinita']},
        'aliases': {'short_name': ['大分'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'テゲバジャーロ宮崎': {
        'league': 'J2',
        'official_site_url': 'https://www.tegevajaro.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/miyazaki/',
        'club_param': 'miyazaki',
        'jleague_club_slug': 'miyazaki',
        'football_lab_url': 'https://www.football-lab.jp/myzk/',
        'official_x_account': {'url': 'https://x.com/55tegevajaro', 'handle': '@55tegevajaro'},
        'official_x_hashtags': {'club': ['#テゲバジャーロ宮崎', '#テゲバ']},
        'aliases': {'short_name': ['宮崎'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    # ------------------------------ J3 ------------------------------
    '福島ユナイテッドＦＣ': {
        'league': 'J3',
        'official_site_url': 'http://fufc.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/fukushima/',
        'club_param': 'fukushima',
        'jleague_club_slug': 'fukushima',
        'football_lab_url': 'https://www.football-lab.jp/fksm/',
        'official_x_account': {'url': 'https://x.com/fufc_staff', 'handle': '@fufc_staff'},
        'official_x_hashtags': {'club': ['#福島ユナイテッド', '#福島ユナイテッドFC']},
        'aliases': {'short_name': ['福島'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '栃木ＳＣ': {
        'league': 'J3',
        'official_site_url': 'http://www.tochigisc.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/tochigi/',
        'club_param': 'tochigi',
        'jleague_club_slug': 'tochigi',
        'football_lab_url': 'https://www.football-lab.jp/to-s/',
        'official_x_account': {'url': 'https://x.com/tochigisc', 'handle': '@tochigisc'},
        'official_x_hashtags': {'club': ['#栃木ＳＣ', '#栃木SC', '#全員戦力']},
        'aliases': {'short_name': ['栃木SC'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ザスパ群馬': {
        'league': 'J3',
        'official_site_url': 'http://www.thespa.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/gunma/',
        'club_param': 'gunma',
        'jleague_club_slug': 'gunma',
        'football_lab_url': 'https://www.football-lab.jp/gnm/',
        'official_x_account': {'url': 'https://x.com/OfficialThespa', 'handle': '@OfficialThespa'},
        'official_x_hashtags': {'club': ['#ザスパ群馬', '#thespa']},
        'aliases': {'short_name': ['群馬'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ＳＣ相模原': {
        'league': 'J3',
        'official_site_url': 'http://www.scsagamihara.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/sagamihara/',
        'club_param': 'sagamihara',
        'jleague_club_slug': 'sagamihara',
        'football_lab_url': 'https://www.football-lab.jp/sagm/',
        'official_x_account': {'url': 'https://x.com/sc_sagamihara', 'handle': '@sc_sagamihara'},
        'official_x_hashtags': {'club': ['#SC相模原', '#SCS']},
        'aliases': {'short_name': ['相模原'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '松本山雅ＦＣ': {
        'league': 'J3',
        'official_site_url': 'https://www.yamaga-fc.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/matsumoto/',
        'club_param': 'matsumoto',
        'jleague_club_slug': 'matsumoto',
        'football_lab_url': 'https://www.football-lab.jp/mats/',
        'official_x_account': {'url': 'https://x.com/yamagafc', 'handle': '@yamagafc'},
        'official_x_hashtags': {'club': ['#松本山雅FC', '#yamaga']},
        'aliases': {'short_name': ['松本'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ＡＣ長野パルセイロ': {
        'league': 'J3',
        'official_site_url': 'https://parceiro.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/nagano/',
        'club_param': 'nagano',
        'jleague_club_slug': 'nagano',
        'football_lab_url': 'https://www.football-lab.jp/naga/',
        'official_x_account': {'url': 'https://x.com/NAGANO_PARCEIRO', 'handle': '@NAGANO_PARCEIRO'},
        'official_x_hashtags': {'club': ['#acnp', '#パルセイロ']},
        'aliases': {'short_name': ['長野'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ツエーゲン金沢': {
        'league': 'J3',
        'official_site_url': 'http://www.zweigen-kanazawa.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/kanazawa/',
        'club_param': 'kanazawa',
        'jleague_club_slug': 'kanazawa',
        'football_lab_url': 'https://www.football-lab.jp/kana/',
        'official_x_account': {'url': 'https://x.com/zweigen_staff', 'handle': '@zweigen_staff'},
        'official_x_hashtags': {'club': ['#ツエーゲン金沢', '#zweigen']},
        'aliases': {'short_name': ['金沢'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ＦＣ岐阜': {
        'league': 'J3',
        'official_site_url': 'http://www.fc-gifu.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/gifu/',
        'club_param': 'gifu',
        'jleague_club_slug': 'gifu',
        'football_lab_url': 'https://www.football-lab.jp/gifu/',
        'official_x_account': {'url': 'https://x.com/fcgifuDREAM', 'handle': '@fcgifuDREAM'},
        'official_x_hashtags': {'club': ['#FC岐阜', '#fcgifu']},
        'aliases': {'short_name': ['岐阜'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'レイラック滋賀ＦＣ': {
        'league': 'J3',
        'official_site_url': 'https://reilac-shiga.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/shiga/',
        'club_param': 'shiga',
        'jleague_club_slug': 'shiga',
        'football_lab_url': 'https://www.football-lab.jp/rsfc/',
        'official_x_account': {'url': 'https://x.com/reilacshiga', 'handle': '@reilacshiga'},
        'official_x_hashtags': {'club': ['#レイラック滋賀']},
        'aliases': {'short_name': ['滋賀'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ＦＣ大阪': {
        'league': 'J3',
        'official_site_url': 'https://fc-osaka.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/fosaka/',
        'club_param': 'fosaka',
        'jleague_club_slug': 'fosaka',
        'football_lab_url': 'https://www.football-lab.jp/f-os/',
        'official_x_account': {'url': 'https://x.com/FCosakaOfficial', 'handle': '@FCosakaOfficial'},
        'official_x_hashtags': {'club': ['#FC大阪', '#fcosaka']},
        'aliases': {'short_name': ['FC大阪'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '奈良クラブ': {
        'league': 'J3',
        'official_site_url': 'https://naraclub.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/nara/',
        'club_param': 'nara',
        'jleague_club_slug': 'nara',
        'football_lab_url': 'https://www.football-lab.jp/nara/',
        'official_x_account': {'url': 'https://x.com/naraclub_info', 'handle': '@naraclub_info'},
        'official_x_hashtags': {'club': ['#奈良クラブ', '#naraclub']},
        'aliases': {'short_name': ['奈良'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ガイナーレ鳥取': {
        'league': 'J3',
        'official_site_url': 'https://www.gainare.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/tottori/',
        'club_param': 'tottori',
        'jleague_club_slug': 'tottori',
        'football_lab_url': 'https://www.football-lab.jp/totr/',
        'official_x_account': {'url': 'https://x.com/gainareofficial', 'handle': '@gainareofficial'},
        'official_x_hashtags': {'club': ['#ガイナーレ鳥取', '#gainare']},
        'aliases': {'short_name': ['鳥取'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'レノファ山口ＦＣ': {
        'league': 'J3',
        'official_site_url': 'http://www.renofa.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/yamaguchi/',
        'club_param': 'yamaguchi',
        'jleague_club_slug': 'yamaguchi',
        'football_lab_url': 'https://www.football-lab.jp/r-ya/',
        'official_x_account': {'url': 'https://x.com/renofayamaguchi', 'handle': '@renofayamaguchi'},
        'official_x_hashtags': {'club': ['#レノファ山口FC', '#renofa']},
        'aliases': {'short_name': ['山口'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'カマタマーレ讃岐': {
        'league': 'J3',
        'official_site_url': 'https://www.kamatamare.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/sanuki/',
        'club_param': 'sanuki',
        'jleague_club_slug': 'sanuki',
        'football_lab_url': 'https://www.football-lab.jp/sanu/',
        'official_x_account': {'url': 'https://x.com/kamatama_kouhou', 'handle': '@kamatama_kouhou'},
        'official_x_hashtags': {'club': ['#カマタマーレ讃岐', '#kamatamare']},
        'aliases': {'short_name': ['讃岐'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '愛媛ＦＣ': {
        'league': 'J3',
        'official_site_url': 'http://www.ehimefc.com/p/index.html',
        'jleague_profile_url': 'https://www.jleague.jp/club/ehime/',
        'club_param': 'ehime',
        'jleague_club_slug': 'ehime',
        'football_lab_url': 'https://www.football-lab.jp/ehim/',
        'official_x_account': {'url': 'https://x.com/ehime_fc', 'handle': '@ehime_fc'},
        'official_x_hashtags': {'club': ['#ehimefc', '#愛媛FC']},
        'aliases': {'short_name': ['愛媛'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '高知ユナイテッドＳＣ': {
        'league': 'J3',
        'official_site_url': 'http://kochi-usc.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/kochi/',
        'club_param': 'kochi',
        'jleague_club_slug': 'kochi',
        'football_lab_url': 'https://www.football-lab.jp/kusc/',
        'official_x_account': {'url': 'https://x.com/kochi_United', 'handle': '@kochi_United'},
        'official_x_hashtags': {'club': ['#高知ユナイテッドSC']},
        'aliases': {'short_name': ['高知'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ギラヴァンツ北九州': {
        'league': 'J3',
        'official_site_url': 'https://www.giravanz.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/kitakyushu/',
        'club_param': 'kitakyushu',
        'jleague_club_slug': 'kitakyushu',
        'football_lab_url': 'https://www.football-lab.jp/kiky/',
        'official_x_account': {'url': 'https://x.com/Giravanz_staff', 'handle': '@Giravanz_staff'},
        'official_x_hashtags': {'club': ['#ギラヴァンツ北九州', '#giravanz']},
        'aliases': {'short_name': ['北九州'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ロアッソ熊本': {
        'league': 'J3',
        'official_site_url': 'http://roasso-k.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/kumamoto/',
        'club_param': 'kumamoto',
        'jleague_club_slug': 'kumamoto',
        'football_lab_url': 'https://www.football-lab.jp/kuma/',
        'official_x_account': {'url': 'https://x.com/roassoofficial', 'handle': '@roassoofficial'},
        'official_x_hashtags': {'club': ['#ロアッソ熊本', '#roasso']},
        'aliases': {'short_name': ['熊本'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    '鹿児島ユナイテッドＦＣ': {
        'league': 'J3',
        'official_site_url': 'http://www.kufc.co.jp/',
        'jleague_profile_url': 'https://www.jleague.jp/club/kagoshima/',
        'club_param': 'kagoshima',
        'jleague_club_slug': 'kagoshima',
        'football_lab_url': 'https://www.football-lab.jp/kufc/',
        'official_x_account': {'url': 'https://x.com/kagoshimaufc', 'handle': '@kagoshimaufc'},
        'official_x_hashtags': {'club': ['#鹿児島ユナイテッドFC', '#kagoshimaunited']},
        'aliases': {'short_name': ['鹿児島'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },
    'ＦＣ琉球': {
        'league': 'J3',
        'official_site_url': 'http://fcryukyu.com/',
        'jleague_profile_url': 'https://www.jleague.jp/club/ryukyu/',
        'club_param': 'ryukyu',
        'jleague_club_slug': 'ryukyu',
        'football_lab_url': 'https://www.football-lab.jp/ryuk/',
        'official_x_account': {'url': 'https://x.com/fcr_info', 'handle': '@fcr_info'},
        'official_x_hashtags': {'club': ['#FC琉球', '#fcryukyu']},
        'aliases': {'short_name': ['琉球'], 'abbreviation': [], 'nickname': [], 'custom': []},
    },}


# normalize_club_key は [3/10] にある（このセルより前の [4/10] でも使うため）。


def build_alias_index(clubs: Optional[Dict[str, ClubNode]] = None) -> Dict[str, str]:
    """正式名称と aliases から検索用の索引を作る。
    キーは正規化済みの名称、値は正式名称。同じ別名を2クラブが使っていると
    誤解決するので例外にする。"""
    clubs = J_LEAGUE_CLUBS if clubs is None else clubs
    index: Dict[str, str] = {}
    for canonical_name, node in clubs.items():
        labels = [canonical_name]
        for values in node["aliases"].values():
            labels.extend(values)
        for label in labels:
            key = normalize_club_key(label)
            if not key:
                continue
            previous = index.get(key)
            if previous is not None and previous != canonical_name:
                raise ValueError(f"別名 {label!r} が {previous!r} と {canonical_name!r} で重複しています")
            index[key] = canonical_name
    return index


ALIAS_INDEX: Dict[str, str] = build_alias_index()


def resolve_club(name_or_alias: str, clubs: Optional[Dict[str, ClubNode]] = None) -> ClubNode:
    """正式名称または登録済み別名からクラブノードを取得する。見つからなければ KeyError。"""
    clubs = J_LEAGUE_CLUBS if clubs is None else clubs
    canonical = build_alias_index(clubs).get(normalize_club_key(name_or_alias))
    if canonical is None:
        raise KeyError(f"未登録のクラブ名または別名です: {name_or_alias}")
    return clubs[canonical]


def register_alias(
    club_name: str,
    alias: str,
    *,
    category: AliasCategory = "custom",
    clubs: Optional[Dict[str, ClubNode]] = None,
) -> None:
    """クラブに略称・愛称などを追加する。club_name は正式名称でも既存別名でもよい。
    他クラブが使っている別名は拒否する。"""
    clubs = J_LEAGUE_CLUBS if clubs is None else clubs
    if not alias or not alias.strip():
        raise ValueError("別名が空です")
    if category not in {"short_name", "abbreviation", "nickname", "custom"}:
        raise ValueError(f"未対応の分類です: {category}")

    alias_index = build_alias_index(clubs)
    canonical = alias_index.get(normalize_club_key(club_name))
    if canonical is None:
        raise KeyError(f"未登録のクラブ名または別名です: {club_name}")
    owner = alias_index.get(normalize_club_key(alias))
    if owner is not None and owner != canonical:
        raise ValueError(f"別名 {alias!r} は既に {owner!r} に登録されています")

    values = clubs[canonical]["aliases"][category]
    if alias not in values:
        values.append(alias)

    ALIAS_INDEX.clear()
    ALIAS_INDEX.update(build_alias_index(clubs))


def clubs_in_league(league: League, clubs: Optional[Dict[str, ClubNode]] = None) -> Dict[str, ClubNode]:
    """指定カテゴリのクラブだけを正式名称キーの辞書で返す。"""
    clubs = J_LEAGUE_CLUBS if clubs is None else clubs
    return {name: node for name, node in clubs.items() if node["league"] == league}

# -*- coding: utf-8 -*-
# ============================================================
# Dankoba Helper Ver.1.19.1  [6/10] クラブ解決と設定の組み立て
# ------------------------------------------------------------
# 前のセルのクラブ情報を、この記事で使う形に薄くかぶせる層。
# クラブ名の解決、ハッシュタグの選択、大会名の自動判定をここで行う。
# ============================================================

# グラぽで実際に使う呼び方を足す。キーは正式名称でも短縮名でもよい。
# 他クラブと衝突する別名は登録されず、警告が出る。
GRAPO_EXTRA_ALIASES: Dict[str, List[str]] = {
    "名古屋": ["グランパス"],
    "鹿島": ["アントラーズ"],
    "浦和": ["レッズ"],
    "千葉": ["ジェフ"],
    "柏": ["レイソル"],
    "東京Ｖ": ["ヴェルディ"],
    "町田": ["ゼルビア"],
    "川崎Ｆ": ["フロンターレ"],
    "横浜FM": ["マリノス"],
    "清水": ["エスパルス"],
    "京都": ["サンガ"],
    "Ｇ大阪": ["ガンバ"],
    "Ｃ大阪": ["セレッソ"],
    "神戸": ["ヴィッセル"],
    "岡山": ["ファジアーノ"],
    "広島": ["サンフレッチェ"],
    "福岡": ["アビスパ"],
    "長崎": ["ヴィファーレン"],
    "札幌": ["コンサドーレ"],
    "仙台": ["ベガルタ"],
    "新潟": ["アルビレックス"],
    "湘南": ["ベルマーレ"],
    "磐田": ["ジュビロ"],
    "鳥栖": ["サガン"],
    "大分": ["トリニータ"],
    "熊本": ["ロアッソ"],
    # Jリーグ公式でよく使われる略称のうち、短縮名に入っていないもの
    "ＦＣ東京": ["F東京"],
    "栃木シティ": ["栃木C"],
    "栃木SC": ["栃木SC"],
    "横浜FC": ["横浜FC"],
    "FC大阪": ["FC大阪"],
}

# 記事タイトルのハッシュタグは #grampus #vvaren のようなラテン文字表記を使うため、
# 候補が複数あるときは ASCII のタグを優先する。
HASHTAG_PREFER_ASCII: Final[bool] = True

# 大会名を自動で組み立てるときの接頭辞。
COMPETITION_PREFIX: Final[str] = "明治安田"


def apply_extra_aliases(report: Optional[RunReport] = None) -> int:
    """GRAPO_EXTRA_ALIASES をクラブ情報へ反映する。衝突は警告にして続行する。"""
    registered = 0
    for club_name, aliases in GRAPO_EXTRA_ALIASES.items():
        for alias in aliases:
            try:
                register_alias(club_name, alias, category="nickname")
                registered += 1
            except (KeyError, ValueError) as error:
                emit_warning(report, "別名を登録できませんでした（%s → %s）: %s", club_name, alias, error)
    logger.debug("独自の別名を %s件 登録しました", registered)
    return registered


@dataclass(frozen=True)
class ClubInfo:
    """1クラブぶんの情報を、この記事で使う項目だけ平らにしたもの。"""

    canonical_name: str
    league: str
    official_site_url: str
    jleague_profile_url: str
    football_lab_url: str
    x_url: str
    x_handle: str
    hashtags: Tuple[str, ...]
    # 日程検索の club= に入れる識別子
    club_param: str = ""

    @property
    def primary_hashtag(self) -> str:
        """記事タイトルに入れるハッシュタグ（先頭の # は含まない）。"""
        candidates = [tag.lstrip("#").strip() for tag in self.hashtags if tag.strip("# ")]
        if not candidates:
            return ""
        if HASHTAG_PREFER_ASCII:
            for tag in candidates:
                if tag.isascii():
                    return tag
        return candidates[0]

    @property
    def reference_links(self) -> List[Tuple[str, str]]:
        return [
            (f"{self.canonical_name} 公式サイト", self.official_site_url),
            (f"{self.canonical_name} Jリーグ公式プロフィール", self.jleague_profile_url),
            (f"{self.canonical_name} Football LAB", self.football_lab_url),
            (f"{self.canonical_name} 公式X {self.x_handle}", self.x_url),
        ]


def lookup_club(name: str, report: Optional[RunReport] = None) -> Optional[ClubInfo]:
    """クラブ名または別名から ClubInfo を引く。見つからなければ None を返して警告する。
    resolve_club は例外を投げるので、実行を止めたくないここで受け止める。"""
    if not str(name or "").strip():
        return None
    try:
        node = resolve_club(name)
    except KeyError:
        emit_warning(
            report,
            "クラブ %r がクラブ情報に見つかりません。正式名称で入力するか、GRAPO_EXTRA_ALIASES に別名を追加してください",
            name,
        )
        return None
    canonical = build_alias_index()[normalize_club_key(name)]
    account = node["official_x_account"]
    return ClubInfo(
        canonical_name=canonical,
        league=node["league"],
        official_site_url=node["official_site_url"],
        jleague_profile_url=node["jleague_profile_url"],
        football_lab_url=node["football_lab_url"],
        x_url=account["url"],
        x_handle=account["handle"],
        hashtags=tuple(node["official_x_hashtags"]["club"]),
        club_param=str(node.get("club_param", "")),
    )


def resolve_club_canonical(name: str) -> Optional[str]:
    """[4/10] の同名関数を、クラブ情報を使う版で置き換える。

    クラブ名を切る位置を総当たりで探す用途なので、見つからないのが普通。
    lookup_club と違って警告は出さない。"""
    return build_alias_index().get(normalize_club_key(name))


def search_name_variants(name: str) -> List[str]:
    """[4/10] の同名関数を、クラブ情報を使う版で置き換える。
    「グランパス」から「名古屋グランパス」「名古屋」まで広げられるようになる。"""
    variants = [str(name or "").strip()]
    club = lookup_club(name)
    if club is not None:
        variants.append(club.canonical_name)
        variants.extend(J_LEAGUE_CLUBS[club.canonical_name]["aliases"]["short_name"])
    return [variant for variant in dict.fromkeys(variants) if variant]


def club_param_of(name: str) -> str:
    """[4/10] の同名関数を、クラブ情報から答える版で置き換える。
    日程検索の club= に入れて、読み込む行を減らす。"""
    club = lookup_club(name)
    return club.club_param if club is not None else ""


def search_league_hint(name: str) -> Optional[str]:
    """[4/10] の同名関数を、クラブ情報から答える版で置き換える。
    自チームのカテゴリが分かるので、日程一覧を1本だけ引けばよくなる。"""
    club = lookup_club(name)
    return club.league if club is not None else None


def build_competition_label(
    manual_value: str,
    my_club: Optional[ClubInfo],
    report: Optional[RunReport] = None,
) -> str:
    """フォームに入力があればそれを使う。空欄なら大会の選択から決める。
    リーグ戦のときだけ、自チームの所属カテゴリと食い違っていないか見る。"""
    manual = str(manual_value or "").strip()
    if manual:
        return manual
    competition = competition_type(COMPETITION_TYPE)
    if competition.is_league and my_club is not None and my_club.league != competition.name:
        emit_warning(
            report,
            "選んだ大会（%s）と %s の所属（%s）が違います。[2/10] の COMPETITION_TYPE を確認してください",
            competition.name, my_club.canonical_name, my_club.league,
        )
    logger.info("大会名: %s", competition.label)
    return competition.label


@dataclass(frozen=True)
class PreviewConfig:
    serial_number: str
    season: str
    competition: str
    round_label: str
    my_team: str
    opponent_team: str
    opponent_hashtag: str
    my_team_is_home: bool
    my_team_formation: str
    opponent_formation: str
    kickoff_date: Optional[date]
    kickoff_time: str
    venue_name: str
    venue_address: str
    venue_map_url: str
    broadcast: str
    weather_text: str
    weather_url: str
    attack_point_count: int
    defense_point_count: int
    include_reference_section: bool
    drive_folder_name: str
    my_club: Optional[ClubInfo] = None
    opponent_club: Optional[ClubInfo] = None
    docs_style: DocsStyleConfig = field(default_factory=DocsStyleConfig)

    # ---- 表示用の組み立て ----
    @property
    def serial_label(self) -> str:
        return f"D{self.serial_number}"

    @property
    def home_team(self) -> str:
        return self.my_team if self.my_team_is_home else self.opponent_team

    @property
    def away_team(self) -> str:
        return self.opponent_team if self.my_team_is_home else self.my_team

    @property
    def home_formation(self) -> str:
        return self.my_team_formation if self.my_team_is_home else self.opponent_formation

    @property
    def away_formation(self) -> str:
        return self.opponent_formation if self.my_team_is_home else self.my_team_formation

    @property
    def my_team_hashtag(self) -> str:
        return self.my_club.primary_hashtag if self.my_club else "grampus"

    @property
    def match_label(self) -> str:
        """「2026/27明治安田J1リーグ第7節」。タイトルと見出しで使い回す。"""
        return f"{self.season}{self.competition}{self.round_label}"

    @property
    def date_dotted(self) -> str:
        """タイトル括弧内の「2026.9.12」。ゼロ埋めしない。"""
        if self.kickoff_date is None:
            return "----.-.-"
        return f"{self.kickoff_date.year}.{self.kickoff_date.month}.{self.kickoff_date.day}"

    @property
    def kickoff_sentence(self) -> str:
        """表に入れる「2026年9月12日土曜日 19:00試合開始」。"""
        if self.kickoff_date is None:
            return ""
        weekday = WEEKDAY_JA[self.kickoff_date.weekday()]
        date_part = f"{self.kickoff_date.year}年{self.kickoff_date.month}月{self.kickoff_date.day}日{weekday}"
        return f"{date_part} {self.kickoff_time}試合開始".strip() if self.kickoff_time else date_part

    @property
    def document_title(self) -> str:
        """Google ドキュメントのファイル名。記事タイトルと同じ並びにする。"""
        tags = [f"#{self.my_team_hashtag}"] if self.my_team_hashtag else []
        if self.opponent_hashtag:
            tags.append(f"#{self.opponent_hashtag}")
        return (
            f"{self.season} {self.competition}{self.round_label}マッチプレビュー "
            f"{self.my_team} vs {self.opponent_team}"
            f"（{self.date_dotted}）{' '.join(tags)} {self.serial_label}"
        )

    @property
    def article_heading(self) -> str:
        """本文冒頭の見出し（H1）。"""
        return f"【マッチプレビュー】{self.match_label} {self.my_team} vs {self.opponent_team}"

    @property
    def reference_links(self) -> List[Tuple[str, str]]:
        links: List[Tuple[str, str]] = []
        if self.opponent_club:
            links.extend(self.opponent_club.reference_links)
        if self.my_club:
            links.append((f"{self.my_club.canonical_name} 公式サイト", self.my_club.official_site_url))
        return [(label, url) for label, url in links if url]


def resolve_display_name(raw_name: str, club: Optional[ClubInfo], use_official: bool) -> str:
    """フォームの入力をそのまま使うか、Jリーグ公式表記に揃えるか。"""
    raw = str(raw_name or "").strip()
    if not use_official or club is None:
        return raw
    if club.canonical_name != raw:
        logger.info("クラブ名を公式表記に揃えました: %s → %s", raw, club.canonical_name)
    return club.canonical_name


def build_preview_config(
    *,
    serial_number: str,
    season: str,
    competition: str,
    round_label: str,
    my_team: str,
    opponent_team: str,
    use_official_club_name: bool,
    opponent_hashtag: str,
    home_or_away: str,
    my_team_formation: str,
    opponent_formation: str,
    kickoff_date: str,
    kickoff_time: str,
    venue_name: str,
    venue_address: str,
    venue_map_url: str,
    broadcast: str,
    weather_text: str,
    weather_url: str,
    attack_point_count: int,
    defense_point_count: int,
    include_reference_section: bool,
    drive_folder_name: str,
    report: Optional[RunReport] = None,
) -> PreviewConfig:
    """フォームの生の文字列を検証済みの設定に変換する。"""
    my_club = lookup_club(my_team, report)
    opponent_club = lookup_club(opponent_team, report)

    # カップ戦はカテゴリをまたぐのが当たり前なので、リーグ戦のときだけ知らせる
    if (competition_type(COMPETITION_TYPE).is_league
            and my_club and opponent_club and my_club.league != opponent_club.league):
        emit_warning(
            report,
            "所属カテゴリが異なります（%s=%s / %s=%s）。[2/10] の COMPETITION_TYPE を確認してください",
            my_club.canonical_name, my_club.league,
            opponent_club.canonical_name, opponent_club.league,
        )

    if not str(home_or_away or "").strip():
        emit_warning(
            report,
            "ホーム／アウェイが決まりませんでした。アウェイとして扱います。"
            "[4/10] の HOME_OR_AWAY で指定してください",
        )

    manual_hashtag = str(opponent_hashtag or "").lstrip("#").strip()
    resolved_hashtag = manual_hashtag or (opponent_club.primary_hashtag if opponent_club else "")
    if not manual_hashtag and resolved_hashtag:
        logger.info("ハッシュタグをクラブ情報から選びました: #%s", resolved_hashtag)
    elif not resolved_hashtag:
        emit_warning(report, "対戦相手のハッシュタグが決まりませんでした。タイトルは自チームのタグだけになります")

    return PreviewConfig(
        serial_number=normalize_serial_number(serial_number),
        season=str(season).strip(),
        competition=build_competition_label(competition, my_club, report),
        round_label=str(round_label or "").strip(),
        my_team=resolve_display_name(my_team, my_club, use_official_club_name),
        opponent_team=resolve_display_name(opponent_team, opponent_club, use_official_club_name),
        opponent_hashtag=resolved_hashtag,
        my_team_is_home=str(home_or_away).strip() == "ホーム",
        my_team_formation=str(my_team_formation).strip(),
        opponent_formation=str(opponent_formation).strip(),
        kickoff_date=parse_kickoff_date(kickoff_date),
        kickoff_time=str(kickoff_time).strip(),
        venue_name=str(venue_name).strip(),
        venue_address=str(venue_address).strip(),
        venue_map_url=str(venue_map_url).strip(),
        broadcast=str(broadcast).strip(),
        weather_text=str(weather_text).strip(),
        weather_url=str(weather_url).strip(),
        attack_point_count=max(1, int(attack_point_count or 1)),
        defense_point_count=max(1, int(defense_point_count or 1)),
        include_reference_section=bool(include_reference_section),
        drive_folder_name=str(drive_folder_name).strip(),
        my_club=my_club,
        opponent_club=opponent_club,
    )

# -*- coding: utf-8 -*-
# ============================================================
# Dankoba Helper Ver.1.19.1  [7/10] クラブ公式サイトからのリンク探索
# ------------------------------------------------------------
# クラブ情報 [5/10] の official_site_url を入口に、記事で毎回貼る
# 4種類のリンクを探す。
#
#   試合情報         … この試合の告知ページ
#   アクセス         … スタジアムへの行き方
#   スタジアムグルメ … スタグル
#   グッズ販売       … スタジアムでのグッズ、オンラインストア
#
# クラブごとにページ構成が違うので、確実に当てることはできない。
# 見つかったらURL、見つからなければその旨を残して、人が判断できる形にする。
# 通信の土台は [3/10] にある。
# ============================================================


@dataclass(frozen=True)
class LinkTarget:
    category: str
    keywords: Tuple[str, ...]
    # この種別は試合ごとに変わるので、日付や相手名で絞り込む
    match_specific: bool = False
    # この種別は開催スタジアムごとに変わるので、会場名で絞り込む。
    # クラブ公式の「アクセス」は常設のホームスタジアム向けに置かれていることが多く、
    # 国立開催などではそのまま貼ると別会場の案内になる。
    venue_specific: bool = False
    # リンクの文字列がこれなら本命。日付の一致より強く効かせる。
    # 名古屋の NEXT MATCH には「試合情報」と「試合LIVEページへ」が並び、
    # URLに日付が入っているのは後者のほうなので、文字列で見分ける必要がある。
    preferred_labels: Tuple[str, ...] = ()


LINK_TARGETS: Final[Tuple[LinkTarget, ...]] = (
    LinkTarget("試合情報", ("試合情報", "ゲーム情報", "game_information", "gameinfo",
                            "match", "game", "対戦", "ホームゲーム"),
               match_specific=True, preferred_labels=("試合情報", "ゲーム情報", "試合詳細")),
    LinkTarget("アクセス", ("アクセス", "交通", "行き方", "来場", "観戦ガイド",
                            "stadium guide", "access", "stadium", "スタジアム"),
               venue_specific=True, preferred_labels=("会場アクセス", "アクセス")),
    LinkTarget("スタジアムグルメ", ("スタグル", "グルメ", "フード", "飲食", "メニュー",
                                    "gourmet", "food", "menu"), venue_specific=True),
    LinkTarget("グッズ販売", ("グッズ", "ストア", "オンラインストア", "ショップ",
                              "store", "shop", "goods")),
    LinkTarget("イベント", ("イベント", "event", "タイムテーブル", "timetable",
                            "スケジュール"), venue_specific=True),
)

# 公式サイトが自動取得を拒否するクラブのための手書きの控え。
# 見つからなかった種別だけ、ここの値で埋める。
# トップページがJavaScriptで組み立てられるクラブは、その要素が出るまで待つ。
# 名古屋は NEXT MATCH の塊が数秒遅れて差し込まれるため、待たずに読むと
# ヘッダのメニューしか拾えない。
CLUB_SITE_WAIT_SELECTORS: Dict[str, str] = {
    "名古屋グランパス": ".home-game_link a",
}

# 静的な取得を拒否すると分かっているサイト。最初からブラウザで開く。
# 実行のたびに「拒否されました→読み直します」を繰り返さずに済む。
CLUB_SITES_NEEDING_BROWSER: Final[Tuple[str, ...]] = (
    "nagoya-grampus.jp",
)

# 公式サイトから拾えなかったときの控え。常設ページだけを書く。
# アクセス・スタジアムグルメ・イベントは開催地ごとに変わるので、ここには
# 書かない（書いても使われない）。ホームスタジアムの案内を控えにすると、
# 国立開催のようなときに別会場の情報を貼ってしまう。
CLUB_LINK_OVERRIDES: Dict[str, Dict[str, str]] = {
    "名古屋グランパス": {
        "試合情報": "https://nagoya-grampus.jp/game/fixtures-results/",
        "グッズ販売": "https://webshop.nagoya-grampus.jp/",
    },
}


# Jリーグ公式の会場表記と、クラブ公式での呼び方が違う場合の対応表。
# 命名権は入れ替わるので、外れたらここに足す。
STADIUM_ALIASES: Dict[str, Tuple[str, ...]] = {
    "ＭＵＦＧスタジアム": ("国立競技場", "国立", "MUFG", "kokuritsu", "national"),
}

# 「味の素スタジアム」「豊田スタジアム」「国立競技場」のようなスタジアム名を拾う。
# 開催地と違うスタジアムの案内をつかんでいないかを見るために使う。
STADIUM_NAME_PATTERN = re.compile(
    r"[^\s、。「」（）()／/|｜]{2,14}(?:スタジアム|競技場|ドーム|フィールド|アリーナ|球技場)"
)

# リンクとして拾いたくないもの
# 次の試合の案内がまとまっている塊。ここに入っているリンクを優先する。
# クラス名にこれらを含む祖先があれば加点する。
NEXT_MATCH_CLASS_HINTS: Final[Tuple[str, ...]] = (
    "nextmatch", "next-match", "next_match", "home-game", "homegame",
)

# 試合情報ページの中にある、ビジター向けグッズ販売の見出し
VISITOR_GOODS_HEADINGS: Final[Tuple[str, ...]] = (
    "ビジターグッズ", "ビジター グッズ", "アウェイグッズ", "グッズ販売", "グッズ情報",
)
VISITOR_GOODS_EXCERPT_CHARS: Final[int] = 220

LINK_EXCLUDE_PATTERN = re.compile(
    r"(facebook\.com|instagram\.com|x\.com|twitter\.com|youtube\.com|line\.me"
    r"|/privacy|/policy|/sitemap|/contact|\.pdf$|\.jpg$|\.png$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FoundLink:
    category: str
    club: str
    label: str = ""
    url: str = ""
    note: str = ""
    # ページから抜き出した本文。ビジターグッズ販売の案内などを入れる
    excerpt: str = ""

    @property
    def found(self) -> bool:
        return bool(self.url)

    def as_reference(self) -> Tuple[str, str]:
        """参考リンクの節に載せる形。URLが無い場合は理由を見出しに入れる。"""
        head = f"{self.category}（{self.club}）"
        if self.label:
            head = f"{head}｜{self.label}"
        if self.excerpt:
            head = f"{head}｜{self.excerpt}"
        if self.found:
            return (head, self.url)
        return (head if self.label or self.excerpt
                else f"{head}｜{self.note or '見つかりませんでした'}", "")


class ClubSiteLinkFinder:
    """クラブ公式サイトのトップから候補を拾い、必要なら1段だけ潜る。

    試合情報は「一覧ページ → 個別の試合ページ」の2段になっているクラブが多い。
    トップで日付の入ったリンクが取れなければ、一覧を1回だけ開いて中を見る。"""

    # 候補を早く打ち切ると、ページ下部にある本命を見逃す。
    # 名古屋の公式サイトは、ヘッダのメニューに「試合」系のリンクが並んだあと、
    # ずっと下の NEXT MATCH に日付入りの試合情報リンクが出る。8件で切ると
    # 前者だけを拾って一覧のURLに落ちてしまうので、広めに集めて点数で選ぶ。
    MAX_CANDIDATES: Final[int] = 60

    def __init__(self, fetcher: PageFetcher, report: Optional[RunReport] = None):
        self.fetcher = fetcher
        self.report = report
        # NEXT MATCH の塊で見つけたURL。候補の点数付けで優遇する
        self._next_match_urls: set = set()

    def _warn(self, message: str, *args: Any) -> None:
        emit_warning(self.report, message, *args)

    # ---- 候補の抽出 --------------------------------------------
    @staticmethod
    def _match_tokens(match_date: Optional[date], opponent_short: str) -> List[str]:
        """この試合を指すリンクを見分けるための手がかり。"""
        tokens: List[str] = []
        if match_date is not None:
            tokens.extend([
                f"{match_date.month}月{match_date.day}日",
                f"{match_date.month}/{match_date.day}",
                f"{match_date.month}.{match_date.day}",
                f"{match_date.month:02d}.{match_date.day:02d}",
                f"{match_date.month:02d}/{match_date.day:02d}",
                f"{match_date.month:02d}{match_date.day:02d}",
                match_date.strftime("%Y%m%d"),
                match_date.strftime("%Y-%m-%d"),
            ])
        if opponent_short:
            tokens.append(opponent_short)
        return [token for token in tokens if token]

    def _candidates(self, pages: Sequence[FetchResult], target: LinkTarget) -> List[Tuple[str, str]]:
        """渡されたページ群からキーワードに当たるリンクを集める。
        トップページに加えて、決まった試合情報ページも対象にする。"""
        found: List[Tuple[str, str]] = []
        seen: set = set()
        for page in pages:
            found.extend(self._candidates_in(page, target, seen))
            if len(found) >= self.MAX_CANDIDATES:
                break
        return found[: self.MAX_CANDIDATES]

    @staticmethod
    def _in_next_match_block(anchor) -> bool:
        """祖先のクラス名に NEXT MATCH らしさがあるか。"""
        node = anchor
        for _ in range(8):
            node = node.parent
            if node is None or getattr(node, "name", None) in ("body", "html", "[document]"):
                break
            classes = " ".join(str(name).lower() for name in (node.get("class") or []))
            if any(hint in classes for hint in NEXT_MATCH_CLASS_HINTS):
                return True
        return False

    def _candidates_in(
        self, page: FetchResult, target: LinkTarget, seen: set
    ) -> List[Tuple[str, str]]:
        found: List[Tuple[str, str]] = []
        for anchor in page.soup.find_all("a", href=True):
            href = str(anchor.get("href")).strip()
            if not href or href in ("#",) or href.startswith(("javascript:", "mailto:", "tel:")):
                continue
            # 「#anc2」のようなページ内アンカーも対象にする。FC東京の
            # スタジアムグルメやタイムテーブルは試合情報ページの節に置かれている。
            absolute = urljoin(page.url, href)
            if LINK_EXCLUDE_PATTERN.search(absolute) or absolute in seen:
                continue
            text = anchor.get_text(" ", strip=True)
            haystack = f"{text} {absolute}".lower()
            if not any(keyword.lower() in haystack for keyword in target.keywords):
                continue
            seen.add(absolute)
            if self._in_next_match_block(anchor):
                self._next_match_urls.add(absolute)
            found.append((text or absolute, absolute))
            if len(found) >= self.MAX_CANDIDATES:
                break
        return found

    @staticmethod
    def _venue_tokens(venue_name: str) -> List[str]:
        """会場名を照合用の断片に崩す。
        「ＭＵＦＧスタジアム」からは "mufgスタジアム" と "mufg" を作る。"""
        if not venue_name:
            return []
        candidates = [venue_name]
        for official, aliases in STADIUM_ALIASES.items():
            if normalize_club_key(official) == normalize_club_key(venue_name):
                candidates.extend(aliases)
        tokens: List[str] = []
        for candidate in candidates:
            cleaned = re.sub(r"[（(]([^）)]*)[）)]", r" \1 ", candidate)
            for part in re.split(r"[\s　]+", cleaned):
                key = normalize_club_key(part)
                if len(key) >= 2:
                    tokens.append(key)
                core = re.sub(r"(スタジアム|競技場|ドーム|フィールド|アリーナ|球技場)$", "", key)
                if len(core) >= 2 and core != key:
                    tokens.append(core)
        return list(dict.fromkeys(tokens))

    @classmethod
    def _venue_score(cls, text: str, venue_tokens: Sequence[str]) -> int:
        """+1 … 開催地の名前が出ている / -1 … 別のスタジアム名しか出ていない
        0 … スタジアム名の手がかりが無い"""
        if not venue_tokens:
            return 0
        haystack = normalize_club_key(text)
        if any(token in haystack for token in venue_tokens):
            return 1
        for found in STADIUM_NAME_PATTERN.findall(text):
            found_key = normalize_club_key(found)
            if not any(token in found_key or found_key in token for token in venue_tokens):
                return -1
        return 0

    def _score(self, label: str, url: str, tokens: Sequence[str],
               target: Optional[LinkTarget] = None) -> int:
        """候補の点数。

        日付の一致だけで選ぶと、名古屋の NEXT MATCH では
        「試合LIVEページへ」（URLに 0919 が入る）が
        「試合情報」（URLは 0917919-fc.php で日付として読めない）に勝ってしまう。
        リンクの文字列がその種別の本命なら、日付より強く効かせる。"""
        score = sum(1 for token in tokens if token and token in f"{label} {url}")
        if target is not None:
            normalized = normalize_club_key(label)
            for preferred in target.preferred_labels:
                if normalized == normalize_club_key(preferred):
                    score += 4
                    break
                if normalize_club_key(preferred) in normalized:
                    score += 2
                    break
        if url in self._next_match_urls:
            score += 2
        return score

    def _best(self, candidates: Sequence[Tuple[str, str]], tokens: Sequence[str],
              target: Optional[LinkTarget] = None):
        scored = sorted(
            ((self._score(label, url, tokens, target), label, url) for label, url in candidates),
            key=lambda item: item[0], reverse=True,
        )
        return scored[0] if scored else (0, "", "")

    # ---- 1種別ぶんを決める --------------------------------------
    def _resolve_target(
        self,
        pages: Sequence[FetchResult],
        club_name: str,
        target: LinkTarget,
        tokens: Sequence[str],
        venue_tokens: Sequence[str] = (),
    ) -> FoundLink:
        logger.info("リンク種別「%s」を探します: %s", target.category, club_name)
        candidates = self._candidates(pages, target)
        if not candidates:
            logger.info("リンク種別「%s」の候補は見つかりませんでした: %s", target.category, club_name)
            return FoundLink(
                category=target.category, club=club_name,
                note="公式サイトのトップに該当するリンクがありませんでした",
            )

        if target.venue_specific:
            result = self._resolve_venue_target(club_name, target, candidates, venue_tokens)
            logger.info(
                "リンク種別「%s」の結果: %s%s",
                target.category,
                result.url or "未発見",
                f"（{result.note}）" if result.note else "",
            )
            return result

        if not target.match_specific:
            label, url = candidates[0]
            logger.info("リンク種別「%s」を選びました: %s (%s)", target.category, label, url)
            return FoundLink(target.category, club_name, label, url)

        # 試合ごとのページは、日付や相手名を含むものを優先する
        best_score, label, url = self._best(candidates, tokens, target)
        if best_score > 0:
            logger.info("リンク種別「%s」を選びました: %s (%s)", target.category, label, url)
            return FoundLink(target.category, club_name, label, url)

        # トップで当たらなければ、一覧を1回だけ開いて中を探す
        index_label, index_url = candidates[0]
        logger.info("試合情報の一覧を開いて中を探します: %s", index_url)
        index_page = self.fetcher.fetch(
            index_url, use_playwright_fallback=bool(USE_PLAYWRIGHT_FALLBACK)
        )
        if index_page is None or index_page.soup is None:
            return FoundLink(
                category=target.category, club=club_name, label=index_label, url=index_url,
                note="一覧ページまでしか辿れませんでした",
            )
        inner_score, inner_label, inner_url = self._best(
            self._candidates([index_page], target), tokens, target
        )
        if inner_score > 0:
            logger.info("一覧から「%s」のリンクを見つけました: %s (%s)", target.category, inner_label, inner_url)
            return FoundLink(target.category, club_name, inner_label, inner_url)
        logger.info("一覧内でも「%s」の試合別リンクは見つかりませんでした", target.category)
        return FoundLink(
            category=target.category, club=club_name, label=index_label, url=index_url,
            note="この試合のページは特定できず、一覧のURLを載せています",
        )

    def _resolve_venue_target(
        self,
        club_name: str,
        target: LinkTarget,
        candidates: Sequence[Tuple[str, str]],
        venue_tokens: Sequence[str],
    ) -> FoundLink:
        """開催地に合うものを選ぶ。

        リンクの文字列とURLだけで判断が付かないときは、そのページを1回開いて
        本文に開催地の名前が出ているかを見る。クラブ公式の「アクセス」は
        文字列が「アクセス」だけのことが多く、外からは見分けられないため。"""
        scored = sorted(
            ((self._venue_score(f"{label} {url}", venue_tokens)
              + (1 if url in self._next_match_urls else 0), label, url)
             for label, url in candidates),
            key=lambda item: item[0], reverse=True,
        )
        best_score, label, url = scored[0]
        if best_score > 0:
            return FoundLink(target.category, club_name, label, url)

        rejected: List[str] = []
        for score, label, url in scored[:2]:
            if score < 0:
                rejected.append(label or url)
                continue
            page = self.fetcher.fetch(url, use_playwright_fallback=bool(USE_PLAYWRIGHT_FALLBACK))
            if page is None or page.soup is None:
                continue
            page_score = self._venue_score(page.soup.get_text(" ", strip=True)[:4000], venue_tokens)
            if page_score > 0:
                return FoundLink(
                    target.category, club_name, label, url,
                    note="ページ本文で開催地の記載を確認しました",
                )
            if page_score < 0:
                rejected.append(label or url)

        note = "開催地と一致するページが見つかりませんでした"
        if rejected:
            note += f"（別会場の案内だったもの: {'、'.join(rejected[:2])}）"
        return FoundLink(category=target.category, club=club_name, note=note)

    @staticmethod
    def extract_visitor_goods(page: FetchResult) -> Tuple[str, str]:
        """試合情報ページから、ビジターグッズ販売の案内を抜き出す。

        アウェイ開催の試合情報ページには、遠征する側に向けたグッズ販売の
        節が置かれることがある。見出しを見つけて、その後ろの本文を
        次の見出しの手前まで拾う。戻り値は (抜粋, その節の中のURL)。"""
        headings = ("h1", "h2", "h3", "h4", "h5", "dt", "strong", "th")
        for node in page.soup.find_all(headings):
            title = node.get_text(" ", strip=True)
            if not any(keyword in title for keyword in VISITOR_GOODS_HEADINGS):
                continue
            parts: List[str] = []
            link_url = ""
            for sibling in node.next_siblings:
                if getattr(sibling, "name", None) in headings:
                    break
                if getattr(sibling, "name", None) is None:
                    text = str(sibling).strip()
                else:
                    text = sibling.get_text(" ", strip=True)
                    if not link_url and (anchor := sibling.find("a", href=True)):
                        link_url = urljoin(page.url, str(anchor.get("href")))
                if text:
                    parts.append(text)
                if sum(len(part) for part in parts) >= VISITOR_GOODS_EXCERPT_CHARS:
                    break
            excerpt = re.sub(r"\s+", " ", " ".join(parts)).strip()
            if not excerpt:
                continue
            if len(excerpt) > VISITOR_GOODS_EXCERPT_CHARS:
                excerpt = excerpt[:VISITOR_GOODS_EXCERPT_CHARS].rstrip() + "…"
            logger.info("試合情報ページから「%s」を抜き出しました", title)
            return f"{title}: {excerpt}", link_url
        return "", ""

    # ---- クラブ1つぶん ----------------------------------------
    def _all_missing(self, club_name: str, note: str, categories) -> List[FoundLink]:
        return [
            FoundLink(category=target.category, club=club_name, note=note)
            for target in LINK_TARGETS
            if categories is None or target.category in categories
        ]

    def find_for_club(
        self,
        club_name: str,
        match_date: Optional[date] = None,
        opponent_name: str = "",
        venue_name: str = "",
        categories: Optional[Sequence[str]] = None,
    ) -> List[FoundLink]:
        logger.info("クラブ公式リンクの収集を開始します: %s", club_name)
        club = lookup_club(club_name, self.report)
        if club is None or not club.official_site_url:
            return self._all_missing(club_name, "クラブ情報に公式サイトURLがありません", categories)

        needs_browser = bool(USE_PLAYWRIGHT_FALLBACK) and any(
            host in club.official_site_url for host in CLUB_SITES_NEEDING_BROWSER
        )
        top_page = self.fetcher.fetch(
            club.official_site_url,
            use_playwright_fallback=bool(USE_PLAYWRIGHT_FALLBACK),
            force_render=needs_browser,
            wait_selector=CLUB_SITE_WAIT_SELECTORS.get(club.canonical_name, ""),
        )
        if top_page is None or top_page.soup is None:
            return self._apply_overrides(club.canonical_name, self._all_missing(
                club.canonical_name,
                f"公式サイトが自動取得を拒否しています（{club.official_site_url}）。ローカルJupyterで再取得するか、手で貼ってください",
                categories,
            ))

        logger.info("%s の公式サイトを %s で読みました: %s",
                    club.canonical_name, top_page.source, top_page.url)

        opponent_short = ""
        if opponent_name:
            opponent_club = lookup_club(opponent_name)
            if opponent_club is not None:
                aliases = J_LEAGUE_CLUBS[opponent_club.canonical_name]["aliases"]["short_name"]
                opponent_short = aliases[0] if aliases else opponent_club.canonical_name
            else:
                opponent_short = opponent_name
        tokens = self._match_tokens(match_date, opponent_short)
        venue_tokens = self._venue_tokens(venue_name)

        def wanted(target: LinkTarget) -> bool:
            return categories is None or target.category in categories

        # 試合情報を先に決める。その試合のページには、グルメ・イベント・
        # タイムテーブルが節（#anc2 など）として置かれていることが多いので、
        # 決まったら探索対象に足す。
        pages: List[FetchResult] = [top_page]
        results: List[FoundLink] = []
        match_target = LINK_TARGETS[0]
        match_link: Optional[FoundLink] = None
        goods_excerpt = goods_url = ""
        if wanted(match_target):
            match_link = self._resolve_target(
                pages, club.canonical_name, match_target, tokens, venue_tokens
            )
            results.append(match_link)
        if match_link is not None and match_link.found:
            match_page = self.fetcher.fetch(
                match_link.url, use_playwright_fallback=bool(USE_PLAYWRIGHT_FALLBACK)
            )
            if match_page is not None and match_page.soup is not None:
                logger.info("試合情報ページの中も探します: %s", match_page.url)
                pages.append(match_page)
                goods_excerpt, goods_url = self.extract_visitor_goods(match_page)

        for target in LINK_TARGETS[1:]:
            if not wanted(target):
                continue
            link = self._resolve_target(
                pages, club.canonical_name, target, tokens, venue_tokens
            )
            # ビジターグッズ販売の案内が試合情報ページにあれば、そちらを優先する。
            # 常設のオンラインストアより、その試合の物販のほうが記事に要る。
            if target.category == "グッズ販売" and goods_excerpt:
                link = FoundLink(
                    category=target.category, club=club.canonical_name,
                    label="試合情報ページのグッズ販売",
                    url=goods_url or (pages[-1].url if len(pages) > 1 else link.url),
                    excerpt=goods_excerpt,
                )
            results.append(link)
            logger.info(
                "リンク種別「%s」の結果: %s%s",
                target.category,
                link.url or "未発見",
                f"（{link.note}）" if link.note else "",
            )
        logger.info("クラブ公式リンクの収集が完了しました: %s", club.canonical_name)
        return self._apply_overrides(club.canonical_name, results)

    @staticmethod
    def _apply_overrides(club_name: str, results: List[FoundLink]) -> List[FoundLink]:
        """見つからなかった種別を、手書きの控えで埋める。

        開催地ごとに変わる種別（アクセス・グルメ・イベント）には当てない。
        常設のホームスタジアムの案内を控えにすると、国立開催のような
        ときに別会場の情報を貼ってしまう。"""
        overrides = CLUB_LINK_OVERRIDES.get(club_name, {})
        if not overrides:
            return results
        venue_specific = {target.category for target in LINK_TARGETS if target.venue_specific}
        filled: List[FoundLink] = []
        for link in results:
            if link.found or link.category not in overrides or link.category in venue_specific:
                filled.append(link)
                continue
            filled.append(FoundLink(
                category=link.category, club=club_name,
                label="CLUB_LINK_OVERRIDES より", url=overrides[link.category],
                note="公式サイトから拾えなかったため、手書きの控えを使いました",
            ))
        return filled


def collect_club_site_links(
    home_team: str,
    away_team: str,
    match_date: Optional[date],
    venue_name: str = "",
    venue_map_url: str = "",
    report: Optional[RunReport] = None,
) -> List[FoundLink]:
    """ホームとアウェイ、両クラブの公式サイトを見る。

    アクセスとグルメはスタジアム側の話なのでホームクラブが本命。
    アウェイ側は試合情報とグッズ（アウェイ限定グッズ）だけ見る。

    開催地がクラブの常設ホームでない場合（国立開催など）、クラブ公式の
    アクセス案内はそのままでは使えない。その場合は Jリーグ公式から取れている
    会場の地図URLを控えとして添える。"""
    logger.info("両クラブの公式サイトからリンクを収集します: %s / %s", home_team, away_team)
    network = build_network_config()
    finder = ClubSiteLinkFinder(PageFetcher(build_session(network), network, report), report)

    results: List[FoundLink] = list(
        finder.find_for_club(home_team, match_date, away_team, venue_name)
    )
    if away_team and normalize_club_key(away_team) != normalize_club_key(home_team):
        results.extend(finder.find_for_club(
            away_team, match_date, home_team, venue_name,
            categories=("試合情報", "グッズ販売"),
        ))

    access_found = any(link.category == "アクセス" and link.found for link in results)
    if not access_found and venue_map_url:
        results.append(FoundLink(
            category="アクセス", club=venue_name or "開催地",
            label="Googleマップ（Jリーグ公式より）", url=venue_map_url,
            note="クラブ公式に開催地のアクセス案内が見つからなかったため、地図を代わりに載せています",
        ))
    logger.info("クラブ公式サイトからのリンク収集が完了しました: %s件", len(results))
    return results


def log_club_site_links(links: Sequence[FoundLink]) -> None:
    if not links:
        return
    width = max(display_width(f"{link.category}（{link.club}）") for link in links)
    lines = ["", "--- クラブ公式サイトのリンク ---"]
    for link in links:
        head = pad_display(f"{link.category}（{link.club}）", width)
        if link.found:
            lines.append(f"  OK  {head}  {link.url}")
            # リンクの文字列も出す。的外れなページを拾っていないか目で確かめられる
            if link.label:
                lines.append(f"      {' ' * display_width(head)}  ↳ {link.label}")
            if link.excerpt:
                lines.append(f"      {' ' * display_width(head)}  ＞ {link.excerpt}")
            if link.note:
                lines.append(f"      {' ' * display_width(head)}  ※ {link.note}")
        else:
            lines.append(f"  --  {head}  {link.note or '見つかりませんでした'}")
    lines.append("--------------------------------")
    logger.info("\n".join(lines))

# ============================================================
# 手元で集めた結果を受け取る
# ------------------------------------------------------------
# Colab から読めないクラブサイトがあるため、取得だけ手元で行って
# 結果を [2/10] の CLUB_LINKS_JSON に貼る経路を用意している。
# 貼られていればそれを使い、無ければ Colab から取りに行く。
#
# 古い貼り付けをそのまま使うと、別の試合のリンクが記事に入る。
# 形式・対戦カード・鮮度の3つを確かめてから採用する。
# ============================================================
PASTED_LINKS_SCHEMA: Final[str] = "dankoba-clubsite-links/1"
PASTED_LINKS_MAX_AGE_HOURS: Final[int] = 48


@dataclass(frozen=True)
class PastedClubLinks:
    links: List[FoundLink]
    generated_at: str = ""
    match: Dict[str, str] = field(default_factory=dict)


def parse_pasted_club_links(raw: str, report: Optional[RunReport] = None) -> Optional[PastedClubLinks]:
    """貼り付けられた文字列を読む。JSONそのままでも base64 でも受ける。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except Exception:
            emit_warning(report, "CLUB_LINKS_JSON を読めません。JSON か base64 を貼ってください")
            return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        emit_warning(report, "CLUB_LINKS_JSON のJSONが壊れています: %s", error)
        return None
    if not isinstance(payload, dict):
        emit_warning(report, "CLUB_LINKS_JSON の中身がオブジェクトではありません")
        return None

    schema = str(payload.get("schema", ""))
    if schema != PASTED_LINKS_SCHEMA:
        emit_warning(
            report, "CLUB_LINKS_JSON の形式が想定と違います（%s / 想定 %s）",
            schema or "指定なし", PASTED_LINKS_SCHEMA,
        )
        return None

    links: List[FoundLink] = []
    allowed = {field_.name for field_ in fields(FoundLink)}
    for item in payload.get("links") or []:
        if not isinstance(item, dict) or not item.get("category"):
            continue
        links.append(FoundLink(**{key: str(value or "") for key, value in item.items()
                                  if key in allowed}))
    if not links:
        emit_warning(report, "CLUB_LINKS_JSON にリンクが入っていません")
        return None

    match = payload.get("match") or {}
    return PastedClubLinks(
        links=links,
        generated_at=str(payload.get("generated_at", "")),
        match={key: str(value or "") for key, value in match.items()} if isinstance(match, dict) else {},
    )


def verify_pasted_club_links(
    pasted: PastedClubLinks,
    home_team: str,
    away_team: str,
    kickoff_date: Optional[date],
    report: Optional[RunReport] = None,
) -> bool:
    """別の試合の貼り付けを使ってしまわないよう突き合わせる。"""
    match = pasted.match
    if not match:
        emit_warning(report, "CLUB_LINKS_JSON に試合の情報が無いため、取り違えを確認できません")
        return True

    mismatches: List[str] = []
    for label, pasted_value, current in (
        ("ホーム", match.get("home", ""), home_team),
        ("アウェイ", match.get("away", ""), away_team),
    ):
        if pasted_value and normalize_club_key(pasted_value) != normalize_club_key(current):
            mismatches.append(f"{label}={pasted_value}（いまは {current}）")
    pasted_date = match.get("kickoff_date", "")
    if pasted_date and kickoff_date is not None and pasted_date != kickoff_date.isoformat():
        mismatches.append(f"開催日={pasted_date}（いまは {kickoff_date.isoformat()}）")

    if mismatches:
        emit_warning(
            report,
            "CLUB_LINKS_JSON は別の試合のものです（%s）。使わずに Colab から取り直します",
            "、".join(mismatches),
        )
        return False

    if pasted.generated_at:
        try:
            generated = datetime.fromisoformat(pasted.generated_at)
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=timezone.utc)
            hours = (datetime.now(timezone.utc) - generated).total_seconds() / 3600
            if hours > PASTED_LINKS_MAX_AGE_HOURS:
                emit_warning(
                    report,
                    "CLUB_LINKS_JSON は %.0f時間前のものです。試合直前に公開されるページは"
                    "入っていない可能性があります",
                    hours,
                )
        except ValueError:
            logger.debug("generated_at を解釈できません: %s", pasted.generated_at)
    return True


def load_pasted_club_links(
    home_team: str,
    away_team: str,
    kickoff_date: Optional[date],
    report: Optional[RunReport] = None,
) -> Optional[List[FoundLink]]:
    """[2/10] に貼られた結果を使えるなら返す。使えなければ None。"""
    pasted = parse_pasted_club_links(CLUB_LINKS_JSON, report)
    if pasted is None:
        return None
    if not verify_pasted_club_links(pasted, home_team, away_team, kickoff_date, report):
        return None
    logger.info(
        "[2/10] に貼られた収集結果を使います（%s生成 / %s件）",
        pasted.generated_at or "生成時刻不明", len(pasted.links),
    )
    return pasted.links

def print_club_links_json_for_paste(
    links: Sequence[FoundLink], config
) -> str:
    """ローカル取得結果を、設定セルへ貼れる検証付きJSONにする。"""
    payload = {
        "schema": PASTED_LINKS_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "match": {
            "home": config.home_team,
            "away": config.away_team,
            "kickoff_date": config.kickoff_date.isoformat() if config.kickoff_date else "",
        },
        "links": [
            {
                "category": link.category,
                "club": link.club,
                "label": link.label,
                "url": link.url,
                "note": link.note,
                "excerpt": link.excerpt,
            }
            for link in links
        ],
    }
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.b64encode(compact.encode("utf-8")).decode("ascii")
    logger.info("次の1行をセル[2/10]の CLUB_LINKS_JSON に貼り付けてください")
    print(encoded)
    return encoded


# -*- coding: utf-8 -*-
# ============================================================
# Dankoba Helper Ver.1.19.1  [8/10] 記事テンプレート定義
# ------------------------------------------------------------
# 章立てを「構造データ」として組み立てる。Docs への書き込み方は
# 次のセルに閉じてあるので、記事の骨格を変えたいときはここだけ直す。
#
# 使えるブロック:
#   heading1 / heading2 / heading3 … 見出し
#   text                          … 本文（bold=True で太字）
#   placeholder                   … 後から書き換える灰色の斜体テキスト
#   bullets                       … 箇条書き
#   links                         … 「ラベル: URL」の行（URL部分がリンクになる）
#   table                         … 表
#   spacer                        … 空行
# ============================================================


def _text(content: str, bold: bool = False) -> Dict[str, Any]:
    return {"type": "text", "content": content, "bold": bold}


def _placeholder(content: str) -> Dict[str, Any]:
    return {"type": "placeholder", "content": content}


def _heading(level: int, content: str) -> Dict[str, Any]:
    return {"type": f"heading{level}", "content": content}


def build_match_guide_table(config: PreviewConfig) -> Dict[str, Any]:
    """観戦ガイドの2列表。空欄の項目はプレースホルダとして灰色で入る。"""
    placeholder_cells: set = set()

    def cell(value: str, fallback: str, row_index: int) -> str:
        if value:
            return value
        placeholder_cells.add((row_index, 1))
        return fallback

    venue_lines: List[str] = []
    if config.venue_name or config.venue_address:
        venue_lines.append(
            f"{config.venue_name}（{config.venue_address}）" if config.venue_address else config.venue_name
        )
    if config.venue_map_url:
        venue_lines.append(config.venue_map_url)
    venue_text = "\n".join(venue_lines)

    weather_lines: List[str] = []
    if config.weather_text:
        weather_lines.append(config.weather_text)
    if config.weather_url:
        weather_lines.append(config.weather_url)
    weather_text = "\n".join(weather_lines)

    rows = [
        [SectionLabel.KICKOFF, cell(config.kickoff_sentence, "（キックオフ日時を入力）", 0)],
        [SectionLabel.VENUE, cell(venue_text, "（会場名・住所・Googleマップのリンク）", 1)],
        [SectionLabel.BROADCAST, cell(config.broadcast, "（DAZN / 地上波・BS などの中継）", 2)],
        [SectionLabel.WEATHER, cell(weather_text, "（天気 / 気温 ℃前後の予想です。＋tenki.jpのリンク）", 3)],
    ]
    return {
        "type": "table",
        "rows": rows,
        "bold_columns": [0],
        "placeholder_cells": placeholder_cells,
        "detect_team_cells": False,
    }


def build_lineup_table(config: PreviewConfig) -> Dict[str, Any]:
    """先発予想の2列表。左がホーム、右がアウェイ。自チームのセルは色が付く。"""
    positions = "GK: \nDF: \nMF: \nFW: "
    rows = [
        [
            f"{config.home_team}({config.home_formation})",
            f"{config.away_team}({config.away_formation})",
        ],
        [positions, positions],
    ]
    return {
        "type": "table",
        "rows": rows,
        "bold_columns": [],
        "placeholder_cells": {(1, 0), (1, 1)},
        "detect_team_cells": True,
    }


# 記事の「🍡 グッズ・イベント・アクセス情報」に並べる項目と、
# そこへ入れる収集結果の種別。前のものから順に当て、URLが重なれば1回だけ載せる。
# スタグルとイベントは、クラブによっては試合情報ページの節に置かれているので、
# 専用のページが無ければ試合情報のリンクで代用する。
ARTICLE_LINK_SECTIONS: Final[Tuple[Tuple[str, Tuple[str, ...]], ...]] = (
    ("交通・アクセス", ("アクセス",)),
    ("スタグル", ("スタジアムグルメ", "試合情報")),
    ("グッズ", ("グッズ販売",)),
    ("イベント", ("イベント", "試合情報")),
)


def build_goods_section(
    config: PreviewConfig,
    club_links: Optional[Sequence[Any]] = None,
) -> List[Dict[str, Any]]:
    """リンク1本ごとに、その上へコメント行を置く形で並べる。

        交通・アクセス：（コメント）
        アクセス情報 ～ …（リンク）

        スタグル：（コメント）
        【試合情報】9月12日(土)…（リンク）
        （コメント）
        施設一覧 ～ …（リンク）
    """
    by_category: Dict[str, List[Any]] = {}
    for link in club_links or []:
        if getattr(link, "url", ""):
            by_category.setdefault(link.category, []).append(link)

    blocks: List[Dict[str, Any]] = []
    for title, categories in ARTICLE_LINK_SECTIONS:
        # 先の種別で取れたらそこで打ち切る。専用ページがあるのに
        # 試合情報ページまで並べると、同じページが3通りのURLで並ぶ。
        found: List[Any] = []
        seen: set = set()
        for category in categories:
            for link in by_category.get(category, []):
                if link.url in seen:
                    continue
                seen.add(link.url)
                found.append(link)
            if found:
                break

        if not found:
            blocks.append({"type": "labeled_placeholder", "label": title,
                           "content": f"（{title}のリンクが見つかりませんでした。手で探して貼る）"})
            blocks.append({"type": "spacer"})
            continue

        for index, link in enumerate(found):
            # 抜粋が取れているものは、それをコメントの下書きにする
            comment = link.excerpt or f"（{title}について、このリンクの一言コメント）"
            if index == 0:
                blocks.append({"type": "labeled_placeholder", "label": title, "content": comment})
            else:
                blocks.append(_placeholder(comment))
            blocks.append({"type": "link", "label": link.label or link.url, "url": link.url})
        blocks.append({"type": "spacer"})
    return blocks


def build_standings_blocks(
    config: PreviewConfig,
    standings: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Jリーグ公式の順位表を表として置く。自チームと対戦相手の行に色を付ける。"""
    if standings is None or not getattr(standings, "rows", None):
        return [_placeholder("（順位表を取得できませんでした。Jリーグ公式から貼る）")]
    highlight = {config.my_team: "my", config.opponent_team: "opponent"}
    return [
        {
            "type": "table",
            "rows": [list(row) for row in standings.rows],
            "bold_columns": [],
            "placeholder_cells": set(),
            "detect_team_cells": True,
            "highlight_clubs": highlight,
        },
        _placeholder(f"（順位表の出典: {standings.url}）"),
    ]


def build_highlight_blocks(
    opponent: str,
    highlight: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """前節のハイライト動画を「一文＋URL」で置く。"""
    if highlight is None or not getattr(highlight, "url", ""):
        return [_placeholder(f"（{opponent}の前節ハイライト動画URLを貼る）")]
    return [
        _text(f"{opponent}の前節のハイライト動画です。"),
        {"type": "link", "label": highlight.url, "url": highlight.url},
    ]


def build_document_structure(
    config: PreviewConfig,
    club_links: Optional[Sequence[Any]] = None,
    standings: Optional[Any] = None,
    highlight: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    opponent = config.opponent_team or "対戦相手"
    structure: List[Dict[str, Any]] = []

    # ---- 冒頭 ----
    structure.append(_heading(1, config.article_heading))
    structure.append(_text(SectionLabel.SPEAKER_DANKOBA, bold=True))
    structure.append(_placeholder(
        f"（リード文：前節の振り返りと、{opponent}戦の位置づけ。"
        "最後は「この試合をプレビューします。」で締める）"
    ))
    structure.append(_placeholder("（告知ポストなどのURLを貼る）"))
    structure.append({"type": "spacer"})

    # ---- 観戦ガイド ----
    structure.append(_heading(2, SectionLabel.GUIDE))
    structure.append(build_match_guide_table(config))
    structure.append({"type": "spacer"})
    structure.append(_text(SectionLabel.GOODS, bold=True))
    structure.extend(build_goods_section(config, club_links))
    structure.append({"type": "spacer"})

    # ---- 両チームの状況と先発予想 ----
    structure.append(_heading(2, SectionLabel.SITUATION))
    structure.extend(build_standings_blocks(config, standings))
    structure.append({"type": "spacer"})
    structure.append(_text(SectionLabel.SPEAKER_DANKOBA, bold=True))
    structure.append(_placeholder(
        f"（{config.my_team}と{opponent}の順位、この試合で取りたい勝ち点の話）"
    ))
    structure.append(_text(SectionLabel.SPEAKER_DANKOBA, bold=True))
    structure.append(_placeholder("（出場停止選手の有無。Jリーグ公式の該当ニュースURL）"))
    structure.append(_placeholder("（負傷者、代表招集による欠場の予想。根拠になるURL）"))
    structure.append(_placeholder("（先発予想の前置き：前節からの入れ替えをどう見たか）"))
    structure.append(build_lineup_table(config))
    structure.append({"type": "spacer"})

    # ---- 勝ち筋 ----
    structure.append(_heading(2, SectionLabel.WIN_PATH.format(my_team=config.my_team)))
    structure.append(_text(SectionLabel.SPEAKER_DANKOBA, bold=True))
    structure.extend(build_highlight_blocks(opponent, highlight))
    structure.append(_placeholder(
        f"（・で始まる着眼点を2〜3行。{opponent}の特徴と、{config.my_team}の狙いどころ）"
    ))
    structure.append({"type": "spacer"})

    structure.append(_heading(3, SectionLabel.ATTACK))
    structure.append(_text(SectionLabel.SPEAKER_ATTACK, bold=True))
    for index in range(1, config.attack_point_count + 1):
        structure.append(_heading(3, f"{index}. （攻撃のポイントの見出し）"))
        structure.append(_placeholder("（本文：相手の弱みと、そこを突くための具体的な手立て）"))

    structure.append(_heading(3, SectionLabel.DEFENSE))
    structure.append(_text(SectionLabel.SPEAKER_DEFENSE, bold=True))
    for index in range(1, config.defense_point_count + 1):
        structure.append(_heading(3, f"{index}. （守備のポイントの見出し）"))
        structure.append(_placeholder("（本文：警戒する相手選手と、対応で徹底したいこと）"))
    structure.append({"type": "spacer"})

    # ---- おわりに ----
    structure.append(_heading(2, SectionLabel.CLOSING))
    structure.append(_text(SectionLabel.SPEAKER_DANKOBA, bold=True))
    structure.append(_placeholder("（締めの文）"))

    # ---- 参考リンク（執筆用。公開前に削除する） ----
    reference_links = list(config.reference_links)
    seen_urls = {url for _, url in reference_links if url}
    for link in club_links or []:
        label, url = link.as_reference()
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        reference_links.append((label, url))
    if config.include_reference_section and reference_links:
        structure.append({"type": "spacer"})
        structure.append(_heading(2, SectionLabel.REFERENCE))
        structure.append({"type": "links", "items": reference_links})

    return structure


def summarize_structure(structure: List[Dict[str, Any]]) -> str:
    """生成前に章立てをログで確認できるようにする。"""
    lines = ["", "--- 生成する章立て ---"]
    for item in structure:
        item_type = str(item.get("type"))
        if item_type == "heading1":
            lines.append(f"  H1  {item.get('content')}")
        elif item_type == "heading2":
            lines.append(f"  H2  {item.get('content')}")
        elif item_type == "heading3":
            lines.append(f"    H3  {item.get('content')}")
        elif item_type == "table":
            rows = item.get("rows") or []
            columns = len(rows[0]) if rows else 0
            lines.append(f"      表  {len(rows)}行 × {columns}列")
        elif item_type == "bullets":
            lines.append(f"      箇条書き  {len(item.get('items') or [])}項目")
        elif item_type == "links":
            lines.append(f"      リンク  {len(item.get('items') or [])}件")
        elif item_type == "labeled_placeholder":
            lines.append(f"      {item.get('label')}")
        elif item_type == "link":
            lines.append(f"        ↳ {item.get('label')}")
    lines.append("----------------------")
    return "\n".join(lines)



# Local GUI settings injected by Dankoba_Helper_Local.py before each run.
MY_TEAM = ""
OPPONENT_TEAM = ""
SERIAL_NUMBER = ""
MY_TEAM_FORMATION = ""
OPPONENT_FORMATION = ""
COMPETITION_TYPE = "J1"
CLUB_LINKS_JSON = ""
RESPECT_ROBOTS_TXT = True
REQUEST_MIN_INTERVAL_SEC = 1.0
PLAYWRIGHT_HEADLESS = False
PLAYWRIGHT_CHANNEL = "chrome"
USE_PLAYWRIGHT_FALLBACK = True
