"""GitHub Actions 에서 실행: 시간이 된 예약 릴스를 인스타에 게시 (한 번 실행에 최대 1개).

- queue/items/<id>.json : 쇼츠 스튜디오가 만든 예약 (언제·영상 주소·문구)
- queue/done/<id>.json  : 결과 (완료 / 실패) — 이 파일이 있으면 다시 시도하지 않음
- 실패해도 자동으로 여러 번 재시도하지 않음 (인스타 'API 차단'을 피하려고). 영상 주소가 아직
  안 열리는 경우만 다음 실행 때 다시 확인.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

TOKEN = os.environ.get("IG_ACCESS_TOKEN", "").strip()
USER = os.environ.get("IG_USER_ID", "").strip()
BASE = "https://graph.instagram.com" if TOKEN.startswith("IG") else "https://graph.facebook.com/v23.0"
ITEMS, DONE = Path("queue/items"), Path("queue/done")
NOW = datetime.now(timezone.utc)


class IGError(RuntimeError):
    pass


def call(method, path, **params):
    params["access_token"] = TOKEN
    fn = requests.post if method == "post" else requests.get
    r = fn(f"{BASE}/{path}", **({"data": params} if method == "post" else {"params": params}), timeout=60)
    d = r.json()
    if "error" in d:
        raise IGError(d["error"].get("message", str(d))[:300])
    return d


def post_reel(item):
    def once(cover):
        extra = {"cover_url": cover} if cover else {}
        c = call("post", f"{USER}/media", media_type="REELS", video_url=item["video_url"],
                 caption=item["caption"], share_to_feed="true", **extra)["id"]
        for _ in range(60):  # 최대 5분 처리 대기
            st = call("get", c, fields="status_code").get("status_code")
            if st == "FINISHED":
                return call("post", f"{USER}/media_publish", creation_id=c)["id"]
            if st in ("ERROR", "EXPIRED"):
                raise IGError(f"인스타 영상 처리 실패: {st}")
            time.sleep(5)
        raise IGError("인스타 영상 처리 시간 초과")
    if item.get("cover_url"):
        try:
            return once(item["cover_url"])
        except IGError as e:
            if "block" in str(e).lower():
                raise
            print("cover failed, retry without cover:", e)
    return once(None)


def live(url):
    try:
        return requests.head(url, timeout=20, allow_redirects=True).status_code == 200
    except requests.RequestException:
        return False


def finish(item, **result):
    DONE.mkdir(parents=True, exist_ok=True)
    result["finished"] = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="minutes")
    (DONE / f"{item['id']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    if result["status"] == "완료":  # 올린 영상은 저장소에서 지움 (용량 관리)
        for k in ("video_file", "cover_file"):
            if item.get(k):
                Path(item[k]).unlink(missing_ok=True)


def main():
    if not ITEMS.exists():
        return
    due = []
    for f in sorted(ITEMS.glob("*.json")):
        if (DONE / f.name).exists():
            continue
        item = json.loads(f.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(item["at"])
        if at <= NOW:
            due.append((at, item))
    if not due:
        print("nothing due")
        return
    at, item = sorted(due, key=lambda x: x[0])[0]
    late = (NOW - at).total_seconds() / 3600
    if late > 24:
        finish(item, status="실패", error="예약 시간이 24시간 넘게 지나서 게시하지 않았어요")
        return
    if not TOKEN or not USER:
        finish(item, status="실패", error="GitHub 비밀 값(IG_ACCESS_TOKEN/IG_USER_ID)이 없어요 → 스튜디오에서 예약을 한 번 더 하면 자동 설정돼요")
        return
    if not live(item["video_url"]):
        if late > 2:
            finish(item, status="실패", error="영상 주소가 열리지 않아요 (번호 페이지 저장소 확인)")
        else:
            print("video not live yet, retry next run")
        return
    try:
        mid = post_reel(item)
        finish(item, status="완료", media_id=mid)
        print("published", item["id"], mid)
    except Exception as e:
        finish(item, status="실패", error=str(e)[:300])
        print("failed", item["id"], e)


if __name__ == "__main__":
    main()
