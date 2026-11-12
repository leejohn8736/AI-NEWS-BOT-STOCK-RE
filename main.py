import os
import json
import html
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from google import genai

# 1. 환경 변수에서 설정값 불러오기
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 2. 감시할 키워드 목록
KEYWORDS = [
    "삼성전자", "AI", "반도체", "증권", "부동산",
    "금리", "주식", "현대차", "SK하이닉스", "배터리", "2차전지",
    "한국항공우주", "방산", "원전", "조선",
    "금", "구리", "통신", "보안", "해킹", "보안주",
    "전쟁", "에너지", "화학", "뷰티", "화장품",
    "제약주", "바이오", "데이터 센터", "데이터 센타",
    "트럼프 대통령", "젠슨황", "엔비디아",
    "항공", "유가", "유류", "석유", "수혜",
    "피지컬 AI", "로봇", "게임", "온라인 게임"
]

SENT_LINKS_FILE = "sent_links.txt"
RUN_COUNT_FILE = "run_count.txt"

def mask_sensitive_info(text):
    if not text:
        return text
    if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN in text:
        text = text.replace(TELEGRAM_BOT_TOKEN, "[SECRET_TOKEN]")
    if GEMINI_API_KEY and GEMINI_API_KEY in text:
        text = text.replace(GEMINI_API_KEY, "[SECRET_KEY]")
    return text

def load_run_count():
    if os.path.exists(RUN_COUNT_FILE):
        try:
            with open(RUN_COUNT_FILE, "r", encoding="utf-8") as f:
                return int(f.read().strip())
        except Exception:
            pass
    return 0

def save_run_count(count):
    try:
        with open(RUN_COUNT_FILE, "w", encoding="utf-8") as f:
            f.write(str(count))
    except Exception:
        pass

def load_sent_links():
    if os.path.exists(SENT_LINKS_FILE):
        try:
            with open(SENT_LINKS_FILE, "r", encoding="utf-8") as f:
                return set(line.strip() for line in f if line.strip())
        except Exception:
            pass
    return set()

def save_sent_links(sent_links):
    try:
        with open(SENT_LINKS_FILE, "w", encoding="utf-8") as f:
            for link in sent_links:
                f.write(f"{link}\n")
    except Exception:
        print("❌ 전송 기록 저장 오류 발생")

def send_telegram_msg(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    if len(text) > 3800:
        text = text[:3800] + "\n\n...(내용이 길어 생략됩니다)"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return True
    except Exception as e:
        print(f"❌ 텔레그램 발송 오류: {mask_sensitive_info(str(e))}")
        return False

def fetch_rss_news(keyword):
    encoded_keyword = urllib.parse.quote(keyword)
    rss_url = f"https://news.google.com/rss/search?q={encoded_keyword}&hl=ko&gl=KR&ceid=KR:ko"
    
    req = urllib.request.Request(rss_url, headers={'User-Agent': 'Mozilla/5.0 (SecureNewsBot/1.0)'})
    articles = []
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read().decode('utf-8')
            items = xml_data.split('<item>')
            
            for item in items[1:]:
                pub_date_str = ""
                if '<pubDate>' in item and '</pubDate>' in item:
                    pub_date_str = item.split('<pubDate>')[1].split('</pubDate>')[0].strip()
                
                is_within_15_min = False
                pub_formatted = ""
                
                if pub_date_str:
                    try:
                        dt = parsedate_to_datetime(pub_date_str).astimezone(kst)
                        pub_formatted = dt.strftime("%H:%M")
                        time_diff_seconds = (now_kst - dt).total_seconds()
                        
                        if 0 <= time_diff_seconds <= 900:
                            is_within_15_min = True
                    except Exception:
                        pass
                
                if not is_within_15_min:
                    continue

                title = ""
                if '<title>' in item and '</title>' in item:
                    title = item.split('<title>')[1].split('</title>')[0]
                    title = title.replace('<![CDATA[', '').replace(']]>', '').strip()
                
                link = ""
                if '<link>' in item and '</link>' in item:
                    link = item.split('<link>')[1].split('</link>')[0].strip()
                
                source = "언론사 미상"
                if '<source' in item and '</source>' in item:
                    source = item.split('>')[1].split('</source>')[0].strip()

                if keyword in title and link:
                    articles.append({
                        'keyword': keyword,
                        'title': title,
                        'link': link,
                        'source': source,
                        'pub_time': pub_formatted
                    })
    except Exception as e:
        print(f"❌ [{keyword}] 뉴스 수집 실패: {mask_sensitive_info(str(e))}")
        
    return articles

def generate_ai_integrated_report(articles_list):
    if not GEMINI_API_KEY:
        return None

    news_text_block = ""
    for idx, item in enumerate(articles_list[:12], 1):
        safe_src = html.escape(item['source'])
        news_text_block += f"{idx}. [{item['keyword']}] {item['title']} ({safe_src}, {item['pub_time']})\n"

    prompt = f"""
다음은 수집된 실시간 시장 뉴스 목록입니다. 이 데이터를 바탕으로 간결하고 핵심만 담긴 투자 브리핑 리포트를 작성해 주세요.

[뉴스 목록]
{news_text_block}

[작성 조건]
1. 상단에 🤖 <b>[AI 실시간 뉴스 & 테마/섹터 분석]</b> 제목을 붙일 것.
2. 핵심 이슈를 **[테마] 및 [섹터]**별로 간단히 요약할 것.
3. 하단에 📈 <b>[시장 수혜주 추천]</b> 섹션을 추가할 것.
4. 시가총액 상위 대형주는 철저히 제외하고, 성장성 있는 일반 중소형주 위주로 선정할 것:
   - **[코스피 수혜주]** (총 10개: 1등주 5개, 2등주 5개)
   - **[코스닥 수혜주]** (총 10개: 1등주 5개, 2등주 5개)
5. 텔레그램 전송용이므로 분량이 너무 길어지지 않게 핵심만 압축해서 작성할 것.
"""

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"⚠️ Gemini AI 호출 제한 또는 에러 발생 (기본 뉴스 모음으로 대체됩니다): {mask_sensitive_info(str(e))}")
        return None

def main():
    # 🕒 운영 시간 체크 (새벽 4시 ~ 밤 11시 50분 KST)
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    current_hour = now_kst.hour
    current_minute = now_kst.minute
    current_total_minutes = current_hour * 60 + current_minute

    start_minutes = 4 * 60          # 새벽 4시 (240분)
    end_minutes = 23 * 60 + 50      # 밤 11시 50분 (1430분)

    if not (start_minutes <= current_total_minutes <= end_minutes):
        print(f"😴 현재 시각({now_kst.strftime('%H:%M')})은 설정된 운영 시간(04:00 ~ 23:50)이 아닙니다. 대기합니다.")
        return

    print("🚀 실시간 뉴스 수집 및 통합 AI 리포트 로봇 실행...")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("🚨 필수 보안 설정: TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 환경 변수가 설정되지 않았습니다.")
        return

    current_run = load_run_count() + 1
    save_run_count(current_run)
    print(f"ℹ️ 현재 실행 회차: {current_run}회차")

    sent_links = load_sent_links()
    collected_articles = []
    
    for keyword in KEYWORDS:
        articles = fetch_rss_news(keyword)
        for article in articles:
            if article['link'] not in sent_links:
                collected_articles.append(article)

    if not collected_articles:
        print("ℹ️ 최근 15분 이내 신규 뉴스가 없습니다.")
        return

    ai_content = generate_ai_integrated_report(collected_articles)
    
    if ai_content:
        message = ai_content
        if collected_articles:
            message += "\n\n<b>📌 주요 기사 링크:</b>\n"
            for item in collected_articles[:5]:
                safe_title = html.escape(item['title'])
                message += f"• <a href='{item['link']}'>{safe_title}</a>\n"
    else:
        message = "🚨 <b>[실시간 신규 뉴스 모음 (일시적 AI 한도 초과 대체)]</b>\n\n"
        for item in collected_articles[:10]:
            safe_title = html.escape(item['title'])
            safe_kw = html.escape(item['keyword'])
            message += f"<b>[{safe_kw}]</b> {safe_title}\n🔗 <a href='{item['link']}'>기사 보기</a>\n\n"

    if send_telegram_msg(message):
        if collected_articles:
            for item in collected_articles:
                sent_links.add(item['link'])
            save_sent_links(sent_links)
        print(f"🎉 {current_run}회차 리포트 발송 완료했습니다.")

if __name__ == '__main__':
    main()
