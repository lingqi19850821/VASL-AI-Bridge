; Query the installed compiler's own version without creating an installer.
#pragma message "VASL_AI_BRIDGE_INNO_VERSION=" + DecodeVer(VER)

[Setup]
AppName=VASL AI Bridge compiler version check
AppVersion=0
CreateAppDir=no
Uninstallable=no
Output=no
