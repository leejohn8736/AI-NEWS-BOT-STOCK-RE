import os
import json
import html
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from flask import Flask, request, abort
from google import genai

app = Flask(__name__)

# 1. 환경 변수에서 설정값 불러오기
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 🔒 cron-job.org만 알 수 있는 나만의 비밀 열쇠 (원하는 영어/숫자로 변경 가능)
CRON_SECRET_KEY = os.environ.get("CRON_SECRET_KEY", "my_secret_key_1234")

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

def generate_ai_briefing(articles_list, is_odd_turn):
    if not GEMINI_API_KEY:
        return None

    news_text_block = ""
    for idx, item in enumerate(articles_list, 1):
        safe_src = html.escape(item['source'])
        news_text_block += f"{idx}. [{item['keyword']}] {item['title']} ({safe_src}, {item['pub_time']})\n"

    if is_odd_turn:
        # 홀수 회차: 뉴스 브리핑 + 핵심 요약 + 테마 및 섹터 정리
        prompt = f"""
다음은 방금 수집된 실시간 뉴스 목록입니다.
이 뉴스들을 바탕으로 텔레그램으로 보낼 '실시간 AI 뉴스 브리핑'을 작성해 주세요.

[뉴스 목록]
{news_text_block}

[작성 조건]
1. 상단에 🤖 <b>[AI 실시간 뉴스 브리핑 & 테마/섹터 분석]</b> 제목을 붙일 것.
2. 수집된 뉴스들을 바탕으로 주요 소식을 테마와 섹터별로 묶어 정리할 것.
3. 각 뉴스(또는 테마별 그룹)마다 **핵심 요약(2~3문장)**과 함께 해당 뉴스가 속한 **[테마] 및 [섹터]**를 명시할 것.
4. HTML 태그(<b>, <i> 등)를 적극 활용하여 가독성을 높일 것.
"""
    else:
        # 짝수 회차: 코스피 5개, 코스닥 5개 (대형주 제외, 일반 중소형주 수혜주 추천)
        prompt = f"""
다음은 최근 수집된 시장 뉴스 데이터입니다.
이 데이터를 참고하여 현재 시점에서 주목할 만한 양호한 주식 종목을 추천해 주세요.

[작성 조건]
1. 상단에 📈 <b>[AI 중소형/일반주 수혜주 추천 리포트]</b> 제목을 붙일 것.
2. **코스피 종목 5개**, **코스닥 종목 5개**를 선정할 것.
3. **절대로 시가총액 상위 대형주(삼성전자, SK하이닉스, 현대차 등)는 제외**하고, 성장성 있는 **일반 중소형주 위주**로 선정할 것.
4. 각 종목별로 간단한 추천 사유나 관련 수혜 모멘텀을 1~2문장으로 함께 기재할 것.
5. HTML 태그(<b>, <i> 등)를 활용하여 가독성 좋게 작성할 것.
"""

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"❌ Gemini AI 콘텐츠 생성 실패: {mask_sensitive_info(str(e))}")
        return None

def run_news_bot():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return "환경 변수 누락"

    current_run = load_run_count() + 1
    save_run_count(current_run)
    is_odd_turn = (current_run % 2 != 0)

    sent_links = load_sent_links()
    collected_articles = []
    
    for keyword in KEYWORDS:
        articles = fetch_rss_news(keyword)
        for article in articles:
            if article['link'] not in sent_links:
                collected_articles.append(article)

    if not collected_articles and is_odd_turn:
        return f"{current_run}회차: 신규 뉴스 없음"

    ai_content = generate_ai_briefing(collected_articles, is_odd_turn)
    
    if ai_content:
        message = ai_content
        if is_odd_turn and collected_articles:
            message += "\n\n<b>📌 관련 기사 링크:</b>\n"
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
        if collected_articles:
            for item in collected_articles:
                sent_links.add(item['link'])
            save_sent_links(sent_links)
        return f"{current_run}회차 발송 성공!"
    return "텔레그램 발송 실패"

# 🌐 cron-job.org가 접속할 웹 주소 경로
@app.route('/run')
def trigger_bot():
    user_key = request.args.get('key')
    if user_key != CRON_SECRET_KEY:
        abort(403)
        
    result = run_news_bot()
    return f"OK - {result}", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
