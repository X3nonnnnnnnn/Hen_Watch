from __future__ import annotations
import hashlib
import os
import re
from dataclasses import dataclass
from typing import List, Dict, Optional
from urllib.parse import quote, urljoin
import html as html_lib

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

    # 环境变量覆盖
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


# ---------- 缩略图提取：完全基于“搜索结果页卡片”，不再打开画廊页 ----------

def _abs_url(base: str, src: str) -> str:
    """把 //、/ 或相对路径补成绝对 URL，并做 HTML 反转义"""
    if not src:
        return ""
    s = html_lib.unescape(src.strip())
    if s.startswith("//"):
        return "https:" + s
    return urljoin(base, s)

def _pick_from_img_tag(img) -> Optional[str]:
    """按优先级从 <img> 提取真实地址：data-* → srcset(最大) → src → noscript 内真实 <img>"""
    # 1) 常见懒加载属性
    for key in ("data-src", "data-lazy", "data-original"):
        v = img.get(key)
        if v:
            return v
    # 2) srcset（取最后一项通常分辨率最高）
    ss = img.get("srcset")
    if ss:
        parts = [p.strip().split(" ")[0] for p in ss.split(",") if p.strip()]
        if parts:
            return parts[-1] or parts[0]
    # 3) 常规 src（排除 data: 占位）
    s = img.get("src")
    if s and not s.startswith("data:"):
        return s
    # 4) 就近的 <noscript> 里再找一次真实 <img>
    ns = None
    cur = img
    for _ in range(3):
        if not cur:
            break
        ns = cur.find_next_sibling("noscript") or cur.find("noscript")
        if ns:
            break
        cur = cur.parent
    if ns and ns.string:
        ns_soup = BeautifulSoup(ns.string, "html.parser")
        real_img = ns_soup.find("img")
        if real_img:
            return real_img.get("data-src") or real_img.get("src")
    return None

def _pick_from_style(el) -> Optional[str]:
    """从 style='background-image:url(...)' 中提取 URL"""
    style = el.get("style") or ""
    m = re.search(r"url\((['\"]?)(.*?)\1\)", style, flags=re.I)
    return m.group(2) if m else None

def _cover_from_result_context(node, anchor, page_base: str) -> str:
    """
    从结果“上下文”尽可能取到缩略图：
    - 在 node / anchor / node.parent / node.parent.parent 这 4 层内找：
      img（data-*/srcset/src/noscript） 或 div 的 background-image 或 div 内 noscript
    """
    candidates = []
    for x in (node, anchor, getattr(node, "parent", None), getattr(getattr(node, "parent", None), "parent", None)):
        if x and x not in candidates:
            candidates.append(x)

    # 先找 <img>
    for ctx in candidates:
        for img in ctx.select("img"):
            u = _pick_from_img_tag(img)
            if u and not u.startswith("data:"):
                return _abs_url(page_base, u)

    # 再找 div 背景图 / noscript
    for ctx in candidates:
        for el in ctx.select("div"):
            u = _pick_from_style(el)
            if u:
                return _abs_url(page_base, u)
            ns = el.find("noscript")
            if ns and ns.string:
                ns_soup = BeautifulSoup(ns.string, "html.parser")
                real_img = ns_soup.find("img")
                if real_img:
                    u2 = real_img.get("data-src") or real_img.get("src")
                    if u2:
                        return _abs_url(page_base, u2)

    return ""


def _extract_items(html: str, cfg: Config) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    if not cfg.result_selector:
        return [{"id": "PAGE", "title": "Full page", "url": "", "cover": ""}]

    page_base = "https://e-hentai.org"
    items: List[Dict[str, str]] = []
    for node in soup.select(cfg.result_selector):
        # 找到卡片上的画廊链接
        if getattr(node, "name", None) == "a" and node.has_attr("href"):
            anchor = node
        else:
            anchor = node.select_one("a[href*='/g/']")
        if not anchor or not anchor.has_attr("href") or "/g/" not in anchor["href"]:
            continue

        # 标题
        title = ""
        if cfg.title_selector.strip():
            tnode = node.select_one(cfg.title_selector)
            if tnode:
                title = _text(tnode.get_text(" "))
        if not title and hasattr(node, "select"):
            for tnode in node.select("[class*='title'], [id*='title']"):
                title = _text(tnode.get_text(" "))
                if title:
                    break
        if not title:
            title = _text(anchor.get_text(" "))

        # 画廊 URL（绝对化）
        url = ""
        if cfg.link_selector.strip():
            lnode = node.select_one(cfg.link_selector)
            if lnode is not None and lnode.has_attr("href"):
                url = lnode["href"]
        if not url:
            url = anchor["href"]
        url = _abs_url(page_base, url)

        # 封面（只在搜索页上下文内解析，不再打开画廊页）
        cover = _cover_from_result_context(node, anchor, page_base)

        # 生成条目
        ident_src = url or title
        ident = hashlib.sha1(ident_src.encode("utf-8")).hexdigest()[:16]
        items.append({
            "id": ident,
            "title": title or "(no title)",
            "url": url,
            "cover": cover,
        })

    # 去重
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
    if not token or not chat or not text:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    if r.status_code != 200:
        try:
            print("TELEGRAM_SEND_ERROR:", r.status_code, r.text[:300])
        except Exception:
            pass


def _send_photo(token: str, chat: str, photo: str, caption: str = "") -> bool:
    if not token or not chat or not photo:
        return False
    payload = {"chat_id": chat, "photo": photo}
    if caption:
        payload["caption"] = caption[:1024]
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        r = requests.post(url, data=payload, timeout=30)
    except Exception as exc:  # pragma: no cover - 网络异常只记录
        print("TELEGRAM_SEND_PHOTO_EXCEPTION:", exc)
        return False
    if r.status_code != 200:
        try:
            print("TELEGRAM_SEND_PHOTO_ERROR:", r.status_code, r.text[:300])
        except Exception:
            pass
        return False
    return True


def run_once(cfg_path: str = "config.toml") -> int:
    cfg = load_config(cfg_path)
    state = read_state()
    authors = [a for a in cfg.authors if a.strip()]
    single_mode = not authors

    if "authors" not in state:
        state["authors"] = {}

    # 是否仓库首次
    is_initial_repo_run = (len(state["authors"]) == 0) and single_mode is False
    added_by_author: Dict[str, List[Dict[str, str]]] = {}
    new_author_baselined: List[str] = []
    processed_existing_authors = 0

    if authors:
        # 清除配置中已删除的作者数据
        for missing_author in [name for name in list(state["authors"].keys()) if name not in authors]:
            del state["authors"][missing_author]

        for name in authors:
            url, items, checksum = _fetch_for_author(name, cfg)
            prev = state["authors"].get(name, {})
            prev_items_dict = {it["id"]: it for it in prev.get("items", [])}

            if not prev:
                new_author_baselined.append(name)
                state["authors"][name] = {"checksum": checksum, "items": items}
                continue

            processed_existing_authors += 1
            added, _ = _diff(prev_items_dict, items)
            if added:
                added_by_author[name] = added

            state["authors"][name] = {"checksum": checksum, "items": items}
    else:
        # 单 URL 模式
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

    # 首次仓库建基线：不通知
    if single_mode is False:
        if is_initial_repo_run and processed_existing_authors == 0:
            write_state(state)
            print("First run (repo): baseline saved for all authors. No notification.")
            return 0

    # 有新增则发新增；否则发 “全都没更新”
    if cfg.telegram_enabled:
        if added_by_author:
            lines = ["🕒 本次巡检结果（仅展示新增）："]
            for a, items in added_by_author.items():
                # 直接裸链接，避免 Telegram 二次确认弹窗
                lines.append(f"{a}: 新增 {len(items)} 条 {_author_url(a)}")
            summary = "\n".join(lines)

            if len(summary) <= 4000:
                _send_text(cfg.telegram_bot_token, cfg.telegram_chat_id, summary)
            else:
                msg = summary
                while msg:
                    chunk = msg[:4000]
                    cut = chunk.rfind("\n")
                    if 0 < cut < 4000:
                        to_send, msg = chunk[:cut], msg[cut+1:]
                    else:
                        to_send, msg = chunk, msg[4000:]
                    _send_text(cfg.telegram_bot_token, cfg.telegram_chat_id, to_send)

            for a, items in added_by_author.items():
                header = f"📚 {a} 新增 {len(items)} 条："
                _send_text(cfg.telegram_bot_token, cfg.telegram_chat_id, header)
                for it in items:
                    caption = f"{it['title']}\n{it['url']}"
                    if not _send_photo(
                        cfg.telegram_bot_token,
                        cfg.telegram_chat_id,
                        it.get("cover", ""),
                        caption,
                    ):
                        _send_text(cfg.telegram_bot_token, cfg.telegram_chat_id, caption)
        else:
            _send_text(cfg.telegram_bot_token, cfg.telegram_chat_id, "全都没更新")

    write_state(state)
    return 0
