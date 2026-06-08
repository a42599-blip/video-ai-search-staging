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

async def search_douyin(kw, count=8):
    """抖音搜索"""
    results = []
    try:
        headers = {"User-Agent":"Mozilla/5.0","Referer":"https://www.douyin.com/"}
        async with httpx.AsyncClient(timeout=10, headers=headers) as c:
            r = await c.get("https://www.douyin.com/aweme/v1/web/general/search/single/",
                params={"keyword":kw,"type":1,"count":count,"offset":0})
        if r.status_code == 200:
            for item in r.json().get("data",[])[:count]:
                v = item.get("video",{}); a = item.get("author",{})
                results.append({"id":item.get("aweme_id",""),"title":item.get("desc",""),"author":a.get("nickname",""),
                    "thumbnail":v.get("cover",{}).get("url_list",[""])[0],
                    "url":f"https://www.douyin.com/video/{item.get('aweme_id','')}","platform":"抖音"})
    except: pass
    return results

async def search_tiktok(kw, count=8):
    """TikTok search"""
    results = []
    try:
        headers = {"User-Agent":"Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=10, headers=headers) as c:
            r = await c.get("https://www.tiktok.com/api/search/item/full/",
                params={"keyword":kw,"count":count})
        if r.status_code == 200:
            for item in r.json().get("data",[])[:count]:
                results.append({"id":item.get("id",""),"title":item.get("desc",""),
                    "author":item.get("author",{}).get("nickname",""),
                    "thumbnail":item.get("video",{}).get("cover",""),
                    "url":f"https://www.tiktok.com/@{item.get('author',{}).get('unique_id','')}/video/{item.get('id','')}","platform":"TikTok"})
    except: pass
    return results

async def search_xiaohongshu(kw, count=8):
    """小紅書搜索"""
    results = []
    try:
        headers = {"User-Agent":"Mozilla/5.0","Referer":"https://www.xiaohongshu.com/"}
        async with httpx.AsyncClient(timeout=10, headers=headers) as c:
            r = await c.post("https://edith.xiaohongshu.com/api/sns/web/v1/search/notes",
                json={"keyword":kw,"page":1,"page_size":count})
        if r.status_code == 200:
            for item in r.json().get("data",{}).get("items",[])[:count]:
                n = item.get("note_card",{})
                results.append({"id":n.get("note_id",""),"title":n.get("title",""),
                    "author":n.get("user",{}).get("nickname",""),
                    "thumbnail":n.get("cover",{}).get("url",""),
                    "url":f"https://www.xiaohongshu.com/explore/{n.get('note_id','')}","platform":"小紅書"})
    except: pass
    return results

PLATFORMS = {"youtube":search_youtube,"bilibili":search_bilibili,"facebook":search_facebook,"instagram":search_instagram,
             "douyin":search_douyin,"tiktok":search_tiktok,"xiaohongshu":search_xiaohongshu}

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
    
    sel = list(PLATFORMS.keys()) if platforms=="all" else [p for p in platforms.split(",") if p.strip().lower() in PLATFORMS]
    all_r, seen = [], set()
    for k in kws:
        for r in await asyncio.gather(*[PLATFORMS[p](k, 8) for p in sel], return_exceptions=True):
            if isinstance(r, list):
                for item in r:
                    u = item.get("url","")
                    if u and u not in seen: seen.add(u); all_r.append(item)
    return {"success":True,"keyword":kw,"count":len(all_r),"results":all_r[:50],"elapsed":round(time.time()-start,2)}

@router.post("/search-by-image")
async def search_by_image(file: UploadFile = File(...), count: int = Form(50)):
    start = time.time()
    image_bytes = await file.read()
    text = ""
    if CLIP_AVAILABLE:
        try:
            from PIL import Image
            model, processor = clip.load("ViT-B/32", device="cpu")
            img = processor(Image.open(io.BytesIO(image_bytes)).convert("RGB")).unsqueeze(0)
            clip_desc = [
                # Furniture & Home
                "a chair","an armchair","a sofa","a couch","a table","a desk","a bed","a bookshelf",
                "a cabinet","a drawer","a lamp","a rug","a curtain","a pillow","a mirror",
                "a door","a window","a wall","a floor","a ceiling","a staircase",
                # Electronics
                "a smartphone","a mobile phone","an iPhone","an Android phone","a laptop","a computer",
                "a tablet","an iPad","a TV","a monitor","a screen","a keyboard","a mouse",
                "headphones","earphones","a speaker","a camera","a webcam","a printer",
                # Clothing & Accessories
                "a dress","a shirt","a t-shirt","a blouse","a jacket","a coat","a sweater","a hoodie",
                "a pair of pants","jeans","shorts","a skirt","a suit","a tie",
                "shoes","sneakers","boots","sandals","high heels","slippers",
                "a hat","a cap","a scarf","gloves","sunglasses","a watch","a necklace",
                "a ring","earrings","a bracelet","a backpack","a handbag","a purse","a wallet","a suitcase",
                # Food & Drink
                "pizza","a burger","a sandwich","a hot dog","french fries","fried chicken",
                "pasta","spaghetti","rice","noodles","soup","salad",
                "a cake","a cupcake","a cookie","a donut","ice cream","chocolate","candy",
                "an apple","a banana","an orange","a strawberry","a watermelon","grapes",
                "coffee","tea","juice","water","a bottle","a glass","a cup","a bowl","a plate",
                # Animals
                "a cat","a dog","a bird","a fish","a horse","a cow","a pig","a sheep",
                "a rabbit","a hamster","a turtle","a snake","a lizard","a frog",
                "a butterfly","a bee","a ladybug","a spider","an ant",
                # Vehicles
                "a car","a truck","a bus","a motorcycle","a bicycle","a scooter",
                "a train","a subway","an airplane","a helicopter","a boat","a ship",
                # Sports & Activities
                "a ball","a soccer ball","a basketball","a baseball","a tennis ball",
                "a football","a volleyball","a golf ball","a bowling ball",
                "a racket","a bat","a glove","a helmet","a skateboard","a surfboard",
                "a bicycle","a gym","a workout","yoga","running","swimming",
                # Nature & Outdoors
                "a tree","a flower","a plant","a leaf","grass","a forest",
                "a mountain","a hill","a river","a lake","an ocean","a beach","a waterfall",
                "a garden","a park","a path","a trail",
                # Buildings & Places
                "a house","a building","an apartment","a school","a hospital","a church",
                "a store","a restaurant","a cafe","a office","a factory",
                "a bridge","a road","a street","a highway","a parking lot",
                # People & Body
                "a person","a man","a woman","a child","a baby","a couple","a family",
                "a hand","an eye","a face","a smile","a portrait","a selfie",
                "a group of people","a crowd","a dancer","a singer","a musician",
                # Beauty & Personal Care
                "makeup","lipstick","foundation","eyeshadow","mascara","perfume",
                "cosmetics","a mirror","a brush","a comb","scissors",
                # Stationery & Office
                "a book","a notebook","a magazine","a newspaper","a pen","a pencil",
                "scissors","a ruler","a stapler","paper","a document","a folder",
                # Toys & Hobbies
                "a toy","a doll","a teddy bear","a lego","a puzzle","a board game",
                "a video game","a controller","a guitar","a piano","a drum","a microphone",
                # Tools & Household
                "a hammer","a screwdriver","a wrench","a drill","a saw","a knife",
                "a spoon","a fork","a pot","a pan","a cutting board",
                # Bags & Luggage
                "a backpack","a handbag","a tote bag","a duffel bag","a suitcase",
                # Miscellaneous
                "a gift","a present","a box","a bag","a basket",
                "a clock","a calendar","a map","a sign","a poster","a painting",
                "a fire","a candle","a light bulb","a key","a lock",
                "a umbrella","a trash can","a bucket","a ladder",
            ]
            with torch.no_grad():
                sim = (100.0 * model.encode_image(img) @ model.encode_text(clip.tokenize(clip_desc)).T).softmax(dim=-1)
                text = clip_desc[sim[0].argmax().item()]
        except: pass
    if not text: return {"success":False,"error":"Unable to identify image"}
    
    kws = await expand_keywords(text)
    all_r, seen = [], set()
    for kw in kws[:2]:
        for r in await asyncio.gather(*[PLATFORMS[p](kw, count) for p in ["youtube","bilibili","facebook","instagram","douyin","tiktok","xiaohongshu"]], return_exceptions=True):
            if isinstance(r, list):
                for item in r:
                    u = item.get("url","")
                    if u and u not in seen: seen.add(u); all_r.append(item)
    return {"success":True,"clip_text":text,"count":len(all_r),"results":all_r[:30],"elapsed":round(time.time()-start,2)}

@router.get("/search-health")
async def search_health():
    return {"status":"ok","clip":CLIP_AVAILABLE,"platforms":list(PLATFORMS.keys())}
