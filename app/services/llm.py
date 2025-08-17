import json
import logging
import re
import time  # 추가: 재시도 지연을 위해
from typing import List, Optional, Tuple

from loguru import logger
from openai import OpenAI, APIError  # 추가: OpenAI 예외 처리

from app.config import config
import requests  # 추가

_max_retries = 5
_retry_delay = 2  # 초 단위 지연 추가

def _generate_response(prompt: str) -> str:
    api_key = config.app.get("openai_api_key")
    model_name = config.app.get("openai_model_name")
    base_url = config.app.get("openai_base_url", "") or "https://api.openai.com/v1"
    if not api_key or not model_name:
        raise ValueError("OpenAI 설정(api_key 및 model_name)이 필요합니다.")
    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        response = client.chat.completions.create(
            model=model_name, messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    except APIError as e:
        raise ValueError(f"OpenAI API 오류: {str(e)}")

def _fetch_news_newsapi(subject: str, language: str = "ko") -> Optional[Tuple[str, str, str]]:
    """
    NewsAPI.org를 사용해 최신 기사 1건(title, description+content, url)을 반환.
    """
    import requests
    api_key = config.app.get("news_api_key", "").strip()
    if not api_key:
        return None
    params = {
        "q": subject,
        "language": language.split("-")[0] if "-" in language else language,
        "sortBy": "publishedAt",
        "pageSize": 5,
        "apiKey": api_key,
    }
    try:
        r = requests.get("https://newsapi.org/v2/everything", params=params, timeout=(15, 30))
        data = r.json()
        articles = data.get("articles") or []
        if not articles:
            return None
        a = articles[0]
        title = a.get("title") or ""
        desc = a.get("description") or ""
        content = a.get("content") or ""
        url = a.get("url") or ""
        body = "\n".join([desc, content]).strip()
        return (title, body, url)
    except Exception:
        return None

def _fetch_news_ddgs(subject: str) -> Optional[Tuple[str, str, str]]:
    """
    ddgs로 텍스트 검색, 상위 결과 1건을 간단히 조합(title, snippet, url)
    """
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = [r for r in ddgs.text(f"{subject} latest news", max_results=5)]
        if not results:
            return None
        r0 = results[0]
        title = r0.get("title") or subject
        body = r0.get("body") or ""
        url = r0.get("href") or ""
        return (title, body, url)
    except Exception:
        return None

def _pick_one_article(subject: str, language: str) -> Tuple[str, str, str]:
    """
    기사 1건 선택. 우선순위: newsapi -> ddgs -> 빈값
    """
    provider = (config.app.get("news_provider", "auto") or "auto").lower()
    if provider in ("auto", "newsapi"):
        res = _fetch_news_newsapi(subject, language)
        if res:
            logger.info(f"news source: newsapi | title: {res[0]} | url: {res[2]}")
            return res
    if provider in ("auto", "ddgs"):
        res = _fetch_news_ddgs(subject)
        if res:
            logger.info(f"news source: ddgs | title: {res[0]} | url: {res[2]}")
            return res
    logger.warning("no news source available, falling back to generic knowledge.")
    return (subject, "", "")

def _normalize_coin_id(subject: str) -> Optional[str]:
    """
    간단 매핑: 사용자가 한글/영문/심볼로 입력해도 CoinGecko id로 정규화 시도.
    필요 시 확장하세요.
    """
    s = (subject or "").strip().lower()
    mapping = {
        "btc": "bitcoin", "bitcoin": "bitcoin",
        "eth": "ethereum", "ethereum": "ethereum",
        "xrp": "ripple", "ripple": "ripple", "리플": "ripple",
        "xmr": "monero", "monero": "monero", "모네로": "monero",
        "doge": "dogecoin", "dogecoin": "dogecoin",
        "sol": "solana", "solana": "solana",
        "pi": "pi-network", "파이": "pi-network",
    }
    return mapping.get(s)

def _fetch_market_data_coingecko(coin_id: str) -> Optional[dict]:
    base = config.app.get("coingecko_base_url", "https://api.coingecko.com/api/v3").rstrip("/")
    try:
        r = requests.get(
            f"{base}/coins/markets",
            params={"vs_currency": "usd", "ids": coin_id},
            timeout=(10, 20),
        )
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
    except Exception:
        return None
    return None

def _mk_market_context(md: Optional[dict]) -> str:
    if not md:
        return ""
    # 사용 가능한 핵심 지표만 추려 간단 문장으로 구성
    price = md.get("current_price")
    chg24 = md.get("price_change_percentage_24h")
    chg7d = md.get("price_change_percentage_7d_in_currency")
    mcap = md.get("market_cap")
    vol = md.get("total_volume")
    parts = []
    if price is not None: parts.append(f"가격: ${price:,.2f}")
    if chg24 is not None: parts.append(f"24h: {chg24:+.2f}%")
    if chg7d is not None: parts.append(f"7d: {chg7d:+.2f}%")
    if mcap is not None: parts.append(f"시총: ${mcap:,.0f}")
    if vol is not None: parts.append(f"거래대금(24h): ${vol:,.0f}")
    return " / ".join(parts)

def generate_script(
    video_subject: str, language: str = "ko-KR", paragraph_number: int = 1
) -> str:
    # 하나의 기사만 선택
    title, article_body, url = _pick_one_article(video_subject, language)

    # 선택적 시장 데이터
    ref_block = ""
    if config.app.get("use_market_data", True):
        coin_id = _normalize_coin_id(video_subject)
        if coin_id:
            md = _fetch_market_data_coingecko(coin_id)
            ctx = _mk_market_context(md)
            if ctx:
                ref_block = f"[참조 데이터] {ctx}"

    # 목표 길이(40~60초) 참고
    target_s = int(config.app.get("target_duration_s", 50))
    # 한국어 속도 고려(대략 8~12자/초), 40~60초 => 320~720자 범위
    min_chars, max_chars = 380, 700

    prompt = f"""
당신은 암호화폐 시장 애널리스트입니다. 아래 기사와 참조 데이터를 바탕으로
하나의 뉴스만 차분히 설명하는 40~60초 분량의 한국어 스크립트를 작성하세요.
형식 가이드(제목 출력 금지, 본문만):
- 인트로(후킹, 상황 한 줄) →
- 배경/맥락(필요시) →
- 핵심 사실(숫자/지표 유지, 출처 맥락) →
- 의미/영향(시장·투자자 관점) →
- 주의사항/리스크 →
- 마무리(관찰 포인트 1~2개 포함)
규칙:
- 헤드라인 나열 금지, 하나의 흐름으로 자연스러운 구어체
- 모를 땐 추정/가능성으로 표현, 과장/투자권유 금지
- 길이 목표: 약 {target_s}초, 글자수 {min_chars}~{max_chars}자 내외
- 출력은 스크립트 본문만(소제목·목차·마크업·괄호 블록 금지)

[제목] {title}
[기사 재료] {article_body}
[출처] {url}
{ref_block}
""".strip()

    final_script = ""
    logger.info(f"subject: {video_subject}")

    def clean_response(text: str) -> str:
        # 불필요한 마크업/메타 제거
        text = re.sub(r"\s+", " ", text).strip()
        return text

    for i in range(_max_retries):
        try:
            response = _generate_response(prompt=prompt)
            if not response:
                raise ValueError("빈 응답")
            script = clean_response(response)
            # 길이 보정
            if (len(script) < min_chars or len(script) > max_chars) and i < _max_retries - 1:
                logger.warning(f"length off ({len(script)} chars). retrying...")
                time.sleep(_retry_delay)
                continue
            final_script = script
            break
        except Exception as e:
            logger.error(f"스크립트 생성 실패: {e}")
            if i < _max_retries - 1:
                logger.warning(f"영상 스크립트 재시도... {i+1}/{_max_retries}, {_retry_delay}s 대기")
                time.sleep(_retry_delay)

    if not final_script:
        raise ValueError("스크립트 생성에 실패했습니다. API 키나 네트워크를 확인하세요.")

    logger.success(f"완료: \n{final_script}")
    return final_script.strip()

def generate_terms(video_subject: str, video_script: str, amount: int = 5) -> List[str]:
    # 개선: prompt 더 구체적으로, JSON 형식 강조
    prompt = f"""
    # 역할: 영상 검색 용어 생성기
    ## 목표:
    '{video_subject}' 관련 {amount}개의 검색 용어를 생성하세요.
    JSON 배열 형식으로만 반환하세요. 예: ["용어1", "용어2"]
    각 용어는 1-3단어로 구성하고, 반드시 주제어를 포함해야 합니다.
    영어로 생성합니다.
    스크립트: {video_script}
    응답은 JSON 배열만! 추가 텍스트 금지.
    """.strip()

    logger.info(f"주제: {video_subject}")

    search_terms = []
    response = ""
    for i in range(_max_retries):
        try:
            response = _generate_response(prompt)
            search_terms = json.loads(response)
            if not isinstance(search_terms, list) or not all(isinstance(term, str) for term in search_terms):
                raise ValueError("response is not a list of strings.")
            break

        except Exception as e:
            logger.warning(f"failed to generate video terms: {str(e)}")
            # 개선: fallback - 응답에서 JSON 배열 추출 시도
            match = re.search(r'\[.*?\]', response, re.DOTALL)
            if match:
                try:
                    search_terms = json.loads(match.group())
                    if isinstance(search_terms, list):
                        break
                except Exception:
                    pass

            if i < _max_retries - 1:
                logger.warning(f"failed to generate video terms, trying again... {i + 1}, { _retry_delay }초 대기")
                time.sleep(_retry_delay)

    if not search_terms:
        # 개선: 완전 실패 시 기본 용어 반환
        search_terms = [f"{video_subject} {i+1}" for i in range(amount)]
        logger.warning(f"fallback terms: {search_terms}")

    logger.success(f"completed: \n{search_terms}")
    return search_terms

if __name__ == "__main__":
    video_subject = "생명의 의미"
    script = generate_script(
        video_subject=video_subject, language="ko-KR", paragraph_number=1
    )
    print("######################")
    print("생성된 스크립트:")
    print(script)
    search_terms = generate_terms(
        video_subject=video_subject, video_script=script, amount=5
    )
    print("######################")
    print("생성된 검색 용어:")
    print(search_terms)