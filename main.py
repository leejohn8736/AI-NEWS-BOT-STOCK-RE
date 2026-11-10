"""
종가 주식 스크리너 - 네이버 금융 기반 실시간 테마 및 안정적 크롤링 메인 스크립트
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

# 디렉토리 설정
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------------
# 1. 텔레그램 메시지 전송 함수
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
# 2. 네이버 금융 주도 테마 및 종목 수집
# ----------------------------------------------------------------------
def fetch_today_hot_themes(top_n_themes: int = 5) -> dict[str, list[dict[str, str]]]:
    url = "https://finance.naver.com/sise/theme.naver"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    theme_data = {}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        
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
        
        for theme in top_themes:
            try:
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
                    "stocks": stocks_in_theme[:7]
                }
                time.sleep(0.2)
            except Exception:
                continue
            
    except Exception as e:
        print(f"테마 데이터 수집 중 오류 발생: {e}")
        
    return theme_data

# ----------------------------------------------------------------------
# 3. 야후 파이낸스 기반 개별 종목 데이터 분석 (한국 종목은 .KS / .KQ 부착)
# ----------------------------------------------------------------------
def get_stock_history_yf(code: str):
    """
    야후 파이낸스를 통해 종목 일봉 데이터를 안전하게 가져옵니다.
    코스피는 .KS, 코스닥은 .KQ를 시도합니다.
    """
    import yfinance as yf
    for suffix in [".KS", ".KQ"]:
        ticker_symbol = code + suffix
        try:
            df = yf.download(ticker_symbol, period="6mo", progress=False, auto_adjust=True)
            if not df.empty and len(df) > 30:
                # MultiIndex 컬럼 평탄화 방어
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                return df
        except Exception:
            continue
    return None

# ----------------------------------------------------------------------
# 4. 메인 스크리닝 및 알림 로직
# ----------------------------------------------------------------------
def run_theme_stock_screener():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[{now_str}] 실시간 테마 주도주 종가 스크리닝 시작...")
    
    hot_themes = fetch_today_hot_themes(top_n_themes=5)
    
    theme_stock_map = {}
    for t_name, t_info in hot_themes.items():
        for s in t_info["stocks"]:
            theme_stock_map[s["code"]] = {
                "name": s["name"],
                "theme": t_name,
                "theme_rate": t_info["rate"]
            }
            
    print(f"포착된 상위 주도 테마 수: {len(hot_themes)}개 (총 {len(theme_stock_map)}개 종목 검토)")

    matched_results = []
    
    for code, info in theme_stock_map.items():
        try:
            df_stock = get_stock_history_yf(code)
            if df_stock is None or len(df_stock) < 20:
                continue
                
            df_stock["SMA5"] = df_stock["Close"].rolling(5).mean()
            df_stock["SMA20"] = df_stock["Close"].rolling(20).mean()
            df_stock["Amount"] = df_stock["Close"] * df_stock["Volume"]
            avg_amount_20 = df_stock["Amount"].tail(20).mean()
            
            recent = df_stock.iloc[-1]
            prev = df_stock.iloc[-2]
            
            volume_ratio = (recent["Volume"] / df_stock["Volume"].tail(20).mean()) * 100
            is_bullish_ma = recent["SMA5"] >= recent["SMA20"]
            is_volume_spike = volume_ratio >= 130 # 거래량 130% 이상 급증
            
            # 조건 검증 (20일 평균 거래대금 20억 이상 및 기술적 조건)
            if avg_amount_20 >= 2_000_000_000 and (is_bullish_ma or is_volume_spike):
                matched_results.append({
                    "code": code,
                    "name": info["name"],
                    "theme": info["theme"],
                    "close": int(recent["Close"]),
                    "change_rate": round(((recent["Close"] - prev["Close"]) / prev["Close"]) * 100, 2),
                    "amount_bg": round(avg_amount_20 / 100_000_000, 1),
                    "vol_ratio": round(volume_ratio, 1)
                })
        except Exception as e:
            print(f"종목 {code} 분석 중 예외: {e}")
            continue
            
    matched_results.sort(key=lambda x: x["change_rate"], reverse=True)
    
    if matched_results:
        msg = f"🔥 *[종가 주식 스크리너 리포트]* ({now_str})\n\n"
        current_theme = ""
        for item in matched_results[:10]:
            if item["theme"] != current_theme:
                current_theme = item["theme"]
                msg += f"\n📌 *테마: {current_theme}*\n"
                
            flag = "🔺" if item["change_rate"] > 0 else "🔻"
            msg += (
                f"• *{item['name']}* ({item['code']})\n"
                f"  - 현재가: {item['close']:,}원 ({flag}{item['change_rate']}%)\n"
                f"  - 20일 평균거래대금: {item['amount_bg']}억원\n"
            )
    else:
        msg = f"ℹ️ *[종가 주식 스크리너]* ({now_str})\n현재 조건에 부합하는 주도 테마 종목이 없습니다."

    print(msg)
    send_telegram_msg(msg)

if __name__ == "__main__":
    run_theme_stock_screener()
