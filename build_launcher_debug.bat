@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
cd /d "C:\Users\root\Documents\GitHub\snappy-detector\dist\SnappyAI"
cl /O2 /MT /GL /LD /nologo /DUNICODE /D_UNICODE /Fe"SnappyAI_debug.exe" "C:\Users\root\AppData\Local\Temp\opencode\snappy_launcher_debug.c" /link /SUBSYSTEM:WINDOWS /ENTRY:wWinMainCRTStartup user32.lib kernel32.lib