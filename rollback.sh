#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run sudo bash rollback.sh /var/lib/remna-bootstrap/backups/NAME'; exit 1; }
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export BOOT_STATE=/var/lib/remna-bootstrap
exec 9>/run/remna-bootstrap.lock
flock -n 9 || { echo 'Another bootstrap/rollback is running'; exit 1; }
BACKUP=${1:?Specify the exact backup directory from the setup output}
# Validate backup and read immutable parameters before requesting the token.
readarray -t PARAMS < <(python3 - "$BACKUP" <<'PY'
import json, pathlib, sys
root = pathlib.Path('/var/lib/remna-bootstrap/backups').resolve()
p = pathlib.Path(sys.argv[1]).resolve()
if not p.is_relative_to(root): raise SystemExit('Invalid backup path')
s = json.loads((p/'panel.json').read_text())['state']
for k in ('panel', 'domain', 'name', 'panel_ip'): print(s[k])
PY
)
[[ ${#PARAMS[@]} == 4 ]] || { echo 'Invalid/incomplete backup'; exit 1; }
export PANEL_URL=${PARAMS[0]} NODE_DOMAIN=${PARAMS[1]} NODE_NAME=${PARAMS[2]} PANEL_IP=${PARAMS[3]}
read -rp 'API-токен панели для отката: ' PANEL_TOKEN; export PANEL_TOKEN
read -rp 'Секретная HTTPS ссылка входа панели (Enter — нет cookie-защиты): ' PANEL_INPUT
if [[ -n $PANEL_INPUT ]]; then
    PANEL_ORIGINAL=$PANEL_URL
    export PANEL_URL=$PANEL_INPUT
    readarray -t PANEL_CONNECTION < <(python3 "$HERE/panel_setup.py" connection)
    [[ ${#PANEL_CONNECTION[@]} == 2 && ${PANEL_CONNECTION[0]} == "$PANEL_ORIGINAL" ]] || { echo 'Access link must belong to the saved panel origin'; exit 1; }
    export PANEL_URL=$PANEL_ORIGINAL PANEL_ACCESS_COOKIE=${PANEL_CONNECTION[1]}
    unset PANEL_INPUT PANEL_CONNECTION PANEL_ORIGINAL
fi
printf 'Откат файлов/настроек и созданных этим запуском объектов панели к %s\n' "$BACKUP"
read -rp 'Введите ROLLBACK: ' confirm
[[ $confirm == ROLLBACK ]] || exit 1
python3 "$HERE/ops.py" validate-backup "$BACKUP"
python3 "$HERE/panel_setup.py" rollback "$BACKUP"
python3 "$HERE/ops.py" restore "$BACKUP"
unset PANEL_TOKEN PANEL_ACCESS_COOKIE
printf 'Rollback finished. Check SSH, DNS and node status. Apt packages/certificates remain installed.\n'
