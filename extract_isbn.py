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
    'AI': 'AI 보조 판독',
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
        # TODO: 실제 배포 리포지토리 경로로 교체하십시오.
        "raw_base_url": "https://raw.githubusercontent.com/<org>/<repo>/refs/heads/main/plugins/metadata/extract_isbn",
        "files": ["extract_isbn.py", "utils_extract_isbn.py", "__init__.py", "VERSION"],
        "version_file": "VERSION",
        "version_key": "plugin version",
        "show_sample_update_button": True,
    }

    config_schema = [
        {"key": "GEMINI_API_KEY", "label": "Gemini/LiteLLM API Key (선택)", "type": "password", "required": False},
        {"key": "LITELLM_ENDPOINT", "label": "LiteLLM API 주소 (선택)", "type": "text", "required": False},
        {"key": "LITELLM_MODEL", "label": "LiteLLM 모델명 (선택)", "type": "text", "required": False},
    ]

    def _find_book(self, gateway, query):
        """검색창에 입력된 텍스트(보통 도서 제목)로 books 테이블에서 대상 도서를 추적.
        title/author/publisher도 함께 가져와, 검색 결과 카드에 실제 값을 그대로 보여주고
        (제목/저자/출판사를 임의 문자열로 바꾸지 않기 위함) 적용 확인창에서의 혼동을 줄인다.
        """
        clean_query_base = re.sub(r'\.(epub|pdf|txt)$', '', query or '', flags=re.IGNORECASE)
        clean_query_base = re.sub(r'\[.*?\]|\(.*?\)', '', clean_query_base).strip()
        if not clean_query_base:
            clean_query_base = (query or '').strip()
        if not clean_query_base:
            return None

        select_cols = "file_path, title, author, publisher"
        book = gateway.fetch_one(f"SELECT {select_cols} FROM books WHERE title = ? LIMIT 1", (clean_query_base,))
        if not book:
            book = gateway.fetch_one(
                f"SELECT {select_cols} FROM books WHERE file_path LIKE ? LIMIT 1",
                (f"%{clean_query_base}%",)
            )
        if not book:
            words = [w for w in clean_query_base.split() if len(w) > 1]
            if len(words) >= 2:
                sub_query = " ".join(words[:2])
                book = gateway.fetch_one(
                    f"SELECT {select_cols} FROM books WHERE title LIKE ? LIMIT 1",
                    (f"%{sub_query}%",)
                )
        return book

    def search(self, db_type, query):
        if not query or not str(query).strip():
            return [_fail_item('❌ 검색어가 비어 있습니다', '도서 제목이나 파일명을 입력해 주세요.')]

        gateway = self.get_db_gateway(db_type)
        book = self._find_book(gateway, query)
        if not book:
            return [_fail_item(
                '❌ 도서를 찾지 못했습니다',
                f'"{query}"와 일치하는 도서를 라이브러리 DB에서 찾을 수 없습니다.'
            )]

        file_path = get_row_val(book, 'file_path')
        if not file_path or not os.path.exists(file_path):
            return [_fail_item('❌ 파일을 찾을 수 없습니다', '도서 레코드는 있으나 실제 파일 경로가 존재하지 않습니다.')]

        ext = os.path.splitext(file_path)[1].lower()
        extractor = _EXTRACTORS.get(ext)
        if not extractor:
            return [_fail_item(
                '❌ 지원하지 않는 파일 형식',
                f'현재 EPUB/PDF/TXT만 지원합니다. (감지된 형식: {ext or "확장자 없음"})'
            )]

        config = self.get_plugin_config(db_type, default={})
        gemini_key = (config.get("GEMINI_API_KEY") or "").strip()
        llm_endpoint = (config.get("LITELLM_ENDPOINT") or "").strip()
        llm_model = (config.get("LITELLM_MODEL") or "").strip()

        try:
            extracted_isbn, method = extractor(
                file_path,
                gemini_key=gemini_key or None,
                llm_endpoint=llm_endpoint or None,
                llm_model=llm_model or None,
            )
        except Exception as e:
            return [_fail_item('❌ 추출 중 오류 발생', str(e))]

        if not extracted_isbn:
            return [_fail_item(
                '❌ ISBN을 찾지 못했습니다',
                '파일 내부(판권지/메타데이터)에서 유효한 ISBN 패턴을 발견하지 못했습니다.'
            )]

        clean_isbn = re.sub(r'[^0-9X]', '', str(extracted_isbn).upper())
        if not (validate_isbn13(clean_isbn) or validate_isbn10(clean_isbn)):
            return [_fail_item('❌ 유효하지 않은 ISBN', f'추출된 값이 체크디지트 검증을 통과하지 못했습니다: {clean_isbn}')]

        method_label = _METHOD_LABEL.get(method, method or '알 수 없음')
        real_title = get_row_val(book, 'title') or query
        real_author = get_row_val(book, 'author')
        real_publisher = get_row_val(book, 'publisher')
        return [{
            'title': real_title,
            'author': real_author,
            'publisher': real_publisher,
            'summary': (
                f'✅ ISBN 추출 성공: {clean_isbn}  (감지 방식: {method_label})\n'
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

        # 안전 조치: books 테이블에 isbn 컬럼 존재 여부 동적 확인
        columns_info = gateway.fetch_all("PRAGMA table_info(books)")
        columns = [col['name'].lower() for col in columns_info] if columns_info else []
        if 'isbn' not in columns:
            return False, "books 테이블에 'isbn' 컬럼이 없어 저장할 수 없습니다."

        gateway.execute("UPDATE books SET isbn = ? WHERE id = ?", (clean_isbn, book_id))
        return True, f"ISBN이 저장되었습니다: {clean_isbn}"
