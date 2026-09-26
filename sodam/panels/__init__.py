"""버튼 패널 모듈 모음. 여기 있는 모듈을 전부 불러오면 각 모듈이 menu.register_* 로 화면·버튼을 등록한다.

새 패널 = 이 폴더에 파일 하나 추가 (menu.py 를 고칠 필요 없음). 규칙은 menu.py 머리말 참고.
"""
import importlib
import pkgutil

for _m in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_m.name}")
