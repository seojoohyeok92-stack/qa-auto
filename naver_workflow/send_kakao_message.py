import time, os, subprocess
import pyautogui
import pyperclip
import pandas as pd
import pygetwindow as gw
SW_RESTORE = 9
SW_SHOWMAXIMIZED = 3
import ctypes

def get_kakao_path(excel_path):
    try:
        df = pd.read_excel(excel_path)
        kakao_path = df.loc[df['설정항목'] == '카카오톡 경로', '값'].values[0]
        if not os.path.exists(kakao_path):
            raise FileNotFoundError(f"카카오톡 경로가 유효하지 않습니다: {kakao_path}")
        return kakao_path
    except Exception as e:
        raise RuntimeError(f"엑셀에서 카카오톡 경로를 불러오지 못했습니다: {e}")

def _find_kakao_windows():
    return (pyautogui.getWindowsWithTitle('카카오톡')
            or pyautogui.getWindowsWithTitle('KakaoTalk')
            or [])

def activate_kakao_window():
    wins = _find_kakao_windows()
    if not wins:
        raise RuntimeError("카카오톡 창을 찾을 수 없습니다.")

    w = wins[0]

    try:
        hwnd = w._hWnd

        # 최소화 상태면 복원
        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.3)

        # pygetwindow activate 시도
        try:
            w.activate()
            time.sleep(0.3)
            return w
        except Exception:
            pass

        # WinAPI로 강제 활성화
        ctypes.windll.user32.BringWindowToTop(hwnd)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)

        # 그래도 안 되면 창 상단 클릭
        x = w.left + max(w.width // 2, 50)
        y = w.top + 10

        pyautogui.moveTo(x, y, duration=0.2)
        pyautogui.click()
        time.sleep(0.3)

        return w

    except Exception as exc:
        raise RuntimeError(
            f"카카오톡 창 활성화에 실패했습니다: {exc}"
        ) from exc


# 마우스가 모서리에 있을 때만 FAILSAFE를 잠시 해제하고 즉시 복원
def move_mouse_away_from_corner() -> None:
    """
    마우스가 화면 모서리에 있으면 중앙으로 이동한다.

    이동하는 짧은 순간에만 PyAutoGUI FailSafe를 해제하고,
    이동 완료 후 원래 설정으로 반드시 복원한다.
    """
    original_failsafe = pyautogui.FAILSAFE

    try:
        screen_width, screen_height = (
            pyautogui.size()
        )

        mouse_x, mouse_y = (
            pyautogui.position()
        )

        margin = 10

        is_corner = (
            mouse_x <= margin
            or mouse_y <= margin
            or mouse_x >= screen_width - margin
            or mouse_y >= screen_height - margin
        )

        if not is_corner:
            return

        # 모서리에서 빠져나올 때만 잠시 해제
        pyautogui.FAILSAFE = False

        pyautogui.moveTo(
            screen_width // 2,
            screen_height // 2,
            duration=0.2,
        )

        print(
            "[KAKAO] 마우스가 화면 모서리에 있어 "
            "중앙으로 이동했습니다."
        )

        time.sleep(0.3)

    except Exception as exc:
        print(
            "[KAKAO] 마우스 위치 보정 실패:",
            exc,
        )
        raise

    finally:
        # FailSafe를 다시 켬
        pyautogui.FAILSAFE = original_failsafe

# def open_kakao(kakao_path, timeout=15):
#     """카카오톡 실행 후 창만 활성화 (Ctrl+Tab 등 전환 없음)."""
#     try:
#         subprocess.Popen([kakao_path], shell=True)
#     except FileNotFoundError:
#         raise RuntimeError(f"카카오톡 실행 파일을 찾을 수 없습니다: {kakao_path}")
#     except Exception as e:
#         raise RuntimeError(f"카카오톡 실행 중 오류 발생: {e}")

#     end = time.time() + timeout
#     last_err = None
#     while time.time() < end:
#         try:
#             if activate_kakao_window():
#                 return True
#         except Exception as e:
#             last_err = e
#         time.sleep(0.8)  # 약간 더 짧게, 응답성 향상
#     raise RuntimeError(f"카카오톡 창 활성화 실패(타임아웃): {last_err}")

def 윈도우_포커스_강제(win):                #1014추가
    """
    주어진 pygetwindow Window 객체를 복원하고 최전면으로 포커스한다.
    """
    try:
        hwnd = win._hWnd
        # 최소화된 경우 복원
        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
        # 최전면으로 포커스
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass

def 창_상단_클릭_pyautogui(win):                     #1014추가
    try:
        x = win.left + win.width // 2
        y = win.top + 10
        pyautogui.moveTo(x, y)
        pyautogui.click()
    except Exception:
        pass

def 창_상단_클릭_winapi(win):                          #1014추가
    try:
        hwnd = win._hWnd
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        x = rect.left + (rect.right - rect.left) // 2
        y = rect.top + 10
        ctypes.windll.user32.SetCursorPos(x, y)
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP   = 0x0004
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    except Exception:
        pass
    
def open_kakao(kakao_path):              #1014추가
    move_mouse_away_from_corner()
    subprocess.Popen([kakao_path], shell=True)
    time.sleep(10)

    for i in range(20):
        try:
            윈도우목록 = gw.getWindowsWithTitle('카카오톡')
            if 윈도우목록:
                # room_name 없이 첫 번째 카카오톡 창만 사용
                target_win = 윈도우목록[0]

                # 1) 최대화
                try:
                    hwnd = target_win._hWnd
                    ctypes.windll.user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
                except Exception:
                    try:
                        target_win.maximize()
                    except Exception:
                        pass

                time.sleep(0.5)

                # 2) 포커스 강제
                윈도우_포커스_강제(target_win)
                time.sleep(0.2)

                # 3) 상단 클릭
                try:
                    창_상단_클릭_pyautogui(target_win)
                except ImportError:
                    창_상단_클릭_winapi(target_win)
                time.sleep(0.2)

                break
        except Exception:
            time.sleep(1)
    else:
        print("카카오톡 창 포커스에 실패했습니다.")
        return

def open_chat(
    recipient,
    settle=3,
):
    """통합검색으로 방에 진입합니다."""
    move_mouse_away_from_corner()

    time.sleep(settle)
    activate_kakao_window()

    move_mouse_away_from_corner()
    pyautogui.hotkey(
        "ctrl",
        "tab",
    )
    time.sleep(settle)

    pyautogui.hotkey(
        "ctrl",
        "f",
    )
    time.sleep(settle)

    pyperclip.copy(
        recipient
    )

    pyautogui.hotkey(
        "ctrl",
        "v",
    )
    time.sleep(settle)

    pyautogui.press(
        "enter"
    )
    time.sleep(
        max(3, settle)
    )

def send_message(
    message,
    settle=2,
):
    """현재 채팅방에 메시지를 전송합니다."""
    move_mouse_away_from_corner()

    pyperclip.copy(
        message
    )

    pyautogui.hotkey(
        "ctrl",
        "v",
    )
    time.sleep(settle)

    pyautogui.press(
        "enter"
    )

def send_file(
    file_path,
    dialog_wait=3.0,
    preview_wait=4.0,
    send_wait=4.0,
):
    """
    현재 열려 있는 카카오톡 채팅방에 파일을 첨부합니다.

    중요:
    이 함수에서는 activate_kakao_window()를 호출하지 않습니다.
    바로 전에 메시지를 보낸 채팅방의 포커스를 그대로 사용합니다.
    """
    file_path = os.path.abspath(
        str(file_path)
    )

    if not os.path.isfile(file_path):
        raise FileNotFoundError(
            "전송할 파일을 찾을 수 없습니다: "
            f"{file_path}"
        )

    print(f"[KAKAO FILE] 전송 파일: {file_path}")

    # 직전에 메시지를 보낸 채팅방의 포커스를 그대로 유지
    time.sleep(1)
    move_mouse_away_from_corner()
    # 채팅방에서 파일 첨부창 열기
    print("[KAKAO FILE] Ctrl+T 입력")
    pyautogui.hotkey("ctrl", "t")
    time.sleep(dialog_wait)

    # 파일 선택창의 파일 이름 입력란에 전체 경로 입력
    print("[KAKAO FILE] 파일 경로 붙여넣기")
    pyperclip.copy(file_path)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(1)

    # 파일 선택창의 열기
    print("[KAKAO FILE] 파일 선택 Enter")
    pyautogui.press("enter")
    time.sleep(preview_wait)

    # 카카오톡 파일 전송 확인창의 전송
    print("[KAKAO FILE] 전송 확인 Enter")
    pyautogui.press("enter")
    time.sleep(send_wait)

    print("[KAKAO FILE] 파일 전송 키 입력 완료")

def reset_to_original_state(minimize_kakao=False):
    """원-샷 모드: 항상 완전 종료에 사용."""
    try:
        win = activate_kakao_window()
        if minimize_kakao:
            try:
                win.minimize(); return
            except Exception:
                pass
    except Exception:
        pass
    os.system('taskkill /F /IM KakaoTalk.exe')

# 단독 테스트용
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("사용법: python send_kakao_message.py <recipient> <message>")
        sys.exit(1)
    rcpt = sys.argv[1]; msg = sys.argv[2]
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    excel_path  = os.path.join(SCRIPT_DIR, '초기설정(경로설정).xlsx')
    kakao_path  = get_kakao_path(excel_path)
    open_kakao(kakao_path)
    open_chat(rcpt)
    send_message(msg)
    reset_to_original_state()  # 테스트에선 종료