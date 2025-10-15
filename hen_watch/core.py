from __future__ import annotations
import hashlib
import os
import re
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .storage import read_state, write_state

EH_BASE = "https://e-hentai.org/?f_search="

@dataclass
class Config:
    search_url: str
    authors: List[str]
    result_selector: str
    title_selector: str
    link_selector: str
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str

def _pick_env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v is not None else default

def load_config(cfg_path: str = "config.toml") -> Config:
    try:
        import tomllib as toml
    except Exception:
        import tomli as toml  # type: ignore

    data: Dict = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, "rb") as f:
            data = toml.load(f)

    tg = data.get("telegram", {}) or {}
    cfg = Config(
        search_url=str(data.get("search_url", "") or ""),
        authors=list(data.get("authors", []) or []),
        result_selector=str(data.get("result_selector", "") or ""),
        title_selector=str(data.get("title_selector", "") or ""),
        link_selector=str(data.get("link_selector", "") or ""),
        telegram_enabled=bool(tg.get("enabled", False)),
        telegram_bot_token=str(tg.get("bot_token", "") or ""),
        telegram_chat_id=str(tg.get("chat_id", "") or ""),
    )

    authors_env = _pick_env("SEARCH_AUTHORS")
    if authors_env:
        parts = re.split(r"[,，、\n\r]+", authors_env)
        cfg.authors = [p.strip() for p in parts if p.strip()]

    surl = _pick_env("SEARCH_URL")
    if surl:
        cfg.search_url = surl

    for key, env_name in [
        ("result_selector", "RESULT_SELECTOR"),
        ("title_selector", "TITLE_SELECTOR"),
        ("link_selector", "LINK_SELECTOR"),
    ]:
        val = _pick_env(env_name)
        if val is not None:
            setattr(cfg, key, val)

    tge = _pick_env("TELEGRAM_ENABLED")
    if tge is not None:
        cfg.telegram_enabled = tge.lower() in ("1", "true", "yes", "on")

    for key, env_name in [
        ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
        ("telegram_chat_id", "TELEGRAM_CHAT_ID"),
    ]:
        val = _pick_env(env_name)
        if val is not None:
            setattr(cfg, key, val)

    if not cfg.authors and not cfg.search_url:
        raise ValueError("必须提供 SEARCH_AUTHORS（多作者）或 SEARCH_URL（单作者）。")

    return cfg

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

def _http_get(url: str, timeout: int = 30) -> str:
    r = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text

def _text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def _checksum(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.extract()
    return hashlib.sha256(_text(soup.get_text(" ")).encode("utf-8")).hexdigest()

def _cover_from_anchor(a) -> str:
    img = a.find("img")
    if not img:
        return ""
    src = (img.get("src") or "").strip()
    if not src:
        return ""
    if src.startswith("//"):
        src = "https:" + src
    if src.startswith("/"):
        src = "https://e-hentai.org" + src
    if not (src.startswith("http://") or src.startswith("https://")):
        return ""
    return src

def _extract_items(html: str, cfg: Config) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    if not cfg.result_selector:
        return [{"id": "PAGE", "title": "Full page", "url": "", "cover": ""}]

    items: List[Dict[str, str]] = []
    for node in soup.select(cfg.result_selector):
        anchor = None
        if getattr(node, "name", None) == "a" and node.has_attr("href"):
            anchor = node
        else:
            anchor = node.select_one("a[href*='/g/']")
        if not anchor or not anchor.has_attr("href") or "/g/" not in anchor["href"]:
            continue
        title = ""
        if cfg.title_selector.strip():
            tnode = node.select_one(cfg.title_selector)
            if tnode:
                title = _text(tnode.get_text(" "))
        if not title:
            title = _text(anchor.get_text(" "))
        url = ""
        if cfg.link_selector.strip():
            lnode = node.select_one(cfg.link_selector)
            if lnode is not None and lnode.has_attr("href"):
                url = lnode["href"]
        if not url:
            url = anchor["href"]
        if url.startswith("/"):
            url = "https://e-hentai.org" + url
        ident_src = url or title
        ident = hashlib.sha1(ident_src.encode("utf-8")).hexdigest()[:16]
        items.append({"id": ident, "title": title or "(no title)", "url": url, "cover": _cover_from_anchor(anchor)})
    uniq, seen = [], set()
    for it in items:
        if it["id"] not in seen:
            seen.add(it["id"])
            uniq.append(it)
    return uniq

def _author_url(name: str) -> str:
    return f"{EH_BASE}{quote(name.strip())}"

def _fetch_for_author(author: str, cfg: Config):
    url = _author_url(author)
    html = _http_get(url)
    return url, _extract_items(html, cfg), _checksum(html)

def _diff(prev_items: Dict[str, Dict[str, str]], new_items: List[Dict[str, str]]):
    old_ids = set(prev_items.keys())
    new_ids = {it["id"] for it in new_items}
    added_ids = new_ids - old_ids
    removed_ids = old_ids - new_ids
    added = [it for it in new_items if it["id"] in added_ids]
    removed = [prev_items[i] for i in removed_ids]
    return added, removed

def _send_text(token: str, chat: str, text: str) -> None:
    if not token or not chat:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=30)

def _send_media_group(token: str, chat: str, medias: List[Dict[str, str]]) -> None:
    if not token or not chat or not medias:
        return
    url = f"https://api.telegram.org/bot{token}/sendMediaGroup"
    requests.post(url, json={"chat_id": chat, "media": medias}, timeout=45)

def run_once(cfg_path: str = "config.toml") -> int:
    cfg = load_config(cfg_path)
    state = read_state()
    authors = [a for a in cfg.authors if a.strip()]
    single_mode = not authors

    if "authors" not in state:
        state["authors"] = {}

    # 是否是“仓库第一次跑”？——只有当 state 本身没任何作者快照时才算
    is_initial_repo_run = (len(state["authors"]) == 0) and single_mode is False

    added_by_author: Dict[str, List[Dict[str, str]]] = {}
    new_author_baselined: List[str] = []
    processed_existing_authors = 0

    if authors:
        for name in authors:
            url, items, checksum = _fetch_for_author(name, cfg)
            prev = state["authors"].get(name, {})
            prev_items_dict = {it["id"]: it for it in prev.get("items", [])}

            if not prev:
                # 仅对“新作者”静默建基线，不影响其他已有作者的通知
                new_author_baselined.append(name)
                state["authors"][name] = {"checksum": checksum, "items": items}
                continue

            processed_existing_authors += 1
            added, _ = _diff(prev_items_dict, items)
            if added:
                added_by_author[name] = added

            state["authors"][name] = {"checksum": checksum, "items": items}
    else:
        # 单作者兼容逻辑维持不变：只有首次才静默
        html_url = cfg.search_url
        html = _http_get(html_url)
        checksum = _checksum(html)
        items = _extract_items(html, cfg)
        prev = state.get("single", {})
        prev_items_dict = {it["id"]: it for it in prev.get("items", [])}
        if not prev:
            state["single"] = {"checksum": checksum, "items": items}
            write_state(state)
            print("First run (single URL): baseline saved. No notification.")
            return 0
        else:
            added, _ = _diff(prev_items_dict, items)
            if added:
                added_by_author[html_url] = added
            state["single"] = {"checksum": checksum, "items": items}

    # ——到这里，所有作者的 state 都已更新——
    # 判定是否需要发送通知：
    # 1) 仓库真正第一次跑：不通知（避免历史 spam）
    # 2) 只新增了“新作者”，但没有任何“既有作者更新”：仍然可以选择不通知，或提示一句
    #    我们按你的需求：既有作者都无更新 → 发 “全都没更新”
    if single_mode is False:
        if is_initial_repo_run and processed_existing_authors == 0:
            write_state(state)
            print("First run (repo): baseline saved for all authors. No notification.")
            return 0

    # 有新增则发新增；否则发 “全都没更新”
    if cfg.telegram_enabled:
        if added_by_author:
            summary_lines = ["🕒 本次巡检结果（仅展示新增）："]
            for a, items in added_by_author.items():
                summary_lines.append(f" 新增 {len(items)} 条  {_author_url(a)}")
            summary = "\n".join(summary_lines)

            # 长度保护 & 发送
            msg = summary
            while msg:
                chunk = msg[:4000]
                cut = chunk.rfind("\n")
                if 0 < cut < 4000:
                    to_send, msg = chunk[:cut], msg[cut+1:]
                else:
                    to_send, msg = chunk, msg[4000:]
                _send_text(cfg.telegram_bot_token, cfg.telegram_chat_id, to_send)

            # 每个作者的封面组
            for name, items in added_by_author.items():
                medias = []
                for it in items:
                    if it.get("cover"):
                        medias.append({"type": "photo", "media": it["cover"], "caption": it["title"][:100]})
                    if len(medias) == 10:
                        _send_media_group(cfg.telegram_bot_token, cfg.telegram_chat_id, medias)
                        medias = []
                if medias:
                    _send_media_group(cfg.telegram_bot_token, cfg.telegram_chat_id, medias)
        else:
            _send_text(cfg.telegram_bot_token, cfg.telegram_chat_id, "全都没更新")

    write_state(state)
    return 0

