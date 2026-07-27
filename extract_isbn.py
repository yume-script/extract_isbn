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
    'LOCAL': '로컬 매칭',
    'AI': 'AI 판독',
}


class ExtractIsbnMetadataProvider(BaseMetadataProvider):
    id = "extract_isbn"
    name = "ISBN 추출기"
    is_searchable = False

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

    # 이 플러그인은 검색형/적용형 메타데이터 제공자가 아니라
    # 컨텍스트 메뉴 전용 유틸리티 플러그인이므로 두 필수 계약은 스텁으로 둡니다.
    def search(self, db_type, query):
        return []

    def apply(self, db_type, book_id, item_data):
        return False, "이 플러그인은 도서 컨텍스트 메뉴에서 'ISBN 추출'로 실행하십시오."

    def get_context_menu_items(self, db_type, context):
        return [
            {
                'id': 'extract_isbn_action',
                'label': 'ISBN 추출 (EPUB/PDF/TXT)',
                'icon': 'fa-solid fa-barcode',
            }
        ]

    def run_context_menu_action(self, db_type, action_id, context):
        if action_id != 'extract_isbn_action':
            return {'success': False, 'error': '알 수 없는 액션입니다.'}

        book_id = context.get('book_id')
        if not book_id:
            return {'success': False, 'error': 'book_id가 없습니다.'}

        gateway = self.get_db_gateway(db_type)
        book = gateway.fetch_one("SELECT file_path FROM books WHERE id = ?", (book_id,))
        if not book:
            return {'success': False, 'error': '도서를 찾을 수 없습니다.'}

        file_path = get_row_val(book, 'file_path')
        if not file_path or not os.path.exists(file_path):
            return {'success': False, 'error': '파일 경로를 찾을 수 없습니다.'}

        ext = os.path.splitext(file_path)[1].lower()
        extractor = _EXTRACTORS.get(ext)
        if not extractor:
            return {'success': False, 'error': f'지원하지 않는 파일 형식입니다: {ext or "(확장자 없음)"}'}

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
            return {'success': False, 'error': f'추출 중 오류: {str(e)}'}

        if not extracted_isbn:
            return {'success': False, 'error': '파일에서 ISBN을 찾지 못했습니다.'}

        clean_isbn = re.sub(r'[^0-9X]', '', str(extracted_isbn).upper())
        if not (validate_isbn13(clean_isbn) or validate_isbn10(clean_isbn)):
            return {'success': False, 'error': '추출된 값이 유효한 ISBN이 아닙니다.'}

        # books 테이블에 isbn 컬럼이 있는지 안전하게 확인 후 자동 저장
        columns_info = gateway.fetch_all("PRAGMA table_info(books)")
        columns = [col['name'].lower() for col in columns_info] if columns_info else []
        if 'isbn' not in columns:
            return {'success': False, 'error': "books 테이블에 'isbn' 컬럼이 없어 저장할 수 없습니다."}

        gateway.execute("UPDATE books SET isbn = ? WHERE id = ?", (clean_isbn, book_id))

        method_label = _METHOD_LABEL.get(method, method or '알 수 없음')
        return {
            'success': True,
            'message': f'ISBN 추출 및 저장 완료 ({method_label}): {clean_isbn}',
        }
