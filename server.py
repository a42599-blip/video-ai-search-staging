# -*- coding: utf-8 -*-
import asyncio, re, json, threading, webbrowser, time, httpx, subprocess, os
try:
    from playwright_stealth import Stealth as _StealthCls
    _stealth_inst = _StealthCls()
    async def _stealth(page):
        await _stealth_inst.apply_stealth_async(page)
except Exception:
    async def _stealth(page): pass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import uvicorn

BASE_DIR          = Path(__file__).parent
DOWNLOAD_DIR      = BASE_DIR / "下載影片"
COOKIES_FILE      = BASE_DIR / "platform_cookies.json"
DOWNLOAD_REGISTRY = DOWNLOAD_DIR / ".download_registry.json"
_reg_lock         = threading.Lock()

def _registry_add(filename: str, device_id: str):
    """記錄 filename 屬於哪個裝置"""
    if not filename or not device_id:
        return
    with _reg_lock:
        try:
            data = json.loads(DOWNLOAD_REGISTRY.read_text(encoding="utf-8")) if DOWNLOAD_REGISTRY.exists() else {}
            data[filename] = device_id
            DOWNLOAD_REGISTRY.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

def _registry_get_files(device_id: str) -> set:
    """回傳此裝置下載過的檔名集合"""
    if not DOWNLOAD_REGISTRY.exists():
        return set()
    try:
        data = json.loads(DOWNLOAD_REGISTRY.read_text(encoding="utf-8"))
        return {k for k, v in data.items() if v == device_id}
    except Exception:
        return set()

def _load_platform_cookies() -> dict:
    """讀取使用者儲存的平台 cookies，格式 {platform: [{name,value,domain,...}]}"""
    try:
        if COOKIES_FILE.exists():
            return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _get_cookies_for_url(url: str) -> list[dict]:
    """根據 URL 取得對應的 cookies 列表"""
    data = _load_platform_cookies()
    for plat, domain in [
        ("douyin", "douyin.com"), ("kuaishou", "kuaishou.com"), ("tiktok", "tiktok.com"),
    ]:
        if domain in url:
            return data.get(plat, [])
    return []

async def _apply_cookies(ctx, url: str):
    """把儲存的 cookies 注入 Playwright context"""
    cookies = _get_cookies_for_url(url)
    if cookies:
        try:
            await ctx.add_cookies(cookies)
        except Exception:
            pass

LUX_PATH = Path(r"D:\tools\lux\lux.exe")
FFMPEG_DIR = r"C:\Users\USER\AppData\Local\Microsoft\WinGet\Links"
# 確保 ffmpeg 在 PATH（Lux 合併 DASH 流需要）
if FFMPEG_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = FFMPEG_DIR + ";" + os.environ.get("PATH", "")

# Lux 支援的平台（下載比 yt-dlp 更穩定）
LUX_DOMAINS = ("bilibili.com", "b23.tv", "iqiyi.com", "youku.com",
               "v.qq.com", "weibo.com", "miaopai.com", "pearvideo.com")

def _is_lux_platform(url: str) -> bool:
    return LUX_PATH.exists() and any(d in url for d in LUX_DOMAINS)
DOWNLOAD_DIR.mkdir(exist_ok=True)

def extract_url_from_text(text: str) -> str:
    """從分享文字中提取第一個 http 連結（處理抖音等分享文字夾帶 URL 的情況）"""
    m = re.search(r'https?://[^\s一-鿿＀-￯　-〿⺀-⻿]+', text)
    if m:
        return m.group(0).rstrip(',.，。！？、')
    return text.strip()

async def resolve_short_url(url: str) -> str:
    """從分享文字提取 URL，並對已知短網址域名追蹤 HTTP 重定向"""
    text_url = extract_url_from_text(url)
    SHORT_DOMAINS = ("v.douyin.com", "v.kuaishou.com", "kuaishou.app.link",
                     "xhslink.com", "t.co", "vm.tiktok.com", "vt.tiktok.com")
    if any(d in text_url for d in SHORT_DOMAINS):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=12,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}) as c:
                r = await c.head(text_url)
                return str(r.url)
        except Exception:
            pass
    return text_url

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
EDGE_PATH  = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/下載影片", StaticFiles(directory=str(DOWNLOAD_DIR)), name="downloads")

SEARCH_MAP = {
    "YouTube": "ytsearch{n}:{q}",
}
executor = ThreadPoolExecutor(max_workers=6)

YOUTUBE_API_KEY = "AIzaSyDUCQ0mQqwcqRLSDa0C2E79qioJYhtZB4A"
YOUTUBE_DEFAULT_COUNT = 50   # YouTube 每次最多拿 50 筆
BILIBILI_DEFAULT_COUNT = 30  # B站每次拿 30 筆
MIN_DURATION_SEC = 60        # 過濾掉 Shorts（< 60 秒）

# Piped 公共節點列表，依序嘗試
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://piped-api.garudalinux.org",
    "https://api.piped.projectsegfau.lt",
]

# ── AI 查詢優化（qwen3:8b，雙版本輸出）──────────────────────────
async def _optimize_query(keyword: str) -> dict:
    """
    回傳 {"youtube": "...", "bilibili": "...", "global": "...", "intent": "..."}
    youtube  : CTR 優化版，給 YouTube（台灣用語）
    bilibili : B站優化版（簡體慣用詞，合集/完整版/UP主）
    global   : 精簡英文，給 Dailymotion/Odysee/PeerTube
    intent   : 意圖分類（教學/娛樂/新聞/商品/人物/其他）
    """
    import datetime as _dt
    year = _dt.datetime.now().year
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(OLLAMA_URL, json={
                "model": "qwen3:8b",
                "prompt": (
                    f"/no_think\n"
                    f"影片搜尋關鍵字優化。用戶輸入：「{keyword}」\n\n"
                    f"規則：\n"
                    f"1. youtube：繁體中文，保留原核心詞，只加1個最相關輔助詞（教學→加「教學」；商品→加「{year}」；其他→不加）\n"
                    f"2. bilibili：可轉簡體，保留原核心詞，加「教程」或「完整版」\n"
                    f"3. global：翻成英文核心詞，最多3個詞\n"
                    f"4. intent：教學/娛樂/新聞/商品/人物/其他\n\n"
                    f"直接輸出JSON，不解釋：\n"
                    f"{{\"youtube\":\"...\",\"bilibili\":\"...\",\"global\":\"...\",\"intent\":\"...\"}}"
                ),
                "stream": False,
                "options": {"num_predict": 120, "temperature": 0.1},
            })
        if resp.status_code == 200:
            raw = resp.json().get("response", "").strip()
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            m = re.search(r'\{[^{}]*"youtube"[^{}]*"bilibili"[^{}]*"global"[^{}]*\}', raw, re.DOTALL)
            if m:
                data = json.loads(m.group())
                yt = data.get("youtube", "").strip()
                bi = data.get("bilibili", "").strip()
                gl = data.get("global", "").strip()
                intent = data.get("intent", "其他").strip()
                if yt and gl:
                    return {"youtube": yt, "bilibili": bi or yt, "global": gl, "intent": intent}
            # fallback: 嘗試舊格式 {"youtube":..., "global":...}
            m2 = re.search(r'\{[^{}]*"youtube"[^{}]*"global"[^{}]*\}', raw, re.DOTALL)
            if m2:
                data = json.loads(m2.group())
                yt = data.get("youtube","").strip()
                gl = data.get("global","").strip()
                if yt and gl:
                    return {"youtube": yt, "bilibili": yt, "global": gl, "intent": "其他"}
            # raw 格式不對（可能是截斷的 thinking），直接用原始 keyword
    except Exception:
        pass
    return {"youtube": keyword, "bilibili": keyword, "global": keyword, "intent": "其他"}

# ── Piped API 搜尋（主力，免費無配額限制）──────────────────────
async def _search_piped(keyword: str, count: int = YOUTUBE_DEFAULT_COUNT) -> list:
    for base in PIPED_INSTANCES:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    f"{base}/search",
                    params={"q": keyword, "filter": "videos"},
                )
            if resp.status_code != 200:
                continue
            items = resp.json().get("items", [])
            results = []
            for item in items:
                dur = item.get("duration", 0) or 0
                if dur > 0 and dur < MIN_DURATION_SEC:
                    continue  # 過濾 Shorts
                rel_url = item.get("url", "")
                vid = rel_url.split("v=")[-1] if "v=" in rel_url else ""
                url = f"https://www.youtube.com{rel_url}" if rel_url.startswith("/") else rel_url
                thumb = item.get("thumbnail", "")
                if not thumb and vid:
                    thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                results.append({
                    "title":      item.get("title", "未知標題"),
                    "url":        url,
                    "thumbnail":  thumb,
                    "duration":   dur,
                    "uploader":   item.get("uploaderName", ""),
                    "platform":   "YouTube",
                    "youtube_id": vid,
                })
                if len(results) >= count:
                    break
            if results:
                return results
        except Exception:
            continue
    return []

# ── YouTube Data API v3 搜尋（備援，每日10,000 units）──────────
async def _search_youtube_api(keyword: str, count: int = YOUTUBE_DEFAULT_COUNT) -> list:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "key": YOUTUBE_API_KEY,
                    "q": keyword,
                    "part": "snippet",
                    "type": "video",
                    "maxResults": min(count, 50),
                    "order": "relevance",
                    "relevanceLanguage": "zh-TW",
                    "videoDuration": "medium",  # 4–20 分鐘，排除 Shorts 和超長片
                },
            )
        if resp.status_code != 200:
            return []
        items = resp.json().get("items", [])
        results = []
        for item in items:
            vid = item.get("id", {}).get("videoId", "")
            snip = item.get("snippet", {})
            thumb = (snip.get("thumbnails", {}).get("high") or
                     snip.get("thumbnails", {}).get("medium") or
                     snip.get("thumbnails", {}).get("default") or {}).get("url", "")
            if not thumb and vid:
                thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
            results.append({
                "title":      snip.get("title", "未知標題"),
                "url":        f"https://www.youtube.com/watch?v={vid}",
                "thumbnail":  thumb,
                "duration":   0,
                "uploader":   snip.get("channelTitle", ""),
                "platform":   "YouTube",
                "youtube_id": vid,
            })
        return results
    except Exception:
        return []

# ── YouTube 統一搜尋：Piped → Data API v3 → yt-dlp ──────────
async def _search_youtube(keyword: str, count: int = YOUTUBE_DEFAULT_COUNT, loop=None) -> list:
    # Priority: 1. API Key 2. yt-dlp fallback
    results = await _search_youtube_api(keyword, count)
    if results:
        return results
    if loop is None:
        loop = asyncio.get_event_loop()
    prefix = SEARCH_MAP["YouTube"].format(n=count, q=keyword)
    return await loop.run_in_executor(executor, _ytdlp_search, prefix, "YouTube", count)

# ── Dailymotion 搜尋（免費公開 API，無需帳號）──────────────────
async def _search_dailymotion(keyword: str, count: int = 20) -> list:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.dailymotion.com/videos",
                params={
                    "search": keyword,
                    "fields": "id,title,thumbnail_url,duration,owner.screenname",
                    "limit": min(count, 50),
                    "sort": "relevance",
                },
            )
        if resp.status_code != 200:
            return []
        results = []
        for item in resp.json().get("list", []):
            vid = item.get("id", "")
            results.append({
                "title":      item.get("title", "未知標題"),
                "url":        f"https://www.dailymotion.com/video/{vid}",
                "thumbnail":  item.get("thumbnail_url", ""),
                "duration":   item.get("duration", 0),
                "uploader":   item.get("owner.screenname", ""),
                "platform":   "Dailymotion",
                "youtube_id": "",
            })
        return results
    except Exception:
        return []

# ── Odysee 搜尋（免費公開 API，無需帳號）───────────────────────
async def _search_odysee(keyword: str, count: int = 20) -> list:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.na-backend.odysee.com/api/v1/proxy",
                json={
                    "method": "claim_search",
                    "params": {
                        "text": keyword,
                        "claim_type": ["stream"],
                        "page_size": min(count, 30),
                        "has_source": True,
                        "order_by": ["trending_group", "trending_mixed"],
                    },
                },
            )
        if resp.status_code != 200:
            return []
        items = resp.json().get("result", {}).get("items", [])
        results = []
        for item in items:
            val = item.get("value", {})
            name = item.get("name", "")
            channel = (item.get("signing_channel") or {}).get("value", {}).get("title", "")
            thumb = (val.get("thumbnail") or {}).get("url", "")
            duration = (val.get("video") or {}).get("duration", 0)
            # lbry 格式用 # 分隔，轉為 odysee web 格式需換成 :（否則 # 被瀏覽器當 fragment 截斷）
            canonical = item.get("canonical_url", "").replace("lbry://", "").replace("#", ":")
            url = f"https://odysee.com/{canonical}" if canonical else f"https://odysee.com/{name}"
            results.append({
                "title":      val.get("title", name),
                "url":        url,
                "thumbnail":  thumb,
                "duration":   duration,
                "uploader":   channel,
                "platform":   "Odysee",
                "youtube_id": "",
            })
        return results
    except Exception:
        return []

# ── PeerTube 搜尋（去中心化，免費無限制）──────────────────────
PEERTUBE_INSTANCES = [
    "https://sepiasearch.org",   # 官方跨站搜尋引擎（最穩定）
    "https://framatube.org",
    "https://tilvids.com",
    "https://peertube.tv",
]

async def _search_peertube(keyword: str, count: int = 20) -> list:
    for base in PEERTUBE_INSTANCES:
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                resp = await client.get(
                    f"{base}/api/v1/search/videos",
                    params={"search": keyword, "count": min(count, 30), "start": 0,
                            "sort": "-match", "nsfw": "false"},
                    headers={"Accept": "application/json"},
                )
            if resp.status_code != 200:
                continue
            items = resp.json().get("data", [])
            results = []
            for item in items:
                uid = item.get("uuid", "")
                if not uid:
                    continue
                thumb = item.get("thumbnailPath", "")
                # sepiasearch 回傳的 thumbnailPath 可能已有完整 URL
                if thumb and thumb.startswith("http"):
                    pass
                elif thumb:
                    # 取影片所在實例的 host
                    acct_host = (item.get("account") or {}).get("host", "")
                    thumb_base = f"https://{acct_host}" if acct_host else base
                    thumb = thumb_base + thumb
                else:
                    thumb = ""
                video_host = (item.get("account") or {}).get("host", "")
                video_base = f"https://{video_host}" if video_host else base
                results.append({
                    "title":      item.get("name", "未知標題"),
                    "url":        f"{video_base}/videos/watch/{uid}",
                    "thumbnail":  thumb,
                    "duration":   item.get("duration", 0),
                    "uploader":   (item.get("account") or {}).get("displayName", ""),
                    "platform":   "PeerTube",
                    "youtube_id": "",
                })
            if results:
                return results
        except Exception:
            continue
    return []

# ── B站搜尋：直接打 Bilibili 搜尋 API（比 yt-dlp bilisearch 更穩定）──
async def _search_bilibili(keyword: str, count: int = BILIBILI_DEFAULT_COUNT) -> JSONResponse:
    import uuid
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Cookie": f"buvid3={uuid.uuid4()}; innersign=0; CURRENT_FNVAL=4048",
        "Origin": "https://www.bilibili.com",
    }
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            resp = await client.get(
                "https://api.bilibili.com/x/web-interface/search/type",
                params={"search_type": "video", "keyword": keyword,
                        "page": 1, "page_size": count}
            )
            data = resp.json()
            if data.get("code") != 0:
                return JSONResponse({"results": [], "error": data.get("message", "B站搜尋失敗")})

            results = []
            for item in (data.get("data") or {}).get("result", [])[:count]:
                bvid = item.get("bvid", "")
                aid  = item.get("aid", "")
                title = re.sub(r'<[^>]+>', '', item.get("title", "B站影片"))
                pic   = item.get("pic", "")
                if pic and pic.startswith("//"):
                    pic = "https:" + pic
                elif pic and pic.startswith("http://"):
                    pic = "https" + pic[4:]
                # duration 是 "mm:ss" 字串
                dur_str = str(item.get("duration", "0"))
                parts = dur_str.split(":")
                dur_sec = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else 0
                url = f"https://www.bilibili.com/video/{bvid}" if bvid \
                      else f"https://www.bilibili.com/video/av{aid}"
                results.append({
                    "title": title, "url": url, "thumbnail": pic,
                    "duration": dur_sec, "uploader": item.get("author", ""),
                    "platform": "B站", "youtube_id": "",
                })
            return JSONResponse({"results": results})
    except Exception as e:
        return JSONResponse({"results": [], "error": str(e)})

DOUYIN_LIB = r"D:\tools\Douyin_TikTok_Download_API"

def _is_douyin(url: str) -> bool:
    return "douyin.com" in url or "douyinvod" in url

def _is_kuaishou(url: str) -> bool:
    return "kuaishou.com" in url

def _is_shopee_url(url: str) -> bool:
    return any(d in url for d in ("shopee.tw", "shopee.sg", "shopee.vn", "shopee.ph",
                                   "shopee.my", "shopee.co.id", "shp.ee", "sv.shopee"))

async def _get_shopee_video_info(url: str) -> dict:
    """解析蝦皮短影音連結，回傳 {title, thumbnail, video_url, platform}"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Referer": "https://shopee.tw/",
    }
    # Step 1: 追蹤重定向
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers=headers) as c:
        r0 = await c.get(url)
        final_url = str(r0.url)

    # Step 2: 從 universal-link 提取 redir 參數
    video_page_url = final_url
    if "universal-link" in final_url:
        from urllib.parse import parse_qs, urlparse as _up
        qs = parse_qs(_up(final_url).query)
        redir = qs.get("redir", [""])[0]
        if redir:
            video_page_url = redir

    # Step 3: 只處理 sv.shopee（短影音頁面），其他格式留給 yt-dlp
    if "sv.shopee" not in video_page_url and "share-video" not in video_page_url:
        return {}

    # Step 4: 抓取影片頁面，從 HTML 提取 .mp4 網址
    async with httpx.AsyncClient(timeout=20, headers=headers) as c:
        r = await c.get(video_page_url)
        html = r.text

    mp4_urls = re.findall(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*", html)
    if not mp4_urls:
        return {}

    video_url = mp4_urls[0]
    title = "蝦皮短影音"
    thumbnail = ""

    # 從 meta tags 取標題和縮圖
    tm = re.search(r'<meta[^>]+(?:og:title)[^>]+content="([^"]+)"', html)
    if tm:
        title = tm.group(1)
    thm = re.search(r'<meta[^>]+og:image[^>]+content="([^"]+)"', html)
    if thm:
        thumbnail = thm.group(1)

    # 嘗試從 __NEXT_DATA__ 取更精確的資料
    nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if nd:
        try:
            nd_data = json.loads(nd.group(1))
            props = nd_data.get("props", {}).get("pageProps", {})
            title = props.get("title") or props.get("videoTitle") or title
            thumbnail = props.get("thumbnail") or props.get("coverUrl") or thumbnail
        except Exception:
            pass

    return {"title": title, "thumbnail": thumbnail, "video_url": video_url,
            "platform": "Shopee", "duration": 0, "uploader": ""}

def _parse_aweme_id(url: str) -> str:
    """從 URL 直接解析 aweme_id（不需要 HTTP 請求）"""
    for pat in (r'/video/(\d+)', r'modal_id=(\d+)', r'[?&]vid=(\d+)', r'/note/(\d+)'):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return ""

async def _resolve_aweme_id(url: str) -> str:
    """短網址先做 HTTP 重定向，再解析 aweme_id"""
    aid = _parse_aweme_id(url)
    if aid:
        return aid
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
            aid = _parse_aweme_id(str(r.url))
            if aid:
                return aid
    except Exception:
        pass
    return ""

def _cookies_to_str(cookie_list: list) -> str:
    """把 [{name,value,...}] 轉成 Cookie 標頭字串"""
    return "; ".join(f"{c['name']}={c['value']}" for c in cookie_list if c.get("name") and c.get("value"))

def _cookies_to_netscape(cookie_list: list, path: str):
    """把 [{name,value,...}] 寫成 Netscape cookies.txt 供 yt-dlp 使用"""
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookie_list:
        domain = c.get("domain", ".douyin.com")
        if not domain.startswith("."):
            domain = "." + domain.lstrip(".")
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expiry = str(int(time.time()) + 86400 * 30)
        lines.append(f"{domain}\t{flag}\t{c.get('path','/')}\t{secure}\t{expiry}\t{c['name']}\t{c['value']}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

async def _get_douyin_info_api(aweme_id: str) -> dict:
    """呼叫抖音 detail API（A-Bogus 簽名 + admin 設定的 cookies）"""
    import sys as _sys
    if DOUYIN_LIB not in _sys.path:
        _sys.path.insert(0, DOUYIN_LIB)

    result = {"title": "抖音影片", "thumbnail": "", "duration": 0,
              "uploader": "", "video_url": None, "aweme_id": aweme_id}
    try:
        from crawlers.douyin.web.utils import BogusManager
        from crawlers.douyin.web.models import PostDetail
        from urllib.parse import urlencode as _ue

        # 優先用 platform_cookies.json 的 cookies，沒有就用 config.yaml 的
        cookie_data = _load_platform_cookies().get("douyin", [])
        if cookie_data:
            cookie_str = _cookies_to_str(cookie_data)
        else:
            import yaml as _yaml, os as _os
            cfg_path = _os.path.join(DOUYIN_LIB, "crawlers/douyin/web/config.yaml")
            with open(cfg_path, encoding="utf-8") as f:
                cfg = _yaml.safe_load(f)
            cookie_str = cfg["TokenManager"]["douyin"]["headers"]["Cookie"]

        UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        params = PostDetail(aweme_id=aweme_id).dict()
        params["msToken"] = ""
        a_bogus = BogusManager.ab_model_2_endpoint(params, UA)
        endpoint = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?{_ue(params)}&a_bogus={a_bogus}"

        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            resp = await client.get(endpoint, headers={
                "User-Agent": UA,
                "Referer": "https://www.douyin.com/",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cookie": cookie_str,
            })
            data = resp.json()

        aweme = data.get("aweme_detail") or {}
        if not aweme:
            fd = data.get("filter_detail", {})
            result["_error"] = fd.get("filter_reason", "no_data")
            return result

        result["title"] = (aweme.get("desc") or "抖音影片")[:80]
        result["duration"] = int(aweme.get("duration", 0) or 0) // 1000
        try: result["uploader"] = aweme["author"]["nickname"] or ""
        except Exception: pass
        try: result["thumbnail"] = aweme["video"]["cover"]["url_list"][0] or ""
        except Exception: pass
        for field in ("play_addr", "download_addr"):
            try:
                all_urls = aweme["video"][field]["url_list"]
                if all_urls:
                    # 挑最快的 CDN 節點（找最近的國際節點）
                    result["video_url"] = await _pick_fastest_url(
                        all_urls,
                        {"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"})
                    if not result["video_url"]:
                        result["video_url"] = all_urls[0]
                    break
            except Exception:
                continue
    except Exception as e:
        print(f"[douyin_api] 錯誤：{e}")
    return result

# ── Playwright 共用啟動函式 ────────────────────────────────
async def _pw_browser(p):
    """統一的 Playwright 瀏覽器啟動，先嘗試 msedge.exe，失敗再用 channel"""
    try:
        return await p.chromium.launch(
            executable_path=EDGE_PATH, headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--autoplay-policy=no-user-gesture-required"])
    except Exception:
        return await p.chromium.launch(channel="msedge", headless=True)

# ── 抖音：Playwright 攔截 CDN 影片 URL ────────────────────
async def _pick_fastest_url(urls: list[str], headers: dict | None = None, timeout: float = 4.0) -> str:
    """並發 HEAD 各 URL，選延遲最低的那個（找最近的 CDN 節點）"""
    if not urls:
        return ""
    if len(urls) == 1:
        return urls[0]
    hdrs = headers or {}
    import time as _time

    async def probe(url: str):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cl:
                t0 = _time.monotonic()
                r = await cl.head(url, headers=hdrs)
                if r.status_code < 400:
                    return (_time.monotonic() - t0, url)
        except Exception:
            pass
        return (999.0, url)

    results = await asyncio.gather(*[probe(u) for u in urls[:6]])
    best = min(results, key=lambda x: x[0])
    print(f"[cdn_pick] best={best[1][:80]}  latency={best[0]:.2f}s")
    return best[1]

async def _get_douyin_cdn(video_url: str) -> dict:
    """用 Playwright 開啟抖音影片頁面。
    優先攔截 aweme/detail API（取完整帶音 MP4 URL + 正確 metadata）；
    若 API 被封鎖則 fallback 到 CDN 攔截。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {}

    result = {"title": "抖音影片", "thumbnail": "", "duration": 0,
              "uploader": "", "cdn_url": None, "cdn_audio_url": None, "formats": []}

    CDN_DOMAINS = ("zjcdn.com", "douyinvod.com", "v26-efforg", "pull-f5",
                   "toutiaoimg.com/obj/tos", "v19-efforg", "v3-efforg",
                   "bytedance.com/obj", "p3-sign", "aweme.snssdk", "douyinvod.com")
    COVER_PATTERNS = ("tos-cn-p", "tos-cn-i", "tos-cn-avt", "douyinpic.com",
                      "p3-sign.douyinpic", "p6-sign", "p9-sign")

    try:
        async with async_playwright() as p:
            browser = await _pw_browser(p)
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800})
            await _apply_cookies(ctx, video_url)  # 注入 admin 設定的 cookies
            page = await ctx.new_page()
            await _stealth(page)

            found = asyncio.Event()
            api_found = asyncio.Event()  # API 處理真正完成（cdn_url 已更新）
            cdn_url: list[str] = []
            cdn_audio_url: list[str] = []
            cover_url: list[str] = []
            api_done: list[bool] = []  # 標記 API 攔截完成，防止 CDN fallback 覆蓋

            async def on_response(resp):
                rurl = resp.url
                ct = resp.headers.get("content-type", "")

                # ── 優先：攔截 aweme/detail API ──
                if "aweme/v1/web/aweme/detail" in rurl and not api_done:
                    api_done.append(True)  # 立即標記，後續 CDN fallback 不再搶 cdn_url
                    try:
                        body = await resp.json()
                        aweme = body.get("aweme_detail") or {}
                        if aweme:
                            # ① 立即設定 cdn_url（清除 CDN fallback 可能搶先放的 DASH 段）
                            for field in ("play_addr", "download_addr"):
                                try:
                                    all_urls = aweme["video"][field]["url_list"]
                                    if all_urls:
                                        cdn_url.clear()           # 清掉可能的 DASH segment
                                        cdn_url.append(all_urls[0])
                                        found.set()
                                        break
                                except Exception:
                                    pass

                            # ② 解析多畫質：按標籤去重，每個畫質只保留最高位元率
                            _lbl_order = {"360P":1,"480P":2,"540P":3,"720P HD":4,"1080P":5,"2K":6,"4K":7}
                            _best: dict = {}  # label -> {id, label, url, bitrate}
                            try:
                                for _br in aweme.get("video", {}).get("bit_rate", []):
                                    _br_urls = _br.get("play_addr", {}).get("url_list", [])
                                    if not _br_urls: continue
                                    _qt  = _br.get("quality_type", 0)
                                    _bps = _br.get("bitrate", 0)
                                    _h   = _br.get("play_addr", {}).get("height", 0) or 0
                                    # 優先用高度定標籤，避免 quality_type 全為 0 導致重複
                                    if _h >= 2160:   _lbl = "4K"
                                    elif _h >= 1440: _lbl = "2K"
                                    elif _h >= 1080: _lbl = "1080P"
                                    elif _h >= 720:  _lbl = "720P HD"
                                    elif _h >= 540:  _lbl = "540P"
                                    elif _h >= 480:  _lbl = "480P"
                                    elif _h > 0:     _lbl = f"{_h}P"
                                    else:
                                        _qt_map = {0:"360P",1:"480P",2:"540P",3:"720P HD",4:"1080P",5:"2K",6:"4K"}
                                        _lbl = _qt_map.get(_qt) or (
                                            "1080P" if _bps > 3_000_000 else
                                            "720P HD" if _bps > 1_500_000 else
                                            "540P"  if _bps > 1_000_000 else
                                            "480P"  if _bps > 700_000 else "360P")
                                    if _lbl not in _best or _bps > _best[_lbl]["bitrate"]:
                                        _best[_lbl] = {"id": str(_qt), "label": _lbl,
                                                       "url": _br_urls[0], "bitrate": _bps}
                            except Exception as _ex:
                                print(f"[douyin_cdn] bit_rate parse: {_ex}")
                            if _best:
                                result["formats"] = sorted(_best.values(),
                                    key=lambda x: _lbl_order.get(x["label"], 0))

                            # ③ metadata
                            dur_ms = int(aweme.get("duration", 0) or 0)
                            result["duration"] = dur_ms // 1000 if dur_ms > 1000 else dur_ms
                            if aweme.get("desc"): result["title"] = aweme["desc"][:80]
                            try: result["uploader"] = aweme["author"]["nickname"] or ""
                            except Exception: pass
                            try: result["thumbnail"] = aweme["video"]["cover"]["url_list"][0] or ""
                            except Exception: pass

                    except Exception as ex:
                        print(f"[douyin_cdn] API 攔截失敗（將 fallback）: {ex}")
                    finally:
                        api_found.set()  # 無論成功失敗，標記 API 處理已完成
                    return

                # ── Fallback：CDN 攔截 ──
                if "douyinstatic.com" in rurl: return
                is_cdn = ("video" in ct or "audio" in ct) or any(d in rurl for d in CDN_DOMAINS)
                if not is_cdn: return

                is_audio = ("audio" in ct) or any(k in rurl for k in ("audio", "mp4a", "aac-", "m4a-", "media-audio"))
                if is_audio:
                    # 音訊 CDN：無論 API 是否完成都要捕捉（DASH 分離音訊軌）
                    if not cdn_audio_url:
                        cdn_audio_url.append(rurl)
                else:
                    # 影片 CDN：只在 API 未完成時才捕捉（防止 API 已有 play_addr 卻被 DASH 段覆蓋）
                    if not api_done and not cdn_url:
                        cdn_url.append(rurl)
                        found.set()
                if not cover_url and any(pat in rurl for pat in COVER_PATTERNS):
                    if "image" in ct or rurl.endswith((".jpg", ".jpeg", ".webp", ".png")):
                        cover_url.append(rurl)

            page.on("response", on_response)
            await page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome={runtime:{}};"
                "window.outerWidth=1280;window.outerHeight=800;")

            await page.goto(video_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            try:
                await page.evaluate("document.querySelector('video')?.play()")
            except Exception:
                pass

            # 最多等 12 秒讓 API 或 CDN 回應
            try:
                await asyncio.wait_for(found.wait(), timeout=12)
            except asyncio.TimeoutError:
                pass

            # found 後再觸發播放，確保 DASH audio CDN 請求能被攔截
            try:
                await page.evaluate("document.querySelector('video')?.play()")
            except Exception:
                pass

            # 如果 CDN 搶先回應但 API 尚未處理完，再等最多 6 秒讓 API 的 cdn_url 更新完成
            if not api_found.is_set():
                try:
                    await asyncio.wait_for(api_found.wait(), timeout=6)
                except asyncio.TimeoutError:
                    pass  # API 超時，保留 CDN fallback 結果

            # 再等 3 秒讓 DASH 音訊 CDN 請求被捕捉
            await page.wait_for_timeout(3000)

            # 補 metadata（API 沒抓到時從頁面 meta 補）
            if not result["title"] or result["title"] == "抖音影片":
                try:
                    result["title"] = (await page.evaluate(
                        "document.querySelector('meta[property=\"og:title\"]')?.content"
                        "||document.querySelector('h1')?.textContent||document.title||'抖音影片'"
                    ) or "抖音影片").replace("- 抖音", "").strip()
                except Exception:
                    pass
            if not result["thumbnail"]:
                try:
                    result["thumbnail"] = await page.evaluate("""
                        document.querySelector('meta[property="og:image"]')?.content
                        || document.querySelector('meta[name="twitter:image"]')?.content
                        || document.querySelector('meta[itemprop="image"]')?.content
                        || document.querySelector('video')?.poster
                        || ''
                    """) or ""
                except Exception:
                    pass
                if not result["thumbnail"] and cover_url:
                    result["thumbnail"] = cover_url[0]

            await browser.close()

            if cdn_url:
                result["cdn_url"] = cdn_url[0]
            if cdn_audio_url:
                result["cdn_audio_url"] = cdn_audio_url[0]
    except Exception as e:
        print(f"[douyin_cdn] 錯誤：{e}")

    return result


# ── 快手：Playwright 攔截 GraphQL 取得 CDN URL ──────────────
async def _get_kuaishou_cdn(video_url: str) -> dict:
    """用 Playwright 開啟快手頁面，攔截 GraphQL API 回應取得 CDN 影片 URL"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {}

    result = {"title": "快手影片", "thumbnail": "", "duration": 0,
              "uploader": "", "cdn_url": None, "formats": []}
    try:
        async with async_playwright() as p:
            browser = await _pw_browser(p)
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800})
            await _apply_cookies(ctx, video_url)
            page = await ctx.new_page()
            await _stealth(page)

            found = asyncio.Event()

            async def on_response(resp):
                if "kuaishou.com/graphql" not in resp.url:
                    return
                try:
                    body = await resp.json()
                    data = body.get("data") or {}
                    photo = None
                    for key in ("visionVideoDetail", "visionVideoDetailExtra",
                                "visionVideoDetailOuter", "visionVideoGetPlayInfo"):
                        obj = data.get(key)
                        if isinstance(obj, dict):
                            photo = obj.get("photo") or obj.get("videoInfo") or obj
                            if photo and (photo.get("mainMvUrls") or photo.get("photoUrl")):
                                break
                            photo = None
                    if not photo:
                        return

                    cdn = ""
                    for field in ("mainMvUrls", "photoUrl", "urls", "videoUrl"):
                        v = photo.get(field)
                        if isinstance(v, list) and v:
                            cdn = (v[0].get("url") or v[0].get("cdn") or "")
                            break
                        elif isinstance(v, str) and v.startswith("http"):
                            cdn = v; break
                    if cdn:
                        result["cdn_url"] = cdn
                        found.set()

                    caption = photo.get("caption") or photo.get("title") or ""
                    if caption: result["title"] = caption[:80]
                    user = photo.get("user") or photo.get("userInfo") or {}
                    result["uploader"] = user.get("name","") or user.get("userName","")
                    covers = photo.get("coverUrls") or photo.get("webpCoverUrls") or []
                    if covers:
                        result["thumbnail"] = covers[0].get("url","")
                    dur = photo.get("duration",0) or 0
                    result["duration"] = dur // 1000 if dur > 1000 else dur
                except Exception as ex:
                    print(f"[kuaishou_cdn] GraphQL parse: {ex}")

            page.on("response", on_response)
            try:
                await page.goto(video_url, wait_until="domcontentloaded", timeout=25000)
                try:
                    await asyncio.wait_for(found.wait(), timeout=20)
                except asyncio.TimeoutError:
                    print("[kuaishou_cdn] 超時：未攔截到 GraphQL 影片 URL")
            except Exception as ex:
                print(f"[kuaishou_cdn] page load: {ex}")
            finally:
                await page.close(); await ctx.close(); await browser.close()
    except Exception as ex:
        print(f"[kuaishou_cdn] 錯誤：{ex}")
    return result



async def _get_tiktok_via_tikwm(url: str) -> dict:
    """用 tikwm.com 免費 API 取得 TikTok 無浮水印 CDN URL"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.post(
                "https://tikwm.com/api/",
                data={"url": url, "hd": "1"},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"},
            )
            d = r.json()
            if d.get("code") == 0 and d.get("data"):
                dat = d["data"]
                # play = tikwm 自己 CDN（公開可存取，無浮水印）
                # hdplay = TikTok 官方 CDN（有 auth token，瀏覽器直接讀會 403）
                cdn = dat.get("play") or dat.get("hdplay") or ""
                return {
                    "title":     dat.get("title", ""),
                    "thumbnail": dat.get("origin_cover") or dat.get("cover", ""),
                    "duration":  dat.get("duration", 0),
                    "uploader":  (dat.get("author") or {}).get("nickname", ""),
                    "cdn_url":   cdn,
                    "platform":  "TikTok",
                }
    except Exception as ex:
        print(f"[tikwm] {ex}")
    return {}


def _lux_info(url: str) -> dict:
    """用 Lux -j 取得影片 title + 真實可用畫質清單（同步，跑在 executor）"""
    r = subprocess.run(
        [str(LUX_PATH), "-j", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25
    )
    try:
        data = json.loads(r.stdout.strip()) if r.stdout.strip() else []
    except json.JSONDecodeError:
        data = []
    item = data[0] if data else {}

    # 從 streams 提取實際可用畫質
    streams = item.get("streams") or {}
    formats = []
    for sid, s in streams.items():
        qs = s.get("quality", "")
        if "4K" in qs or "2160" in qs:        lbl = "4K"
        elif "2K" in qs or "1440" in qs:      lbl = "2K"
        elif "1080" in qs and "60" in qs:      lbl = "1080P 60fps"
        elif "1080" in qs:                     lbl = "1080P"
        elif "720" in qs:                      lbl = "720P HD"
        elif "480" in qs:                      lbl = "480P"
        elif "360" in qs:                      lbl = "360P"
        else:                                  lbl = qs[:12] or sid
        formats.append({"id": sid, "label": lbl})
    _ord = {"360P":0,"480P":1,"720P HD":2,"1080P":3,"1080P 60fps":4,"2K":5,"4K":6}
    formats.sort(key=lambda f: _ord.get(f["label"], 3))

    return {"title": item.get("title", ""), "thumbnail": "",
            "duration": 0, "uploader": item.get("site", ""), "formats": formats}

def _lux_download(url: str, out_dir: Path) -> tuple[str, str]:
    """用 Lux 下載影片，返回 (filename, dir_str)"""
    before = set(out_dir.glob("*"))
    r = subprocess.run(
        [str(LUX_PATH), "-o", str(out_dir), url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300
    )
    # 找到新增的檔案
    after = set(out_dir.glob("*"))
    new_files = [f for f in (after - before) if f.suffix.lower() in (".mp4", ".mkv", ".flv", ".webm", ".m4v")]
    if new_files:
        return new_files[0].name, str(out_dir)
    # fallback：找最新
    vids = sorted([f for f in out_dir.iterdir() if f.suffix.lower() in (".mp4", ".mkv", ".flv", ".webm")],
                  key=lambda x: x.stat().st_mtime, reverse=True)
    if vids:
        return vids[0].name, str(out_dir)
    raise Exception(f"Lux 下載失敗：{(r.stderr or r.stdout)[:200]}")

async def _download_from_cdn(cdn_url: str, out_dir: Path, title: str,
                             cdn_audio_url: str | None = None) -> tuple[str, str]:
    """用 httpx 直接從 CDN 下載影片；有音訊時用 ffmpeg 合併"""
    safe = re.sub(r'[\\/:*?"<>|]', '_', title)[:60]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.douyin.com/",
    }

    async def _dl_file(url: str, fpath: Path):
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            async with client.stream("GET", url, headers={**headers, "Range": "bytes=0-"}) as r:
                with open(fpath, "wb") as f:
                    async for chunk in r.aiter_bytes(512 * 1024):
                        f.write(chunk)

    if cdn_audio_url:
        video_tmp = out_dir / f"{safe}_v.mp4"
        audio_tmp = out_dir / f"{safe}_a.m4a"
        final     = out_dir / f"{safe}.mp4"
        await asyncio.gather(_dl_file(cdn_url, video_tmp), _dl_file(cdn_audio_url, audio_tmp))
        ffmpeg_bin = FFMPEG_DIR + r"\ffmpeg.exe"
        subprocess.run(
            [ffmpeg_bin, "-y", "-i", str(video_tmp), "-i", str(audio_tmp),
             "-c", "copy", str(final)],
            capture_output=True, timeout=120
        )
        video_tmp.unlink(missing_ok=True)
        audio_tmp.unlink(missing_ok=True)
        if not final.exists():
            # ffmpeg 失敗就用純視訊
            await _dl_file(cdn_url, final)
        return final.name, str(out_dir)
    else:
        fpath = out_dir / f"{safe}.mp4"
        await _dl_file(cdn_url, fpath)
        return fpath.name, str(out_dir)


# ── Cookies 管理 ──────────────────────────────────────────
@app.post("/api/cookies/save")
async def save_cookies(platform: str = Form(...), cookies_json: str = Form(...)):
    """儲存平台 cookies（JSON 陣列格式）"""
    try:
        cookies = json.loads(cookies_json)
        if not isinstance(cookies, list):
            return JSONResponse({"ok": False, "error": "格式必須是 JSON 陣列"})
        # 標準化 cookies 格式
        normalized = []
        for c in cookies:
            if not c.get("name") or not c.get("value"):
                continue
            normalized.append({
                "name": c["name"], "value": c["value"],
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
            })
        data = _load_platform_cookies()
        data[platform.lower()] = normalized
        COOKIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return JSONResponse({"ok": True, "count": len(normalized)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/api/cookies/status")
def cookies_status():
    """回傳各平台 cookies 數量"""
    data = _load_platform_cookies()
    return JSONResponse({
        plat: len(data.get(plat, [])) for plat in ["douyin", "kuaishou", "tiktok"]
    })

@app.delete("/api/cookies/{platform}")
def delete_cookies(platform: str):
    data = _load_platform_cookies()
    data.pop(platform.lower(), None)
    COOKIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse({"ok": True})

# ── 首頁 ──────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(str(BASE_DIR / "index.html"),
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

@app.get("/admin")
def admin():
    return FileResponse(str(BASE_DIR / "admin.html"),
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

# ── yt-dlp 搜尋（YouTube / B站）────────────────────────────
def _ytdlp_search(prefix: str, platform: str, count: int) -> list:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(prefix, download=False)
            entries = (info or {}).get("entries", [])
            results = []
            for e in entries[:count]:
                vid = e.get("id", "")
                thumb = e.get("thumbnail", "")
                # YouTube extract_flat 有時不帶縮圖，用 ytimg 補上
                if not thumb and platform == "YouTube" and vid:
                    thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                # B站 thumbnail 有時是相對路徑或帶 // 開頭
                if thumb and thumb.startswith("//"):
                    thumb = "https:" + thumb
                url = e.get("url") or e.get("webpage_url", "")
                # YouTube 補全 URL
                if platform == "YouTube" and vid and not url.startswith("http"):
                    url = f"https://www.youtube.com/watch?v={vid}"
                results.append({
                    "title":      e.get("title", "未知標題"),
                    "url":        url,
                    "thumbnail":  thumb,
                    "duration":   e.get("duration", 0),
                    "uploader":   e.get("uploader", ""),
                    "platform":   platform,
                    "youtube_id": vid if platform == "YouTube" else "",
                })
            return results
    except Exception:
        return []

@app.post("/api/search")  
async def search(keyword: str = Form(...), platform: str = Form("YouTube"), count: int = Form(10)):
    opt = await _optimize_query(keyword)
    yt_kw = opt["youtube"]            # YouTube 版（台灣繁體 CTR 擴展）
    bi_kw = opt.get("bilibili", yt_kw)  # B站版（簡體慣用詞）
    gl_kw = opt["global"]             # 國際平台版（精簡英文）
    if platform == "抖音":
        return await _search_douyin(keyword, count)
    if platform == "TikTok":
        return await _search_tiktok(keyword, count)
    if platform == "快手":
        return await _search_kuaishou(keyword, count)
    if platform == "B站":
        return await _search_bilibili(bi_kw, count)
    if platform == "Dailymotion":
        results = await _search_dailymotion(gl_kw, count)
        return JSONResponse({"results": results, "keyword_optimized": gl_kw})
    if platform == "Odysee":
        results = await _search_odysee(gl_kw, count)
        return JSONResponse({"results": results, "keyword_optimized": gl_kw})
    if platform == "PeerTube":
        results = await _search_peertube(gl_kw, count)
        return JSONResponse({"results": results, "keyword_optimized": gl_kw})
    if platform == "YouTube":
        loop = asyncio.get_event_loop()
        results = await _search_youtube(yt_kw, count, loop)
        resp_data = {"results": results, "keyword_optimized": yt_kw,
                     "keyword_global": gl_kw, "intent": opt.get("intent","其他")}
        if yt_kw != keyword:
            resp_data["keyword_original"] = keyword
        return JSONResponse(resp_data)
    if platform not in SEARCH_MAP:
        return JSONResponse({"error": f"不支援 {platform}，請用 URL下載分頁", "results": []})
    prefix = SEARCH_MAP[platform].format(n=count, q=keyword)
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(executor, _ytdlp_search, prefix, platform, count)
    return JSONResponse({"results": results})

# ── 全網搜尋 ──────────────────────────────────────────────
# @app.post("/api/search-all")  
async def search_all(keyword: str = Form(...), count: int = Form(6)):
    loop = asyncio.get_event_loop()
    opt    = await _optimize_query(keyword)
    yt_kw  = opt["youtube"]              # YouTube 版（台灣繁體）
    bi_kw  = opt.get("bilibili", yt_kw)  # B站版（簡體慣用詞）
    gl_kw  = opt["global"]               # 國際平台版（精簡英文）

    async def one_ytdlp(plt):
        prefix = SEARCH_MAP[plt].format(n=count, q=yt_kw)
        return await loop.run_in_executor(executor, _ytdlp_search, prefix, plt, count)

    async def one_playwright(fn):
        try:
            resp = await asyncio.wait_for(fn(keyword, count), timeout=30)
            data = json.loads(resp.body)
            return data.get("results", [])
        except Exception:
            return []

    async def one_bili():
        try:
            resp = await asyncio.wait_for(_search_bilibili(bi_kw, count), timeout=15)
            data = json.loads(resp.body)
            return data.get("results", [])
        except Exception:
            return []

    async def one_youtube():
        return await _search_youtube(yt_kw, count, loop)

    results_list = await asyncio.gather(one_youtube(), one_bili(), return_exceptions=True)

    merged, max_len = [], max((len(r) for r in results_list if isinstance(r, list)), default=0)
    for i in range(max_len):
        for r in results_list:
            if isinstance(r, list) and i < len(r):
                merged.append(r[i])
    return JSONResponse({"results": merged})

# ── 快手搜尋：Playwright + HTML 解析 ─────────────────────
async def _search_kuaishou(keyword: str, count: int = 10):
    import urllib.parse
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return JSONResponse({"results": [], "error": "playwright 未安裝"})

    video_items: list[dict] = []
    try:
        async with async_playwright() as p:
            browser = await _pw_browser(p)
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900}, locale="zh-CN")
            await _apply_cookies(ctx, "https://www.kuaishou.com/")
            page = await ctx.new_page()
            await _stealth(page)
            await page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});window.chrome={runtime:{}};")
            await page.goto(
                f"https://www.kuaishou.com/search/video?searchKey={urllib.parse.quote(keyword)}",
                wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            # 滾動頁面觸發動態載入
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(1000)

            html = await page.content()

            # 法1：從 HTML 抽取 photoId + coverUrl
            try:
                photo_blocks = re.findall(
                    r'"photoId"\s*:\s*"([A-Za-z0-9_-]{6,})"[^}]{0,400}?"coverUrl"\s*:\s*"([^"]+)"',
                    html)
                seen: set = set()
                for vid, cover in photo_blocks:
                    if vid not in seen:
                        seen.add(vid)
                        thumb = cover if cover.startswith("http") else ("https:" + cover if cover.startswith("//") else "")
                        video_items.append({"url": f"https://www.kuaishou.com/short-video/{vid}", "thumb": thumb})
            except Exception:
                pass

            # 法2：只抓 photoId（無縮圖）
            if not video_items:
                ids = re.findall(r'"photoId"\s*:\s*"([A-Za-z0-9_-]{6,})"', html)
                seen = set()
                for vid in ids:
                    if vid not in seen:
                        seen.add(vid)
                        video_items.append({"url": f"https://www.kuaishou.com/short-video/{vid}", "thumb": ""})

            # 法3：CSS 選擇器
            if not video_items:
                try:
                    items_js = await page.evaluate("""
                        () => {
                            const seen = new Set(), items = [];
                            document.querySelectorAll('a[href*="/short-video/"],a[href*="/f/"]').forEach(a => {
                                const m = a.href.match(/\\/short-video\\/([A-Za-z0-9_-]+)/);
                                if (!m || seen.has(m[1])) return;
                                seen.add(m[1]);
                                const c = a.closest('div[class],li');
                                const img = c?.querySelector('img[src*="http"]');
                                items.push({ url: a.href, thumb: img?.src || '' });
                            });
                            return items;
                        }
                    """)
                    video_items = items_js or []
                except Exception:
                    pass

            await browser.close()
    except Exception as ex:
        print(f"[kuaishou_search] {ex}")

    if not video_items:
        return JSONResponse({"results": [], "error": "快手未找到結果（可能需要登入或被偵測）"})

    results = [
        {"title": "快手影片", "url": x["url"], "thumbnail": x.get("thumb", ""),
         "duration": 0, "uploader": "", "platform": "快手", "youtube_id": ""}
        for x in video_items[:count]
    ]
    return JSONResponse({"results": results})

# ── TikTok 搜尋：yt-dlp ttsearch 前綴 ────────────────────
async def _search_tiktok(keyword: str, count: int = 10):
    loop = asyncio.get_event_loop()

    def _do_search():
        opts = {
            "quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True,
            "http_headers": {
                "User-Agent": "com.zhiliaoapp.musically/2022600030 (Linux; U; Android 7.1.2; en_US; Pixel 4; Build/N2G48H; Cronet/58.0.2991.0)",
            },
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ttsearch{count}:{keyword}", download=False)
                entries = (info or {}).get("entries", []) or []
                results = []
                for e in entries[:count]:
                    vid = e.get("id", "")
                    thumb = e.get("thumbnail", "")
                    vurl = e.get("url") or e.get("webpage_url") or ""
                    if not vurl and vid:
                        vurl = f"https://www.tiktok.com/@_/video/{vid}"
                    if not vurl: continue
                    results.append({
                        "title": e.get("title", "TikTok影片") or "TikTok影片",
                        "url": vurl, "thumbnail": thumb,
                        "duration": e.get("duration", 0) or 0,
                        "uploader": e.get("uploader", "") or "",
                        "platform": "TikTok", "youtube_id": "",
                    })
                return results
        except Exception as e:
            print(f"[tiktok_ttsearch] {e}")
            return []

    results = await loop.run_in_executor(executor, _do_search)
    if results:
        return JSONResponse({"results": results})

    # fallback：Playwright
    return await _search_tiktok_playwright(keyword, count)

async def _search_tiktok_playwright(keyword: str, count: int = 10):
    import urllib.parse
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return JSONResponse({"results": [], "error": "playwright 未安裝"})
    video_items: list[dict] = []
    try:
        async with async_playwright() as p:
            browser = await _pw_browser(p)
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                viewport={"width": 390, "height": 844}, locale="zh-TW")
            await _apply_cookies(ctx, "https://www.tiktok.com/")
            page = await ctx.new_page()
            await _stealth(page)
            await page.goto(
                f"https://www.tiktok.com/search/video?q={urllib.parse.quote(keyword)}",
                wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(6000)
            html = await page.content()
            # 從 HTML 抓 video ID
            ids = re.findall(r'(?:video/|itemId["\s:]+)(\d{15,20})', html)
            seen: set = set()
            for vid in ids:
                if vid not in seen:
                    seen.add(vid)
                    video_items.append({"url": f"https://www.tiktok.com/@_/video/{vid}", "thumb": ""})
            await browser.close()
    except Exception:
        pass
    results = [{"title":"TikTok影片","url":x["url"],"thumbnail":"","duration":0,
                "uploader":"","platform":"TikTok","youtube_id":""} for x in video_items[:count]]
    return JSONResponse({"results": results})

# ── 抖音搜尋：X-Bogus API 直接呼叫（需要後台設定 Cookies）─────────────
async def _search_douyin(keyword: str, count: int = 10):
    import sys as _sys
    from urllib.parse import quote as _quote
    _dy_lib = r"D:\tools\Douyin_TikTok_Download_API"
    if _dy_lib not in _sys.path:
        _sys.path.insert(0, _dy_lib)

    try:
        from crawlers.douyin.web.utils import BogusManager, TokenManager
    except ImportError:
        return JSONResponse({"error": "缺少抖音簽名庫，請確認 D:\\tools\\Douyin_TikTok_Download_API 已克隆", "results": []})

    cookie_data = _load_platform_cookies().get("douyin", [])
    if not cookie_data:
        return JSONResponse({"error": "未設定抖音 Cookies，請前往後台 /admin 設定後再搜尋", "results": [], "need_cookies": True})

    cookie_str = "; ".join(
        f"{c['name']}={c['value']}" for c in cookie_data if c.get("name") and c.get("value")
    )

    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
    try:
        msToken = TokenManager().gen_real_msToken()
    except Exception:
        msToken = ""

    params = {
        "keyword": _quote(keyword),
        "search_channel": "aweme_video_web",
        "enable_history": "1",
        "search_source": "normal_search",
        "query_correct_type": "1",
        "is_filter_search": "0",
        "offset": "0",
        "count": str(count),
        "need_filter_settings": "1",
        "list_type": "single",
        "version_name": "23.5.0",
        "version_code": "170400",
        "webid": "7380000000000000000",
        "msToken": msToken,
    }

    try:
        signed_url = BogusManager.xb_model_2_endpoint(
            "https://www.douyin.com/aweme/v1/web/search/item/", params, UA)
    except Exception as e:
        return JSONResponse({"error": f"抖音簽名失敗: {e}", "results": []})

    headers = {
        "User-Agent": UA,
        "Referer": "https://www.douyin.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie_str + (f"; msToken={msToken}" if msToken else ""),
    }

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(signed_url, headers=headers)
            data = resp.json()
            items = data.get("aweme_list") or []

            if not items:
                return JSONResponse({"error": "抖音搜尋無結果（Cookies 可能已過期，請至後台更新）", "results": [], "need_cookies": True})

            results = []
            for aw in items[:count]:
                aid = aw.get("aweme_id", "")
                if not aid:
                    continue
                desc = (aw.get("desc") or "抖音影片")[:80]
                thumb = ""
                try:
                    url_list = aw.get("video", {}).get("cover", {}).get("url_list", [])
                    thumb = url_list[0] if url_list else ""
                except Exception:
                    pass
                dur = 0
                try:
                    dur = int(aw.get("duration", 0) or 0) // 1000
                except Exception:
                    pass
                uploader = ""
                try:
                    uploader = aw.get("author", {}).get("nickname", "") or ""
                except Exception:
                    pass
                results.append({
                    "title": desc,
                    "url": f"https://www.douyin.com/video/{aid}",
                    "thumbnail": thumb,
                    "duration": dur,
                    "uploader": uploader,
                    "platform": "抖音",
                    "youtube_id": "",
                })

            return JSONResponse({"results": results})
    except Exception as e:
        return JSONResponse({"error": f"抖音搜尋錯誤: {e}", "results": []})

# ── 圖片搜尋 ─────────────────────────────────────────────
VISION_MODELS = {
    "qwen2.5vl:7b":  "qwen2.5vl:7b（快速推薦，7B）",
    "gemma4:latest": "gemma4:latest（準確，較慢）",
    "gemma4:26b":    "gemma4:26b（最準，非常慢）",
}
DEFAULT_VISION_MODEL = "qwen2.5vl:7b"

@app.get("/api/vision-models")
def get_vision_models():
    return JSONResponse({"models": VISION_MODELS, "default": DEFAULT_VISION_MODEL})

@app.post("/api/search-by-image")  
async def search_by_image(
    file:    UploadFile = File(...),
    platform: str = Form("全網"),
    count:   int  = Form(6),
    model:   str  = Form(DEFAULT_VISION_MODEL),
):
    import base64, httpx
    contents = await file.read()
    b64      = base64.b64encode(contents).decode()
    keyword  = "影片"
    vision_model = model if model in VISION_MODELS else DEFAULT_VISION_MODEL
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(OLLAMA_URL, json={
                "model":  vision_model,
                "prompt": (
                    "Look at this image carefully. Output the single best YouTube search query "
                    "that would find videos about the main subject of this image. "
                    "Be SPECIFIC: if it's a cat playing basketball, say '貓咪打籃球' not just '貓咪'. "
                    "If it's a person, identify who they are if possible. "
                    "If it's a product, include the brand and model. "
                    "Output ONLY the search query in Traditional Chinese (2-8 words), nothing else."
                ),
                "images": [b64],
                "stream": False,
                "options": {"temperature": 0.05, "num_predict": 30},
            })
            if resp.status_code == 200:
                raw = resp.json().get("response", "").strip()
                # 只取第一行，去掉模型可能多輸出的解釋
                keyword = raw.split('\n')[0].strip().split('。')[0].strip() or keyword
    except Exception:
        pass

    result = await search_all(keyword=keyword, count=count) if platform == "全網" \
             else await search(keyword=keyword, platform=platform, count=count * 2)

    data = json.loads(result.body)
    data["keyword_detected"] = keyword
    data["model_used"] = vision_model
    return JSONResponse(data)

# ── URL 預覽 ──────────────────────────────────────────────
@app.get("/api/video-info")
async def video_info(url: str):
    real_url = await resolve_short_url(url)

    # ══ 蝦皮短影音 ══
    if _is_shopee_url(real_url):
        info = await _get_shopee_video_info(real_url)
        if info.get("video_url"):
            return JSONResponse({
                "title": info["title"], "thumbnail": info["thumbnail"],
                "duration": 0, "uploader": info["uploader"],
                "platform": "Shopee", "url": real_url,
                "has_video": True, "cdn_url": info["video_url"],
                "formats": [{"id": "best", "label": "原始畫質", "height": 0}],
            })
        # 解析失敗時交給 yt-dlp fallback（不中斷）

    if _is_douyin(real_url):
        from urllib.parse import quote as _q
        cdn_info = await _get_douyin_cdn(real_url)
        cdn = cdn_info.get("cdn_url") or ""
        proxy = f"/api/proxy-video?url={_q(cdn, safe='')}" if cdn else ""
        return JSONResponse({
            "title":         cdn_info.get("title", "抖音影片"),
            "thumbnail":     cdn_info.get("thumbnail", ""),
            "duration":      cdn_info.get("duration", 0),
            "uploader":      cdn_info.get("uploader", ""),
            "platform":      "Douyin",
            "url":           real_url,
            "has_video":     bool(cdn),
            "proxy_url":     proxy,
            "cdn_url":       cdn,
            "cdn_audio_url": cdn_info.get("cdn_audio_url") or "",
            "formats":       cdn_info.get("formats", []),
        })

    # ══ 快手：Playwright GraphQL 攔截 ══
    if _is_kuaishou(real_url):
        from urllib.parse import quote as _q
        cdn_info = await _get_kuaishou_cdn(real_url)
        cdn = cdn_info.get("cdn_url") or ""
        proxy = f"/api/proxy-video?url={_q(cdn, safe='')}&referer=https://www.kuaishou.com/" if cdn else ""
        if cdn or cdn_info.get("title","快手影片") != "快手影片":
            return JSONResponse({
                "title":     cdn_info.get("title", "快手影片"),
                "thumbnail": cdn_info.get("thumbnail", ""),
                "duration":  cdn_info.get("duration", 0),
                "uploader":  cdn_info.get("uploader", ""),
                "platform":  "Kuaishou",
                "url":       real_url,
                "has_video": bool(cdn),
                "proxy_url": proxy,
                "cdn_url":   cdn,
                "formats":   [{"id":"best","label":"原始畫質","height":0}],
            })
        # Playwright 失敗 → 繼續往下走 yt-dlp（_info() 已自動帶入 kuaishou cookies）

    loop = asyncio.get_event_loop()

    # ══ TikTok：tikwm.com 無浮水印 API > yt-dlp fallback ══
    if "tiktok.com" in real_url:
        from urllib.parse import quote as _qtk
        tk = await _get_tiktok_via_tikwm(real_url)
        if tk.get("cdn_url"):
            return JSONResponse({
                **tk, "url": real_url,
                "proxy_url": "",
                "formats": [{"id": "best", "label": "原始畫質（無浮水印）", "height": 0}],
            })
        # tikwm 失敗 → yt-dlp（_info() 會帶入 tiktok cookies）

    # B站等 Lux 平台：先試 Lux（速度快），失敗再 fallback yt-dlp
    # B站：無論 Lux 成功與否，都嘗試從 Bilibili API 取標題/縮圖，並回傳 embed_url
    is_bilibili = "bilibili.com" in real_url or "b23.tv" in real_url
    if _is_lux_platform(real_url):
        # ── B站專屬處理：先用 Bilibili API 取 meta，再嘗試 Lux 取畫質清單 ──
        if is_bilibili:
            bvid_m = re.search(r'BV\w+', real_url)
            if bvid_m:
                bvid = bvid_m.group()
                embed_url = f"https://player.bilibili.com/player.html?bvid={bvid}&high_quality=1&danmaku=0"
                bili_title, bili_thumb, bili_dur, bili_author = "", "", 0, ""
                try:
                    async with httpx.AsyncClient(timeout=8) as c:
                        resp = await c.get("https://api.bilibili.com/x/web-interface/view",
                                           params={"bvid": bvid},
                                           headers={"User-Agent":"Mozilla/5.0","Referer":"https://www.bilibili.com/"})
                        d = resp.json()
                        ddata = d.get("data") or {}
                        bili_title  = ddata.get("title", "")
                        bili_thumb  = ddata.get("pic", "")
                        bili_dur    = ddata.get("duration", 0)
                        bili_author = (ddata.get("owner") or {}).get("name", "")
                        if bili_thumb.startswith("//"): bili_thumb = "https:" + bili_thumb
                        elif bili_thumb.startswith("http://"): bili_thumb = "https" + bili_thumb[4:]
                except Exception:
                    pass
                # 嘗試 Lux 取畫質清單（可選，失敗不影響）
                bili_fmts: list = []
                try:
                    lux_info = await loop.run_in_executor(executor, _lux_info, real_url)
                    bili_fmts = lux_info.get("formats") or []
                    if not bili_title:
                        bili_title = lux_info.get("title", "")
                except Exception:
                    pass
                if not bili_fmts:
                    bili_fmts = [{"id":"best","label":"最高畫質","height":0}]
                return JSONResponse({
                    "title": bili_title or bvid, "thumbnail": bili_thumb,
                    "duration": bili_dur, "uploader": bili_author,
                    "platform": "Bilibili", "url": real_url,
                    "formats": bili_fmts, "embed_url": embed_url,
                })
        # ── 其他 Lux 平台（非B站）────────────────────────────────
        try:
            info = await loop.run_in_executor(executor, _lux_info, real_url)
            if info and info.get("title"):
                bili_fmts = info.get("formats") or []
                return JSONResponse({"title": info["title"], "thumbnail": "",
                                     "duration": info.get("duration", 0), "uploader": info.get("uploader", ""),
                                     "platform": "Lux", "url": real_url, "formats": bili_fmts})
        except Exception:
            pass  # fallback to yt-dlp

    # 其他平台：走 yt-dlp
    def _info():
        from urllib.parse import urlparse as _up
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
        }
        # 若有儲存平台 cookies，寫入臨時 Netscape 格式 cookie file
        import tempfile, os as _os
        cookies_list = _get_cookies_for_url(real_url)
        _tmp_cookie_file = None
        if cookies_list:
            try:
                ck_lines = ["# Netscape HTTP Cookie File\n"]
                for c in cookies_list:
                    dom = c.get("domain","")
                    if dom and not dom.startswith("."): dom = "." + dom
                    ck_lines.append("\t".join([
                        dom, "TRUE", c.get("path","/"),
                        "TRUE" if c.get("secure") else "FALSE",
                        str(int(c.get("expires",0) or 0)),
                        c.get("name",""), c.get("value","")
                    ]) + "\n")
                tf = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
                tf.writelines(ck_lines); tf.close()
                opts["cookiefile"] = tf.name
                _tmp_cookie_file = tf.name
            except Exception:
                pass
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(real_url, download=False)
        finally:
            if _tmp_cookie_file:
                try: _os.unlink(_tmp_cookie_file)
                except Exception: pass
    try:
        info = await loop.run_in_executor(executor, _info)
        # 從 formats 抽取可用畫質
        all_fmts = info.get("formats") or []
        seen_h: set = set()
        yt_formats = []
        for f in sorted(all_fmts, key=lambda x: x.get("height") or 0, reverse=True):
            h = f.get("height")
            if h and h not in seen_h and f.get("vcodec","none") != "none":
                seen_h.add(h)
                lbl = ("4K" if h>=2160 else "2K" if h>=1440 else f"{h}P") + (" HD" if h==720 else "")
                yt_formats.append({"id": f"h{h}", "label": lbl, "height": h})
            if len(yt_formats) >= 5: break
        if not yt_formats:
            yt_formats = [{"id":"best","label":"最高畫質","height":0}]

        # 提取最佳直接 CDN URL 供前端預覽
        from urllib.parse import quote as _q2
        best_cdn = ""; cdn_audio = ""

        # 在選格式時就排除 manifest/DASH/HLS，避免先選高畫質 DASH 再清空的問題
        _MANIFEST_PROTO = ("http_dash_segments", "m3u8", "m3u8_native", "dash")
        def _is_mf(f):
            u = (f.get("url") or "").lower()
            return (any(x in u for x in ('.m3u8', '.mpd', 'm3u8?', 'manifest'))
                    or f.get("protocol","") in _MANIFEST_PROTO)

        # 優先：直接 combined（video+audio，非 manifest）
        combined = [f for f in all_fmts
                    if f.get("vcodec","none") != "none"
                    and f.get("acodec","none") != "none"
                    and f.get("url") and not _is_mf(f)]
        if combined:
            best = max(combined, key=lambda x: (x.get("height") or 0, x.get("tbr") or 0))
            best_cdn = best.get("url","")
        else:
            # 嘗試直接 video-only URL（非 manifest）
            vfmts = [f for f in all_fmts
                     if f.get("vcodec","none") != "none"
                     and f.get("url") and not _is_mf(f)]
            if vfmts:
                best_cdn = max(vfmts, key=lambda x: x.get("height") or 0).get("url","")
            # 嘗試找獨立音頻流（FB/YouTube DASH 音頻）
            afmts = [f for f in all_fmts
                     if f.get("acodec","none") != "none"
                     and f.get("vcodec","none") == "none"
                     and f.get("url") and not _is_mf(f)]
            cdn_audio = max(afmts, key=lambda x: x.get("abr") or 0).get("url","") if afmts else ""
            if not best_cdn:
                for f in reversed(all_fmts):
                    if f.get("url") and not _is_mf(f):
                        best_cdn = f["url"]; break
        # 備用：info["url"]（TikTok/XHS 等單流平台）
        if not best_cdn:
            u = info.get("url","")
            if u and not any(x in u.lower() for x in ('.m3u8','.mpd','m3u8?','manifest')):
                best_cdn = u

        origin = re.sub(r'(https?://[^/]+).*', r'\1', real_url)
        proxy_url = f"/api/proxy-video?url={_q2(best_cdn, safe='')}&referer={_q2(origin, safe='')}" if best_cdn else ""

        return JSONResponse({"title": info.get("title",""), "thumbnail": info.get("thumbnail",""),
                             "duration": info.get("duration",0), "uploader": info.get("uploader",""),
                             "platform": info.get("extractor_key",""), "url": real_url,
                             "proxy_url": proxy_url, "cdn_url": best_cdn,
                             "cdn_audio_url": cdn_audio,
                             "formats": yt_formats})
    except Exception as ex:
        err_str = str(ex)
        el = err_str.lower()
        hint = ""
        if any(k in el for k in ("login", "cookie", "sign in", "authentication", "403", "forbidden", "private")):
            hint = "此影片需要登入 Cookies，請至設定頁面貼上 Cookies 後再試"
        elif any(k in el for k in ("geo", "region", "not available in your country", "georestrict")):
            hint = "此影片有地區限制，無法從目前位置觀看"
        elif any(k in el for k in ("not found", "removed", "deleted", "does not exist", "404")):
            hint = "此影片已刪除或不存在"
        elif _is_kuaishou(real_url):
            hint = "快手影片解析失敗。如需下載，請至設定頁面貼上快手 Cookies 後再試"
        elif "tiktok.com" in real_url:
            hint = "TikTok 影片解析失敗，請至設定頁面貼上 TikTok Cookies"
        return JSONResponse({"error": err_str, "error_hint": hint, "resolved_url": real_url})

# ── 下載去水印 ────────────────────────────────────────────
@app.post("/api/download")
async def download_video(url: str = Form(...), title: str = Form("影片"), save_path: str = Form("")):
    real_url = await resolve_short_url(url)
    if save_path and Path(save_path).is_absolute():
        try:
            out_dir = Path(save_path)
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            out_dir = DOWNLOAD_DIR
    else:
        out_dir = DOWNLOAD_DIR

    if _is_douyin(real_url):
        # 抖音：優先 yt-dlp（最穩定，需要 cookies），再 API 直下，再 Playwright
        import tempfile as _tf

        cookie_data = _load_platform_cookies().get("douyin", [])
        tmp_ck = None

        # --- 1. yt-dlp + cookies ---
        if cookie_data:
            tmp_ck = _tf.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
            _cookies_to_netscape(cookie_data, tmp_ck.name)
            tmp_ck.close()
            safe = re.sub(r'[\\/:*?"<>|]', '_', title)[:60]
            tmpl = str(out_dir / f"{safe}.%(ext)s")
            opts_dy = {
                "format": "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
                "outtmpl": tmpl, "quiet": True, "no_warnings": True,
                "merge_output_format": "mp4", "cookiefile": tmp_ck.name,
                "concurrent_fragment_downloads": 8,
            }
            loop = asyncio.get_event_loop()
            def _dl_ytdlp():
                with yt_dlp.YoutubeDL(opts_dy) as ydl:
                    info2 = ydl.extract_info(real_url, download=True)
                    raw = ydl.prepare_filename(info2)
                    for ext in (".mp4", ".webm", ".mkv", ".mov"):
                        c2 = Path(raw).with_suffix(ext)
                        if c2.exists(): return c2.name, str(out_dir)
                    return Path(raw).name, str(out_dir)
            try:
                fname, saved_dir = await asyncio.wait_for(loop.run_in_executor(executor, _dl_ytdlp), timeout=90)
                fpath = Path(saved_dir) / fname
                if fpath.exists() and fpath.stat().st_size > 50000:
                    try: Path(tmp_ck.name).unlink()
                    except: pass
                    return JSONResponse({"success": True, "filename": fname, "saved_dir": saved_dir,
                                         "download_url": None, "size_mb": round(fpath.stat().st_size/1024/1024, 1)})
            except Exception as e:
                print(f"[dy_ytdlp] 失敗：{e}")
            try: Path(tmp_ck.name).unlink()
            except: pass

        # --- 2. API + httpx 直下 ---
        try:
            aweme_id = await _resolve_aweme_id(real_url)
            if aweme_id:
                info = await _get_douyin_info_api(aweme_id)
                video_url = info.get("video_url")
                if video_url:
                    use_title = info.get("title") or title
                    safe = re.sub(r'[\\/:*?"<>|]', '_', use_title)[:60]
                    fpath = out_dir / f"{safe}.mp4"
                    cookie_str = _cookies_to_str(cookie_data) if cookie_data else ""
                    dy_h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            "Referer": "https://www.douyin.com/",
                            **({"Cookie": cookie_str} if cookie_str else {})}
                    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as cl:
                        async with cl.stream("GET", video_url, headers=dy_h) as r:
                            r.raise_for_status()
                            with open(fpath, "wb") as f:
                                async for chunk in r.aiter_bytes(512*1024): f.write(chunk)
                    size = fpath.stat().st_size if fpath.exists() else 0
                    if size > 50000:
                        return JSONResponse({"success": True, "filename": fpath.name, "saved_dir": str(out_dir),
                                             "download_url": None, "size_mb": round(size/1024/1024, 1)})
        except Exception as e:
            print(f"[dy_api_dl] 失敗：{e}")

        # --- 3. Playwright CDN（最後手段）---
        try:
            cdn_info = await _get_douyin_cdn(real_url)
            cdn = cdn_info.get("cdn_url")
            if not cdn:
                return JSONResponse({"success": False, "error": "無法取得抖音影片，請至後台設定 Cookies 後再試"})
            use_title = cdn_info.get("title") or title
            audio_cdn = cdn_info.get("cdn_audio_url")
            fname, saved_dir = await _download_from_cdn(cdn, out_dir, use_title, audio_cdn)
            fpath = Path(saved_dir) / fname
            size = fpath.stat().st_size if fpath.exists() else 0
            return JSONResponse({"success": True, "filename": fname, "saved_dir": saved_dir,
                                 "download_url": None, "size_mb": round(size/1024/1024, 1)})
        except Exception as ex:
            return JSONResponse({"success": False, "error": f"抖音下載失敗：{ex}"})

    loop = asyncio.get_event_loop()

    # B站等 Lux 平台：優先用 Lux（更穩定，支援高畫質）
    if _is_lux_platform(real_url):
        try:
            fname, saved_dir = await loop.run_in_executor(executor, _lux_download, real_url, out_dir)
            fpath = Path(saved_dir) / fname
            size  = fpath.stat().st_size if fpath.exists() else 0
            dl_url = f"/下載影片/{fname}" if Path(saved_dir) == DOWNLOAD_DIR else None
            return JSONResponse({"success": True, "filename": fname, "saved_dir": saved_dir,
                                 "download_url": dl_url, "size_mb": round(size/1024/1024, 1)})
        except Exception as lux_err:
            print(f"[lux] 失敗，fallback yt-dlp：{lux_err}")

    # 其他平台（或 Lux 失敗）：走 yt-dlp
    safe  = re.sub(r'[\\/:*?"<>|]', '_', title)[:60]
    tmpl  = str(out_dir / f"{safe}.%(ext)s")
    opts  = {"format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
             "outtmpl": tmpl, "quiet": True, "no_warnings": True, "merge_output_format": "mp4",
             "concurrent_fragment_downloads": 8, "updatetime": False,
             "postprocessor_args": {"default": ["-map_metadata", "-1"]}}

    def _dl():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(real_url, download=True)
            raw  = ydl.prepare_filename(info)
            for ext in (".mp4", ".webm", ".mkv", ".mov"):
                c = Path(raw).with_suffix(ext)
                if c.exists():
                    return c.name, str(out_dir)
            return Path(raw).name, str(out_dir)

    try:
        fname, saved_dir = await loop.run_in_executor(executor, _dl)
        fpath = Path(saved_dir) / fname
        size  = fpath.stat().st_size if fpath.exists() else 0
        dl_url = f"/下載影片/{fname}" if Path(saved_dir) == DOWNLOAD_DIR else None
        return JSONResponse({"success": True, "filename": fname, "saved_dir": saved_dir,
                             "download_url": dl_url, "size_mb": round(size/1024/1024, 1)})
    except Exception as ex:
        return JSONResponse({"success": False, "error": str(ex)})

# ── 已下載清單 ────────────────────────────────────────────
@app.get("/api/downloads")
def list_downloads(device_id: str = ""):
    all_files = [{"name": f.name, "size_mb": round(f.stat().st_size/1024/1024,1), "url": f"/下載影片/{f.name}"}
                 for f in DOWNLOAD_DIR.iterdir() if f.suffix.lower() in (".mp4",".webm",".mkv",".mov")]
    if device_id:
        allowed = _registry_get_files(device_id)
        files = [f for f in all_files if f["name"] in allowed]
    else:
        files = all_files  # 無 device_id → 顯示全部（管理員用）
    return JSONResponse(sorted(files, key=lambda x: x["name"]))

@app.get("/api/douyin-cdn")
async def douyin_cdn(aweme_id: str):
    """背景抓取抖音 CDN 直鏈（供 video 預覽用），Playwright 方式，約 10-15 秒"""
    url = f"https://www.douyin.com/video/{aweme_id}"
    info = await _get_douyin_cdn(url)
    cdn = info.get("cdn_url") or ""
    return JSONResponse({"cdn_url": cdn, "ok": bool(cdn)})

@app.get("/api/proxy-video")
async def proxy_video(request: Request, url: str, referer: str = ""):
    """代理影片串流，支援 Range 請求（讓瀏覽器可 seek）"""
    from fastapi.responses import StreamingResponse
    from urllib.parse import unquote
    target = unquote(url)
    if not target.startswith("http"):
        return JSONResponse({"error": "invalid url"}, status_code=400)
    # 使用傳入的 referer，或自動從目標 URL 推斷來源域名
    if not referer:
        m = re.match(r'(https?://[^/]+)', unquote(referer) if referer else target)
        referer = m.group(1) if m else "https://www.douyin.com/"
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Referer": unquote(referer),
        "Accept": "*/*",
    }
    # 轉發 Range header，讓影片可 seek
    range_hdr = request.headers.get("range")
    if range_hdr:
        req_headers["Range"] = range_hdr

    client = httpx.AsyncClient(timeout=120, follow_redirects=True)
    req2 = client.build_request("GET", target, headers=req_headers)
    resp = await client.send(req2, stream=True)
    ct = resp.headers.get("content-type", "video/mp4")

    resp_headers: dict = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for hdr in ("content-range", "content-length"):
        if hdr in resp.headers:
            resp_headers[hdr.replace("-", "-").title().replace("Content-", "Content-")] = resp.headers[hdr]
    # 直接用小寫 key 確保 FastAPI 正確傳遞
    if "content-range" in resp.headers:
        resp_headers["Content-Range"] = resp.headers["content-range"]
    if "content-length" in resp.headers:
        resp_headers["Content-Length"] = resp.headers["content-length"]

    async def _stream():
        try:
            async for chunk in resp.aiter_bytes(512 * 1024):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(_stream(), status_code=resp.status_code,
                             media_type=ct, headers=resp_headers)

@app.get("/api/serve-file")
async def serve_file(filename: str = "", path: str = "", cleanup: bool = False, inline: bool = False):
    """把已下載的影片回傳給瀏覽器。
    filename：只傳檔名（推薦），從 DOWNLOAD_DIR 組合完整路徑
    path：傳絕對路徑（舊方式，保留相容）
    inline=true：Safari 直接播放（iOS 長按可儲存影片到相簿）"""
    from urllib.parse import quote as _uq
    from starlette.background import BackgroundTask
    if filename:
        fpath = DOWNLOAD_DIR / filename
    else:
        fpath = Path(path)
        try:
            fpath.resolve().relative_to(DOWNLOAD_DIR.resolve())
        except ValueError:
            return JSONResponse({"error": "forbidden"}, status_code=403)
    if not fpath.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    encoded_name = _uq(fpath.name, safe="")
    ext = fpath.suffix.lower()
    mime = {"mp4":"video/mp4","mkv":"video/x-matroska","webm":"video/webm","m4v":"video/mp4"}.get(ext.lstrip("."), "application/octet-stream")
    bg = None
    if cleanup:
        def _rm():
            try:
                if fpath.exists(): fpath.unlink()
            except Exception: pass
        bg = BackgroundTask(_rm)
    # inline=True：不加 attachment header，Safari 直接播放（iOS 長按可儲存影片到相簿）
    hdrs = {} if inline else {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"}
    return FileResponse(str(fpath), media_type=mime, headers=hdrs, background=bg)

@app.get("/api/dl-stream")
async def dl_stream(request: Request, url: str, title: str = "影片", referer: str = ""):
    """串流 CDN 影片直接到客戶端裝置（不寫入伺服器磁碟），供手機一鍵下載"""
    from fastapi.responses import StreamingResponse
    from urllib.parse import quote as _uq, unquote as _uuq
    if not url.startswith("http"):
        return JSONResponse({"error": "invalid url"}, status_code=400)
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', title)[:80]
    if not re.search(r'\.\w{2,4}$', safe_name):
        safe_name += '.mp4'
    encoded = _uq(safe_name, safe="")
    origin_ref = _uuq(referer) if referer else re.match(r'(https?://[^/]+)', url)
    if hasattr(origin_ref, 'group'):
        origin_ref = origin_ref.group(1)
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Referer": origin_ref or url,
        "Accept": "*/*",
    }
    range_hdr = request.headers.get("range")
    if range_hdr:
        req_headers["Range"] = range_hdr

    client = httpx.AsyncClient(timeout=120, follow_redirects=True)
    req2 = client.build_request("GET", url, headers=req_headers)
    resp = await client.send(req2, stream=True)
    ct = resp.headers.get("content-type", "video/mp4")
    if "text" in ct or "html" in ct:
        await resp.aclose(); await client.aclose()
        return JSONResponse({"error": "cdn returned non-video response"}, status_code=502)

    resp_headers: dict = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
    }
    for hdr in ("content-length", "content-range"):
        if hdr in resp.headers:
            resp_headers[hdr.title()] = resp.headers[hdr]

    async def _stream():
        try:
            async for chunk in resp.aiter_bytes(512 * 1024):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(_stream(), status_code=resp.status_code,
                             media_type=ct, headers=resp_headers)

@app.get("/api/pick-folder")
def pick_folder():
    """彈出原生 Windows 資料夾選取視窗，回傳所選路徑"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        folder = filedialog.askdirectory(
            parent=root,
            title="選擇影片下載資料夾",
            initialdir=str(DOWNLOAD_DIR),
        )
        root.destroy()
        if folder:
            return JSONResponse({"path": str(Path(folder))})
        return JSONResponse({"path": ""})
    except Exception as e:
        return JSONResponse({"path": "", "error": str(e)})

@app.get("/api/download-progress")
async def download_progress_sse(request: Request, url: str, title: str = "影片",
                                save_path: str = "", cdn_url: str = "", cdn_audio_url: str = "",
                                quality: str = "best", device_id: str = ""):
    """SSE 下載進度串流。cdn_url/cdn_audio_url 為預覽時已取得的 URL，可跳過 Playwright；
    quality = 'best'|'h1080'|'h720'|'h480'|抖音畫質id"""
    real_url = await resolve_short_url(url)
    if save_path and Path(save_path).is_absolute():
        try:
            out_dir = Path(save_path)
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            out_dir = DOWNLOAD_DIR
    else:
        out_dir = DOWNLOAD_DIR

    async def event_gen():
        try:
            async for evt in _dl_progress(real_url, title, out_dir,
                                           hint_cdn=cdn_url, hint_audio=cdn_audio_url,
                                           quality=quality):
                if await request.is_disconnected():
                    return
                # 下載完成時記錄到 registry（按裝置隔離）
                if evt.get("type") == "done" and device_id and evt.get("filename"):
                    _registry_add(evt["filename"], device_id)
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        except Exception as ex:
            yield f"data: {json.dumps({'type':'error','message':str(ex)}, ensure_ascii=False)}\n\n"

    from fastapi.responses import StreamingResponse as _SR
    return _SR(event_gen(), media_type="text/event-stream",
               headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                        "Connection": "keep-alive"})

async def _dl_progress(real_url: str, title: str, out_dir: Path,
                       hint_cdn: str = "", hint_audio: str = "", quality: str = "best"):
    """async generator：yield {type:'progress',pct,msg} / {type:'done',...} / {type:'error',...}"""
    loop = asyncio.get_running_loop()

    # ── httpx 多線程 Range 下載（比單線程快 3-4x）────────────────
    async def httpx_dl(url, fpath, headers, s=10, e=95, workers=4):
        # 先探測是否支援 Range + 取得 content-length
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as probe:
                hr = await probe.head(url, headers=headers)
                total = int(hr.headers.get("content-length", 0))
                supports_range = hr.headers.get("accept-ranges", "").lower() == "bytes"
        except Exception:
            total, supports_range = 0, False

        if not supports_range or total < 4*1024*1024:
            # 小檔或不支援 Range → 單線程
            async with httpx.AsyncClient(timeout=180, follow_redirects=True) as cl:
                async with cl.stream("GET", url, headers=headers) as r:
                    r.raise_for_status()
                    tot = int(r.headers.get("content-length", 0)) or total
                    done = 0
                    with open(fpath, "wb") as f:
                        async for chunk in r.aiter_bytes(512*1024):
                            f.write(chunk); done += len(chunk)
                            pct = s+int((done/tot)*(e-s)) if tot else (s+e)//2
                            yield {"type":"progress","pct":min(pct,e),
                                   "msg":f"下載中 {done//1048576}MB{'/{:.0f}MB'.format(tot/1048576) if tot else ''}"}
            return

        # 多線程 Range 下載
        workers = min(workers, 8)
        chunk = total // workers
        ranges = [(i*chunk, (i+1)*chunk-1 if i<workers-1 else total-1) for i in range(workers)]
        tmps = [fpath.parent/f".tmp_{fpath.stem}_{i}{fpath.suffix}" for i in range(workers)]
        done_arr = [0]*workers
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        last_pct = [s]

        async def dl_chunk(idx, start, end, tmp):
            try:
                hdrs = {**headers, "Range": f"bytes={start}-{end}"}
                async with httpx.AsyncClient(timeout=180, follow_redirects=True) as cl:
                    async with cl.stream("GET", url, headers=hdrs) as r:
                        with open(tmp, "wb") as f:
                            async for c in r.aiter_bytes(512*1024):
                                f.write(c); done_arr[idx] += len(c)
                                td = sum(done_arr)
                                pct = s+int(td/total*(e-s))
                                if pct > last_pct[0]:
                                    last_pct[0] = pct
                                    await q.put({"type":"progress","pct":min(pct,e),
                                                 "msg":f"下載中 {td//1048576}MB/{total//1048576}MB ({workers}線程並行)"})
            finally:
                await q.put(None)

        tasks = [asyncio.create_task(dl_chunk(i, r[0], r[1], tmps[i])) for i, r in enumerate(ranges)]
        finished = 0
        while finished < workers:
            evt = await asyncio.wait_for(q.get(), timeout=120)
            if evt is None: finished += 1
            else: yield evt
        await asyncio.gather(*tasks, return_exceptions=True)
        with open(fpath, "wb") as out:
            for tmp in tmps:
                if tmp.exists():
                    with open(tmp, "rb") as inp: out.write(inp.read())
                    tmp.unlink(missing_ok=True)
        yield {"type":"progress","pct":e,"msg":"組合完成"}

    # ── yt-dlp（async generator，結果放入 res_list）───────────────
    async def ytdlp_dl(opts, url, res_list, err_list):
        q = asyncio.Queue()
        def hook(d):
            if d['status'] == 'downloading':
                dl = d.get('downloaded_bytes') or 0
                tot = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                pct = max(5, min(95, int(dl/tot*90)+5)) if tot else 50
                asyncio.run_coroutine_threadsafe(
                    q.put({"type":"progress","pct":pct,"msg":f"下載中 {d.get('_percent_str','').strip()}"}), loop)
            elif d['status'] == 'finished':
                asyncio.run_coroutine_threadsafe(q.put({"type":"progress","pct":99,"msg":"合併格式..."}), loop)
        def run():
            try:
                with yt_dlp.YoutubeDL({**opts,"progress_hooks":[hook]}) as ydl:
                    info = ydl.extract_info(url, download=True)
                    raw = ydl.prepare_filename(info)
                    for ext in (".mp4",".webm",".mkv",".mov"):
                        c = Path(raw).with_suffix(ext)
                        if c.exists(): res_list.append(c); return
                    res_list.append(Path(raw))
            except Exception as ex: err_list.append(str(ex))
            finally: asyncio.run_coroutine_threadsafe(q.put(None), loop)
        loop.run_in_executor(executor, run)
        while True:
            try: item = await asyncio.wait_for(q.get(), timeout=180)
            except asyncio.TimeoutError: err_list.append("下載超時"); break
            if item is None: break
            yield item

    # ── 標準 ffmpeg 合併 ──────────────────────────────────────────
    def ffmerge(video, audio, out):
        ffmpeg_bin = FFMPEG_DIR + r"\ffmpeg.exe"
        subprocess.run([ffmpeg_bin,"-y","-i",str(video),"-i",str(audio),"-c","copy",str(out)],
                       capture_output=True, timeout=120)
        video.unlink(missing_ok=True); audio.unlink(missing_ok=True)

    DY_HEADERS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                  "Referer":"https://www.douyin.com/"}

    # ══ 抖音 ══════════════════════════════════════════════════════
    if _is_douyin(real_url):
        import tempfile as _tf
        cookie_data = _load_platform_cookies().get("douyin", [])

        # ── 快速路徑：預覽已取得 CDN URL，直接下載（跳過 Playwright）──
        if hint_cdn:
            yield {"type":"progress","pct":5,"msg":f"下載中（{quality if quality != 'best' else '最高畫質'}）..."}
            safe = re.sub(r'[\\/:*?"<>|]', '_', title)[:60]
            if hint_audio:
                vt = out_dir/f"{safe}_v.mp4"; at = out_dir/f"{safe}_a.m4a"; final = out_dir/f"{safe}.mp4"
                yield {"type":"progress","pct":10,"msg":"下載影片軌..."}
                async for evt in httpx_dl(hint_cdn, vt, DY_HEADERS, 10, 58): yield evt
                yield {"type":"progress","pct":60,"msg":"下載音訊軌..."}
                async for evt in httpx_dl(hint_audio, at, DY_HEADERS, 60, 83): yield evt
                yield {"type":"progress","pct":86,"msg":"合併音訊..."}
                ffmerge(vt, at, final)
                if not final.exists():
                    async for evt in httpx_dl(hint_cdn, final, DY_HEADERS, 86, 98): yield evt
            else:
                final = out_dir/f"{safe}.mp4"
                async for evt in httpx_dl(hint_cdn, final, DY_HEADERS, 5, 95): yield evt
            sz = final.stat().st_size if final.exists() else 0
            if sz > 50000:
                yield {"type":"done","filename":final.name,"saved_dir":str(out_dir),"size_mb":round(sz/1024/1024,1)}
                return
            yield {"type":"progress","pct":5,"msg":"快取 URL 已過期，重新抓取..."}

        # Tier 1: yt-dlp + cookies
        if cookie_data:
            yield {"type":"progress","pct":2,"msg":"初始化 yt-dlp..."}
            tmp_ck = _tf.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
            _cookies_to_netscape(cookie_data, tmp_ck.name); tmp_ck.close()
            safe = re.sub(r'[\\/:*?"<>|]', '_', title)[:60]
            opts_dy = {"format":"bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
                       "outtmpl":str(out_dir/f"{safe}.%(ext)s"),"quiet":True,"no_warnings":True,
                       "merge_output_format":"mp4","cookiefile":tmp_ck.name,
                       "concurrent_fragment_downloads":8}
            res1, err1 = [], []
            async for evt in ytdlp_dl(opts_dy, real_url, res1, err1): yield evt
            try: Path(tmp_ck.name).unlink()
            except: pass
            if res1 and Path(res1[0]).exists() and Path(res1[0]).stat().st_size > 50000:
                sz = round(Path(res1[0]).stat().st_size/1024/1024, 1)
                yield {"type":"done","filename":Path(res1[0]).name,"saved_dir":str(out_dir),"size_mb":sz}
                return

        # Tier 2: API + httpx
        yield {"type":"progress","pct":5,"msg":"嘗試 API 取得影片..."}
        try:
            aweme_id = await _resolve_aweme_id(real_url)
            if aweme_id:
                info = await _get_douyin_info_api(aweme_id)
                vurl = info.get("video_url")
                if vurl:
                    use_title = info.get("title") or title
                    safe = re.sub(r'[\\/:*?"<>|]', '_', use_title)[:60]
                    fpath = out_dir / f"{safe}.mp4"
                    ck_str = _cookies_to_str(cookie_data) if cookie_data else ""
                    hdrs = {**DY_HEADERS, **({"Cookie":ck_str} if ck_str else {})}
                    async for evt in httpx_dl(vurl, fpath, hdrs, 10, 95): yield evt
                    if fpath.exists() and fpath.stat().st_size > 50000:
                        yield {"type":"done","filename":fpath.name,"saved_dir":str(out_dir),"size_mb":round(fpath.stat().st_size/1024/1024,1)}
                        return
        except Exception as e: print(f"[dy_api_dl] {e}")

        # Tier 3: Playwright CDN
        yield {"type":"progress","pct":5,"msg":"啟動瀏覽器擷取影片..."}
        try:
            cdn_info = await _get_douyin_cdn(real_url)
            cdn = cdn_info.get("cdn_url")
            if not cdn:
                yield {"type":"error","message":"無法取得影片，請至後台設定 Cookies"}; return
            safe = re.sub(r'[\\/:*?"<>|]', '_', cdn_info.get("title") or title)[:60]
            audio_cdn = cdn_info.get("cdn_audio_url")
            if audio_cdn:
                vt = out_dir/f"{safe}_v.mp4"; at = out_dir/f"{safe}_a.m4a"; final = out_dir/f"{safe}.mp4"
                yield {"type":"progress","pct":20,"msg":"下載影片軌..."}
                async for evt in httpx_dl(cdn, vt, DY_HEADERS, 20, 60): yield evt
                yield {"type":"progress","pct":62,"msg":"下載音訊軌..."}
                async for evt in httpx_dl(audio_cdn, at, DY_HEADERS, 62, 85): yield evt
                yield {"type":"progress","pct":88,"msg":"合併音訊..."}
                ffmerge(vt, at, final)
                if not final.exists():
                    async for evt in httpx_dl(cdn, final, DY_HEADERS, 88, 98): yield evt
            else:
                final = out_dir/f"{safe}.mp4"
                async for evt in httpx_dl(cdn, final, DY_HEADERS, 10, 95): yield evt
            sz = final.stat().st_size if final.exists() else 0
            yield {"type":"done","filename":final.name,"saved_dir":str(out_dir),"size_mb":round(sz/1024/1024,1)}
        except Exception as ex:
            yield {"type":"error","message":f"抖音下載失敗：{ex}"}
        return

    # ══ 快手 ══════════════════════════════════════════════════════
    if _is_kuaishou(real_url):
        yield {"type":"progress","pct":5,"msg":"解析快手影片..."}
        try:
            cdn = hint_cdn
            use_title = title
            if not cdn:
                ks_info = await _get_kuaishou_cdn(real_url)
                cdn = ks_info.get("cdn_url") or ""
                use_title = ks_info.get("title") or title
            if not cdn:
                yield {"type":"error","message":"無法取得快手影片 CDN（可能需要登入，請在設定頁面貼上快手 Cookies）"}
                return
            safe = re.sub(r'[\\/:*?"<>|]', '_', use_title)[:60]
            fpath = out_dir / f"{safe}.mp4"
            ks_h = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer":"https://www.kuaishou.com/"}
            yield {"type":"progress","pct":10,"msg":"下載快手影片..."}
            async for evt in httpx_dl(cdn, fpath, ks_h, 10, 95): yield evt
            sz = fpath.stat().st_size if fpath.exists() else 0
            if sz > 50000:
                yield {"type":"done","filename":fpath.name,"saved_dir":str(out_dir),
                       "size_mb":round(sz/1024/1024,1)}
                return
            yield {"type":"error","message":"下載失敗，CDN 連結可能已過期，請重新解析"}
        except Exception as ex:
            yield {"type":"error","message":f"快手下載失敗：{ex}"}
        return

    # ══ 蝦皮短影音 ══════════════════════════════════════════════
    if _is_shopee_url(real_url):
        yield {"type": "progress", "pct": 5, "msg": "解析蝦皮影片（重新取 CDN）..."}
        try:
            # 蝦皮 CDN URL 有時效性，下載時永遠重新解析以確保連結有效
            use_title = title
            shopee_info = await _get_shopee_video_info(real_url)
            vurl = shopee_info.get("video_url") or hint_cdn
            use_title = shopee_info.get("title") or title
            if not vurl:
                yield {"type": "error", "message": "無法解析蝦皮影片網址，請確認連結是否為短影音分享連結"}
                return
            safe = re.sub(r'[\\/:*?"<>|]', '_', use_title)[:60]
            fpath = out_dir / f"{safe}.mp4"
            sp_h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Referer": "https://shopee.tw/"}
            yield {"type": "progress", "pct": 10, "msg": "下載蝦皮影片..."}
            async for evt in httpx_dl(vurl, fpath, sp_h, 10, 95): yield evt
            sz = fpath.stat().st_size if fpath.exists() else 0
            if sz > 50000:
                yield {"type": "done", "filename": fpath.name, "saved_dir": str(out_dir),
                       "size_mb": round(sz / 1024 / 1024, 1)}
                return
            yield {"type": "error", "message": "下載失敗，CDN 連結可能已過期，請重新貼上連結"}
        except Exception as ex:
            yield {"type": "error", "message": f"蝦皮下載失敗：{ex}"}
        return

    # ══ B站 / Lux ════════════════════════════════════════════════
    if _is_lux_platform(real_url):
        yield {"type":"progress","pct":5,"msg":"啟動 Lux..."}
        before = set(out_dir.glob("*"))
        lux_done, lux_err = [], []
        # quality 轉為 Lux -f 參數（B站格式 id）
        lux_fmt_args = ["-f", quality] if quality and quality not in ("best", "h1080", "h720", "h480", "h360") else []
        def _lux_run():
            try:
                cmd = [str(LUX_PATH),"-o",str(out_dir)] + lux_fmt_args + [real_url]
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
                lux_done.append(r)
            except Exception as ex: lux_err.append(str(ex))
        fut = loop.run_in_executor(executor, _lux_run)
        pct = 10
        wait_count = 0
        while not fut.done():
            await asyncio.sleep(2)
            wait_count += 1
            # 前段快速上升，後段緩慢趨近 95（讓用戶知道還在跑）
            if pct < 60:
                pct = min(60, pct + 8)
            elif pct < 85:
                pct = min(85, pct + 3)
            elif pct < 93:
                pct = min(93, pct + 1)
            # 超過 3 分鐘仍未完成，提示說明
            msg = "Lux 下載中..." if wait_count < 90 else "Lux 合併中（大檔案需較長時間）..."
            yield {"type":"progress","pct":pct,"msg":msg}
        if lux_err:
            yield {"type":"error","message":f"Lux 失敗：{lux_err[0]}"}; return
        after = set(out_dir.glob("*"))
        new_files = [f for f in (after-before) if f.suffix.lower() in (".mp4",".mkv",".flv",".webm",".m4v")]
        if not new_files:
            vids = sorted([f for f in out_dir.iterdir() if f.suffix.lower() in (".mp4",".mkv",".flv",".webm")],
                          key=lambda x: x.stat().st_mtime, reverse=True)
            if not vids: yield {"type":"error","message":"Lux 下載失敗，無輸出檔案"}; return
            new_files = [vids[0]]
        yield {"type":"done","filename":new_files[0].name,"saved_dir":str(out_dir),"size_mb":round(new_files[0].stat().st_size/1024/1024,1)}
        return

    # ══ 通用快速路徑：有 hint_cdn 時直接 httpx 下載（跳過 yt-dlp）══
    if hint_cdn:
        yield {"type":"progress","pct":5,"msg":"下載影片..."}
        safe = re.sub(r'[\\/:*?"<>|]', '_', title)[:60]
        gen_h = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Referer": re.sub(r'(https?://[^/]+).*', r'\1', real_url) or "https://www.google.com/",
        }
        if hint_audio:
            vt = out_dir/f"{safe}_v.mp4"; at = out_dir/f"{safe}_a.m4a"; final = out_dir/f"{safe}.mp4"
            yield {"type":"progress","pct":8,"msg":"下載影片軌..."}
            async for evt in httpx_dl(hint_cdn, vt, gen_h, 8, 55): yield evt
            yield {"type":"progress","pct":57,"msg":"下載音訊軌..."}
            async for evt in httpx_dl(hint_audio, at, gen_h, 57, 83): yield evt
            yield {"type":"progress","pct":85,"msg":"合併音訊..."}
            ffmerge(vt, at, final)
            if not final.exists():
                async for evt in httpx_dl(hint_cdn, final, gen_h, 85, 98): yield evt
        else:
            final = out_dir/f"{safe}.mp4"
            async for evt in httpx_dl(hint_cdn, final, gen_h, 5, 95): yield evt
        sz = final.stat().st_size if final.exists() else 0
        if sz > 50000:
            yield {"type":"done","filename":final.name,"saved_dir":str(out_dir),"size_mb":round(sz/1024/1024,1)}
            return
        yield {"type":"progress","pct":2,"msg":"CDN URL 已過期，改用 yt-dlp 重新下載..."}

    # ══ 其他平台（yt-dlp）════════════════════════════════════════
    yield {"type":"progress","pct":2,"msg":"初始化下載..."}
    safe = re.sub(r'[\\/:*?"<>|]', '_', title)[:60]
    # quality → yt-dlp format selector
    _h = re.search(r'h(\d+)', quality)
    if _h:
        _hv = _h.group(1)
        _fmt = f"bestvideo[height<={_hv}][ext=mp4]+bestaudio[ext=m4a]/best[height<={_hv}][ext=mp4]/best[height<={_hv}]"
    else:
        _fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    opts = {"format": _fmt,
            "outtmpl":str(out_dir/f"{safe}.%(ext)s"),"quiet":True,"no_warnings":True,
            "merge_output_format":"mp4","concurrent_fragment_downloads":8,
            "updatetime":False,
            # 清除 YouTube 原始上傳日期 metadata，否則 iOS Photos 會用舊日期排序影片
            "postprocessor_args":{"default":["-map_metadata","-1"]}}
    res2, err2 = [], []
    async for evt in ytdlp_dl(opts, real_url, res2, err2): yield evt
    if err2: yield {"type":"error","message":err2[0]}; return
    if res2:
        sz = round(Path(res2[0]).stat().st_size/1024/1024,1) if Path(res2[0]).exists() else 0
        yield {"type":"done","filename":Path(res2[0]).name,"saved_dir":str(out_dir),"size_mb":sz}
    else:
        yield {"type":"error","message":"下載失敗，無輸出檔案"}


def open_browser():
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:7788")

from search_module import router as search_router
app.include_router(search_router)

if __name__ == "__main__":
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=7788)
