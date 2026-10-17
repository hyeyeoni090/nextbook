# nextbook (다음책)
도서관 대출 데이터와 책 소개를 결합한 하이브리드 도서 추천 서비스

**배포 주소:** https://nextbook-hy.streamlit.app/

## 왜 만들었나
시중의 도서 앱은 대부분 독서 기록 위주여서, 내 취향을 반영한 추천을 받을 수 없었습니다.
친구들에게 추천받다가 읽을 책이 떨어져서, 직접 만들었습니다.

## 주요 기능
- 추천 도서를 "읽고 싶어요", "관심 없어요" 등으로 분류해 취향 반영
- 아동 도서 별도 필터링
- 같은 작가의 책을 몇 권까지 추천할지 설정
- 내용 정보와 대출 정보의 반영 비율 설정
- 중복 장르 필터링

## 사용 기술
- Python, Streamlit
- Supabase (독서 기록 저장)
- 카카오 책 검색 API, 도서관 정보나루 API

## 파일 구성
- `app.py` : Streamlit 화면
- `recommender.py`

## 실행 방법
```bash
pip install -r requirements.txt
streamlit run app.py
```

API 키(카카오, 정보나루, Supabase)는 코드에 직접 넣지 않고 Streamlit Cloud의 Secrets에 저장해 `st.secrets`로 불러옵니다. 로컬 설정 파일(`.streamlit/secrets.toml`)은 `.gitignore`로 저장소에서 제외했고, 공개 저장소의 커밋 이력에도 키가 포함되지 않았습니다.

## 개발 메모
알라딘 API 서비스가 종료된 것을 확인하고 카카오 책 검색 API로 대체했습니다.
