"""다음책(nextbook) 추천 엔진
- 카카오 책 검색 API: 책 검색, 표지, 책 소개
- 도서관 정보나루 API: 함께 대출된 책(협업 필터링), 분야별 인기 대출 도서
- Supabase: 읽은 책 기록, 피드백, 책 정보 캐시
"""
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

BOOK_COLS = ["isbn13", "title", "authors", "publisher", "thumbnail", "contents"]
WORKERS = 8  # 동시에 보내는 API 요청 수

# ---------- 서버 메모리 캐시 (같은 요청은 일정 시간 동안 다시 보내지 않음) ----------
_CACHE, _LOCK = {}, threading.Lock()


def cached(key, ttl, fn):
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    if val:  # 빈 결과(오류 포함)는 저장하지 않음
        with _LOCK:
            _CACHE[key] = (now, val)
    return val


def pmap(fn, items, workers=WORKERS):
    """여러 요청을 동시에 보내고, 결과는 입력 순서대로 돌려줌"""
    items = list(items)
    if len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))

# ===== 기본 설정 =====
DEFAULTS = {
    "W_CONTENT": 0.5,                     # 콘텐츠 비중 (0~1). 나머지는 대출 데이터 비중
    "MAX_PER_AUTHOR": 2,                  # 한 작가당 최대 추천 권수
    "NEW_AUTHOR_SLOTS": 3,                # 안 읽어본 작가 책 최소 권수
    "TOPIC_SLOTS": 3,                     # '전체' 모드에서 관심 분야 권수
    "TOPIC_GENRES": ["사회과학", "심리학"],  # '전체' 모드의 관심 분야
    "AGE": "20",                          # 관심 분야 인기 대출 연령대 ("" 이면 전체)
    "GENRE_PAGES": 5,                     # 장르 모드 후보 수 (1 = 100권)
    "GENRE_DAYS": 1095,                   # 장르 모드 대출 기록 기간 (일)
    "GENRE_AGE": "",                      # 장르 모드 연령 제한 ("" 이면 전 연령)
    "POP_WEIGHT": 0.0,                    # 장르 모드 인기도 반영 (0 = 순수 취향순)
    "MIN_PER_GENRE": 1,                   # '내 취향 우선' 모드에서 장르마다 최소 권수
}

FEW_BOOKS = 5   # 기록이 이보다 적으면 인기 대출 순위를 섞어서 추천
COLD_GENRES = ["소설(전체)", "에세이", "사회과학", "심리학", "인생·처세"]  # 기록 없을 때 '전체' 모드 장르

GENRES = {  # 도서관 분류번호(KDC) 기준
    # 문학
    "소설(전체)":   {"kdc": "8", "class_re": r"^8\d3"},
    "한국소설":     {"kdc": "8", "class_re": r"^813"},
    "외국소설":     {"kdc": "8", "class_re": r"^8[2-9]3"},
    "일본소설":     {"kdc": "8", "class_re": r"^833"},
    "영미소설":     {"kdc": "8", "class_re": r"^843"},
    "에세이":       {"kdc": "8", "class_re": r"^8\d4"},
    "시":           {"kdc": "8", "class_re": r"^8\d1"},
    # 소설 세부 장르 (책 소개 단어로 구분)
    "추리·미스터리": {"kdc": "8", "class_re": r"^8\d3", "pages": 10,
                     "keywords": ["추리", "미스터리", "탐정", "살인", "형사", "범인", "수사", "용의자", "실종", "트릭"]},
    "스릴러":       {"kdc": "8", "class_re": r"^8\d3", "pages": 10,
                     "keywords": ["스릴러", "서스펜스", "납치", "연쇄", "복수", "추격", "사이코패스", "광기"]},
    "로맨스":       {"kdc": "8", "class_re": r"^8\d3", "pages": 10,
                     "keywords": ["로맨스", "연애", "연인", "첫사랑", "짝사랑", "설렘", "사랑에 빠"]},
    "SF":           {"kdc": "8", "class_re": r"^8\d3", "pages": 10,
                     "keywords": ["SF", "과학소설", "우주", "로봇", "인공지능", "행성", "외계", "안드로이드", "시간여행", "디스토피아"]},
    "판타지":       {"kdc": "8", "class_re": r"^8\d3", "pages": 10,
                     "keywords": ["판타지", "마법", "마녀", "드래곤", "왕국", "이세계", "요정", "주술"]},
    "호러":         {"kdc": "8", "class_re": r"^8\d3", "pages": 10,
                     "keywords": ["공포", "호러", "괴담", "귀신", "유령", "저주", "괴이"]},
    "역사소설":     {"kdc": "8", "class_re": r"^8\d3", "pages": 10,
                     "keywords": ["역사소설", "조선", "고려", "왕조", "임진왜란", "일제강점기", "궁궐", "임금"]},
    "성장·청소년":  {"kdc": "8", "class_re": r"^8\d3", "pages": 10,
                     "keywords": ["성장", "청소년", "열일곱", "열여덟", "학교", "교실", "사춘기"]},
    "힐링":         {"kdc": "8", "class_re": r"^8\d3", "pages": 10,
                     "keywords": ["힐링", "위로", "따뜻한", "다정한", "치유", "온기", "쉼"]},
    "가족":         {"kdc": "8", "class_re": r"^8\d3", "pages": 10,
                     "keywords": ["가족", "엄마", "아빠", "아버지", "어머니", "할머니", "할아버지", "자매", "형제"]},
    # 사회
    "사회과학":     {"kdc": "3"},
    "사회·사회문제": {"kdc": "3", "class_re": r"^33"},
    "정치·외교":    {"kdc": "3", "class_re": r"^34"},
    "경제경영":     {"kdc": "3", "class_re": r"^32"},
    "교육":         {"kdc": "3", "class_re": r"^37"},
    # 인문
    "심리학":       {"kdc": "1", "class_re": r"^18"},
    "철학":         {"kdc": "1", "class_re": r"^1[0-7]"},
    "인생·처세":    {"kdc": "1", "class_re": r"^19"},
    "역사(전체)":   {"kdc": "9", "class_re": r"^9[0-7]"},
    "한국사":       {"kdc": "9", "class_re": r"^911"},
    "여행·지리":    {"kdc": "9", "class_re": r"^98"},
    "종교":         {"kdc": "2"},
    # 과학·기술
    "과학":         {"kdc": "4"},
    "의학·건강":    {"kdc": "5", "class_re": r"^51"},
    "요리":         {"kdc": "5", "class_re": r"^594"},
    "IT·컴퓨터":    {"kdc": "0", "class_re": r"^00[45]"},
    # 예술
    "예술(전체)":   {"kdc": "6"},
    "미술·디자인":  {"kdc": "6", "class_re": r"^6[0-5]"},
    "음악":         {"kdc": "6", "class_re": r"^67"},
    "영화·공연":    {"kdc": "6", "class_re": r"^68"},
}

PARTICLE = r"(에서|에게|으로|처럼|까지|부터|은|는|이|가|을|를|의|에|로|와|과|도|만)$"
STOP = {"그리고", "하지만", "그러나", "이야기", "소설", "작가", "작품", "세상", "사람", "사람들",
        "우리", "자신", "모든", "위해", "통해", "대한", "있는", "없는", "하는", "한다", "했다", "그녀", "그것"}


# ---------- 도우미 ----------
def _clean(t):
    return re.sub(r"\(.*?\)|\[.*?\]|[^가-힣A-Za-z0-9]", "", t or "")


def norm_title(t):
    """판본이 달라도 같은 책이면 같은 값이 되도록 제목을 정리
    예) '아몬드 :손원평 장편소설', '아몬드(100만 부 기념 특별판)' → '아몬드'"""
    t = re.sub(r"\(.*?\)|\[.*?\]", "", t or "")
    # 부제 구분자(앞이나 뒤에 공백이 있는 :, =, ;, -)에서 자르기. 'Re:제로'처럼 붙어 있으면 유지
    main = _clean(re.split(r"\s+[:=;]|[:=;]\s+|\s+[-―–]\s+", t)[0])
    return main if len(main) >= 2 else _clean(t)


def norm_author(s):
    s = re.split(r"[,;/(]", s or "")[0].strip()
    s = re.sub(r"\s*(지음|저|글|그림|엮음|옮김|역)$", "", s)
    return s.replace(" ", "")


def author_of(b):
    return norm_author(b.get("authors")) or b["isbn13"]


def minmax(a):
    a = np.asarray(a, dtype=float)
    return (a - a.min()) / (a.max() - a.min()) if a.max() > a.min() else np.zeros_like(a)


def keywords_from(texts, n=10):
    words = []
    for t in texts:
        for w in re.findall(r"[가-힣]{2,}", t or ""):
            w = re.sub(PARTICLE, "", w)
            if len(w) >= 2 and w not in STOP:
                words.append(w)
    return [w for w, _ in Counter(words).most_common(n)]


def to_rating(v):
    return float(v) if v is not None else None


class Recommender:
    def __init__(self, sb, kakao_key, naru_key, user_id="hyeyeon", config=None, progress=None,
                 exclude=None):
        self.sb = sb
        self.kakao_key = kakao_key
        self.naru_key = naru_key
        self.user_id = user_id
        self.cfg = {**DEFAULTS, **(config or {})}
        self.progress = progress or (lambda msg: None)
        # exclude: 이번에 빼고 싶은 책들 (예: 이번 접속에서 이미 추천받은 책)
        exclude = list(exclude or [])
        self.ex_isbns = {b.get("isbn13") for b in exclude if b.get("isbn13")}
        self.ex_titles = {norm_title(b.get("title")) for b in exclude} - {""}

    # ---------- 외부 API ----------
    def kakao_search(self, query, target=None, size=10):
        return cached(("kakao", query, target, size), 86400,
                      lambda: self._kakao_search(query, target, size))

    def _kakao_search(self, query, target=None, size=10):
        params = {"query": query, "size": size}
        if target:
            params["target"] = target
        try:
            r = requests.get("https://dapi.kakao.com/v3/search/book",
                             headers={"Authorization": f"KakaoAK {self.kakao_key}"},
                             params=params, timeout=10)
        except requests.RequestException:
            return []
        if r.status_code != 200:
            return []
        out = []
        for b in r.json().get("documents", []):
            isbn13 = next((x for x in b["isbn"].split() if len(x) == 13), None)
            if isbn13:
                out.append({"isbn13": isbn13, "title": b["title"], "authors": ", ".join(b["authors"]),
                            "publisher": b["publisher"], "thumbnail": b["thumbnail"],
                            "contents": b["contents"]})
        return out

    def naru_recommend(self, isbn13, n=10):
        return cached(("naru_rec", isbn13, n), 3 * 86400,
                      lambda: self._naru_recommend(isbn13, n))

    def _naru_recommend(self, isbn13, n=10):
        try:
            r = requests.get("http://data4library.kr/api/recommandList",
                             params={"authKey": self.naru_key, "isbn13": isbn13, "format": "json"},
                             timeout=10)
            docs = r.json().get("response", {}).get("docs", [])
        except Exception:
            return []
        out = []
        for d in docs[:n]:
            b = d.get("book", {})
            isbn = (b.get("isbn13") or "").strip()
            if len(isbn) == 13:
                out.append({"isbn13": isbn, "title": b.get("bookname", ""), "authors": b.get("authors", "")})
        return out

    def naru_popular(self, gcfg, pages=2, days=365, age=""):
        key = ("naru_pop", gcfg["kdc"], gcfg.get("class_re"), pages, days, age, date.today().isoformat())
        return cached(key, 86400, lambda: self._naru_popular(gcfg, pages, days, age))

    def _naru_popular(self, gcfg, pages, days, age):
        end = date.today()
        start = end - timedelta(days=days)

        def fetch(page):
            params = {"authKey": self.naru_key, "startDt": start.isoformat(), "endDt": end.isoformat(),
                      "kdc": gcfg["kdc"], "pageNo": page, "pageSize": 100, "format": "json"}
            if age:
                params["age"] = age
            try:
                r = requests.get("http://data4library.kr/api/loanItemSrch", params=params, timeout=20)
                return r.json().get("response", {}).get("docs", [])
            except Exception:
                return []

        out, seen = [], set()
        for docs in pmap(fetch, range(1, pages + 1), workers=5):
            if not docs:
                break
            for x in docs:
                d = x.get("doc", {})
                isbn = (d.get("isbn13") or "").strip()
                cls = str(d.get("class_no") or "").strip()
                if len(isbn) != 13 or isbn in seen:
                    continue
                if gcfg.get("class_re") and not re.match(gcfg["class_re"], cls):
                    continue
                seen.add(isbn)
                out.append({"isbn13": isbn, "title": d.get("bookname", ""),
                            "authors": d.get("authors", ""), "rank": len(out) + 1})
        return out

    def fill_details(self, b):
        found = self.kakao_search(b["isbn13"], target="isbn", size=1)
        if found:
            return found[0]
        return {"isbn13": b["isbn13"], "title": b.get("title", ""), "authors": b.get("authors", ""),
                "publisher": "", "thumbnail": "", "contents": ""}

    # ---------- DB: 기록·피드백 ----------
    def save_book(self, b):
        self.sb.table("books").upsert({c: b.get(c) for c in BOOK_COLS}).execute()

    def save_read(self, b, rating=None):
        self.save_book(b)
        self.sb.table("reading_log").upsert(
            {"user_id": self.user_id, "isbn13": b["isbn13"], "rating": rating},
            on_conflict="user_id,isbn13").execute()
        self.remove_feedback(b["isbn13"], "want")

    def update_rating(self, isbn13, rating):
        (self.sb.table("reading_log").update({"rating": rating})
         .eq("user_id", self.user_id).eq("isbn13", isbn13).execute())

    def delete_read(self, isbn13):
        (self.sb.table("reading_log").delete()
         .eq("user_id", self.user_id).eq("isbn13", isbn13).execute())

    def save_feedback(self, b, ftype):
        self.save_book(b)
        self.sb.table("feedback").upsert(
            {"user_id": self.user_id, "isbn13": b["isbn13"], "type": ftype},
            on_conflict="user_id,isbn13,type").execute()

    def remove_feedback(self, isbn13, ftype):
        (self.sb.table("feedback").delete()
         .eq("user_id", self.user_id).eq("isbn13", isbn13).eq("type", ftype).execute())

    def list_read(self):
        res = (self.sb.table("reading_log")
               .select(f"isbn13, rating, read_date, created_at, books({','.join(BOOK_COLS)})")
               .eq("user_id", self.user_id).order("created_at", desc=True).execute())
        return [{**(r.get("books") or {}), "isbn13": r["isbn13"],
                 "rating": to_rating(r["rating"]), "read_date": r.get("read_date")} for r in res.data]

    def list_feedback(self, ftype):
        res = (self.sb.table("feedback")
               .select(f"isbn13, created_at, books({','.join(BOOK_COLS)})")
               .eq("user_id", self.user_id).eq("type", ftype)
               .order("created_at", desc=True).execute())
        return [{**(r.get("books") or {}), "isbn13": r["isbn13"]} for r in res.data]

    def list_want(self):
        return self.list_feedback("want")

    # ---------- DB: 추천용 ----------
    def load_my_books(self):
        res = (self.sb.table("reading_log").select("isbn13, rating, books(title, authors, contents)")
               .eq("user_id", self.user_id).execute())
        rows = []
        for r in res.data:
            b = r.get("books") or {}
            rows.append({"isbn13": r["isbn13"], "rating": to_rating(r["rating"]),
                         "title": b.get("title") or "", "authors": b.get("authors") or "",
                         "contents": b.get("contents") or ""})
        return pd.DataFrame(rows)

    def load_feedback(self):
        """피드백 준 책의 ISBN과 제목 (같은 책의 다른 판본도 거르기 위해 제목도 사용)"""
        res = (self.sb.table("feedback").select("isbn13, books(title)")
               .eq("user_id", self.user_id).execute())
        isbns = {r["isbn13"] for r in res.data}
        titles = {norm_title((r.get("books") or {}).get("title")) for r in res.data}
        return isbns | self.ex_isbns, (titles | self.ex_titles) - {""}

    def fill_contents(self, cands):
        need = [k for k, v in cands.items() if "contents" not in v]
        for i in range(0, len(need), 100):
            res = (self.sb.table("books").select(",".join(BOOK_COLS))
                   .in_("isbn13", need[i:i + 100]).execute())
            for r in res.data:
                cands[r["isbn13"]] = {**cands[r["isbn13"]], **r}
        todo = [k for k in need if "contents" not in cands[k]]
        new_rows = []
        if todo:
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futs = {ex.submit(self.fill_details, cands[k]): k for k in todo}
                for n, f in enumerate(as_completed(futs), 1):
                    k = futs[f]
                    d = f.result()
                    d["isbn13"] = k
                    cands[k] = {**cands[k], **d}
                    new_rows.append({c: d.get(c) for c in BOOK_COLS})
                    if n % 20 == 0:
                        self.progress(f"책 정보 모으는 중... {n}/{len(todo)}")
        for i in range(0, len(new_rows), 200):
            self.sb.table("books").upsert(new_rows[i:i + 200]).execute()

    # ---------- 추천 공통 단계 ----------
    def prepare(self):
        mine = self.load_my_books()
        if mine.empty:
            return None
        liked = mine[mine["rating"].fillna(3) >= 3.5]
        if liked.empty:
            liked = mine
        fb_isbns, fb_titles = self.load_feedback()
        return {"mine": mine, "liked": liked,
                "weight": dict(zip(liked["isbn13"], (liked["rating"].fillna(3) - 2.5).clip(lower=0.5))),
                "read_authors": {norm_author(a) for s in mine["authors"] for a in s.split(", ") if a},
                "skip": set(mine["isbn13"]) | fb_isbns,
                # 읽은 책·피드백 준 책·이미 본 책의 제목 → 다른 판본도 제외
                "skip_titles": {norm_title(t) for t in mine["title"]} | fb_titles}

    def get_collab(self, weight):
        collab, cands = Counter(), {}
        items = list(weight.items())
        lists = pmap(lambda it: self.naru_recommend(it[0]), items, workers=5)
        for (isbn, wt), recs in zip(items, lists):
            for rank, b in enumerate(recs, 1):
                collab[b["isbn13"]] += wt / np.sqrt(rank)
                cands.setdefault(b["isbn13"], b)
        return collab, cands

    def make_pool(self, cands, u, raw_k):
        seen = set(u["skip_titles"])
        pool = []
        for b in sorted(cands.values(), key=lambda b: -raw_k.get(b["isbn13"], 0)):
            if b["isbn13"] in u["skip"]:
                continue
            nt = norm_title(b.get("title"))
            if not nt or nt in seen:
                continue
            if not (b.get("contents") or raw_k.get(b["isbn13"])):
                continue
            seen.add(nt)
            pool.append(b)
        return pool

    def score(self, pool, u, raw_k):
        liked, weight = u["liked"], u["weight"]
        text = lambda d: f"{d.get('title') or ''} {d.get('contents') or ''}"
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3))
        X = vec.fit_transform([text(r) for _, r in liked.iterrows()] + [text(b) for b in pool])
        L, C = X[:len(liked)], X[len(liked):]
        w = np.array([weight[i] for i in liked["isbn13"]]).reshape(-1, 1)
        profile = np.asarray(L.multiply(w).sum(axis=0)) / w.sum()
        c = minmax(cosine_similarity(C, profile).ravel())
        k = minmax([raw_k.get(b["isbn13"], 0) for b in pool])
        W = self.cfg["W_CONTENT"]
        return W * c + (1 - W) * k, c, k

    def _result(self, b, tag=None, score=None, c=None, k=None, is_new=False):
        out = {col: b.get(col) for col in BOOK_COLS}
        out.update({"tag": tag, "is_new": bool(is_new),
                    "score": None if score is None else float(score),
                    "c": None if c is None else float(c),
                    "k": None if k is None else float(k)})
        return out

    # ---------- '전체' 모드 ----------
    def recommend_all(self, top_n=10):
        cfg = self.cfg
        self.progress("내 기록 불러오는 중...")
        u = self.prepare()
        if not u:
            return self.recommend_popular([], top_n)
        self.progress("함께 대출된 책 찾는 중...")
        collab, cands = self.get_collab(u["weight"])
        self.progress("비슷한 책 검색 중...")
        liked = u["liked"]
        queries = ([(a, "person") for a in sorted({a for s in liked["authors"] for a in s.split(", ") if a})]
                   + [(kw, None) for kw in keywords_from(liked["contents"].tolist())])
        for found in pmap(lambda q: self.kakao_search(q[0], target=q[1]), queries):
            for b in found:
                cands.setdefault(b["isbn13"], b)
        cands = {k: v for k, v in cands.items() if k not in u["skip"]}
        self.fill_contents(cands)
        pool = self.make_pool(cands, u, collab)
        if not pool:
            return [], ["추천 후보가 없어요."]
        self.progress("취향 점수 계산 중...")
        final, c, k = self.score(pool, u, collab)

        topic_genres = [g for g in cfg["TOPIC_GENRES"] if g in GENRES]
        n_topic = cfg["TOPIC_SLOTS"] if topic_genres else 0
        cnt = Counter()
        picked = self._pick_simple(pool, final, top_n - n_topic, u, cnt)
        results = [self._result(pool[i], "취향", final[i], c[i], k[i],
                                author_of(pool[i]) not in u["read_authors"]) for i in picked]

        if n_topic:
            self.progress("관심 분야 찾는 중...")
            taken = (set(u["skip_titles"])
                     | {norm_title(pool[i]["title"]) for i in picked})
            lists = {g: self.naru_popular(GENRES[g], age=cfg["AGE"]) for g in topic_genres}
            topic = []
            while len(topic) < n_topic and any(lists.values()):
                for g in lists:
                    while lists[g]:
                        b = lists[g].pop(0)
                        nt, a = norm_title(b["title"]), author_of(b)
                        if (b["isbn13"] in u["skip"] or nt in taken
                                or cnt[a] >= cfg["MAX_PER_AUTHOR"]):
                            continue
                        taken.add(nt)
                        cnt[a] += 1
                        d = self.fill_details(b)
                        d["isbn13"] = b["isbn13"]
                        topic.append((g, d))
                        break
                    if len(topic) >= n_topic:
                        break
            for g, d in topic:
                results.append(self._result(d, g, is_new=author_of(d) not in u["read_authors"]))
        return results, []

    def _pick_simple(self, pool, final, n, u, cnt):
        cfg = self.cfg
        order = final.argsort()[::-1]
        picked = []

        def try_pick(i):
            a = author_of(pool[i])
            if i in picked or cnt[a] >= cfg["MAX_PER_AUTHOR"]:
                return
            picked.append(i)
            cnt[a] += 1

        for i in order:
            if len(picked) >= min(cfg["NEW_AUTHOR_SLOTS"], n):
                break
            if author_of(pool[i]) not in u["read_authors"]:
                try_pick(i)
        for i in order:
            if len(picked) >= n:
                break
            try_pick(i)
        picked.sort(key=lambda i: -final[i])
        return picked

    # ---------- 장르 선택 모드 ----------
    def recommend_genre(self, names, balanced=True, top_n=10):
        cfg = self.cfg
        names = [g for g in dict.fromkeys(names) if g in GENRES]
        if not names:
            return self.recommend_all(top_n)
        self.progress("내 기록 불러오는 중...")
        u = self.prepare()
        if not u:
            return self.recommend_popular(names, top_n)
        warnings = []
        few = len(u["mine"]) < FEW_BOOKS
        cands, pop, src = {}, Counter(), {}
        for g in names:
            self.progress(f"'{g}' 후보 모으는 중...")
            gcfg = GENRES[g]
            for b in self.naru_popular(gcfg, pages=gcfg.get("pages", cfg["GENRE_PAGES"]),
                                       days=cfg["GENRE_DAYS"], age=cfg["GENRE_AGE"]):
                pop[b["isbn13"]] = max(pop[b["isbn13"]], 1 / np.sqrt(b["rank"]))
                cands.setdefault(b["isbn13"], b)
                src.setdefault(b["isbn13"], set()).add(g)
        if not cands:
            return [], ["이 장르의 도서를 못 가져왔어요. 잠시 후 다시 시도해 주세요."]
        self.progress("함께 대출된 책 찾는 중...")
        collab, _ = self.get_collab(u["weight"])
        cands = {k: v for k, v in cands.items() if k not in u["skip"]}
        self.fill_contents(cands)

        def matched(k, v):
            t = f"{v.get('title') or ''} {v.get('contents') or ''}"
            return {g for g in src[k]
                    if not GENRES[g].get("keywords") or any(kw in t for kw in GENRES[g]["keywords"])}
        tags = {k: matched(k, v) for k, v in cands.items()}
        cands = {k: v for k, v in cands.items() if tags[k]}

        pop_w = max(cfg["POP_WEIGHT"], 0.5) if few else cfg["POP_WEIGHT"]
        if few:
            warnings.append(f"기록이 {FEW_BOOKS}권 미만이라 인기 대출 순위를 함께 반영했어요. "
                            "기록이 늘수록 취향 맞춤 추천이 강해져요.")
        raw_k = {k: collab.get(k, 0) + pop_w * pop[k] for k in cands}
        pool = self.make_pool(cands, u, raw_k)
        if not pool:
            return [], ["추천 후보가 없어요."]
        self.progress("취향 점수 계산 중...")
        final, c, k = self.score(pool, u, raw_k)

        per_genre = max(1, top_n // len(names)) if balanced else cfg["MIN_PER_GENRE"]
        order = list(final.argsort()[::-1])
        picked, cnt = [], Counter()

        def ok(i):
            return i not in picked and cnt[author_of(pool[i])] < cfg["MAX_PER_AUTHOR"]

        def take(i):
            picked.append(i)
            cnt[author_of(pool[i])] += 1

        if len(names) > 1:
            for g in names:
                have = 0
                for i in order:
                    if have >= per_genre:
                        break
                    if g in tags[pool[i]["isbn13"]] and ok(i):
                        take(i)
                        have += 1
                if have == 0:
                    warnings.append(f"{g}: 조건에 맞는 책을 못 찾았어요")
                elif have < per_genre:
                    warnings.append(f"{g}: 후보가 부족해서 {have}권만 나왔어요")
        is_new = lambda i: author_of(pool[i]) not in u["read_authors"]
        for i in order:
            if len(picked) >= top_n or sum(is_new(j) for j in picked) >= cfg["NEW_AUTHOR_SLOTS"]:
                break
            if is_new(i) and ok(i):
                take(i)
        for i in order:
            if len(picked) >= top_n:
                break
            if ok(i):
                take(i)
        picked = sorted(picked[:top_n], key=lambda i: -final[i])

        results = []
        for i in picked:
            b = pool[i]
            tag = "/".join(g for g in names if g in tags[b["isbn13"]])
            results.append(self._result(b, tag, final[i], c[i], k[i], is_new(i)))
        return results, warnings

    # ---------- 기록이 없는 신규 사용자: 인기 대출 순 추천 ----------
    def recommend_popular(self, names, top_n=10):
        cfg = self.cfg
        names = [g for g in dict.fromkeys(names or COLD_GENRES) if g in GENRES]
        skip, skip_titles = self.load_feedback()
        cands, lists = {}, {}
        for g in names:
            self.progress(f"'{g}' 인기 도서 모으는 중...")
            gcfg = GENRES[g]
            pop = self.naru_popular(gcfg, pages=gcfg.get("pages", cfg["GENRE_PAGES"]),
                                    days=cfg["GENRE_DAYS"], age=cfg["GENRE_AGE"])
            # 키워드로 거르는 세부 장르는 넉넉히, 나머지는 상위권만 살펴봄
            pop = [b for b in pop if b["isbn13"] not in skip][:300 if gcfg.get("keywords") else 60]
            lists[g] = pop
            for b in pop:
                cands.setdefault(b["isbn13"], b)
        if not cands:
            return [], ["인기 도서를 못 가져왔어요. 잠시 후 다시 시도해 주세요."]
        self.fill_contents(cands)

        def fits(g, b):
            kws = GENRES[g].get("keywords")
            t = f"{b.get('title') or ''} {b.get('contents') or ''}"
            return not kws or any(kw in t for kw in kws)

        queues = {g: [cands[b["isbn13"]] for b in lst if fits(g, cands[b["isbn13"]])]
                  for g, lst in lists.items()}
        picked, taken, cnt = [], set(skip_titles), Counter()
        # 장르를 돌아가며 인기 순으로 한 권씩
        while len(picked) < top_n and any(queues.values()):
            for g in names:
                q = queues[g]
                while q:
                    b = q.pop(0)
                    nt, a = norm_title(b.get("title")), author_of(b)
                    if not nt or nt in taken or cnt[a] >= cfg["MAX_PER_AUTHOR"]:
                        continue
                    taken.add(nt)
                    cnt[a] += 1
                    picked.append(self._result(b, g))
                    break
                if len(picked) >= top_n:
                    break
        note = (f"아직 기록이 없어서 {cfg['GENRE_AGE'] + '대 ' if cfg['GENRE_AGE'] else ''}"
                "도서관 인기 대출 순으로 골랐어요. '책 기록하기'에서 읽은 책을 남기면 취향 맞춤 추천으로 바뀌어요.")
        return picked, [note]
