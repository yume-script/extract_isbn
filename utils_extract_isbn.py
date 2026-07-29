# -*- coding: utf-8 -*-
"""
Extract ISBN 플러그인 전용 유틸 모듈.
unified_book 플러그인의 utils_unified.py 중 ISBN 추출과 직접 관련된 부분만
분리/이식했습니다. (검색/서점 API 연동 로직은 포함하지 않음)
"""
import os
import re
import sys
import json
import html
import zipfile
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False


ISBN_PATTERN = re.compile(
    r'\b(?:97[89][-\s.]?)?\d{1,5}[-\s.]?\d{1,7}[-\s.]?\d{1,6}[-\s.]?[\dX]\b'
)

# ISBN-13은 978/979 접두사 + 체크디지트로 이미 상당히 구체적이라 별도 문맥 확인 없이 신뢰합니다.
# ISBN-10은 접두사가 없어 체크디지트만으로는 우연히 통과하는 임의의 숫자열(전화번호/일련번호 등)과
# 구분이 어렵습니다(약 1/11 확률로 우연히 통과). 그래서 매치 주변에 "ISBN" 문구가 있는지로
# 신뢰도를 나눕니다.
ISBN10_CONTEXT_WINDOW = 30
ISBN10_CONTEXT_KEYWORDS = ('isbn', 'ｉｓｂｎ')  # 전각 변형 등 필요시 추가

# 로컬 스캔 시 앞/뒤로 살펴볼 구간 수. 값을 줄이면 I/O와 LLM 프롬프트 크기가 함께 줄어들어
# 더 효율적으로 동작하지만, 판권지가 이 범위 밖에 있으면 놓칠 수 있으니 필요시 조정하십시오.
EPUB_SCAN_SECTIONS = 5   # 앞 N개 + 뒤 N개 spine(챕터) 파일
PDF_SCAN_PAGES = 5       # 앞 N페이지 + 뒤 N페이지
# TXT 앞/뒤에서 각각 읽어들일 바이트 수 (판권지가 파일 앞쪽/뒤쪽 어디에 있어도 대응)
TXT_SCAN_BYTES = 8000
# LLM 프롬프트에 실어보낼 최대 문자 수
LLM_TEXT_LIMIT = 12000
# LLM 호출 기본 타임아웃(초). 플러그인 설정(REQUEST_TIMEOUT_SEC)으로 덮어쓸 수 있습니다.
DEFAULT_LLM_TIMEOUT_SEC = 15
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"


class StepLogger:
    """추출 과정의 각 단계를 순서대로 기록하는 경량 로거.

    - log()를 호출할 때마다 내부 리스트에 쌓는 동시에 서버 stderr(콘솔/도커 로그)에도
      같은 내용을 즉시 남긴다. 그래서 UI 결과 카드까지 확인하지 않아도 서버 로그만으로
      추적할 수 있다.
    - as_text()는 결과 카드의 summary 뒤에 그대로 덧붙일 수 있는 번호 매김 텍스트를 만든다.
    - 원문 텍스트(판권지 본문 등)는 절대 로그에 남기지 않는다. 파일명/구간 번호/개수/성공여부
      같은 메타 정보만 기록한다.
    """

    def __init__(self, prefix="[ISBN추출]"):
        self._entries = []
        self._prefix = prefix

    def log(self, message):
        self._entries.append(str(message))
        try:
            print(f"{self._prefix} {message}", file=sys.stderr)
        except Exception:
            pass

    def as_text(self):
        if not self._entries:
            return ""
        numbered = "\n".join(f"{i + 1}. {m}" for i, m in enumerate(self._entries))
        return f"\n\n[처리 단계 로그]\n{numbered}"


def _log(logger, message):
    """logger가 None이어도 안전하게 무시되는 헬퍼 (기존 호출부와의 하위 호환용)."""
    if logger is not None:
        logger.log(message)


def get_row_val(row, key, default=''):
    """sqlite3.Row 및 dict 호환을 위해 에러 없이 안전하게 값을 추출하는 헬퍼"""
    try:
        val = row[key]
        return val if val is not None else default
    except (KeyError, TypeError, IndexError):
        return default


def validate_isbn13(isbn):
    """ISBN-13 체크디지트 검사 (Mod 10 방식)"""
    if len(isbn) != 13:
        return False
    try:
        digits = [int(char) for char in isbn]
        checksum = sum(d * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
        return checksum % 10 == 0
    except ValueError:
        return False


def validate_isbn10(isbn):
    """ISBN-10 체크디지트 검사 (Mod 11 방식)"""
    if len(isbn) != 10:
        return False
    try:
        val = 0
        for i in range(9):
            val += int(isbn[i]) * (10 - i)
        last = isbn[9]
        if last == 'X':
            val += 10
        else:
            val += int(last)
        return val % 11 == 0
    except ValueError:
        return False


def extract_isbn_via_llm(text, api_key, endpoint=None, model=None, book_title=None, file_name=None,
                          timeout_sec=None, logger=None):
    """구글 Gemini API 및 LiteLLM(OpenAI 호환) 프록시를 모두 지원하는 통합 지능형 판독 엔진.

    book_title/file_name은 어디까지나 '참고용 힌트'이며, 실제 ISBN은 반드시 text 안에
    실존하는 문자열에서만 추출하도록 프롬프트에 강하게 못박아 둔다. 이렇게 하지 않으면
    모델이 본문에서 못 찾았을 때 자기 사전지식으로 '그럴듯한' ISBN을 추측해 답할 위험이 있고,
    그런 값은 체크디지트 검증은 통과하지만 실제로는 틀린 값일 수 있다.
    """
    if not text or not text.strip():
        _log(logger, "AI 판독 생략: 본문 텍스트가 비어 있음")
        return None

    timeout = timeout_sec if timeout_sec and timeout_sec > 0 else DEFAULT_LLM_TIMEOUT_SEC
    gemini_model = (model.strip() if (model and model.strip() and not endpoint) else DEFAULT_GEMINI_MODEL)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={api_key}"

    ref_lines = []
    if book_title:
        ref_lines.append(f"- 도서 제목(참고용 힌트, 본문 검색에만 활용): {book_title}")
    if file_name:
        ref_lines.append(f"- 파일명(참고용 힌트, 본문 검색에만 활용): {file_name}")
    ref_block = ("[참고 정보]\n" + "\n".join(ref_lines) + "\n\n") if ref_lines else ""

    prompt = (
        "다음 도서 판권지/본문 텍스트에서 ISBN 번호만 추출해줘.\n"
        f"{ref_block}"
        "중요 규칙(반드시 지킬 것):\n"
        "1) 참고 정보(제목/파일명)는 아래 [텍스트 본문]에서 ISBN을 더 잘 찾기 위한 힌트일 뿐이다.\n"
        "2) 절대로 너의 사전 지식이나 추측으로 ISBN을 만들어내면 안 된다. 오직 [텍스트 본문]에 "
        "실제로 등장하는 숫자 조합만 답으로 사용해야 한다.\n"
        "3) [텍스트 본문] 안에서 유효한 ISBN을 찾지 못했다면, 절대 추측하지 말고 빈 문자열을 반환하라.\n"
        "출력은 반드시 다른 미사여구 없이 JSON 형식으로만 해야 하며, 그 구조는 반드시 다음 스키마를 따라야 해:\n"
        "{\"isbn\": \"공백이나 하이픈을 제거한 오직 10자리 또는 13자리 숫자(마지막 X 허용) 문자열 (본문에서 발견되지 않으면 반드시 빈 문자열)\"}\n\n"
        f"[텍스트 본문]\n{text}"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
            "maxOutputTokens": 100
        }
    }

    # 1. LiteLLM / OpenAI 호환 모드
    if endpoint and endpoint.strip():
        url = endpoint.strip()
        target_model = model.strip() if model and model.strip() else "gemini/gemini-3.5-flash-lite"
        _log(logger, f"AI 판독 요청 전송 (LiteLLM 모드, 모델: {target_model}, 타임아웃: {timeout}초, 본문 길이: {len(text)}자)")

        payload = {
            "model": target_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"}
        }

        headers = {'Content-Type': 'application/json'}
        if api_key and api_key.strip():
            headers['Authorization'] = f"Bearer {api_key.strip()}"

        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                res_data = json.loads(response.read().decode('utf-8'))
                choices = res_data.get('choices', [])
                if choices:
                    raw_content = choices[0].get('message', {}).get('content', '').strip()
                    res_json = json.loads(raw_content)
                    raw_isbn = res_json.get('isbn', '')
                    clean = re.sub(r'[^0-9X]', '', str(raw_isbn).upper())
                    if validate_isbn13(clean) or validate_isbn10(clean):
                        _log(logger, f"AI 판독 성공(LiteLLM): {clean}")
                        return clean
                    _log(logger, "AI 판독 응답 수신했으나 유효한 ISBN 아님 (빈 값이거나 체크디지트 불일치)")
                else:
                    _log(logger, "AI 판독 응답에 choices가 없음")
        except urllib.error.HTTPError as he:
            error_msg = he.read().decode('utf-8', errors='ignore')
            _log(logger, f"AI 판독 실패(LiteLLM HTTP {he.code})")
            print(f"[LiteLLM API HTTP 에러 {he.code}] 이유: {error_msg}", file=sys.stderr)
        except Exception as e:
            _log(logger, f"AI 판독 실패(LiteLLM 예외): {str(e)}")
            print(f"[LiteLLM API 에러] 사유: {str(e)}", file=sys.stderr)

    # 2. 순수 Google Gemini 공식 API 모드
    else:
        if not api_key:
            _log(logger, "AI 판독 생략: API Key와 LiteLLM 엔드포인트 모두 미설정")
            return None
        _log(logger, f"AI 판독 요청 전송 (Gemini 다이렉트 모드, 모델: {gemini_model}, 타임아웃: {timeout}초, 본문 길이: {len(text)}자)")
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                res_data = json.loads(response.read().decode('utf-8'))
                candidates = res_data.get('candidates', [])
                if candidates:
                    parts = candidates[0].get('content', {}).get('parts', [])
                    if parts:
                        raw_text = parts[0].get('text', '').strip()
                        res_json = json.loads(raw_text)
                        raw_isbn = res_json.get('isbn', '')
                        clean = re.sub(r'[^0-9X]', '', str(raw_isbn).upper())
                        if validate_isbn13(clean) or validate_isbn10(clean):
                            _log(logger, f"AI 판독 성공(Gemini): {clean}")
                            return clean
                        _log(logger, "AI 판독 응답 수신했으나 유효한 ISBN 아님 (빈 값이거나 체크디지트 불일치)")
                    else:
                        _log(logger, "AI 판독 응답에 parts가 없음")
                else:
                    _log(logger, "AI 판독 응답에 candidates가 없음")
        except urllib.error.HTTPError as he:
            error_msg = he.read().decode('utf-8', errors='ignore')
            _log(logger, f"AI 판독 실패(Gemini HTTP {he.code})")
            print(f"[Gemini API HTTP 에러 {he.code}] 이유: {error_msg}", file=sys.stderr)
        except Exception as e:
            _log(logger, f"AI 판독 실패(Gemini 예외): {str(e)}")
            print(f"[Gemini API 에러] 사유: {str(e)}", file=sys.stderr)

    return None


def _has_isbn_context(text_content, match_start, match_end):
    """매치된 위치 앞뒤 ISBN10_CONTEXT_WINDOW 글자 안에 'ISBN' 문구가 있는지 확인.
    있으면 우연히 통과한 임의 숫자열이 아니라 실제 ISBN 표기일 가능성이 높다고 본다.
    """
    window_start = max(0, match_start - ISBN10_CONTEXT_WINDOW)
    window_end = min(len(text_content), match_end + ISBN10_CONTEXT_WINDOW)
    context = text_content[window_start:window_end].lower()
    return any(kw in context for kw in ISBN10_CONTEXT_KEYWORDS)


def _scan_text_for_isbn(text_content):
    """텍스트 블록 하나에서 ISBN13(우선) 또는 ISBN10 후보를 추출.

    ISBN13은 접두사(978/979)+체크디지트만으로 오탐 가능성이 낮아 즉시 확정 채택한다.
    ISBN10은 체크디지트만으로는 우연히 통과하는 숫자열(전화번호, 일련번호 등)과 구분이
    어려우므로, 주변에 'ISBN' 문구가 있는 것(confident)과 없는 것(weak)을 분리해서 반환한다.

    반환: (isbn13_확정값 또는 None, isbn10_confident_리스트, isbn10_weak_리스트)
    """
    isbn10_confident = []
    isbn10_weak = []
    for match in ISBN_PATTERN.finditer(text_content):
        raw = match.group(0)
        clean = re.sub(r'[^0-9X]', '', raw.upper())
        if validate_isbn13(clean):
            return clean, isbn10_confident, isbn10_weak
        elif validate_isbn10(clean):
            if _has_isbn_context(text_content, match.start(), match.end()):
                isbn10_confident.append(clean)
            else:
                isbn10_weak.append(clean)
    return None, isbn10_confident, isbn10_weak


def _finalize_isbn10(compiled_texts, isbn10_confident, isbn10_weak,
                      gemini_key, llm_endpoint, llm_model, timeout_sec, book_title, file_name,
                      logger=None):
    """앞/뒤 스캔이 끝난 뒤 ISBN10 후보를 최종 판정한다.

    우선순위: 문맥 확인된(confident) 후보 > (AI 사용 가능하면) AI 교차검증 > 문맥 미확인(weak) 후보.
    즉, weak 후보만 있고 AI를 쓸 수 있는 상황이라면 weak 값을 바로 채택하지 않고 AI로 한 번
    검증해서 오탐 가능성을 줄인다.
    """
    _log(
        logger,
        f"본문 스캔 완료 — ISBN13: 미발견, ISBN10 신뢰 후보 {len(isbn10_confident)}개, "
        f"문맥 미확인 후보 {len(isbn10_weak)}개"
    )

    if isbn10_confident:
        _log(logger, f'"ISBN" 문구 근접 확인된 신뢰 후보 채택: {isbn10_confident[0]} (LOCAL)')
        return isbn10_confident[0], "LOCAL"

    ai_available = bool(gemini_key or (llm_endpoint and llm_endpoint.strip()))

    if isbn10_weak and not ai_available:
        _log(logger, f"AI 미설정 상태 — 문맥 미확인 후보를 그대로 채택: {isbn10_weak[0]} (LOCAL_WEAK)")
        return isbn10_weak[0], "LOCAL_WEAK"

    if compiled_texts and ai_available:
        _log(logger, "로컬 매칭 결과가 불확실하여 AI 교차검증 단계로 진행")
        full_text = "\n".join(compiled_texts)[:LLM_TEXT_LIMIT]
        llm_isbn = extract_isbn_via_llm(
            full_text,
            api_key=gemini_key,
            endpoint=llm_endpoint,
            model=llm_model,
            book_title=book_title,
            file_name=file_name,
            timeout_sec=timeout_sec,
            logger=logger,
        )
        if llm_isbn:
            return llm_isbn, "AI"

    if isbn10_weak:
        _log(logger, f"AI 판독 실패/미설정으로 문맥 미확인 후보로 최종 폴백: {isbn10_weak[0]} (LOCAL_WEAK)")
        return isbn10_weak[0], "LOCAL_WEAK"

    _log(logger, "ISBN 후보를 전혀 찾지 못함")
    return None, None


def extract_isbn_from_epub(epub_path, gemini_key=None, llm_endpoint=None, llm_model=None,
                            book_title=None, file_name=None, timeout_sec=None, logger=None):
    """EPUB 내부 컨테이너 구조 및 본문 파일 분석 후 ISBN 추출 (지능형 LLM 듀얼 분기 가동)"""
    _log(logger, f"EPUB 열기 시도: {os.path.basename(epub_path)}")
    try:
        with zipfile.ZipFile(epub_path, 'r') as epub:
            container_content = epub.read('META-INF/container.xml')
            root = ET.fromstring(container_content)
            opf_path = ""
            for elem in root.iter():
                if elem.tag.endswith('rootfile'):
                    opf_path = elem.attrib.get('full-path', '')
                    break
            if not opf_path:
                _log(logger, "container.xml에서 OPF 경로를 찾지 못함 — 손상되었거나 표준 구조가 아닌 EPUB")
                return None, None
            _log(logger, f"OPF 경로 확인: {opf_path}")

            opf_content = epub.read(opf_path)
            opf_root = ET.fromstring(opf_content)

            # 1단계: 표준 메타데이터 태그(<dc:identifier>)에서 ISBN 탐색
            _log(logger, "1단계: <dc:identifier> 메타데이터 태그 확인 중")
            for elem in opf_root.iter():
                if elem.tag.endswith('identifier') and elem.text:
                    clean = re.sub(r'[^0-9X]', '', elem.text.upper())
                    if validate_isbn13(clean) or validate_isbn10(clean):
                        _log(logger, f"메타데이터 태그에서 ISBN 발견: {clean} (LOCAL)")
                        return clean, "LOCAL"
            _log(logger, "메타데이터 태그에서 유효한 ISBN 없음 — 2단계(본문 스캔)로 진행")

            # 2단계 백업: 본문 XHTML 파일 분석 (앞쪽 N장 + 뒤쪽 N장 대역 확장 분석)
            manifest = {}
            for elem in opf_root.iter():
                if elem.tag.endswith('item'):
                    item_id = elem.attrib.get('id')
                    href = elem.attrib.get('href')
                    if item_id and href:
                        manifest[item_id] = href

            spine_item_ids = []
            for elem in opf_root.iter():
                if elem.tag.endswith('itemref'):
                    idref = elem.attrib.get('idref')
                    if idref:
                        spine_item_ids.append(idref)

            num_spines = len(spine_item_ids)
            n = EPUB_SCAN_SECTIONS
            target_spines = list(range(min(n, num_spines)))
            if num_spines > n:
                target_spines.extend(list(range(max(n, num_spines - n), num_spines)))
            target_spines = sorted(list(set(target_spines)))
            _log(logger, f"전체 spine {num_spines}개 중 앞/뒤 {n}개씩, 총 {len(target_spines)}개 구간을 스캔 대상으로 선정")

            opf_dir = os.path.dirname(opf_path)

            # [초고속 조기 종료 필터]: 만화책/스캔본 전용 EPUB 판별
            _log(logger, "스캔본(이미지 전용) 여부 확인을 위해 앞부분 표본 텍스트 추출 중")
            sample_epub_text = ""
            for idx in target_spines[:3]:
                spine_id = spine_item_ids[idx]
                href = manifest.get(spine_id)
                if href:
                    href = urllib.parse.unquote(href)
                    full_href = os.path.join(opf_dir, href) if opf_dir else href
                    full_href = full_href.replace('\\', '/')
                    try:
                        html_data = epub.read(full_href).decode('utf-8', errors='ignore')
                        text_data = re.sub('<[^<]+?>', '', html.unescape(html_data))
                        sample_epub_text += text_data.strip()
                    except Exception:
                        pass
            if len(re.sub(r'\s', '', sample_epub_text)) < 20:
                _log(logger, "표본 텍스트가 20자 미만 — 이미지 전용(스캔본)으로 판단, 추출 중단")
                return None, None  # 이미지 전용책이므로 실시간 수색 종료
            _log(logger, "텍스트 도서로 확인됨 — 본문 정규식 스캔 시작")

            isbn10_confident = []
            isbn10_weak = []
            compiled_texts = []

            for idx in target_spines:
                spine_id = spine_item_ids[idx]
                href = manifest.get(spine_id)
                if href:
                    href = urllib.parse.unquote(href)
                    full_href = os.path.join(opf_dir, href) if opf_dir else href
                    full_href = full_href.replace('\\', '/')

                    try:
                        raw_data = epub.read(full_href).decode('utf-8', errors='ignore')
                        html_content = html.unescape(raw_data)
                        text_content = re.sub('<[^<]+?>', '', html_content)
                        text_content = re.sub(r'[\u2012-\u2015\u00ad.]', '-', text_content)

                        if text_content.strip():
                            compiled_texts.append(text_content)

                        found13, found_conf, found_weak = _scan_text_for_isbn(text_content)
                        if found13:
                            _log(logger, f"구간 스캔 중 ISBN13 즉시 발견 ({os.path.basename(full_href)}): {found13} (LOCAL)")
                            return found13, "LOCAL"
                        isbn10_confident.extend(found_conf)
                        isbn10_weak.extend(found_weak)
                    except Exception as e:
                        _log(logger, f"구간 읽기 실패, 건너뜀 ({os.path.basename(full_href)}): {e}")

            return _finalize_isbn10(
                compiled_texts, isbn10_confident, isbn10_weak,
                gemini_key, llm_endpoint, llm_model, timeout_sec, book_title, file_name,
                logger=logger,
            )

    except Exception as e:
        _log(logger, f"EPUB 처리 중 예외 발생: {e}")
    return None, None


def extract_isbn_from_pdf(pdf_path, gemini_key=None, llm_endpoint=None, llm_model=None,
                           book_title=None, file_name=None, timeout_sec=None, logger=None):
    """PDF 메타데이터 및 전후면 판권 페이지 고속 스캔 (지능형 LLM 듀얼 분기 가동)"""
    if not PYMUPDF_AVAILABLE:
        _log(logger, "PyMuPDF(fitz) 패키지 미설치 감지")
        # 예전에는 여기서 조용히 (None, None)을 반환해 "ISBN을 찾지 못했습니다"라는
        # 오해의 소지가 있는 일반 메시지로만 노출됐다. 원인을 명확히 알 수 있도록 예외로 전파한다.
        # 호출부(extract_isbn.py의 search())가 이미 Exception을 잡아 메시지를 그대로 보여준다.
        raise RuntimeError("PyMuPDF(fitz) 패키지가 설치되어 있지 않아 PDF를 읽을 수 없습니다. 'pip install pymupdf'로 설치해 주세요.")

    _log(logger, f"PDF 열기 시도: {os.path.basename(pdf_path)}")
    doc = None
    try:
        doc = fitz.open(pdf_path)
        num_pages = doc.page_count
        if num_pages == 0:
            _log(logger, "PDF 페이지 수가 0 — 처리 중단")
            return None, None
        _log(logger, f"총 페이지 수: {num_pages}")

        # [초고속 조기 종료 필터]: 스캔본(통 이미지) 전용 PDF 판별
        check_indices = list(range(1, min(6, num_pages)))
        if num_pages > 5:
            check_indices.extend(list(range(max(5, num_pages - 5), num_pages)))
        check_indices = sorted(list(set(check_indices))) or [0]

        sample_text = ""
        for idx in check_indices:
            try:
                p_text = doc[idx].get_text()
                if p_text:
                    sample_text += p_text.strip()
            except Exception:
                pass
        if not sample_text.strip():
            _log(logger, "표본 페이지에서 텍스트 추출 불가 — 스캔본(이미지 전용)으로 판단, 추출 중단")
            return None, None  # 전후방 모두 글자가 전혀 긁히지 않는 스캔 도서
        _log(logger, "텍스트 추출 가능한 PDF로 확인됨")

        pages_to_scan = list(range(min(PDF_SCAN_PAGES, num_pages)))
        if num_pages > PDF_SCAN_PAGES:
            pages_to_scan.extend(list(range(max(PDF_SCAN_PAGES, num_pages - PDF_SCAN_PAGES), num_pages)))
        pages_to_scan = sorted(list(set(pages_to_scan)))
        _log(logger, f"앞/뒤 {PDF_SCAN_PAGES}페이지씩, 총 {len(pages_to_scan)}개 페이지 스캔 시작")

        isbn10_confident = []
        isbn10_weak = []
        compiled_texts = []

        for page_idx in pages_to_scan:
            text = doc[page_idx].get_text()
            if not text:
                continue

            text = re.sub(r'[\u2012-\u2015\u00ad.]', '-', text)

            if text.strip():
                compiled_texts.append(text)

            found13, found_conf, found_weak = _scan_text_for_isbn(text)
            if found13:
                _log(logger, f"{page_idx + 1}페이지에서 ISBN13 즉시 발견: {found13} (LOCAL)")
                return found13, "LOCAL"
            isbn10_confident.extend(found_conf)
            isbn10_weak.extend(found_weak)

        return _finalize_isbn10(
            compiled_texts, isbn10_confident, isbn10_weak,
            gemini_key, llm_endpoint, llm_model, timeout_sec, book_title, file_name,
            logger=logger,
        )

    except RuntimeError:
        raise
    except Exception as e:
        _log(logger, f"PDF 처리 중 예외 발생: {e}")
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
    return None, None


def _decode_bytes(raw, encoding_hints=('utf-8', 'cp949', 'euc-kr')):
    """바이트 청크를 여러 인코딩으로 순차 시도해 디코딩. 모두 실패하면 errors='ignore'로 강제 디코딩."""
    for enc in encoding_hints:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('utf-8', errors='ignore')


def extract_isbn_from_txt(txt_path, gemini_key=None, llm_endpoint=None, llm_model=None,
                           book_title=None, file_name=None, timeout_sec=None, logger=None):
    """TXT 파일 앞/뒤 구간을 스캔하여 ISBN 추출 (지능형 LLM 듀얼 분기 가동).
    판권지가 파일 맨 앞(표지 다음)이나 맨 뒤(colophon) 어디에 있어도 대응하도록
    파일의 앞쪽 TXT_SCAN_BYTES와 뒤쪽 TXT_SCAN_BYTES만 읽어 대용량 파일에서도 가볍게 동작합니다.
    """
    _log(logger, f"TXT 열기 시도: {os.path.basename(txt_path)}")
    try:
        file_size = os.path.getsize(txt_path)
        if file_size == 0:
            _log(logger, "파일 크기가 0바이트 — 처리 중단")
            return None, None

        with open(txt_path, 'rb') as f:
            if file_size <= TXT_SCAN_BYTES * 2:
                _log(logger, f"파일 크기 {file_size}바이트로 작아 전체를 한 번에 읽음")
                front_text = _decode_bytes(f.read())
                back_text = ""
            else:
                _log(logger, f"파일 크기 {file_size}바이트 — 앞/뒤 {TXT_SCAN_BYTES}바이트씩 분리해서 읽음")
                f.seek(0)
                front_text = _decode_bytes(f.read(TXT_SCAN_BYTES))

                f.seek(max(0, file_size - TXT_SCAN_BYTES))
                back_text = _decode_bytes(f.read())

        if not front_text.strip() and not back_text.strip():
            _log(logger, "앞/뒤 구간 모두 텍스트 없음 — 처리 중단")
            return None, None

        isbn10_confident = []
        isbn10_weak = []
        compiled_texts = []

        for label, text_content in (('앞부분', front_text), ('뒷부분', back_text)):
            if not text_content.strip():
                continue
            text_content = re.sub(r'[\u2012-\u2015\u00ad.]', '-', text_content)
            compiled_texts.append(text_content)

            found13, found_conf, found_weak = _scan_text_for_isbn(text_content)
            if found13:
                _log(logger, f"{label} 스캔 중 ISBN13 즉시 발견: {found13} (LOCAL)")
                return found13, "LOCAL"
            isbn10_confident.extend(found_conf)
            isbn10_weak.extend(found_weak)

        return _finalize_isbn10(
            compiled_texts, isbn10_confident, isbn10_weak,
            gemini_key, llm_endpoint, llm_model, timeout_sec, book_title, file_name,
            logger=logger,
        )

    except Exception as e:
        _log(logger, f"TXT 처리 중 예외 발생: {e}")
    return None, None
