"""AI Search Tool - Module 1 (Text Search) + Module 2 (Image Search)"""
import os, re, json, time, asyncio, io
import httpx
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
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://translate.googleapis.com/translate_a/single",
                    params={"client":"gtx","sl":"auto","tl":lang,"dt":"t","q":keyword})
                if r.status_code == 200:
                    t = "".join(p[0] for p in r.json()[0] if p[0])
                    if t and t.lower() != keyword.lower(): kws.append(t)
        except: pass
    return list(set(kws))

async def search_youtube(kw, count=10):
    results = []
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://www.googleapis.com/youtube/v3/search", params={
                "part":"snippet","q":kw,"maxResults":count,"type":"video","key":YT_API_KEY})
        if r.status_code == 200:
            for item in r.json().get("items",[]):
                s = item.get("snippet",{}); vid = item.get("id",{}).get("videoId","")
                thumb = s.get("thumbnails",{}).get("high",{}).get("url",s.get("thumbnails",{}).get("medium",{}).get("url",""))
                results.append({"id":vid,"title":s.get("title",""),"author":s.get("channelTitle",""),"thumbnail":thumb,"url":f"https://youtu.be/{vid}","platform":"YouTube"})
    except: pass
    return results

async def search_bilibili(kw, count=10):
    results = []
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent":"Mozilla/5.0","Referer":"https://www.bilibili.com/"}) as c:
            r = await c.get("https://api.bilibili.com/x/web-interface/search/type", params={"search_type":"video","keyword":kw,"page":1,"page_size":count})
        if r.status_code == 200:
            for item in r.json().get("data",{}).get("result",[])[:count]:
                t = item.get("title","").replace("<em>","").replace("</em>","")
                p = f"https://i0.hdslb.com/bfs/archive/{item.get('pic','')}" if item.get("pic") else ""
                results.append({"id":str(item.get("aid","")),"title":t,"author":item.get("author",""),"thumbnail":p,"url":f"https://www.bilibili.com/video/av{item.get('aid','')}","platform":"Bilibili"})
    except: pass
    return results

async def search_dailymotion(kw, count=10):
    results = []
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.dailymotion.com/videos", params={"search":kw,"limit":count,"fields":"id,title,owner.username,thumbnail_360_url,url"})
        if r.status_code == 200:
            for item in r.json().get("list",[]):
                results.append({"id":item.get("id",""),"title":item.get("title",""),"author":item.get("owner.username",""),"thumbnail":item.get("thumbnail_360_url",""),"url":item.get("url",""),"platform":"Dailymotion"})
    except: pass
    return results

async def search_peertube(kw, count=8):
    results = []
    for inst in ["https://tube.sh","https://peertube.tv"]:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(inst+"/api/v1/search/videos", params={"search":kw,"count":count})
            if r.status_code == 200:
                for item in r.json().get("data",[])[:count]:
                    results.append({"id":item.get("uuid",""),"title":item.get("name",""),"author":item.get("account",{}).get("displayName",""),"thumbnail":item.get("thumbnailUrl",""),"url":f"{inst}/w/{item.get('uuid','')}","platform":"PeerTube"})
                break
        except: continue
    return results

PLATFORMS = {"youtube":search_youtube,"bilibili":search_bilibili,"dailymotion":search_dailymotion,"peertube":search_peertube}
PLATFORM_NAMES = {"youtube":"YouTube","bilibili":"BiliBili","dailymotion":"Dailymotion","peertube":"PeerTube"}

@router.post("/search")
async def search(keyword: str = Form(...), platforms: str = Form("all")):
    start = time.time()
    kws = await expand_keywords(keyword.strip())
    sel = list(PLATFORMS.keys()) if platforms=="all" else [p for p in platforms.split(",") if p in PLATFORMS]
    all_r, seen = [], set()
    for kw in kws:
        for r in await asyncio.gather(*[PLATFORMS[p](kw, 8) for p in sel], return_exceptions=True):
            if isinstance(r, list):
                for item in r:
                    u = item.get("url","")
                    if u and u not in seen: seen.add(u); all_r.append(item)
    return {"success":True,"keyword":keyword,"count":len(all_r),"results":all_r[:50],"elapsed":round(time.time()-start,2)}

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
            clip_desc = ["a handbag","a backpack","shoes","a dress","a shirt","a jacket","a hat","watch","a phone","a laptop","a car","a cat","a dog","food","a book","furniture","makeup","sports","beach","mountain"]
            with torch.no_grad():
                sim = (100.0 * model.encode_image(img) @ model.encode_text(clip.tokenize(clip_desc)).T).softmax(dim=-1)
                text = clip_desc[sim[0].argmax().item()]
        except: pass
    if not text: return {"success":False,"error":"Unable to identify image"}
    
    kws = await expand_keywords(text)
    all_r, seen = [], set()
    for kw in kws:
        for r in await asyncio.gather(*[PLATFORMS[p](kw, 8) for p in ["youtube","bilibili","dailymotion"]], return_exceptions=True):
            if isinstance(r, list):
                for item in r:
                    u = item.get("url","")
                    if u and u not in seen: seen.add(u); all_r.append(item)
    return {"success":True,"clip_text":text,"count":len(all_r),"results":all_r[:30],"elapsed":round(time.time()-start,2)}

@router.get("/search-health")
async def search_health():
    return {"status":"ok","clip":CLIP_AVAILABLE,"platforms":list(PLATFORMS.keys())}
