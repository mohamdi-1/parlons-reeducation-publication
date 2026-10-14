# Runs in GitHub Actions a little before the posting time: publishes due Instagram posts from queue.json.
# Secrets (never printed): PAGE_TOKEN, IG_USER_ID. Env: GRAPH_VERSION, LEAD_MINUTES, GITHUB_TOKEN, GITHUB_REPOSITORY.
import base64, datetime as dt, json, os, sys, time, requests

V = os.environ.get("GRAPH_VERSION", "v25.0"); TOK = os.environ["PAGE_TOKEN"]; IG = os.environ["IG_USER_ID"]
G = "https://graph.facebook.com/" + V
LEAD = int(os.environ.get("LEAD_MINUTES", "35")); GRACE_H = 36; MAX_TRIES = 3
REPO = os.environ["GITHUB_REPOSITORY"]; GH = requests.Session()
GH.headers.update({"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"], "Accept": "application/vnd.github+json"})
now = dt.datetime.now(dt.timezone.utc)

def gh_get(path):
    r = GH.get("https://api.github.com/repos/%s/contents/%s" % (REPO, path))
    if r.status_code == 404: return None, None
    r.raise_for_status(); j = r.json(); return json.loads(base64.b64decode(j["content"])), j["sha"]

def gh_put(path, obj, msg):
    _, sha = gh_get(path); body = {"message": msg, "content": base64.b64encode(json.dumps(obj, ensure_ascii=False, indent=1).encode()).decode()}
    if sha: body["sha"] = sha
    GH.put("https://api.github.com/repos/%s/contents/%s" % (REPO, path), json=body).raise_for_status()

class Fail(Exception): pass

def chk(r):
    try: j = r.json()
    except ValueError: raise Fail("HTTP %s %s" % (r.status_code, r.text[:200]))
    if r.status_code >= 400 or "error" in j: e = j.get("error", {}); raise Fail("%s (code %s)" % (e.get("message", r.text[:200]), e.get("code")))
    return j

def post(path, data): data = dict(data); data["access_token"] = TOK; return chk(requests.post(G + path, data=data, timeout=120))

def wait_ready(cid, limit=420):
    t0 = time.time()
    while time.time() - t0 < limit:
        j = chk(requests.get(G + "/" + cid, params={"fields": "status_code,status", "access_token": TOK}, timeout=60))
        s = j.get("status_code")
        if s == "FINISHED": return
        if s in ("ERROR", "EXPIRED"): raise Fail("container %s: %s" % (s, j.get("status", "")))
        time.sleep(10)
    raise Fail("container not ready after %d s" % limit)

def reel(it):
    base = {"media_type": "REELS", "caption": it["caption"], "share_to_feed": "true"}
    if it.get("cover"): base["cover_url"] = it["cover"]
    try:  # A: resumable upload of the original file (most reliable)
        c = post("/%s/media" % IG, dict(base, upload_type="resumable"))
        data = requests.get(it["video_url"], timeout=600).content
        r = requests.post("https://rupload.facebook.com/ig-api-upload/%s/%s" % (V, c["id"]), data=data,
                          headers={"Authorization": "OAuth " + TOK, "offset": "0", "file_size": str(len(data))}, timeout=900)
        chk(r); cid = c["id"]; print("  uploaded (resumable)")
    except Fail as e:  # B: let Instagram fetch the public file
        print("  resumable upload not available (%s) -> video_url" % e)
        cid = post("/%s/media" % IG, dict(base, video_url=it["video_url"]))["id"]
    wait_ready(cid); return cid

def image(it):
    cid = post("/%s/media" % IG, {"image_url": it["images"][0], "caption": it["caption"]})["id"]; wait_ready(cid, 120); return cid

def carousel(it):
    kids = []
    for u in it["images"]:
        k = post("/%s/media" % IG, {"image_url": u, "is_carousel_item": "true"})["id"]; wait_ready(k, 120); kids.append(k)
    cid = post("/%s/media" % IG, {"media_type": "CAROUSEL", "children": ",".join(kids), "caption": it["caption"]})["id"]; wait_ready(cid, 180); return cid

def main():
    q, _ = gh_get("queue.json"); st, _ = gh_get("state.json")
    q = q or {"items": []}; st = st or {"published": {}, "failed": {}}
    due = []
    for it in q["items"]:
        if it["id"] in st["published"]: continue
        at = dt.datetime.strptime(it["publish_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        if at > now + dt.timedelta(minutes=LEAD): continue
        f = st["failed"].get(it["id"], {})
        if at < now - dt.timedelta(hours=GRACE_H):
            if f.get("error") != "missed": st["failed"][it["id"]] = {"error": "missed", "tries": f.get("tries", 0)}; gh_put("state.json", st, "missed " + it["id"])
            continue
        if f.get("tries", 0) >= MAX_TRIES: continue
        due.append(it)
    print("due now:", [x["id"] for x in due] or "nothing")
    for it in due:
        print("->", it["id"], it["kind"])
        try:
            cid = {"reel": reel, "image": image, "carousel": carousel}[it["kind"]](it)
            mid = post("/%s/media_publish" % IG, {"creation_id": cid})["id"]
            st["published"][it["id"]] = {"media_id": mid, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
            st["failed"].pop(it["id"], None); print("  PUBLISHED", mid)
        except Exception as e:
            f = st["failed"].get(it["id"], {}); st["failed"][it["id"]] = {"error": str(e)[:300], "tries": f.get("tries", 0) + 1, "last": now.isoformat(timespec="seconds")}
            print("  FAILED:", str(e)[:300])
        gh_put("state.json", st, "instagram " + it["id"])
    if any(it["id"] in st["failed"] and st["failed"][it["id"]].get("error") != "missed" for it in due): sys.exit(1)

if __name__ == "__main__": main()
