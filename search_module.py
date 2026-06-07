"""AI Search Tool - Module 1 (Text Search) + Module 2 (Image Search)"""
import os, re, json, time, asyncio, io
import httpx, yt_dlp
from fastapi import APIRouter, UploadFile, File, Form, Query
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api", tags=["search"])

YT_API_KEY = os.environ.get("YT_API_KEY", "AIzaSyDUCQ0mQqwcqRLSDa0C2E79qioJYhtZB4A")
CLIP_AVAILABLE = False
try:
    import clip; import torch; CLIP_AVAILABLE = True
except: pass

async def expand_keywords(keyword):
    kws = [keyword]
    for lang in ["en","ja","ko"]:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get("https://translate.googleapis.com/translate_a/single",
                    params={"client":"gtx","sl":"auto","tl":lang,"dt":"t","q":keyword})
                if r.status_code == 200:
                    t = "".join(p[0] for p in r.json()[0] if p[0])
                    if t and t.lower() != keyword.lower(): kws.append(t)
        except: pass
    return list(set(kws))

async def search_youtube(kw, count=8):
    results = []
    try:
        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL({"quiet":True,"no_warnings":True}) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(f"ytsearch{count}:{kw}", download=False))
            if info and "entries" in info:
                for e in info["entries"][:count]:
                    if e:
                        results.append({
                            "id": e.get("id",""), "title": e.get("title",""),
                            "author": e.get("uploader",""), "thumbnail": e.get("thumbnail",""),
                            "url": f"https://youtu.be/{e.get('id','')}", "platform":"YouTube"})
    except Exception as ex:
        print(f"[YT Search Error] {ex}")
    return results

async def search_bilibili(kw, count=8):
    results = []
    try:
        async with httpx.AsyncClient(timeout=8, headers={"User-Agent":"Mozilla/5.0","Referer":"https://www.bilibili.com/"}) as c:
            r = await c.get("https://api.bilibili.com/x/web-interface/search/type", params={"search_type":"video","keyword":kw,"page":1,"page_size":count})
        if r.status_code == 200:
            for item in r.json().get("data",{}).get("result",[])[:count]:
                t = item.get("title","").replace("<em>","").replace("</em>","")
                p = f"https://i0.hdslb.com/bfs/archive/{item.get('pic','')}" if item.get("pic") else ""
                results.append({"id":str(item.get("aid","")),"title":t,"author":item.get("author",""),"thumbnail":p,"url":f"https://www.bilibili.com/video/av{item.get('aid','')}","platform":"Bilibili"})
    except: pass
    return results

async def search_facebook(kw, count=8):
    """Facebook search - needs IPRoyal proxy"""
    return []  # TODO: implement with IPRoyal

async def search_instagram(kw, count=8):
    """Instagram search - needs IPRoyal proxy"""
    return []  # TODO: implement with IPRoyal

PLATFORMS = {"youtube":search_youtube,"bilibili":search_bilibili,"facebook":search_facebook,"instagram":search_instagram}

@router.post("/search")
async def search(keyword: str = Form(...), platforms: str = Form("all")):
    start = time.time()
    kw = keyword.strip()
    if not kw: return {"success":False,"error":"no keyword"}
    
    # Only expand for non-English keywords, limit expansion
    kws = [kw]
    if re.search(r'[\u4e00-\u9fff]', kw):  # Chinese chars detected
        kws = await expand_keywords(kw)
        kws = kws[:2]  # Limit to 2 keywords max for speed
    
    sel = list(PLATFORMS.keys()) if platforms=="all" else [p for p in platforms.split(",") if p in PLATFORMS]
    all_r, seen = [], set()
    for k in kws:
        for r in await asyncio.gather(*[PLATFORMS[p](k, 8) for p in sel], return_exceptions=True):
            if isinstance(r, list):
                for item in r:
                    u = item.get("url","")
                    if u and u not in seen: seen.add(u); all_r.append(item)
    return {"success":True,"keyword":kw,"count":len(all_r),"results":all_r[:50],"elapsed":round(time.time()-start,2)}

@router.post("/search-by-image")
async def search_by_image(file: UploadFile = File(...)):
    start = time.time()
    image_bytes = await file.read()
    text = ""
    if CLIP_AVAILABLE:
        try:
            from PIL import Image
            model, processor = clip.load("ViT-B/32", device="cpu")
            img = processor(Image.open(io.BytesIO(image_bytes)).convert("RGB")).unsqueeze(0)
            clip_desc = ["a handbag","a backpack","shoes","a dress","a shirt","a jacket","a hat","watch","a phone","a laptop","a car","a cat","a dog","food","a book","furniture","makeup","sports","beach","mountain","cosmetics","jewelry","sunglasses","camera","headphones","bicycle","pizza","cake","plant","guitar","tablet","toys"]
            with torch.no_grad():
                sim = (100.0 * model.encode_image(img) @ model.encode_text(clip.tokenize(clip_desc)).T).softmax(dim=-1)
                text = clip_desc[sim[0].argmax().item()]
        except: pass
    if not text: return {"success":False,"error":"Unable to identify image"}
    
    kws = await expand_keywords(text)
    all_r, seen = [], set()
    for kw in kws[:2]:
        for r in await asyncio.gather(*[PLATFORMS[p](kw, 8) for p in ["youtube","bilibili"]], return_exceptions=True):
            if isinstance(r, list):
                for item in r:
                    u = item.get("url","")
                    if u and u not in seen: seen.add(u); all_r.append(item)
    return {"success":True,"clip_text":text,"count":len(all_r),"results":all_r[:30],"elapsed":round(time.time()-start,2)}

@router.get("/search-health")
async def search_health():
    return {"status":"ok","clip":CLIP_AVAILABLE,"platforms":list(PLATFORMS.keys())}
