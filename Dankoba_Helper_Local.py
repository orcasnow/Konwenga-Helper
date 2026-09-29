# -*- coding: utf-8 -*-
"""Dankoba Helper local GUI. Runs independently of Google Colab and Google Docs."""

from __future__ import annotations

import queue
import logging
import os
import re
import shutil
import sys
import threading
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    # PyInstaller one-file実行では同梱ブラウザーが一時展開先にある。
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(sys._MEIPASS) / "pw-browsers")

try:
    import dankoba_local_core as core
except ImportError as exc:
    raise SystemExit(
        "ローカル用依存パッケージを読み込めません。README_LOCAL.md の手順で"
        f"依存関係をインストールしてください。\n\n詳細: {exc}"
    ) from exc


APP_VERSION = "2.0"
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")


class _QueueLogHandler(logging.Handler):
    """ワーカースレッドのログをGUIの処理ログにも流す。"""

    def __init__(self, events: queue.Queue):
        super().__init__(logging.INFO)
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.put(("log", self.format(record)))
        except Exception:
            self.handleError(record)


def _fill_match_details(values: dict, report: core.RunReport) -> None:
    """選択大会の日程から試合情報を空欄に補う。"""
    network = core.build_network_config()
    fetcher = core.PageFetcher(core.build_session(network), network, report)
    lookup = core.JLeagueScheduleLookup(
        fetcher,
        report,
        render_on_miss=bool(core.USE_PLAYWRIGHT_FALLBACK),
    )
    competition = core.competition_type(values["competition_type"])
    core.logger.info("%s の日程から %s vs %s の試合を検索します",
                     competition.name, values["my_team"], values["opponent_team"])
    info = None
    try:
        info = lookup.resolve(values["my_team"], values["opponent_team"], competition)
    except Exception as exc:
        core.logger.exception("大会日程からの試合情報取得に失敗しました")
        report.warn(f"大会日程からの自動取得に失敗しました: {exc}")

    if info is None:
        core.logger.info("日程から試合を特定できませんでした。入力済みの値で続けます")
    else:
        core.MATCH_PAGE_URL = info.url
        discovered = {
            "round_label": info.round_label,
            "kickoff_date": info.kickoff_date,
            "kickoff_time": info.kickoff_time,
            "venue_name": info.venue_name,
            "venue_address": info.venue_address,
            "venue_map_url": info.venue_map_url,
            "broadcast": info.broadcast,
        }
        if info.home_team:
            discovered["home_or_away"] = (
                "ホーム"
                if core.JLeagueScheduleLookup._loose_match(info.home_team, values["my_team"])
                else "アウェイ"
            )
        filled = []
        for key, value in discovered.items():
            if value and not str(values.get(key, "")).strip():
                values[key] = str(value)
                filled.append(key)
        core.logger.info("日程から取得した値を反映しました: %s",
                         "、".join(filled) if filled else "入力済みの値を維持しました")
        core.logger.info("試合情報の取得元: %s", info.url)

    if not core.AUTO_FILL_WEATHER or str(values.get("weather_text", "")).strip():
        return
    try:
        forecast = core.TenkiJpForecast(fetcher, report).resolve(
            values.get("venue_name", ""),
            values.get("venue_address", ""),
            values.get("kickoff_date", ""),
            values.get("kickoff_time", ""),
        )
    except Exception as exc:
        core.logger.exception("天気の自動取得に失敗しました")
        report.warn(f"天気の自動取得に失敗しました: {exc}")
        return
    if forecast is None:
        return
    if forecast.text and not str(values.get("weather_text", "")).strip():
        values["weather_text"] = forecast.text
    if forecast.url and not str(values.get("weather_url", "")).strip():
        values["weather_url"] = forecast.url


class ScrollableFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, **kwargs):
        super().__init__(master, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas, padding=12)
        self.window_id = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.body.bind("<Configure>", self._update_scrollregion)
        self.canvas.bind("<Configure>", self._resize_body)
        self.canvas.bind_all("<MouseWheel>", self._mousewheel)

    def _update_scrollregion(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_body(self, event):
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def _mousewheel(self, event):
        if self.winfo_exists() and self.winfo_ismapped():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


class DankobaLocalApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"Dankoba Helper Local Ver.{APP_VERSION}")
        self.root.geometry("1120x860")
        self.root.minsize(900, 680)
        self.vars: dict[str, tk.StringVar] = {}
        self.texts: dict[str, tk.Text] = {}
        self.selected_images: list[Path] = []
        self.events: queue.Queue = queue.Queue()
        self.generate_button: ttk.Button | None = None
        self.status_var = tk.StringVar(value="入力して Markdown を作成してください")
        self.competition_var = tk.StringVar(value="J1")
        self.collect_club_links_var = tk.BooleanVar(value=True)
        self.collect_standings_var = tk.BooleanVar(value=True)
        self.collect_highlight_var = tk.BooleanVar(value=True)
        self.playwright_var = tk.BooleanVar(value=True)
        self.competition_var.trace_add("write", self._on_competition_changed)
        self._build_ui()
        self.root.after(150, self._poll_events)

    def _var(self, key: str, default: str = "") -> tk.StringVar:
        value = tk.StringVar(value=default)
        self.vars[key] = value
        return value

    def _entry(
        self,
        parent: tk.Misc,
        label: str,
        key: str,
        default: str = "",
        width: int = 44,
        required: bool = False,
    ):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        label_options = {"text": f"{label} ＊" if required else label, "width": 20}
        if required:
            label_options["foreground"] = "#b3261e"
        ttk.Label(row, **label_options).pack(side="left", anchor="nw")
        entry = ttk.Entry(row, textvariable=self._var(key, default), width=width)
        entry.pack(side="left", fill="x", expand=True)
        return entry

    def _text(self, parent: tk.Misc, label: str, key: str, height: int = 4):
        box = ttk.LabelFrame(parent, text=label, padding=6)
        box.pack(fill="x", expand=False, pady=5)
        text = tk.Text(box, height=height, wrap="word", undo=True)
        text.pack(fill="x", expand=True)
        self.texts[key] = text
        return text

    def _on_competition_changed(self, *_args):
        self.collect_standings_var.set(self.competition_var.get() in {"J1", "J2", "J3"})

    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=(10, 4))
        competition_box = ttk.LabelFrame(top, text="大会種別", padding=(12, 7))
        competition_box.pack(side="left", fill="y")
        ttk.Label(competition_box, text="この記事の大会 ＊必須", foreground="#b3261e").pack(anchor="w")
        competition_radios = ttk.Frame(competition_box)
        competition_radios.pack(anchor="w")
        for item in core.COMPETITION_TYPES:
            ttk.Radiobutton(
                competition_radios, text=item.name, value=item.name, variable=self.competition_var
            ).pack(side="left", padx=(0, 7))
        self._build_local_settings(top)

        tabs = ttk.Notebook(self.root)
        tabs.pack(fill="both", expand=True, padx=10, pady=6)
        match_tab = ScrollableFrame(tabs)
        article_tab = ScrollableFrame(tabs)
        output_tab = ScrollableFrame(tabs)
        tabs.add(match_tab, text="試合情報")
        tabs.add(article_tab, text="記事本文")
        tabs.add(output_tab, text="参考リンク・画像・取得設定")
        self._build_match_tab(match_tab.body)
        self._build_article_tab(article_tab.body)
        self._build_output_tab(output_tab.body)

        footer = ttk.Frame(self.root, padding=(12, 5, 12, 10))
        footer.pack(fill="x")
        self.generate_button = ttk.Button(
            footer, text="Markdown を作成", command=self._start_generation
        )
        self.generate_button.pack(side="right")
        ttk.Label(footer, textvariable=self.status_var).pack(side="left", fill="x", expand=True)

    def _build_match_tab(self, parent: tk.Misc):
        ttk.Label(
            parent,
            text="大会種別と両クラブから試合日程を検索し、日時・会場・中継などを空欄に補います。記事番号や布陣は未入力でも先に検索し、取得結果を確認してから Markdown を作成できます。",
            wraplength=980,
        ).pack(anchor="w", pady=(0, 10))
        self._entry(parent, "記事番号", "serial_number")
        today = date.today()
        season_start_year = today.year if today.month >= 8 else today.year - 1
        season_default = f"{season_start_year}/{str(season_start_year + 1)[-2:]}"
        self._entry(parent, "シーズン", "season", season_default)
        self._entry(parent, "大会名（任意）", "competition")
        self._entry(parent, "節・ラウンド", "round_label")
        self._entry(parent, "自チーム", "my_team", required=True)
        self._entry(parent, "対戦相手", "opponent_team", required=True)
        self._entry(parent, "対戦相手のハッシュタグ", "opponent_hashtag", width=30)

        home_row = ttk.Frame(parent)
        home_row.pack(fill="x", pady=5)
        ttk.Label(home_row, text="自チームの開催区分", width=20).pack(side="left")
        self._var("home_or_away", "")
        ttk.Radiobutton(home_row, text="ホーム", value="ホーム", variable=self.vars["home_or_away"]).pack(side="left", padx=8)
        ttk.Radiobutton(home_row, text="アウェイ", value="アウェイ", variable=self.vars["home_or_away"]).pack(side="left", padx=8)

        self._entry(parent, "キックオフ日", "kickoff_date", "", width=20)
        ttk.Label(parent, text="日付は YYYY-MM-DD 形式で入力してください。", foreground="#666").pack(anchor="w", padx=(160, 0))
        self._entry(parent, "キックオフ時刻", "kickoff_time", "", width=20)
        self._entry(parent, "自チームの布陣", "my_team_formation", width=20)
        self._entry(parent, "相手の布陣", "opponent_formation", width=20)
        self._entry(parent, "会場", "venue_name")
        self._entry(parent, "会場住所", "venue_address")
        self._entry(parent, "地図URL", "venue_map_url")
        self._entry(parent, "中継", "broadcast")
        self._entry(parent, "天気のメモ", "weather_text")
        self._entry(parent, "天気URL", "weather_url")

    def _build_article_tab(self, parent: tk.Misc):
        ttk.Label(
            parent,
            text="記事の下書き欄です。空欄は既存テンプレートの執筆メモとして出力されます。",
            wraplength=980,
        ).pack(anchor="w", pady=(0, 8))
        self._text(parent, "リード文", "lead", 4)
        self._entry(parent, "告知ポスト等のURL", "announcement_url")
        self._text(parent, "両チームの状況・順位について", "team_situation", 3)
        self._text(parent, "出場停止について", "suspensions", 2)
        self._text(parent, "負傷者・代表招集について", "absences", 2)
        self._text(parent, "先発予想の前置き", "lineup_intro", 3)
        self._text(parent, "勝ち筋・試合の見どころ", "win_path", 3)

        tactics = ttk.LabelFrame(parent, text="攻撃のポイント", padding=8)
        tactics.pack(fill="x", pady=5)
        for index in (1, 2):
            self._entry(tactics, f"{index}. 見出し", f"attack_{index}_title")
            self._text(tactics, f"{index}. 本文", f"attack_{index}_body", 3)
        tactics = ttk.LabelFrame(parent, text="守備のポイント", padding=8)
        tactics.pack(fill="x", pady=5)
        for index in (1, 2):
            self._entry(tactics, f"{index}. 見出し", f"defense_{index}_title")
            self._text(tactics, f"{index}. 本文", f"defense_{index}_body", 3)
        self._text(parent, "締めの文", "closing", 3)

    def _build_output_tab(self, parent: tk.Misc):
        ttk.Label(
            parent,
            text="参考リンクは1行に「表示名 | URL」と入力してください。画像を選ぶと Markdown と同じ場所の attachments フォルダーへコピーされます。",
            wraplength=980,
        ).pack(anchor="w", pady=(0, 8))
        self._text(parent, "参考リンク（任意）", "reference_links", 7)

        image_box = ttk.LabelFrame(parent, text="添付画像", padding=8)
        image_box.pack(fill="both", expand=True, pady=6)
        self.image_list = tk.Listbox(image_box, height=6, selectmode="extended")
        self.image_list.pack(side="left", fill="both", expand=True)
        image_buttons = ttk.Frame(image_box)
        image_buttons.pack(side="left", fill="y", padx=8)
        ttk.Button(image_buttons, text="画像を追加", command=self._add_images).pack(fill="x", pady=3)
        ttk.Button(image_buttons, text="選択を外す", command=self._remove_images).pack(fill="x", pady=3)

        self.log_box = tk.Text(parent, height=8, wrap="word", state="disabled")
        ttk.Label(parent, text="処理ログ").pack(anchor="w", pady=(8, 2))
        self.log_box.pack(fill="x", expand=True)

    def _build_local_settings(self, parent: tk.Misc):
        options = ttk.LabelFrame(parent, text="ローカル取得設定", padding=8)
        options.pack(side="left", fill="both", expand=True, padx=(8, 0))
        ttk.Checkbutton(
            options, text="対戦する2クラブの公式サイトから参考リンクを収集する",
            variable=self.collect_club_links_var,
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            options, text="順位表を追加で取得する（Jリーグ公式）",
            variable=self.collect_standings_var,
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            options, text="相手の前節ハイライト動画を検索する（既定で有効）",
            variable=self.collect_highlight_var,
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            options, text="静的HTMLで判定できない場合、Playwrightで再取得する",
            variable=self.playwright_var,
        ).pack(anchor="w", pady=2)
        ttk.Label(
            options,
            text="ブラウザー再取得でも拒否される場合は取得できません。試合情報は入力欄とクラブ公式サイトを使ってください。",
            foreground="#555",
            wraplength=580,
        ).pack(anchor="w", padx=24, pady=(3, 0))

    def _add_images(self):
        paths = filedialog.askopenfilenames(
            title="Markdown に添付する画像を選択",
            filetypes=[("画像ファイル", "*.png *.jpg *.jpeg *.gif *.webp *.bmp *.tif *.tiff"), ("すべてのファイル", "*.*")],
        )
        for value in paths:
            path = Path(value)
            if path.suffix.lower() not in IMAGE_TYPES:
                continue
            if path not in self.selected_images:
                self.selected_images.append(path)
                self.image_list.insert("end", str(path))

    def _remove_images(self):
        selected = list(self.image_list.curselection())
        for index in reversed(selected):
            self.image_list.delete(index)
            del self.selected_images[index]

    def _snapshot(self) -> dict:
        values = {key: value.get().strip() for key, value in self.vars.items()}
        values.update({key: widget.get("1.0", "end-1c").strip() for key, widget in self.texts.items()})
        values.update(
            competition_type=self.competition_var.get(),
            collect_club_links=self.collect_club_links_var.get(),
            collect_standings=self.collect_standings_var.get(),
            collect_highlight=self.collect_highlight_var.get(),
            playwright=self.playwright_var.get(),
            images=list(self.selected_images),
        )
        return values

    def _start_generation(self):
        values = self._snapshot()
        required_fields = (
            ("my_team", "自チーム"),
            ("opponent_team", "対戦相手"),
        )
        missing = [label for key, label in required_fields if not values.get(key, "").strip()]
        valid_competitions = {item.name for item in core.COMPETITION_TYPES}
        if values.get("competition_type") not in valid_competitions:
            missing.insert(0, "大会種別")
        if missing:
            messagebox.showwarning(
                "必須項目を入力してください",
                "次の必須項目が未入力です:\n\n・" + "\n・".join(missing),
                parent=self.root,
            )
            return
        self.generate_button.configure(state="disabled")
        self.status_var.set("大会日程から試合情報を取得しています…")
        self._append_log("作成を開始しました。大会日程を検索します。")
        threading.Thread(target=self._generate_worker, args=(values,), daemon=True).start()

    def _apply_review_values(self, values: dict) -> None:
        """自動取得値を入力欄にも反映し、確認後に手修正できるようにする。"""
        for key, variable in self.vars.items():
            if key in values:
                variable.set(str(values.get(key, "")))

    def _review_match_info(self, values: dict, response: dict, ready: threading.Event) -> None:
        """自動取得結果を一覧し、確認後に保存先を選んでもらう。"""
        try:
            self._apply_review_values(values)
            config = response["config"]
            fields = (
                ("選択した大会種別", values.get("competition_type", "")),
                ("記事に載せる大会名", config.competition),
                ("シーズン", config.season),
                ("記事番号", values.get("serial_number", "")),
                ("自チーム", config.my_team),
                ("対戦相手", config.opponent_team),
                ("対戦相手のハッシュタグ", values.get("opponent_hashtag", "")),
                ("自チームの布陣", config.my_team_formation),
                ("相手の布陣", config.opponent_formation),
                ("節・ラウンド", values.get("round_label", "")),
                ("開催区分", values.get("home_or_away", "")),
                ("キックオフ", " ".join(
                    value for value in (values.get("kickoff_date", ""), values.get("kickoff_time", ""))
                    if value
                )),
                ("会場", values.get("venue_name", "")),
                ("住所", values.get("venue_address", "")),
                ("地図URL", values.get("venue_map_url", "")),
                ("中継", values.get("broadcast", "")),
                ("天気", values.get("weather_text", "")),
                ("天気URL", values.get("weather_url", "")),
            )
            lines = [
                f"{label}: {value or '（未取得・必要なら試合情報タブで入力）'}"
                for label, value in fields
            ]
            lines.extend(("", "検索で取得した付加情報"))
            found_links = [
                link for link in response.get("club_links", [])
                if getattr(link, "found", False) and getattr(link, "url", "")
            ]
            lines.append(f"クラブ公式サイトの参考リンク: {len(found_links)}件")
            for link in found_links[:5]:
                lines.append(f"・{link.category}: {link.label or link.url} — {link.url[:90]}")
            if len(found_links) > 5:
                lines.append(f"・ほか {len(found_links) - 5}件（参考リンクに追加）")
            standings = response.get("standings")
            if standings and getattr(standings, "rows", None):
                lines.append(f"順位表: 取得済み（{standings.league}、{len(standings.rows) - 1}クラブ）")
            elif values.get("collect_standings"):
                lines.append("順位表: 未取得")
            highlight = response.get("highlight")
            if highlight and getattr(highlight, "url", ""):
                lines.append(f"前節ハイライト: {highlight.title or highlight.url}")
            elif values.get("collect_highlight"):
                lines.append("前節ハイライト: 未取得")
            warnings = response.get("warnings", [])
            if warnings:
                lines.append(f"検索時の注意: {len(warnings)}件（詳細は処理ログを確認）")
            lines.extend(("", "記事番号や布陣など、検索対象外の項目が空欄でも作成できます。"))
            lines.extend(("", "この内容で Markdown を作成しますか？"))
            accepted = messagebox.askyesno(
                "検索結果の確認",
                "\n".join(lines),
                parent=self.root,
            )
            if not accepted:
                return

            output_value = filedialog.asksaveasfilename(
                parent=self.root,
                title="Markdown の保存先",
                initialfile=self._suggest_filename(values),
                defaultextension=".md",
                filetypes=[("Markdown", "*.md")],
            )
            if not output_value:
                return
            output_path = Path(output_value)
            if output_path.suffix.lower() != ".md":
                output_path = output_path.with_suffix(".md")
            response["output_path"] = output_path
            response["accepted"] = True
            self.status_var.set("Markdown を作成しています…")
            self._append_log("試合情報を確認しました。Markdown を作成します。")
        except Exception as exc:
            core.logger.exception("試合情報の確認ダイアログを表示できませんでした")
            messagebox.showerror("確認画面のエラー", str(exc), parent=self.root)
        finally:
            ready.set()

    @staticmethod
    def _safe_name(value: str) -> str:
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" ._")
        return value[:90] or "match-preview"

    def _suggest_filename(self, values: dict) -> str:
        serial = values.get("serial_number") or ""
        prefix = f"D{serial}_" if serial else ""
        return self._safe_name(prefix + values.get("my_team", "home") + "_vs_" + values.get("opponent_team", "away")) + ".md"

    def _generate_worker(self, values: dict):
        report = core.RunReport()
        copied_images: list[tuple[str, str]] = []
        log_handler = _QueueLogHandler(self.events)
        log_handler.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        core.logger.addHandler(log_handler)
        try:
            core.logger.info("Dankoba Helper Local Ver.%s", APP_VERSION)
            core.logger.info("Markdown作成を開始します")
            core.logger.info(
                "設定: 公式リンク=%s、順位表=%s、ハイライト=%s、Playwright=%s",
                "有効" if values["collect_club_links"] else "無効",
                "有効" if values["collect_standings"] else "無効",
                "有効" if values["collect_highlight"] else "無効",
                "有効" if values["playwright"] else "無効",
            )
            core.COMPETITION_TYPE = values["competition_type"]
            core.RESPECT_ROBOTS_TXT = True
            core.REQUEST_MIN_INTERVAL_SEC = 1.0
            core.PLAYWRIGHT_HEADLESS = False
            core.PLAYWRIGHT_CHANNEL = ""
            core.USE_PLAYWRIGHT_FALLBACK = bool(values["playwright"])
            core.CLUB_LINKS_JSON = ""
            core.MY_TEAM = values["my_team"]
            core.OPPONENT_TEAM = values["opponent_team"]
            core.SERIAL_NUMBER = values["serial_number"]
            core.MY_TEAM_FORMATION = values["my_team_formation"]
            core.OPPONENT_FORMATION = values["opponent_formation"]
            core.MATCH_PAGE_URL = ""
            core.apply_extra_aliases(report)

            _fill_match_details(values, report)

            config = core.build_preview_config(
                serial_number=values.get("serial_number", ""),
                season=values.get("season", ""),
                competition=values.get("competition", ""),
                round_label=values.get("round_label", ""),
                my_team=values["my_team"],
                opponent_team=values["opponent_team"],
                use_official_club_name=True,
                opponent_hashtag=values.get("opponent_hashtag", ""),
                home_or_away=values.get("home_or_away", "ホーム"),
                my_team_formation=values.get("my_team_formation", ""),
                opponent_formation=values.get("opponent_formation", ""),
                kickoff_date=values.get("kickoff_date", ""),
                kickoff_time=values.get("kickoff_time", ""),
                venue_name=values.get("venue_name", ""),
                venue_address=values.get("venue_address", ""),
                venue_map_url=values.get("venue_map_url", ""),
                broadcast=values.get("broadcast", ""),
                weather_text=values.get("weather_text", ""),
                weather_url=values.get("weather_url", ""),
                attack_point_count=2,
                defense_point_count=2,
                include_reference_section=True,
                drive_folder_name="",
                report=report,
            )
            if not str(values.get("competition", "")).strip():
                values["competition"] = config.competition
            if not str(values.get("opponent_hashtag", "")).strip():
                values["opponent_hashtag"] = config.opponent_hashtag
            core.logger.info("試合情報を確定しました: %s", config.document_title)
            core.logger.info(
                "対戦カード: %s vs %s / %s %s / %s",
                config.home_team,
                config.away_team,
                config.kickoff_date.isoformat() if config.kickoff_date else "日付未設定",
                config.kickoff_time or "時刻未設定",
                config.venue_name or "会場未設定",
            )

            club_links = []
            if values["collect_club_links"]:
                core.logger.info("クラブ公式サイトのリンク収集を開始します")
                try:
                    club_links = core.collect_club_site_links(
                        config.home_team,
                        config.away_team,
                        config.kickoff_date,
                        venue_name=config.venue_name,
                        venue_map_url=config.venue_map_url,
                        report=report,
                    )
                    found_count = sum(1 for link in club_links if getattr(link, "found", False))
                    report.ok("クラブ公式サイト", f"リンク {found_count}件")
                    core.logger.info("クラブ公式サイトのリンク収集が完了しました: %s件", found_count)
                except Exception as exc:
                    report.warn(f"クラブ公式サイトのリンク取得に失敗しました: {exc}")
                    club_links = []

            standings = None
            highlight = None
            if values["collect_standings"]:
                try:
                    core.logger.info("Jリーグ順位表を取得します: %s", config.my_team)
                    standings = core.fetch_standings(config.my_team, report)
                    core.logger.info("Jリーグ順位表の取得が完了しました")
                except Exception as exc:
                    report.warn(f"順位表の取得に失敗しました: {exc}")
            if values["collect_highlight"]:
                try:
                    core.logger.info("対戦相手の前節ハイライトを検索します: %s", config.opponent_team)
                    highlight = core.fetch_previous_highlight(config.opponent_team, config.season, report)
                    core.logger.info("前節ハイライトの検索が完了しました")
                except Exception as exc:
                    report.warn(f"前節ハイライトの取得に失敗しました: {exc}")

            review_response = {
                "config": config,
                "accepted": False,
                "club_links": club_links,
                "standings": standings,
                "highlight": highlight,
                "warnings": list(report.warnings),
            }
            review_ready = threading.Event()
            self.events.put(("review", values, review_response, review_ready))
            review_ready.wait()
            if not review_response["accepted"]:
                self.events.put(("cancelled",))
                return
            values["output_path"] = review_response["output_path"]

            core.logger.info("Markdownの構成を組み立てます")
            structure = core.build_document_structure(
                config, club_links=club_links, standings=standings, highlight=highlight
            )
            self._apply_article_text(structure, values)
            self._append_reference_links(structure, values.get("reference_links", ""))
            core.logger.info("入力内容と参考リンクを反映しました")

            output_path: Path = values["output_path"]
            output_path.parent.mkdir(parents=True, exist_ok=True)
            for source in values.get("images", []):
                try:
                    core.logger.info("画像を添付します: %s", source)
                    attached = self._copy_image(source, output_path.parent / "attachments")
                    relative = "attachments/" + quote(attached.name)
                    copied_images.append((source.name, relative))
                except Exception as exc:
                    report.warn(f"画像を添付できませんでした（{source.name}）: {exc}")

            markdown = self._render_markdown(structure, copied_images)
            core.logger.info("Markdownを書き込みます: %s", output_path)
            output_path.write_text(markdown, encoding="utf-8")
            core.logger.info("Markdownの書き込みが完了しました (%s文字)", len(markdown))
            self.events.put(("complete", str(output_path), report.warnings, len(copied_images)))
        except Exception as exc:
            core.logger.exception("Markdown作成中にエラーが発生しました")
            self.events.put(("error", str(exc)))
        finally:
            core.logger.removeHandler(log_handler)

    @staticmethod
    def _copy_image(source: Path, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        base = source.stem
        suffix = source.suffix.lower()
        target = destination / f"{base}{suffix}"
        index = 2
        while target.exists():
            target = destination / f"{base}-{index}{suffix}"
            index += 1
        shutil.copy2(source, target)
        return target

    @staticmethod
    def _apply_article_text(structure: list[dict], values: dict):
        replacements = (
            ("（リード文：", "lead"),
            ("（告知ポストなどのURLを貼る）", "announcement_url"),
            ("（出場停止選手の有無。", "suspensions"),
            ("（負傷者、代表招集による欠場の予想。", "absences"),
            ("（先発予想の前置き：", "lineup_intro"),
            ("（締めの文）", "closing"),
        )
        tactic = None
        for item in structure:
            kind = item.get("type")
            content = str(item.get("content", ""))
            if kind == "placeholder":
                if content.startswith("（順位表を取得できませんでした"):
                    continue
                if content.startswith("（") and "順位、この試合で取りたい勝ち点" in content:
                    key = "team_situation"
                    candidate = values.get(key, "")
                    if candidate:
                        item["content"] = candidate
                        item["type"] = "text"
                    continue
                for prefix, key in replacements:
                    if content == prefix or content.startswith(prefix):
                        candidate = values.get(key, "")
                        if candidate:
                            if key == "announcement_url":
                                item["type"] = "link"
                                item["label"] = "告知ポスト"
                                item["url"] = candidate
                            else:
                                item["content"] = candidate
                                item["type"] = "text"
                        break
                if content.startswith("（・で始まる着眼点"):
                    candidate = values.get("win_path", "")
                    if candidate:
                        item["content"] = candidate
                        item["type"] = "text"
                elif content.startswith("（本文：") and tactic:
                    candidate = values.get(f"{tactic[0]}_{tactic[1]}_body", "")
                    if candidate:
                        item["content"] = candidate
                        item["type"] = "text"
                    tactic = None
            elif kind == "heading3":
                match = re.match(r"^(\d+)\. （(攻撃|守備)のポイント", content)
                if match:
                    number = int(match.group(1))
                    prefix = "attack" if match.group(2) == "攻撃" else "defense"
                    tactic = (prefix, number)
                    candidate = values.get(f"{prefix}_{number}_title", "")
                    if candidate:
                        item["content"] = f"{number}. {candidate}"

    @staticmethod
    def _append_reference_links(structure: list[dict], raw: str):
        manual = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                label, url = (part.strip() for part in line.split("|", 1))
            else:
                label, url = line, line
            if url:
                manual.append((label or url, url))
        if not manual:
            return
        reference = next((item for item in structure if item.get("type") == "links"), None)
        if reference is None:
            structure.extend(({"type": "spacer"}, {"type": "heading2", "content": core.SectionLabel.REFERENCE}))
            reference = {"type": "links", "items": []}
            structure.append(reference)
        existing = {url for _label, url in reference.get("items", [])}
        reference.setdefault("items", [])
        for label, url in manual:
            if url not in existing:
                reference["items"].append((label, url))
                existing.add(url)

    @staticmethod
    def _markdown_cell(value, placeholder=False, bold=False):
        value = str(value).replace("|", "\\|").replace("\n", "<br>").strip()
        if re.fullmatch(r"https?://\S+", value):
            value = f"<{value}>"
        if placeholder:
            value = f"*{value}*"
        if bold:
            value = f"**{value}**"
        return value

    @classmethod
    def _render_markdown(cls, structure: list[dict], images: list[tuple[str, str]]) -> str:
        lines: list[str] = []
        for item in structure:
            kind = item.get("type", "")
            content = str(item.get("content", ""))
            if kind.startswith("heading"):
                level = int(kind.removeprefix("heading"))
                lines.extend(["#" * max(1, min(level, 6)) + " " + content, ""])
            elif kind == "text":
                lines.extend([f"**{content}**" if item.get("bold") else content, ""])
            elif kind == "placeholder":
                lines.extend([f"> *{content}*", ""])
            elif kind == "labeled_placeholder":
                lines.extend([f"**{item.get('label', '')}**", f"> *{content}*", ""])
            elif kind == "spacer":
                lines.append("")
            elif kind == "link":
                label = item.get("label") or item.get("url") or "リンク"
                lines.extend([f"[{label}]({item.get('url', '')})", ""])
            elif kind == "links":
                for label, url in item.get("items", []):
                    if url:
                        lines.append(f"- [{label or url}]({url})")
                lines.append("")
            elif kind == "bullets":
                lines.extend(f"- {value}" for value in item.get("items", []))
                lines.append("")
            elif kind == "table":
                rows = item.get("rows") or []
                if not rows:
                    continue
                uses_first_row_as_header = bool(item.get("detect_team_cells"))
                header = rows[0] if uses_first_row_as_header else ["項目", "内容"]
                body = rows[1:] if uses_first_row_as_header else rows
                width = max([len(header)] + [len(row) for row in body])
                header = list(header) + [""] * (width - len(header))
                lines.append("| " + " | ".join(cls._markdown_cell(value) for value in header) + " |")
                lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
                placeholders = item.get("placeholder_cells", set())
                for body_index, row in enumerate(body):
                    cells = []
                    source_index = body_index + (1 if uses_first_row_as_header else 0)
                    for column, value in enumerate(list(row) + [""] * (width - len(row))):
                        is_placeholder = (source_index, column) in placeholders
                        bold = column in item.get("bold_columns", [])
                        cells.append(cls._markdown_cell(value, is_placeholder, bold))
                    lines.append("| " + " | ".join(cells) + " |")
                lines.append("")
        if images:
            lines.extend(["## 添付画像", ""])
            for name, relative in images:
                lines.extend([f"![{name}]({relative})", ""])
        return "\n".join(lines).rstrip() + "\n"

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    self._append_log(event[1])
                elif event[0] == "review":
                    _, values, response, ready = event
                    self.status_var.set("検索が完了しました。取得結果を確認してください")
                    self._append_log("有効にした検索が完了しました。取得結果を確認します。")
                    self._review_match_info(values, response, ready)
                elif event[0] == "cancelled":
                    self.status_var.set("作成をキャンセルしました")
                    self._append_log("作成をキャンセルしました。入力内容はフォームに残っています。")
                    self.generate_button.configure(state="normal")
                elif event[0] == "complete":
                    _, path, warnings, image_count = event
                    self.status_var.set(f"Markdown を保存しました: {path}")
                    self._append_log(f"保存先: {path}")
                    self._append_log(f"画像添付: {image_count}件")
                    if warnings:
                        self._append_log("取得メモ: " + " / ".join(warnings))
                    else:
                        self._append_log("取得時の警告はありませんでした。")
                    messagebox.showinfo("作成完了", f"Markdown を保存しました。\n\n{path}")
                    self.generate_button.configure(state="normal")
                elif event[0] == "error":
                    self.status_var.set("作成に失敗しました")
                    self._append_log("エラー: " + event[1])
                    messagebox.showerror("作成に失敗しました", event[1])
                    self.generate_button.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(150, self._poll_events)

    def _append_log(self, message: str):
        if not re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", message):
            message = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")


def main():
    root = tk.Tk()
    DankobaLocalApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
