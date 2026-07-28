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
    import pypdf
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False


ISBN_PATTERN = re.compile(
    r'\b(?:97[89][-\s.]?)?\d{1,5}[-\s.]?\d{1,7}[-\s.]?\d{1,6}[-\s.]?[\dX]\b'
)

# 로컬 스캔 시 앞/뒤로 살펴볼 구간 수. 값을 줄이면 I/O와 LLM 프롬프트 크기가 함께 줄어들어
# 더 효율적으로 동작하지만, 판권지가 이 범위 밖에 있으면 놓칠 수 있으니 필요시 조정하십시오.
EPUB_SCAN_SECTIONS = 5   # 앞 N개 + 뒤 N개 spine(챕터) 파일
PDF_SCAN_PAGES = 5       # 앞 N페이지 + 뒤 N페이지
# TXT 앞/뒤에서 각각 읽어들일 바이트 수 (판권지가 파일 앞쪽/뒤쪽 어디에 있어도 대응)
TXT_SCAN_BYTES = 8000
# LLM 프롬프트에 실어보낼 최대 문자 수
LLM_TEXT_LIMIT = 12000


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


def extract_isbn_via_llm(text, api_key, endpoint=None, model=None, book_title=None, file_name=None):
    """구글 Gemini API 및 LiteLLM(OpenAI 호환) 프록시를 모두 지원하는 통합 지능형 판독 엔진.

    book_title/file_name은 어디까지나 '참고용 힌트'이며, 실제 ISBN은 반드시 text 안에
    실존하는 문자열에서만 추출하도록 프롬프트에 강하게 못박아 둔다. 이렇게 하지 않으면
    모델이 본문에서 못 찾았을 때 자기 사전지식으로 '그럴듯한' ISBN을 추측해 답할 위험이 있고,
    그런 값은 체크디지트 검증은 통과하지만 실제로는 틀린 값일 수 있다.
    """
    if not text or not text.strip():
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={api_key}"

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
            with urllib.request.urlopen(req, timeout=15) as response:
                res_data = json.loads(response.read().decode('utf-8'))
                choices = res_data.get('choices', [])
                if choices:
                    raw_content = choices[0].get('message', {}).get('content', '').strip()
                    res_json = json.loads(raw_content)
                    raw_isbn = res_json.get('isbn', '')
                    clean = re.sub(r'[^0-9X]', '', str(raw_isbn).upper())
                    if validate_isbn13(clean) or validate_isbn10(clean):
                        return clean
        except urllib.error.HTTPError as he:
            error_msg = he.read().decode('utf-8', errors='ignore')
            print(f"[LiteLLM API HTTP 에러 {he.code}] 이유: {error_msg}", file=sys.stderr)
        except Exception as e:
            print(f"[LiteLLM API 에러] 사유: {str(e)}", file=sys.stderr)

    # 2. 순수 Google Gemini 공식 API 모드
    else:
        if not api_key:
            return None
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=12) as response:
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
                            return clean
        except urllib.error.HTTPError as he:
            error_msg = he.read().decode('utf-8', errors='ignore')
            print(f"[Gemini API HTTP 에러 {he.code}] 이유: {error_msg}", file=sys.stderr)
        except Exception as e:
            print(f"[Gemini API 에러] 사유: {str(e)}", file=sys.stderr)

    return None


def _scan_text_for_isbn(text_content):
    """텍스트 블록 하나에서 ISBN13(우선) 또는 ISBN10 후보를 추출.
    반환: (isbn13_확정값 또는 None, isbn10_후보_리스트)
    """
    isbn10_candidates = []
    for match in ISBN_PATTERN.findall(text_content):
        clean = re.sub(r'[^0-9X]', '', match.upper())
        if validate_isbn13(clean):
            return clean, isbn10_candidates
        elif validate_isbn10(clean):
            isbn10_candidates.append(clean)
    return None, isbn10_candidates


def extract_isbn_from_epub(epub_path, gemini_key=None, llm_endpoint=None, llm_model=None,
                            book_title=None, file_name=None):
    """EPUB 내부 컨테이너 구조 및 본문 파일 분석 후 ISBN 추출 (지능형 LLM 듀얼 분기 가동)"""
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
                return None, None

            opf_content = epub.read(opf_path)
            opf_root = ET.fromstring(opf_content)

            # 1단계: 표준 메타데이터 태그(<dc:identifier>)에서 ISBN 탐색
            for elem in opf_root.iter():
                if elem.tag.endswith('identifier') and elem.text:
                    clean = re.sub(r'[^0-9X]', '', elem.text.upper())
                    if validate_isbn13(clean) or validate_isbn10(clean):
                        return clean, "LOCAL"

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

            opf_dir = os.path.dirname(opf_path)

            # [초고속 조기 종료 필터]: 만화책/스캔본 전용 EPUB 판별
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
                return None, None  # 이미지 전용책이므로 실시간 수색 종료

            isbn10_candidates = []
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

                        found13, found10s = _scan_text_for_isbn(text_content)
                        if found13:
                            return found13, "LOCAL"
                        isbn10_candidates.extend(found10s)
                    except Exception:
                        pass

            if isbn10_candidates:
                return isbn10_candidates[0], "LOCAL"

            if (gemini_key or (llm_endpoint and llm_endpoint.strip())) and compiled_texts:
                full_text = "\n".join(compiled_texts)[:LLM_TEXT_LIMIT]
                llm_isbn = extract_isbn_via_llm(
                    full_text, gemini_key, endpoint=llm_endpoint, model=llm_model,
                    book_title=book_title, file_name=file_name,
                )
                if llm_isbn:
                    return llm_isbn, "AI"

    except Exception:
        pass
    return None, None


def extract_isbn_from_pdf(pdf_path, gemini_key=None, llm_endpoint=None, llm_model=None,
                           book_title=None, file_name=None):
    """PDF 메타데이터 및 전후면 판권 페이지 고속 스캔 (지능형 LLM 듀얼 분기 가동)"""
    if not PYPDF_AVAILABLE:
        return None, None

    try:
        with open(pdf_path, 'rb') as f:
            reader = pypdf.PdfReader(f)
            num_pages = len(reader.pages)
            if num_pages == 0:
                return None, None

            # [초고속 조기 종료 필터]: 스캔본(통 이미지) 전용 PDF 판별
            check_indices = list(range(1, min(6, num_pages)))
            if num_pages > 5:
                check_indices.extend(list(range(max(5, num_pages - 5), num_pages)))
            check_indices = sorted(list(set(check_indices))) or [0]

            sample_text = ""
            for idx in check_indices:
                try:
                    p_text = reader.pages[idx].extract_text()
                    if p_text:
                        sample_text += p_text.strip()
                except Exception:
                    pass
            if not sample_text.strip():
                return None, None  # 전후방 모두 글자가 전혀 긁히지 않는 스캔 도서

            pages_to_scan = list(range(min(PDF_SCAN_PAGES, num_pages)))
            if num_pages > PDF_SCAN_PAGES:
                pages_to_scan.extend(list(range(max(PDF_SCAN_PAGES, num_pages - PDF_SCAN_PAGES), num_pages)))
            pages_to_scan = sorted(list(set(pages_to_scan)))

            isbn10_candidates = []
            compiled_texts = []

            for page_idx in pages_to_scan:
                text = reader.pages[page_idx].extract_text()
                if not text:
                    continue

                text = re.sub(r'[\u2012-\u2015\u00ad.]', '-', text)

                if text.strip():
                    compiled_texts.append(text)

                found13, found10s = _scan_text_for_isbn(text)
                if found13:
                    return found13, "LOCAL"
                isbn10_candidates.extend(found10s)

            if isbn10_candidates:
                return isbn10_candidates[0], "LOCAL"

            if (gemini_key or (llm_endpoint and llm_endpoint.strip())) and compiled_texts:
                full_text = "\n".join(compiled_texts)[:LLM_TEXT_LIMIT]
                llm_isbn = extract_isbn_via_llm(
                    full_text, gemini_key, endpoint=llm_endpoint, model=llm_model,
                    book_title=book_title, file_name=file_name,
                )
                if llm_isbn:
                    return llm_isbn, "AI"

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
                           book_title=None, file_name=None):
    """TXT 파일 앞/뒤 구간을 스캔하여 ISBN 추출 (지능형 LLM 듀얼 분기 가동).
    판권지가 파일 맨 앞(표지 다음)이나 맨 뒤(colophon) 어디에 있어도 대응하도록
    파일의 앞쪽 TXT_SCAN_BYTES와 뒤쪽 TXT_SCAN_BYTES만 읽어 대용량 파일에서도 가볍게 동작합니다.
    """
    try:
        file_size = os.path.getsize(txt_path)
        if file_size == 0:
            return None, None

        with open(txt_path, 'rb') as f:
            if file_size <= TXT_SCAN_BYTES * 2:
                front_text = _decode_bytes(f.read())
                back_text = ""
            else:
                f.seek(0)
                front_text = _decode_bytes(f.read(TXT_SCAN_BYTES))

                f.seek(max(0, file_size - TXT_SCAN_BYTES))
                back_text = _decode_bytes(f.read())

        if not front_text.strip() and not back_text.strip():
            return None, None

        isbn10_candidates = []
        compiled_texts = []

        for text_content in (front_text, back_text):
            if not text_content.strip():
                continue
            text_content = re.sub(r'[\u2012-\u2015\u00ad.]', '-', text_content)
            compiled_texts.append(text_content)

            found13, found10s = _scan_text_for_isbn(text_content)
            if found13:
                return found13, "LOCAL"
            isbn10_candidates.extend(found10s)

        if isbn10_candidates:
            return isbn10_candidates[0], "LOCAL"

        if (gemini_key or (llm_endpoint and llm_endpoint.strip())) and compiled_texts:
            full_text = "\n".join(compiled_texts)[:LLM_TEXT_LIMIT]
            llm_isbn = extract_isbn_via_llm(
                full_text, gemini_key, endpoint=llm_endpoint, model=llm_model,
                book_title=book_title, file_name=file_name,
            )
            if llm_isbn:
                return llm_isbn, "AI"

    except Exception:
        pass
    return None, None
