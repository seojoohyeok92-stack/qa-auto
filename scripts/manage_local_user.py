from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from repositories.database import Database
from services.local_auth_service import LocalAuthService


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Q&A auto 개발 PC 로컬 사용자 관리"
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name")
    parser.add_argument("--change-password", action="store_true")
    args = parser.parse_args()
    database = Database()
    database.initialize()
    service = LocalAuthService(database)
    if args.change_password:
        old = getpass.getpass("현재 비밀번호: ")
        first = getpass.getpass("새 비밀번호(10자 이상): ")
        second = getpass.getpass("새 비밀번호 확인: ")
        if first != second:
            raise SystemExit("새 비밀번호가 일치하지 않습니다.")
        user = service.authenticate(args.username, old)
        service.change_password(user, old, first)
        print("비밀번호를 변경했습니다.")
        return
    if not args.display_name:
        raise SystemExit("초기 ADMIN 생성에는 --display-name이 필요합니다.")
    first = getpass.getpass("초기 비밀번호(10자 이상): ")
    second = getpass.getpass("초기 비밀번호 확인: ")
    if first != second:
        raise SystemExit("비밀번호가 일치하지 않습니다.")
    service.bootstrap_admin(
        username=args.username,
        display_name=args.display_name,
        password=first,
    )
    print("초기 ADMIN을 생성했습니다. 첫 로그인 후 비밀번호를 변경하세요.")


if __name__ == "__main__":
    main()
