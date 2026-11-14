#!/bin/zsh
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGE_SH="${BASE_DIR}/manage.sh"

CHOICE=$(/usr/bin/osascript <<'EOF'
set theChoices to {"Show Battery Health & Forensics HUD on Screen", "Pause Fullscreen Alerts for 2 Hours", "Pause Fullscreen Alerts for 4 Hours", "Resume Fullscreen Alerts Now"}
set selectedItem to choose from list theChoices with title "Battery Guardian - Quick Menu" with prompt "Choose an action (all alerts are silent; critical <15% email alerts stay active):" default items {"Pause Fullscreen Alerts for 2 Hours"} OK button name "Run" cancel button name "Cancel"
if selectedItem is false then
    return "CANCEL"
else
    return item 1 of selectedItem
end if
EOF
)

case "$CHOICE" in
    "Show Battery Health & Forensics HUD on Screen")
        "$MANAGE_SH" hud
        ;;
    "Pause Fullscreen Alerts for 2 Hours")
        "$MANAGE_SH" pause 120
        /usr/bin/osascript -e 'display notification "Fullscreen battery alerts paused for 2 hours (<15% critical email stays active)." with title "Battery Guardian: Meeting Mode (2h)"'
        ;;
    "Pause Fullscreen Alerts for 4 Hours")
        "$MANAGE_SH" pause 240
        /usr/bin/osascript -e 'display notification "Fullscreen battery alerts paused for 4 hours (<15% critical email stays active)." with title "Battery Guardian: Meeting Mode (4h)"'
        ;;
    "Resume Fullscreen Alerts Now")
        "$MANAGE_SH" unmute
        /usr/bin/osascript -e 'display notification "Silent fullscreen battery alerts (>70% / <30%) resumed." with title "Battery Guardian: Active"'
        ;;
esac
