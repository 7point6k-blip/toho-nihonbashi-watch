"""TOHOシネマズ日本橋 アルバイト求人ウォッチャー

公式求人サイト(tohocinemas-recruit.net)を実ブラウザで開き、
劇場名に「日本橋」を含む求人を職種名に関係なく全部拾う。
状態は state.json に保存し、変化があれば ntfy でスマホに通知する。
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin

from playwright.sync_api import sync_playwright

JST = timezone(timedelta(hours=9))
BASE = os.environ.get("TOHO_BASE", "https://tohocinemas-recruit.net").rstrip("/")
TARGET = "日本橋"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
FORCE_TEST = os.environ.get("FORCE_TEST_NOTIFY") == "1"

# 2つの独立した入口から探す（片方の仕様変更・取りこぼしに備える）
SOURCES = [
    ("関東全域の一覧", f"{BASE}/jobfind-pc/area/Kanto/All"),   # 全求人。これを基準にする
    ("中央区の一覧", f"{BASE}/jobfind-pc/area/Kanto/Tokyo/13102"),
    ("関東フリーワード検索", f"{BASE}/jobfind-pc/area/Kanto/All?freeword={quote(TARGET)}"),
]
EMPTY_TEXT = "該当する求人情報公開中店舗がありません"
CLOSE_CONFIRM_RUNS = 2        # 何回連続で消えていたら「掲載終了」とみなすか
FAIL_ALERT_RUNS = 3           # 何回連続で読めなかったら異常通知を出すか
FAIL_REALERT_HOURS = 12       # 異常が続くとき再通知する間隔
HEARTBEAT_DAYS = 7            # 正常稼働の定期報告の間隔
RUN_BUDGET_SEC = 240          # 1回の確認にかける最大時間（超えたら打ち切って必ず記録・通知まで進む）


def now():
    return datetime.now(JST)


def fmt(dt):
    return dt.strftime("%m/%d %H:%M")


def norm(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


# ---------------------------------------------------------------- 取得
JS_EXTRACT = """
() => {
  const out = [];
  document.querySelectorAll('a[href*="/jobfind-pc/job/"]').forEach(a => {
    const dt = a.closest('dt');
    if (!dt) return;                       // 一覧カードの劇場名リンクだけ
    let dd = dt.nextElementSibling;
    while (dd && dd.tagName !== 'DD') dd = dd.nextElementSibling;
    const card = dt.closest('dl') || dt.parentElement;
    out.push({
      theater: a.innerText,
      href: a.getAttribute('href'),
      title: dd ? dd.innerText : '',
      card: card ? card.innerText : ''
    });
  });
  return out;
}
"""


def read_listing(page, label, url):
    """1つの一覧URLを最後のページまで読み、(全求人, 表示件数) を返す。読めなければ例外。"""
    jobs = {}
    total = None
    visited = set()
    next_url = url
    for _ in range(30):  # ページ送りの上限（無限ループ防止）
        if next_url in visited:
            break
        visited.add(next_url)
        page.goto(next_url, wait_until="domcontentloaded", timeout=30000)
        # 一覧の描画完了 = 「◯件あり」か「該当なし」のどちらかが出るまで待つ
        page.wait_for_function(
            "(t) => document.body && (document.body.innerText.includes('件あり') || document.body.innerText.includes(t))",
            arg=EMPTY_TEXT, timeout=20000,
        )
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        if "求人情報一覧" not in body:
            raise RuntimeError(f"{label}: 想定外のページが表示された")
        if EMPTY_TEXT in body and "件あり" not in body:
            return {}, 0
        m = re.search(r"([0-9０-９,]+)\s*件あり", body)
        if not m:
            raise RuntimeError(f"{label}: 件数表示が読めない")
        total = int(norm(m.group(1)).replace(",", ""))
        for item in page.evaluate(JS_EXTRACT):
            mid = re.search(r"/jobfind-pc/job/[^/]+/(\d+)", item["href"] or "")
            if not mid:
                continue
            jid = mid.group(1)
            wage = ""
            mw = re.search(r"(時給|日給|月給)\s*[0-9０-９,，]+円(?:\s*[～~〜-]\s*[0-9０-９,，]+円)?(?:以上)?", item["card"] or "")
            if mw:
                wage = norm(mw.group(0))
            jobs[jid] = {
                "id": jid,
                "theater": norm(item["theater"]),
                "title": norm(item["title"]),
                "wage": wage,
                "url": f"{BASE}/jobfind-pc/job/All/{jid}",
            }
        # 次ページ
        nxt = page.evaluate(
            """() => { const a=[...document.querySelectorAll('a')].find(x => x.innerText.trim().startsWith('次へ') && x.getAttribute('href') && x.getAttribute('href') !== '#'); return a ? a.href : null; }"""
        )
        if not nxt or len(jobs) >= total:
            break
        next_url = urljoin(BASE, nxt)
    if total is None:
        raise RuntimeError(f"{label}: 一覧が読めない")
    if len(jobs) < total:
        raise RuntimeError(f"{label}: 表示件数{total}件に対し{len(jobs)}件しか読めない")
    return jobs, total


def fetch_all():
    """全入口を読む。戻り値: (日本橋の求人dict, 基準(関東全域)が読めたか, 1つでもOKか, エラー文のリスト)"""
    found, errors, ok_count, base_ok = {}, [], 0, False
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)
        deadline = time.monotonic() + RUN_BUDGET_SEC
        for label, url in SOURCES:
            last_err = f"{label}: 時間切れで未確認"
            for attempt in range(3):  # 1入口につき3回まで再試行
                if time.monotonic() > deadline:
                    break
                try:
                    jobs, total = read_listing(page, label, url)
                    hits = {k: v for k, v in jobs.items() if TARGET in v["theater"]}
                    print(f"[OK] {label}: 全{total}件 / 日本橋 {len(hits)}件 {sorted(hits)}")
                    found.update(hits)
                    ok_count += 1
                    if label == SOURCES[0][0]:
                        base_ok = True
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = f"{label}: {type(e).__name__}: {str(e).splitlines()[0][:160] if str(e) else ''}"
                    print(f"[RETRY {attempt + 1}/3] {last_err}")
                    time.sleep(3)
            if last_err:
                errors.append(last_err)
        browser.close()
    return found, base_ok, ok_count > 0, errors


# ---------------------------------------------------------------- 通知
def send_ntfy(msg):
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC が未設定")
    body = json.dumps({"topic": NTFY_TOPIC, **msg}, ensure_ascii=False).encode("utf-8")
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                NTFY_SERVER, data=body, method="POST",
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                if 200 <= r.status < 300:
                    return
                last = f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"通知送信に失敗: {last}")


def job_msg(kind, job):
    head = {"new": "🎬 TOHOシネマズ日本橋に新しいバイト求人",
            "reopen": "🎬 TOHOシネマズ日本橋の求人が再掲載",
            "closed": "TOHOシネマズ日本橋の求人が掲載終了"}[kind]
    lines = [job["title"] or "(職種名なし)"]
    if job.get("wage"):
        lines.append(job["wage"])
    lines.append(job["url"])
    m = {"title": head, "message": "\n".join(lines), "click": job["url"],
         "tags": ["movie_camera"] if kind != "closed" else ["wave"]}
    m["priority"] = 5 if kind in ("new", "reopen") else 3
    return m


# ---------------------------------------------------------------- 本体
def main():
    state = load_state()
    t = now()
    first_run = "jobs" not in state
    state.setdefault("jobs", {})          # id -> {..., status: open/closed, miss: n}
    state.setdefault("outbox", [])        # 送れなかった通知の再送キュー
    state.setdefault("fail_streak", 0)
    state.setdefault("events", [])        # Claude通知用の出来事ログ（新しい順ではなく追記順）

    def log_event(kind, job=None, note=""):
        e = {"ts": t.isoformat(), "kind": kind, "note": note}
        if job:
            e.update({k: job.get(k, "") for k in ("id", "title", "wage", "url")})
        state["events"] = (state["events"] + [e])[-100:]

    try:
        found, all_ok, any_ok, errors = fetch_all()
    except Exception as e:  # noqa: BLE001  ブラウザ自体が起動しない等でも必ず記録・通知まで進む
        found, all_ok, any_ok, errors = {}, False, False, [f"ブラウザ起動失敗: {type(e).__name__}: {str(e)[:200]}"]
    events = []

    if not any_ok:
        state["fail_streak"] += 1
        print("全入口で読み込み失敗:", errors)
        last_alert = state.get("last_fail_alert")
        due = state["fail_streak"] == FAIL_ALERT_RUNS or (
            state["fail_streak"] > FAIL_ALERT_RUNS and last_alert
            and t - datetime.fromisoformat(last_alert) >= timedelta(hours=FAIL_REALERT_HOURS))
        if due:
            events.append({"title": "⚠️ 日本橋求人の監視でエラー",
                           "message": f"{state['fail_streak']}回連続で求人ページを読めていません。\n" + "\n".join(errors)[:600],
                           "priority": 4, "tags": ["warning"]})
            state["last_fail_alert"] = t.isoformat()
            log_event("error", note=" / ".join(errors)[:300])
    else:
        if state["fail_streak"] >= FAIL_ALERT_RUNS:
            events.append({"title": "✅ 日本橋求人の監視が復旧", "message": "求人ページを再び読めるようになりました。",
                           "priority": 3, "tags": ["white_check_mark"]})
            log_event("recovered")
        state["fail_streak"] = 0
        state["last_success_date"] = t.strftime("%Y-%m-%d")
        state["last_success_hour"] = t.strftime("%Y-%m-%dT%H:00+09:00")  # 生存確認用（1時間単位で更新）

        # 出現（新着・再掲載）: 1つでも入口が読めれば判定
        for jid, job in found.items():
            prev = state["jobs"].get(jid)
            if prev is None:
                if not first_run:
                    events.append(job_msg("new", job))
                    log_event("new", job)
                state["jobs"][jid] = {**job, "status": "open", "miss": 0,
                                      "first_seen": t.isoformat(), "last_seen": t.isoformat()}
            else:
                if prev.get("status") == "closed":
                    events.append(job_msg("reopen", job))
                    log_event("reopen", job)
                prev.update({**job, "status": "open", "miss": 0, "last_seen": t.isoformat()})

        # 消滅（掲載終了）: 全求人が載る関東全域の一覧を最後まで読めた回だけ判定（読み込み失敗を終了と誤判定しない）
        if all_ok:
            for jid, prev in state["jobs"].items():
                if prev.get("status") == "open" and jid not in found:
                    prev["miss"] = prev.get("miss", 0) + 1
                    if prev["miss"] >= CLOSE_CONFIRM_RUNS:
                        prev["status"] = "closed"
                        prev["closed_at"] = t.isoformat()
                        events.append(job_msg("closed", prev))
                        log_event("closed", prev)

        open_jobs = [j for j in state["jobs"].values() if j.get("status") == "open"]
        listing = "\n".join(f"・{j['title']} ({j['wage']})\n  {j['url']}" for j in open_jobs) or "なし"
        if first_run:
            events.append({"title": "✅ 日本橋求人の監視を開始しました",
                           "message": f"今後、新着・再掲載・掲載終了を通知します。\n現在掲載中:\n{listing}",
                           "priority": 3, "tags": ["white_check_mark"]})
            state["last_heartbeat"] = t.isoformat()
        else:
            hb = state.get("last_heartbeat")
            if not hb or t - datetime.fromisoformat(hb) >= timedelta(days=HEARTBEAT_DAYS):
                events.append({"title": "📡 日本橋求人の監視は正常に動いています",
                               "message": f"週1回の動作報告です（最終確認 {fmt(t)}）。\n現在掲載中:\n{listing}",
                               "priority": 2, "tags": ["satellite"]})
                state["last_heartbeat"] = t.isoformat()

    if FORCE_TEST:
        events.append({"title": "🔔 テスト通知", "message": f"通知テストです（{fmt(t)}）。これが届けば設定OK。",
                       "priority": 4, "tags": ["bell"]})

    # 送信（失敗したものは次回再送）
    if not NTFY_TOPIC:
        events, state["outbox"] = [], []
    queue = state["outbox"] + events
    remaining, send_error = [], None
    for msg in queue:
        try:
            send_ntfy(msg)
            print("[通知送信]", msg["title"])
        except Exception as e:  # noqa: BLE001
            send_error = str(e)
            remaining.append(msg)
    state["outbox"] = remaining[-50:]
    state["last_send_error"] = (send_error or "")[:300]
    state["topic_set"] = bool(NTFY_TOPIC)
    save_state(state)

    if send_error:
        print("通知の送信に失敗（次回再送）:", send_error)
        sys.exit(1)
    if not any_ok:
        sys.exit(1)  # GitHub上でも失敗として赤く表示させる


if __name__ == "__main__":
    main()
