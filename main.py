"""
종가 주식 스크리너 - 실시간 테마/뉴스 분석 & 기술적 스크리닝 결합 메인 스크립트
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import FinanceDataReader as fdr
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

# 디렉토리 설정
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------------
# 1. 텔레그램 메세지 전송 함수
# ----------------------------------------------------------------------
def send_telegram_msg(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if not token or not chat_id:
        print("[경고] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 환경변수가 설정되지 않았습니다.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print("텔레그램 알림 발송 성공!")
            return True
        else:
            print(f"텔레그램 발송 실패: {res.text}")
            return False
    except Exception as e:
        print(f"텔레그램 전송 예외 발생: {e}")
        return False

# ----------------------------------------------------------------------
# 2. 당일 상승 주도 테마 & 뉴스가 쏠린 섹터 종목 크롤링
# ----------------------------------------------------------------------
def fetch_today_hot_themes(top_n_themes: int = 5) -> dict[str, list[dict[str, str]]]:
    """
    네이버 금융 실시간 테마 페이지에서 당일 등락률 상위 주도 테마와
    해당 테마에 속한 주요 주도 종목들을 수집합니다.
    """
    url = "https://finance.naver.com/sise/theme.naver"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    theme_data = {}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        
        # 테마 테이블 추출
        theme_rows = soup.select("table.type_1 tr")
        top_themes = []
        
        for row in theme_rows:
            col = row.select("td.col_type1 a")
            rate_col = row.select("td.col_type2")
            if col and rate_col:
                t_name = col[0].text.strip()
                t_link = "https://finance.naver.com" + col[0]["href"]
                t_rate = rate_col[0].text.strip()
                top_themes.append({"name": t_name, "link": t_link, "rate": t_rate})
                if len(top_themes) >= top_n_themes:
                    break
        
        # 각 상위 테마별 속한 주도 종목 상세 수집
        for theme in top_themes:
            t_res = requests.get(theme["link"], headers=headers, timeout=10)
            t_soup = BeautifulSoup(t_res.text, "html.parser")
            stock_rows = t_soup.select("table.type_5 tr")
            
            stocks_in_theme = []
            for s_row in stock_rows:
                name_cell = s_row.select("td.name a")
                if name_cell:
                    s_name = name_cell[0].text.strip()
                    code_match = re.search(r"code=(\d+)", name_cell[0]["href"])
                    s_code = code_match.group(1) if code_match else ""
                    if s_code:
                        stocks_in_theme.append({"code": s_code, "name": s_name})
            
            theme_data[theme["name"]] = {
                "rate": theme["rate"],
                "stocks": stocks_in_theme[:7]  # 테마당 상위 7개 주도주 위주
            }
            time.sleep(0.3)
            
    except Exception as e:
        print(f"테마 데이터 수집 중 오류 발생: {e}")
        
    return theme_data

# ----------------------------------------------------------------------
# 3. 기술적 스크리닝 및 당일 테마 결합 로직
# ----------------------------------------------------------------------
def run_theme_stock_screener():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[{now_str}] 당일 실시간 테마 및 뉴스 주도주 스크리닝 시작...")
    
    # 1) 당일 시장 상승 주도 테마 수집
    hot_themes = fetch_today_hot_themes(top_n_themes=5)
    
    theme_stock_codes = {}
    for t_name, t_info in hot_themes.items():
        for s in t_info["stocks"]:
            theme_stock_codes[s["code"]] = {
                "name": s["name"],
                "theme": t_name,
                "theme_rate": t_info["rate"]
            }
            
    print(f"포착된 상위 주도 테마 개수: {len(hot_themes)}개 (총 {len(theme_stock_codes)}개 주도 종목 추출)")

    # 2) KRX 전체 상장 종목 데이터 로드
    try:
        df_krx = fdr.StockListing("KRX")
    except Exception as e:
        print(f"KRX 종목 목록 수집 실패: {e}")
        return

    # 기본 필터링 (시가총액 2,000억 이상, 주가 1,000원 이상)
    df_filtered = df_krx[
        (df_krx["Marcap"] >= 200_000_000_000) & 
        (df_krx["Close"] >= 1000)
    ].copy()
    
    # 테마에 포함된 종목 + 시장 거래대금 우량주 혼합 검토 대상
    target_codes = set(df_filtered["Code"].tolist()).union(set(theme_stock_codes.keys()))
    
    matched_results = []
    
    for code in target_codes:
        # 종목 정보 추출
        stock_info = theme_stock_codes.get(code, {})
        theme_name = stock_info.get("theme", "일반 거래대금 우량주")
        
        try:
            # 120일 일봉 데이터 조회
            df_stock = fdr.DataReader(code)
            if len(df_stock) < 60:
                continue
            
            # 기술적 지표 계산 (이동평균선 & 거래대금)
            df_stock["SMA5"] = df_stock["Close"].rolling(5).mean()
            df_stock["SMA20"] = df_stock["Close"].rolling(20).mean()
            df_stock["SMA60"] = df_stock["Close"].rolling(60).mean()
            
            df_stock["Amount"] = df_stock["Close"] * df_stock["Volume"]
            avg_amount_20 = df_stock["Amount"].tail(20).mean() # 20일 평균 거래대금
            
            recent = df_stock.iloc[-1]
            prev = df_stock.iloc[-2]
            
            # 조건 1: 20일 평균 거래대금 30억 원 이상
            # 조건 2: 5일선이 20일선 위에 있거나, 오늘 양봉으로 거래량이 급증하며 5일선 돌파
            volume_ratio = (recent["Volume"] / df_stock["Volume"].tail(20).mean()) * 100
            
            is_bullish_ma = recent["SMA5"] >= recent["SMA20"]
            is_volume_spike = volume_ratio >= 150 # 평소 대비 150% 이상 거래량 증가
            
            if avg_amount_20 >= 3_000_000_000 and (is_bullish_ma or is_volume_spike):
                # 종목명 예외 처리 방지
                matched_name = stock_info.get("name")
                if not matched_name:
                    name_series = df_krx.loc[df_krx["Code"] == code, "Name"]
                    matched_name = name_series.values[0] if not name_series.empty else code

                matched_results.append({
                    "code": code,
                    "name": matched_name,
                    "theme": theme_name,
                    "close": int(recent["Close"]),
                    "change_rate": round(((recent["Close"] - prev["Close"]) / prev["Close"]) * 100, 2),
                    "amount_bg": round(avg_amount_20 / 100_000_000, 1), # 억원
                    "vol_ratio": round(volume_ratio, 1)
                })
        except Exception:
            continue
            
    # 3) 결과 정렬 (등락률 및 거래대금 기준)
    matched_results.sort(key=lambda x: (x["theme"] != "일반 거래대금 우량주", x["change_rate"]), reverse=True)
    
    # 4) 텔레그램 결과 발송 리포트 작성
    if matched_results:
        msg = f"🔥 *[당일 주도 테마 & 종가 스크리너 리포트]* ({now_str})\n\n"
        
        current_theme = ""
        for item in matched_results[:12]: # 상위 12개 종목 노출
            if item["theme"] != current_theme:
                current_theme = item["theme"]
                msg += f"\n📌 *테마: {current_theme}*\n"
                
            flag = "🔺" if item["change_rate"] > 0 else "🔻"
            msg += (
                f"• *{item['name']}* ({item['code']})\n"
                f"  - 현재가: {item['close']:,}원 ({flag}{item['change_rate']}%)\n"
                f"  - 20일 평균거래대금: {item['amount_bg']}억원 (거래량 {item['vol_ratio']}%)\n"
            )
    else:
        msg = f"ℹ️ *[당일 주도 테마 스크리너]* ({now_str})\n오늘 기술적 조건과 테마 이슈에 부합하는 종목이 없습니다."

    print(msg)
    send_telegram_msg(msg)

if __name__ == "__main__":
    run_theme_stock_screener()
