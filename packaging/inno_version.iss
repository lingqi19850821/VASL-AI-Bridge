; Query the installed compiler's own version without creating an installer.
#pragma message "ASL_AI_INNO_VERSION=" + DecodeVer(VER)

[Setup]
AppName=ASL_AI compiler version check
AppVersion=0
CreateAppDir=no
Uninstallable=no
Output=no
