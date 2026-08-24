from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from qna_auto.excel_io import answer_excel
from qna_auto.learning import apply_accepted_learning
from qna_auto.naver_workflow import (
    NaverRunOptions,
    collect_learning_review_sync,
    run_naver_qna_sync,
)
from qna_auto.ojeplus_selenium import run_ojeplus_selenium_check


ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
QUESTION_DIR = ROOT / "질문건"
OUTPUT_DIR = ROOT / "outputs"


def setup_console() -> None:
    if os.name == "nt":
        os.system("chcp 65001 > nul")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def wait() -> None:
    try:
        input("\n종료하려면 Enter를 눌러주세요.")
    except EOFError:
        pass


def find_latest_excel() -> Path | None:
    if not QUESTION_DIR.exists():
        return None
    files = [path for path in QUESTION_DIR.glob("*.xlsx") if not path.name.startswith("~$")]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def run_excel_mode() -> int:
    source = find_latest_excel()
    if source is None:
        print("\n질문건 폴더에서 엑셀 파일을 찾지 못했습니다.")
        print(f"엑셀 파일을 넣을 위치: {QUESTION_DIR}")
        return 1

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = OUTPUT_DIR / "qna_auto" / f"{source.stem}_자동답변_{timestamp}.xlsx"

    print(f"\n입력 파일: {source.name}")
    print("답변을 생성하는 중입니다...")
    result = answer_excel(source, output)
    print("\n완료되었습니다.")
    print(f"결과 파일: {result}")
    return 0


def run_naver_mode(
    post: bool,
    yes_post: bool = False,
) -> int:
    if post:
        print(
            "\n주의: 실제 네이버 Q&A에 "
            "답변을 등록합니다."
        )

        if not yes_post:
            confirm = input(
                "계속하려면 POST 를 입력하세요: "
            ).strip()

            if confirm != "POST":
                print("실제 등록을 취소했습니다.")
                return 0

    options = NaverRunOptions(
        post=post,
    )

    if post:
        print(
            "\n네이버 문의를 수집하고 "
            "자동답변을 등록하는 중입니다..."
        )
    else:
        print(
            "\n네이버 문의를 수집하고 "
            "답변 후보만 생성하는 중입니다..."
        )

    result = run_naver_qna_sync(
        options
    )

    print("\n완료되었습니다.")
    print(f"결과 파일: {result}")

    if not post:
        print(
            "드라이런 모드라 실제 네이버 답변은 "
            "등록하지 않았습니다."
        )

    return 0


def run_learning_collect_mode() -> int:
    print("\n답변완료 문의를 다시 수집해 학습검수대기 파일을 갱신하는 중입니다...")
    result = collect_learning_review_sync(
        NaverRunOptions(
            limit=100,
            page_size=50,
            days=30,
            post=False,
            output=OUTPUT_DIR / "learning" / "학습검수대기.xlsx",
        )
    )
    print("\n완료되었습니다.")
    print(f"검수 파일: {result}")
    return 0


def run_learning_apply_mode() -> int:
    print("\n학습검수대기.xlsx의 채택 항목을 configuration.xlsx에 반영하는 중입니다...")
    result = apply_accepted_learning(ROOT)
    print("\n완료되었습니다.")
    print(f"설정 파일: {result}")
    return 0


def run_ojeplus_check_mode() -> int:
    print("\nOJPLUS 셀레니움 실행 준비상태를 점검하는 중입니다...")
    result = run_ojeplus_selenium_check()
    print("\n완료되었습니다.")
    print(f"점검 파일: {result}")
    return 0


def print_menu() -> None:
    print("\nQ&A 자동답변 프로그램")
    print("=" * 32)
    print(f"작업 폴더: {ROOT}")
    print(f"질문 폴더: {QUESTION_DIR}")
    print("")
    print("1. 엑셀 질문건 답변 생성")
    print("2. 네이버 Q&A 조회 후 답변 후보 생성")
    print("3. 네이버 Q&A 실제 답변 등록")
    print("4. 학습자료수집")
    print("5. 검수결과반영")
    print("6. OJPLUS 셀레니움 점검")

    print("7. 네이버 Q&A 실제 답변 반복 감시")
    
    print("0. 종료")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--mode",
        choices=[
            "menu",
            "excel",
            "dryrun",
            "post",
            "watch",
            "learning",
            "apply_learning",
            "ojeplus_check",
        ],
        default="menu",
    )

    parser.add_argument("--yes-post", action="store_true")

    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="감시 모드 반복 간격(분)",
    )

    return parser.parse_args()


def run_selected_mode(args: argparse.Namespace) -> int:
    if args.mode == "excel":
        return run_excel_mode()

    if args.mode == "dryrun":
        return run_naver_mode(post=False)

    if args.mode == "post":
        return run_naver_mode(
            post=True,
            yes_post=args.yes_post,
        )

    if args.mode == "watch":
        return run_naver_watch_mode(
            interval_minutes=args.interval,
        )

    if args.mode == "learning":
        return run_learning_collect_mode()

    if args.mode == "apply_learning":
        return run_learning_apply_mode()

    if args.mode == "ojeplus_check":
        return run_ojeplus_check_mode()

    return 1


def main() -> int:
    setup_console()
    args = parse_args()
    QUESTION_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "qna_auto").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "naver_api").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "learning").mkdir(parents=True, exist_ok=True)

    # if args.mode != "menu":
    #     try:
    #         code = run_selected_mode(args)
    #     except Exception as exc:  # pragma: no cover - console safety net
    #         print("\n오류가 발생했습니다.")
    #         print(str(exc))
    #         code = 1
    #     wait()
    #     return code
    if args.mode != "menu":
        try:
            code = run_selected_mode(args)
        except Exception as exc:
            print("\n오류가 발생했습니다.")
            print(str(exc))
            code = 1

        return code

    print_menu()
    choice = input("\n번호를 선택하세요: ").strip()

    try:
        if choice == "1":
            code = run_excel_mode()
        elif choice == "2":
            code = run_naver_mode(post=False)
        elif choice == "3":
            code = run_naver_mode(post=True)
        elif choice == "4":
            code = run_learning_collect_mode()
        elif choice == "5":
            code = run_learning_apply_mode()
        elif choice == "6":
            code = run_ojeplus_check_mode()
        elif choice == "7":
            interval_text = input(
                "반복 간격을 분 단위로 입력하세요. 기본값 10분: "
            ).strip()

            try:
                interval_minutes = int(interval_text) if interval_text else 10
            except ValueError:
                print("숫자가 아니므로 기본값 10분을 사용합니다.")
                interval_minutes = 10

            confirm = input(
                "실제 답변을 반복 등록합니다. 계속하려면 WATCH 를 입력하세요: "
            ).strip()

            if confirm != "WATCH":
                print("감시 모드를 취소했습니다.")
                code = 0
            else:
                code = run_naver_watch_mode(interval_minutes)
        elif choice == "0":
            return 0
        else:
            print("선택값을 확인해 주세요.")
            code = 1
    except Exception as exc:  # pragma: no cover - console safety net
        print("\n오류가 발생했습니다.")
        print(str(exc))
        code = 1

    wait()
    return code


import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path


def run_naver_watch_mode(interval_minutes: int = 10) -> int:
    interval_seconds = max(interval_minutes, 1) * 60

    print("\n네이버 Q&A 자동답변 감시 모드를 시작합니다.")
    print(f"실행 간격: {interval_minutes}분")
    print("종료하려면 Ctrl+C를 누르세요.")

    run_count = 0

    try:
        while True:
            run_count += 1
            started_at = datetime.now()

            print("\n" + "=" * 50)
            print(f"[{started_at:%Y-%m-%d %H:%M:%S}] {run_count}회차 실행 시작")
            print("=" * 50)

            try:
                code = run_naver_mode(
                    post=True,
                    yes_post=True,
                )

                if code == 0:
                    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 정상 완료")
                else:
                    print(
                        f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
                        f"실행 종료코드: {code}"
                    )

            except Exception as exc:
                print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 실행 오류")
                print(str(exc))

            print(
                f"\n다음 실행 예정: "
                f"{datetime.fromtimestamp(time.time() + interval_seconds):%Y-%m-%d %H:%M:%S}"
            )

            time.sleep(interval_seconds)

    except KeyboardInterrupt:
        print("\n감시 모드를 종료합니다.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
