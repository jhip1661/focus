import subprocess
import os
import time

def run_script(filename, required=True):
    """파이썬 스크립트 실행 함수"""
    if not os.path.isfile(filename):
        print(f"⚠️ 파일 없음: {filename}", flush=True)
        return False if not required else exit(1)

    print(f"\n▶️ 실행 중: {filename}", flush=True)
    try:
        # 실시간 출력이 되도록 capture_output 제거
        result = subprocess.run(["python", filename], check=True)
        print(f"✅ 완료: {filename}", flush=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ 실패: {filename}\n{e}", flush=True)
        if required:
            exit(1)
        return False

def main():
    print("🚀 통합 실행 시작\n", flush=True)

    # ======================================
    # 🔹 1단계: 기본 실행 (항상 실행)
    # ======================================
    print("=== [1단계] 기본 실행 시작 ===", flush=True)
    run_script("raindrop.py")
    run_script("publi_ad.py")
    print("=== [1단계] 완료 ===", flush=True)

    # ======================================
    # 🔸 2단계: 확장 실행 (현재는 항상 실행, 향후 선택적 실행 가능)
    # ======================================
    print("\n=== [2단계] 확장 실행 시작 ===", flush=True)
    run_script("xls.py", required=False)    # 나중에 옵션 실행으로 전환 가능
    run_script("image.py", required=False)  # 나중에 옵션 실행으로 전환 가능
    print("=== [2단계] 완료 ===", flush=True)

    # ======================================
    # 🔺 3단계: 종합 실행
    # ======================================
    print("\n=== [3단계] 종합 실행 시작 ===", flush=True)
    run_script("publi_sup.py")

    if os.path.isfile("xls.py"):
        print("📎 xls.py가 존재하므로 publi_xls.py 실행 시도", flush=True)
        run_script("publi_xls.py")
    else:
        print("⏩ xls.py가 없으므로 publi_xls.py 건너뜀", flush=True)

    print("\n🏁 전체 실행 완료", flush=True)

if __name__ == "__main__":
    main()
