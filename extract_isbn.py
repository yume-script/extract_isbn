# -*- coding: utf-8 -*-
import os
import re

from plugins.metadata.base import BaseMetadataProvider


def _import_local_module(module_name):
    """패키지 임포트 실패 시(단일 파일 로드 등) 경로 우회 동적 임포트"""
    import importlib.util
    current_dir = os.path.dirname(os.path.abspath(__file__))
    module_path = os.path.join(current_dir, f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    from .utils_extract_isbn import (
        extract_isbn_from_epub,
        extract_isbn_from_pdf,
        extract_isbn_from_txt,
        validate_isbn13,
        validate_isbn10,
        get_row_val,
    )
except ImportError:
    _utils_mod = _import_local_module("utils_extract_isbn")
    extract_isbn_from_epub = _utils_mod.extract_isbn_from_epub
    extract_isbn_from_pdf = _utils_mod.extract_isbn_from_pdf
    extract_isbn_from_txt = _utils_mod.extract_isbn_from_txt
    validate_isbn13 = _utils_mod.validate_isbn13
    validate_isbn10 = _utils_mod.validate_isbn10
    get_row_val = _utils_mod.get_row_val


# 확장자 -> 추출 함수 매핑 (신규 포맷 대응 시 이 테이블만 확장하면 됨)
_EXTRACTORS = {
    '.epub': extract_isbn_from_epub,
    '.pdf': extract_isbn_from_pdf,
    '.txt': extract_isbn_from_txt,
}

_METHOD_LABEL = {
    'LOCAL': '로컬 정규식 매칭',
    'LOCAL_WEAK': '로컬 정규식 매칭 (문맥 미확인 · 낮은 신뢰도)',
    'AI': 'AI 보조 판독',
    'CACHED': '기존 저장값',
}


def _fail_item(title, summary):
    """추출 실패/불가 상황을 검색 결과 카드 한 장으로 표현.
    _not_applicable 플래그로 apply() 단계에서 실수로 저장되지 않도록 막는다.
    """
    return {
        'title': title,
        'author': '',
        'publisher': '',
        'summary': summary,
        'isbn': '',
        'cover': '',
        'pubDate': '',
        '_not_applicable': True,
    }


class ExtractIsbnMetadataProvider(BaseMetadataProvider):
    id = "extract_isbn"
    name = "ISBN 추출기"
    # 메타데이터 검색 모달(도서 메타데이터 검색 창)의 제공자 드롭다운에 노출됩니다.
    is_searchable = True

    update_manifest = {
        "enabled": True,
        "provider": "github-raw",
        "raw_base_url": "https://raw.githubusercontent.com/yume-script/extract_isbn/refs/heads/main",
        "files": ["extract_isbn.py", "utils_extract_isbn.py", "__init__.py", "VERSION"],
        "version_file": "VERSION",
        "version_key": "plugin version",
        "show_sample_update_button": True,
    }

    config_schema = [
        {"key": "GEMINI_API_KEY", "label": "Gemini/LiteLLM API Key (선택)", "type": "password", "required": False},
        {"key": "LITELLM_ENDPOINT", "label": "LiteLLM API 주소 (선택)", "type": "text", "required": False},
        {"key": "LITELLM_MODEL", "label": "LiteLLM 모델명 (선택)", "type": "text", "required": False},
        {"key": "GEMINI_MODEL", "label": "Gemini 다이렉트 모드 모델명 (선택, 기본: gemini-3.5-flash-lite)",
         "type": "text", "required": False},
        {"key": "REQUEST_TIMEOUT_SEC", "label": "LLM 요청 타임아웃(초, 기본 15)", "type": "number", "required": False},
        {"key": "DASHBOARD_LIMIT", "label": "대시보드에 표시할 'ISBN 미보유 도서' 최대 개수 (기본 10)",
         "type": "number", "required": False},
    ]

    dashboard_widget = {
        'title': 'ISBN 미보유 도서',
        'subtitle': 'ISBN 값이 비어 있는 도서 목록',
        'provider': 'ISBN 추출기',
        'icon': 'fa-solid fa-barcode',
        'limit': 10,
    }

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _has_isbn_column(self, gateway):
        try:
            columns_info = gateway.fetch_all("PRAGMA table_info(books)")
            columns = [col['name'].lower() for col in columns_info] if columns_info else []
            return 'isbn' in columns
        except Exception:
            return False

    def _find_book(self, gateway, query):
        """검색창에 입력된 텍스트(보통 도서 제목)로 books 테이블에서 대상 도서를 추적.

        코어 계약상 search()는 book_id를 받지 못하므로 제목/파일명 기반으로 추정할 수밖에
        없다. 이 추정이 항상 정확하다고 보장할 수 없으므로, 매칭 방식(match_type)을 함께
        반환해서 search()가 신뢰도를 사용자에게 명시적으로 보여줄 수 있게 한다.

        반환: (book, match_type) 또는 (None, None)
        match_type: 'exact_title' | 'file_path_like' | 'title_fuzzy'
        """
        clean_query_base = re.sub(r'\.(epub|pdf|txt)$', '', query or '', flags=re.IGNORECASE)
        clean_query_base = re.sub(r'\[.*?\]|\(.*?\)', '', clean_query_base).strip()
        if not clean_query_base:
            clean_query_base = (query or '').strip()
        if not clean_query_base:
            return None, None

        has_isbn = self._has_isbn_column(gateway)
        select_cols = "file_path, title, author, publisher" + (", isbn" if has_isbn else "")

        book = gateway.fetch_one(f"SELECT {select_cols} FROM books WHERE title = ? LIMIT 1", (clean_query_base,))
        if book:
            return book, 'exact_title'

        book = gateway.fetch_one(
            f"SELECT {select_cols} FROM books WHERE file_path LIKE ? LIMIT 1",
            (f"%{clean_query_base}%",)
        )
        if book:
            return book, 'file_path_like'

        words = [w for w in clean_query_base.split() if len(w) > 1]
        if len(words) >= 2:
            sub_query = " ".join(words[:2])
            book = gateway.fetch_one(
                f"SELECT {select_cols} FROM books WHERE title LIKE ? LIMIT 1",
                (f"%{sub_query}%",)
            )
            if book:
                return book, 'title_fuzzy'

        return None, None

    # ------------------------------------------------------------------
    # 필수 계약: search / apply
    # ------------------------------------------------------------------

    def search(self, db_type, query):
        if not query or not str(query).strip():
            return [_fail_item('❌ 검색어가 비어 있습니다', '도서 제목이나 파일명을 입력해 주세요.')]

        gateway = self.get_db_gateway(db_type)
        book, match_type = self._find_book(gateway, query)
        if not book:
            return [_fail_item(
                '❌ 도서를 찾지 못했습니다',
                f'"{query}"와 일치하는 도서를 라이브러리 DB에서 찾을 수 없습니다.'
            )]

        file_path = get_row_val(book, 'file_path')
        real_title = get_row_val(book, 'title') or query
        real_author = get_row_val(book, 'author')
        real_publisher = get_row_val(book, 'publisher')

        if not file_path or not os.path.exists(file_path):
            return [_fail_item('❌ 파일을 찾을 수 없습니다', '도서 레코드는 있으나 실제 파일 경로가 존재하지 않습니다.')]

        # 검색어와 완전히 같은 제목으로 찾은 게 아니라면(유사 매칭), 사용자가 지금 편집하려는
        # 책과 실제로 파일을 열어 추출하는 책이 다를 위험이 있다. 반드시 확인할 수 있도록
        # 파일 경로를 그대로 보여주고 명시적으로 경고한다.
        match_warning = ""
        if match_type != 'exact_title':
            match_warning = (
                f'⚠️ 검색어와 제목이 정확히 일치하지 않아 유사한 도서로 추정했습니다.\n'
                f'대상 파일: {file_path}\n'
                f'추정된 도서: "{real_title}" — 지금 편집 중인 책이 맞는지 반드시 확인 후 적용하세요.\n\n'
            )

        ext = os.path.splitext(file_path)[1].lower()
        extractor = _EXTRACTORS.get(ext)
        if not extractor:
            return [_fail_item(
                '❌ 지원하지 않는 파일 형식',
                f'현재 EPUB/PDF/TXT만 지원합니다. (감지된 형식: {ext or "확장자 없음"})'
            )]

        # 이미 유효한 ISBN이 저장되어 있다면, 굳이 파일을 다시 읽거나(특히 비용이 드는 AI 호출)
        # 재추출하지 않는다. 값을 다시 확인하고 싶다면 DB에서 값을 비운 뒤 재실행하면 된다.
        existing_isbn_raw = get_row_val(book, 'isbn')
        existing_isbn = re.sub(r'[^0-9X]', '', str(existing_isbn_raw or '').upper())
        if existing_isbn and (validate_isbn13(existing_isbn) or validate_isbn10(existing_isbn)):
            return [{
                'title': real_title,
                'author': real_author,
                'publisher': real_publisher,
                'summary': (
                    f'{match_warning}'
                    f'ℹ️ 이미 유효한 ISBN이 저장되어 있어 재추출하지 않았습니다: {existing_isbn}\n'
                    f'다시 추출하려면 이 도서의 ISBN 값을 비운 뒤 재검색하세요.'
                ),
                'isbn': existing_isbn,
                'cover': '',
                'pubDate': '',
                'source': 'ISBN 추출기',
            }]

        config = self.get_plugin_config(db_type, default={})
        gemini_key = (config.get("GEMINI_API_KEY") or "").strip()
        llm_endpoint = (config.get("LITELLM_ENDPOINT") or "").strip()
        llm_model = (config.get("LITELLM_MODEL") or config.get("GEMINI_MODEL") or "").strip()
        try:
            timeout_sec = float(config.get("REQUEST_TIMEOUT_SEC") or 0) or None
        except (TypeError, ValueError):
            timeout_sec = None

        try:
            extracted_isbn, method = extractor(
                file_path,
                gemini_key=gemini_key or None,
                llm_endpoint=llm_endpoint or None,
                llm_model=llm_model or None,
                book_title=real_title or None,
                file_name=os.path.basename(file_path),
                timeout_sec=timeout_sec,
            )
        except Exception as e:
            return [_fail_item('❌ 추출 중 오류 발생', f'{match_warning}{str(e)}')]

        if not extracted_isbn:
            return [_fail_item(
                '❌ ISBN을 찾지 못했습니다',
                f'{match_warning}파일 내부(판권지/메타데이터)에서 유효한 ISBN 패턴을 발견하지 못했습니다.'
            )]

        clean_isbn = re.sub(r'[^0-9X]', '', str(extracted_isbn).upper())
        if not (validate_isbn13(clean_isbn) or validate_isbn10(clean_isbn)):
            return [_fail_item('❌ 유효하지 않은 ISBN', f'{match_warning}추출된 값이 체크디지트 검증을 통과하지 못했습니다: {clean_isbn}')]

        method_label = _METHOD_LABEL.get(method, method or '알 수 없음')
        confidence_note = ""
        if method == 'LOCAL_WEAK':
            confidence_note = '\n⚠️ 문맥에서 "ISBN" 표기를 확인하지 못했습니다. 값이 우연히 체크디지트를 통과한 다른 숫자열일 수 있으니 적용 전 확인해 주세요.'

        return [{
            'title': real_title,
            'author': real_author,
            'publisher': real_publisher,
            'summary': (
                f'{match_warning}'
                f'✅ ISBN 추출 성공: {clean_isbn}  (감지 방식: {method_label}){confidence_note}\n'
                f'※ 이 항목을 적용해도 제목/저자/출판사/표지/설명은 변경되지 않으며, ISBN만 갱신됩니다.'
            ),
            'isbn': clean_isbn,
            'cover': '',
            'pubDate': '',
            'source': 'ISBN 추출기',
        }]

    def apply(self, db_type, book_id, item_data):
        if item_data.get('_not_applicable'):
            return False, "적용할 수 없는 결과입니다. (추출 실패 안내 카드)"

        raw_isbn = item_data.get('isbn', '')
        clean_isbn = re.sub(r'[^0-9X]', '', str(raw_isbn).upper()) if raw_isbn else ''
        if not (validate_isbn13(clean_isbn) or validate_isbn10(clean_isbn)):
            return False, "유효한 ISBN이 없습니다."

        gateway = self.get_db_gateway(db_type)

        if not self._has_isbn_column(gateway):
            return False, "books 테이블에 'isbn' 컬럼이 없어 저장할 수 없습니다."

        gateway.execute("UPDATE books SET isbn = ? WHERE id = ?", (clean_isbn, book_id))
        return True, f"ISBN이 저장되었습니다: {clean_isbn}"

    # ------------------------------------------------------------------
    # 선택 계약: 대시보드 위젯 (ISBN 미보유 도서 목록)
    # ------------------------------------------------------------------

    def get_dashboard_data(self, db_type, limit=10):
        gateway = self.get_db_gateway(db_type)

        if not self._has_isbn_column(gateway):
            return {'success': False, 'error': "books 테이블에 'isbn' 컬럼이 없습니다."}

        config = self.get_plugin_config(db_type, default={})
        try:
            configured_limit = int(config.get("DASHBOARD_LIMIT") or 0) or limit
        except (TypeError, ValueError):
            configured_limit = limit

        try:
            rows = gateway.fetch_all(
                "SELECT id, title, author, file_path FROM books "
                "WHERE COALESCE(is_deleted, 0) = 0 AND (isbn IS NULL OR isbn = '') "
                "ORDER BY id DESC LIMIT ?",
                (configured_limit,),
            )
        except Exception as e:
            return {'success': False, 'error': str(e)}

        items = []
        for row in (rows or []):
            file_path = get_row_val(row, 'file_path')
            ext = os.path.splitext(file_path)[1].lower() if file_path else ''
            items.append({
                'title': get_row_val(row, 'title') or '(제목 없음)',
                'subtitle': get_row_val(row, 'author') or '',
                'meta': ext.lstrip('.').upper() if ext in _EXTRACTORS else f'미지원 형식({ext or "?"})',
            })

        return {'success': True, 'items': items}
