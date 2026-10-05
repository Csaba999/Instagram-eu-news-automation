#!/bin/bash
# A Mac saját időzítője (launchd) indítja a "Daily EU news post" workflow-t a megadott
# időpontokban, a GitHub (néha kihagyó) ütemezője helyett. Ha a Mac épp alszik, ébredéskor
# pótolja. A GitHub-token a macOS kulcskarikájába kerül, fájlba nem.
#
# Futtatás a Terminálban:  bash install-trigger.sh
set -euo pipefail

REPO="Csaba999/Instagram-eu-news-automation"
REF="claude/instagram-telex-autopost-bot-khc4fy"
LABEL="hu.epas.eu-bot-trigger"
DIR="$HOME/.eu-bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TIMES="21:00"   # magyar idő (a Mac órája szerint)

mkdir -p "$DIR" "$HOME/Library/LaunchAgents"

read -rsp "GitHub token (Actions: Read and write) – beillesztés után Enter: " TOKEN
echo
security add-generic-password -U -a "$USER" -s eu-bot-github -w "$TOKEN"
unset TOKEN

cat > "$DIR/trigger.sh" <<EOS
#!/bin/bash
TOKEN=\$(security find-generic-password -a "\$USER" -s eu-bot-github -w)
if curl -fsS -X POST \\
    -H "Authorization: Bearer \$TOKEN" -H "Accept: application/vnd.github+json" \\
    https://api.github.com/repos/$REPO/actions/workflows/daily.yml/dispatches \\
    -d '{"ref":"$REF","inputs":{"dry_run":"false","scheduled":"true","max_per_run":"20"}}'; then
  echo "\$(date '+%F %R') elindítva"
else
  echo "\$(date '+%F %R') HIBA az indításnál"
fi
EOS
chmod +x "$DIR/trigger.sh"

{
  echo '<?xml version="1.0" encoding="UTF-8"?>'
  echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
  echo '<plist version="1.0"><dict>'
  echo "  <key>Label</key><string>$LABEL</string>"
  echo "  <key>ProgramArguments</key><array><string>$DIR/trigger.sh</string></array>"
  echo '  <key>StartCalendarInterval</key><array>'
  for t in $TIMES; do
    echo "    <dict><key>Hour</key><integer>$((10#${t%:*}))</integer><key>Minute</key><integer>$((10#${t#*:}))</integer></dict>"
  done
  echo '  </array>'
  echo "  <key>StandardOutPath</key><string>$DIR/trigger.log</string>"
  echo "  <key>StandardErrorPath</key><string>$DIR/trigger.log</string>"
  echo '</dict></plist>'
} > "$PLIST"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Kész. Időpontok: $TIMES"
echo "Napló: $DIR/trigger.log"
