' Windows launcher for the feedback applet (scripts/feedback_app.py) in the default WSL distro.
' Copy it to the Windows Desktop and double-click; window 0 = no console left open behind the app.
CreateObject("WScript.Shell").Run "wsl.exe -e bash -lc ""cd ~/jobsearch && .venv/bin/python scripts/feedback_app.py""", 0, False
