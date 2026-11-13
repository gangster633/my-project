/* 황금 키워드 찾기 실행 파일
 * 같은 폴더의 runtime\pythonw.exe 로 app\keyword_finder.py 를 콘솔 창 없이 실행한다.
 * 빌드: x86_64-w64-mingw32-gcc -municode -mwindows -O2 launcher.c icon.o -o 황금키워드찾기.exe
 */
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE h, HINSTANCE p, PWSTR cmd, int show) {
    wchar_t dir[MAX_PATH], py[MAX_PATH], script[MAX_PATH], line[MAX_PATH * 3];
    DWORD n = GetModuleFileNameW(NULL, dir, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return 1;
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = 0;

    swprintf(py, MAX_PATH, L"%ls\\runtime\\pythonw.exe", dir);
    swprintf(script, MAX_PATH, L"%ls\\app\\keyword_finder.py", dir);
    if (GetFileAttributesW(py) == INVALID_FILE_ATTRIBUTES || GetFileAttributesW(script) == INVALID_FILE_ATTRIBUTES) {
        MessageBoxW(NULL, L"프로그램 파일이 없습니다.\n압축을 푼 폴더 안에서 그대로 실행해 주세요.", L"황금 키워드 찾기", MB_ICONERROR);
        return 1;
    }
    swprintf(line, MAX_PATH * 3, L"\"%ls\" \"%ls\"", py, script);

    STARTUPINFOW si = { sizeof(si) };
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(py, line, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, dir, &si, &pi)) {
        MessageBoxW(NULL, L"프로그램을 시작하지 못했습니다.", L"황금 키워드 찾기", MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
