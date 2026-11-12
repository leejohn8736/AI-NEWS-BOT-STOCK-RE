import os
import sys
import json
import time
import html
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from google import genai

# 1. Secrets에서 설정값 불러오기
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 2. 감시할 키워드 목록
KEYWORDS = [
    "삼성전자", "AI", "반도체", "증권", "부동산",
    "금리", "주식", "현대차", "SK하이닉스", "배터리"
]

SENT_LINKS_FILE = "sent_links.txt"

def load_sent_links():
    if os.path.exists(SENT_LINKS_FILE):
        with open(SENT_LINKS_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_sent_links(sent_links):
    with open(SENT_LINKS_FILE, "w", encoding="utf-8") as f:
        for link in sent_links:
            f.write(f"{link}\n")

def send_telegram_msg(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ 텔레그램 설정값이 없습니다.")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
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
        print(f"❌ 텔레그램 발송 오류: {e}")
        return False

def fetch_rss_news(keyword):
    """최근 15분 이내 뉴스만 수집"""
    encoded_keyword = urllib.parse.quote(keyword)
    rss_url = f"https://news.google.com/rss/search?q={encoded_keyword}&hl=ko&gl=KR&ceid=KR:ko"
    
    req = urllib.request.Request(rss_url, headers={'User-Agent': 'Mozilla/5.0'})
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
                        
                        if 0 <= time_diff_seconds <= 900:  # 15분 이내
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
        print(f"❌ [{keyword}] 뉴스 수집 실패: {e}")
        
    return articles

def generate_ai_briefing(articles_list):
    """Gemini 3.6 Flash 모델을 사용하여 브리핑 작성"""
    if not GEMINI_API_KEY:
        print("⚠️ GEMINI_API_KEY가 없어 기본 목록 형태로 발송합니다.")
        return None

    news_text_block = ""
    for idx, item in enumerate(articles_list, 1):
        news_text_block += f"{idx}. [{item['keyword']}] {item['title']} ({item['source']}, {item['pub_time']})\n"

    prompt = f"""
다음은 방금 수집된 실시간 뉴스 목록입니다.
이 뉴스들을 바탕으로 텔레그램으로 보낼 '실시간 AI 뉴스 브리핑'을 작성해 주세요.

[뉴스 목록]
{news_text_block}

[작성 조건]
1. HTML 태그(<b>, <i> 등)를 적절히 활용하여 읽기 쉽게 가독성을 높일 것.
2. 상단에 🤖 <b>[AI 실시간 뉴스 브리핑]</b> 제목을 붙일 것.
3. 주요 뉴스들을 핵심 주제별로 2~3문장 내외로 명확하게 요약 정리할 것.
4. 문맥이 매끄럽고 보고서처럼 인사이트를 줄 수 있도록 정리할 것.
"""

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        # 최신 지정 표준 모델명 'gemini-3.6-flash' 사용
        response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"❌ Gemini AI 요약 생성 실패: {e}")
        return None

def main():
    print("🚀 실시간 뉴스 수집 및 AI 브리핑 로봇 실행...")
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

    ai_briefing = generate_ai_briefing(collected_articles)
    
    if ai_briefing:
        message = ai_briefing + "\n\n<b>📌 관련 기사 링크:</b>\n"
        for item in collected_articles:
            safe_title = html.escape(item['title'])
            message += f"• <a href='{item['link']}'>{safe_title}</a>\n"
    else:
        message = "🚨 <b>[실시간 신규 뉴스 모음]</b>\n\n"
        for item in collected_articles:
            safe_title = html.escape(item['title'])
            safe_kw = html.escape(item['keyword'])
            message += f"<b>[{safe_kw}]</b> {safe_title}\n🔗 <a href='{item['link']}'>기사 보기</a>\n\n"

    if send_telegram_msg(message):
        for item in collected_articles:
            sent_links.add(item['link'])
        save_sent_links(sent_links)
        print(f"🎉 총 {len(collected_articles)}건의 뉴스를 브리핑하여 발송 완료했습니다.")

if __name__ == "__main__":
    main()
