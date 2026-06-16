# -*- coding: utf-8 -*-
"""万词 Phase 1 月度刷新服务 (Zeabur)
n8n 月度 cron -> POST /sorftime/backfill (fire-and-forget, 绕开 165s 网关)
后台线程用 Sorftime KeywordRequest 回填两个作战台表1 的月搜量/词级/需供比/CVR/CPC/更新日, 完成发飞书给 Frankie.
所有密钥走 env, 不入 repo.
"""
import os, json, time, datetime, urllib.request
from fastapi import FastAPI, BackgroundTasks, Header, HTTPException

app = FastAPI()

SFT_KEY   = os.environ.get("SORFTIME_KEY", "")
FS_APP    = os.environ.get("FEISHU_APP_ID", "")
FS_SEC    = os.environ.get("FEISHU_SECRET", "")
AUTH      = os.environ.get("BACKFILL_TOKEN", "")
NOTIFY_ID = os.environ.get("NOTIFY_OPENID", "")  # Frankie 聪哥1号 open_id

REG_APP = os.environ.get("REG_APP", "W8LPboJSMaVqlwsizQ8cPVDIn2c")  # 万词总台
REG_TBL = os.environ.get("REG_TBL", "tbl2g78DcPnxWNwO")            # 作战台注册表

def _req(url, body=None, headers=None, method="POST", timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read().decode("utf-8", "replace"))

def fs_tok():
    return _req("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": FS_APP, "app_secret": FS_SEC}, {"Content-Type": "application/json"})["tenant_access_token"]

def fs(tok, method, path, body=None):
    return _req("https://open.feishu.cn" + path, body,
                {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}, method)

def fs_all(tok, app_, tbl):
    out, pt = [], None
    while True:
        p = "/open-apis/bitable/v1/apps/%s/tables/%s/records/search?page_size=500" % (app_, tbl)
        if pt: p += "&page_token=" + pt
        d = fs(tok, "POST", p, {"automatic_fields": False})["data"]
        out += d.get("items", [])
        if d.get("has_more"): pt = d["page_token"]
        else: break
    return out

def sft(keyword, domain=1):
    return _req("https://cli.sorftime.com/api/KeywordRequest?domain=%d" % domain, {"keyword": keyword},
                {"Authorization": "BasicAuth " + SFT_KEY, "Browser": "Cli", "Content-Type": "application/json"})

def fetch_registry(tok):
    out = []
    for it in fs_all(tok, REG_APP, REG_TBL):
        f = it["fields"]
        if txt(f.get("状态")) != "在跑":
            continue
        app_ = txt(f.get("作战台App_token")); t1 = txt(f.get("词库表id"))
        if not app_ or not t1:
            continue
        try: domain = int(f.get("Sorftime_domain") or 1)
        except Exception: domain = 1
        out.append({"name": txt(f.get("产品")) + "-" + txt(f.get("站点")), "app": app_, "t1": t1, "domain": domain})
    return out

def txt(f):
    if isinstance(f, list) and f: return f[0].get("text", "")
    if isinstance(f, str): return f
    return ""

def grade(sv):
    if sv >= 10000: return "大词"
    if sv >= 1000: return "中词"
    return "小词"

def notify(tok, text):
    if not NOTIFY_ID: return
    try:
        fs(tok, "POST", "/open-apis/im/v1/messages?receive_id_type=open_id",
           {"receive_id": NOTIFY_ID, "msg_type": "text", "content": json.dumps({"text": text})})
    except Exception:
        pass

def run_backfill(only_empty=False):
    tok = fs_tok()
    today_ms = int(time.time() * 1000)
    summary = []
    for P in fetch_registry(tok):
        name, app_, tbl, domain = P["name"], P["app"], P["t1"], P["domain"]
        recs = fs_all(tok, app_, tbl)
        filled = nodata = err = 0
        last_left = None
        for it in recs:
            kw = txt(it["fields"].get("关键词")).strip()
            if not kw: continue
            if only_empty and it["fields"].get("月搜索量"): continue
            try:
                r = sft(kw, domain)
            except Exception:
                err += 1; time.sleep(1); continue
            last_left = r.get("RequestLeft", last_left)
            if r.get("Code") != 0:
                nodata += 1; continue
            d = r.get("Data") or {}
            sv = d.get("SearchVolume")
            if not sv:
                nodata += 1; continue
            pc = d.get("ProductCount") or 0
            cvr = d.get("SearchConversionRate"); cpc = d.get("Cpc")
            fields = {"月搜索量": sv, "词级": grade(sv),
                      "需供比": round(sv / pc, 2) if pc else 0, "数据更新日": today_ms}
            if isinstance(cvr, (int, float)): fields["CVR%"] = cvr
            if isinstance(cpc, (int, float)): fields["CPC$"] = round(cpc / 100.0, 2)
            try:
                fs(tok, "PUT", "/open-apis/bitable/v1/apps/%s/tables/%s/records/%s" % (app_, tbl, it["record_id"]),
                   {"fields": fields})
                filled += 1
            except Exception:
                err += 1
            time.sleep(0.25)
        summary.append("%s: 填%d/无ABA%d/错%d" % (name, filled, nodata, err))
    notify(tok, "🟡 [AMZ·P2] 万词月搜量刷新完成 · Sorftime\n" + " | ".join(summary) +
                ("\nSorftime RequestLeft=%s" % last_left if last_left else ""))

@app.get("/")
def health():
    return {"ok": True, "service": "sorftime-wanci-backfill"}

@app.get("/selftest")
def selftest():
    """从 Zeabur IP 同步打 1 次 Sorftime, 验证可达性 + IP 白名单 (1 Rq)"""
    try:
        r = sft("switch 2 dock")
        return {"sorftime_reachable": True, "code": r.get("Code"),
                "request_left": r.get("RequestLeft"),
                "search_volume": (r.get("Data") or {}).get("SearchVolume")}
    except Exception as e:
        return {"sorftime_reachable": False, "error": type(e).__name__ + ": " + str(e)[:200]}

@app.post("/sorftime/backfill")
def backfill(background: BackgroundTasks, authorization: str = Header(None), only_empty: bool = False):
    if AUTH and authorization != "Bearer " + AUTH:
        raise HTTPException(status_code=401, detail="unauthorized")
    background.add_task(run_backfill, only_empty)
    return {"ok": True, "status": "started", "only_empty": only_empty}
