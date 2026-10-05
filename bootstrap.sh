#!/usr/bin/env bash
# Ubuntu 24.04 initial setup. Run the file, do not source it.
set -Eeuo pipefail
umask 077
REVERSE_COMMIT=7ba662b17ed57d2ba75a57e3c8bada671df65b9f
JOLY_TAG=v26.9.5-0936
STATE=/var/lib/remna-bootstrap
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CORE_PENDING=0
CORE_BACKUP=''
SETUP_BACKUP=''
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
finish() {
    local rc=$?
    trap - EXIT ERR
    if [[ $rc != 0 && $CORE_PENDING == 1 ]]; then
        printf 'Core verification failed; restoring prior binary and configuration...\n' >&2
        python3 "$HERE/ops.py" restore "$CORE_BACKUP" || printf 'Automatic restore failed; use rollback.sh and inspect services.\n' >&2
    fi
    if [[ $rc != 0 && -n $SETUP_BACKUP ]]; then
        printf 'Setup backup: %s. Full explicit rollback: sudo bash rollback.sh %s\n' "$SETUP_BACKUP" "$SETUP_BACKUP" >&2
    fi
    unset PANEL_TOKEN PANEL_ACCESS_COOKIE PANEL_INPUT
    exit "$rc"
}
trap finish EXIT
trap 'printf "Setup stopped at line %s. State: %s\n" "$LINENO" "$STATE" >&2' ERR
[[ $EUID == 0 ]] || die 'Run sudo bash bootstrap.sh'
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || die 'Ubuntu 24.04 required'
for file in panel_setup.py ops.py monitor.py host_options.sh; do
    [[ -f $HERE/$file ]] || die "Keep $file next to bootstrap.sh"
done
exec 9>/run/remna-bootstrap.lock
flock -n 9 || die 'Another setup is running'
[[ -t 0 ]] || die 'Run from an interactive terminal'

read -rp 'Домен ноды (DNS-only, A-запись на этот VPS): ' NODE_DOMAIN
read -rp 'Публичный IPv4 панели (источник соединений к ноде): ' PANEL_IP
read -rp 'URL панели (можно полную секретную ссылку входа): ' PANEL_INPUT
PANEL_URL=$PANEL_INPUT
read -rp 'Имя ноды/профиля (3–20 букв, цифр, _ или -): ' NODE_NAME
read -rp 'API-токен панели: ' PANEL_TOKEN
[[ $NODE_DOMAIN =~ ^([a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$ ]] || die 'Invalid domain'
[[ $NODE_NAME =~ ^[A-Za-z0-9_-]{3,20}$ ]] || die 'Invalid node name'
NODE_DOMAIN=${NODE_DOMAIN,,}
PANEL_URL=${PANEL_URL%/}
export NODE_DOMAIN PANEL_IP PANEL_URL NODE_NAME PANEL_TOKEN BOOT_STATE=$STATE
readarray -t PANEL_CONNECTION < <(python3 "$HERE/panel_setup.py" connection)
[[ ${#PANEL_CONNECTION[@]} == 2 ]] || die 'Invalid panel URL/access link'
PANEL_URL=${PANEL_CONNECTION[0]}
PANEL_ACCESS_COOKIE=${PANEL_CONNECTION[1]}
export PANEL_URL PANEL_ACCESS_COOKIE
unset PANEL_INPUT PANEL_CONNECTION
case ${1:-} in
    --check-api) python3 "$HERE/panel_setup.py" check-api; exit ;;
    --test-user) python3 "$HERE/panel_setup.py" test-user; exit ;;
    --cleanup-test) python3 "$HERE/panel_setup.py" cleanup-test; exit ;;
    '') ;;
    *) die 'Supported options: --check-api, --test-user, --cleanup-test' ;;
esac
read -rp 'Включить BBR/fq после проверки ядра? [y/N]: ' ENABLE_BBR
read -rp 'Настроить локальный Unbound с DNS-over-TLS? [y/N]: ' ENABLE_DNS
read -rp 'Установить мониторинг состояния/диска/сертификата? [Y/n]: ' ENABLE_MONITOR
MONITOR_WEBHOOK=''
if [[ ${ENABLE_MONITOR,,} != n ]]; then
    read -rsp 'HTTPS webhook для уведомлений (Enter — только журнал systemd): ' MONITOR_WEBHOOK; printf '\n'
    [[ -z $MONITOR_WEBHOOK || $MONITOR_WEBHOOK == https://* ]] || die 'HTTPS webhook required'
fi

printf 'Updating Ubuntu packages...\n'
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
apt-get install -y ca-certificates curl gnupg jq openssl unzip python3 ufw dnsutils git
python3 -c 'import ipaddress,os; ipaddress.IPv4Address(os.environ["PANEL_IP"])'

if ! command -v docker >/dev/null; then
    # Refuse incompatible installations instead of removing packages automatically.
    for pkg in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
        if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
            die "Conflicting package $pkg: choose/migrate the Docker installation first"
        fi
    done
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL --retry 3 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable\n' \
        "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
    printf 'Docker installed: skipping installation.\n'
fi
if ! docker compose version >/dev/null 2>&1; then
    # Ubuntu docker.io and Docker CE use different package names.
    if dpkg-query -W -f='${Status}' docker.io 2>/dev/null | grep -q 'install ok installed'; then
        apt-get install -y docker-compose-v2
    else
        apt-get install -y docker-compose-plugin
    fi
fi
systemctl enable --now docker
docker info >/dev/null
docker compose version

install -d -m 700 "$STATE"
if [[ ! -e $STATE/stack-owned ]]; then
    for target in /opt/remnanode/docker-compose.yml /opt/remnanode/Caddyfile /opt/remnanode/xray-custom; do
        [[ ! -e $target ]] || die "Existing file $target: refusing to overwrite it"
    done
    for container in remnanode caddy-remnawave; do
        if docker container inspect "$container" >/dev/null 2>&1; then
            die "Existing container $container: refusing to adopt it"
        fi
    done
fi
if [[ ! -e $STATE/stack-owned || ! -f /opt/remnanode/docker-compose.yml || ! -f /opt/remnanode/Caddyfile ]]; then
    ss -H -ltn | awk '{print $4}' | grep -Eq ':(80|443|2222)$' && die '80, 443 or 2222 already occupied'
fi
PUBLIC_IP=$(curl -4 -fsS --retry 3 --max-time 15 https://api.ipify.org)
dig +short A "$NODE_DOMAIN" | grep -Fxq "$PUBLIC_IP" || die "A record must point to $PUBLIC_IP, without CDN proxy"
if [[ -n $(dig +short AAAA "$NODE_DOMAIN") ]]; then
    die 'This IPv4 setup requires removing AAAA first (IPv6 can be configured separately)'
fi
python3 "$HERE/panel_setup.py" prepare
SETUP_BACKUP=$(python3 "$HERE/ops.py" snapshot)
python3 "$HERE/panel_setup.py" snapshot "$SETUP_BACKUP"
source "$HERE/host_options.sh"
[[ ${ENABLE_BBR,,} != y ]] || configure_bbr
[[ ${ENABLE_DNS,,} != y ]] || configure_dns

REPO=$STATE/reverse-proxy
if [[ ! -d $REPO/.git ]]; then
    git clone https://github.com/eGamesAPI/remnawave-reverse-proxy.git "$REPO"
fi
git -C "$REPO" checkout --detach "$REVERSE_COMMIT"
[[ $(git -C "$REPO" rev-parse HEAD) == "$REVERSE_COMMIT" ]] || die 'Wrong installer revision'
[[ -z $(git -C "$REPO" status --porcelain --untracked-files=no) ]] || die 'Installer checkout modified'
# Upstream has no general unattended CLI. Load its definitions before the menu.
# The pinned marker is checked; no stdin sequence is fed into its menu.
python3 - "$REPO" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
t = (p/'install_remnawave.sh').read_text()
marker = '\ndetect_broken_ipv6\n'
if t.count(marker) != 1:
    raise SystemExit('Upstream entrypoint changed; adapter must be reviewed')
(p/'bootstrap-library.sh').write_text(t.split(marker)[0] + '\n')
PY
set +e +u
source "$REPO/bootstrap-library.sh"
set -Eeuo pipefail
LOCAL_SRC_DIR=$REPO/src
SOURCE_BRANCH=$REVERSE_COMMIT
SOURCE_BASE_URL="https://raw.githubusercontent.com/eGamesAPI/remnawave-reverse-proxy/$REVERSE_COMMIT"
set +u
set_language ru
load_caddy_node_module
load_node_core_module
set -u
# Translate upstream prompts into the parameters already collected.
reading() {
    case "$2" in
        SELFSTEAL_DOMAIN) printf -v "$2" '%s' "$NODE_DOMAIN" ;;
        PANEL_IP) printf -v "$2" '%s' "$PANEL_IP" ;;
        *) die "Unexpected upstream prompt: $2" ;;
    esac
}
read_yn() { printf -v "$1" '%s' y; return 0; }
reading_yn() { printf -v "$2" '%s' y; return 0; }
check_domain() { return 0; } # Already checked strictly above.

if [[ ! -e $STATE/stack-owned || ! -f /opt/remnanode/docker-compose.yml || ! -f /opt/remnanode/Caddyfile ]]; then
    # Claim ownership before creation so an interrupted generation is recoverable.
    touch "$STATE/stack-owned"
    (set +u; install_node_caddy < "$STATE/node-secret")
    install -d -m 755 /var/www/html
    if [[ ! -e /var/www/html/index.html ]]; then
        printf '<!doctype html><html lang="en"><meta charset="utf-8"><title>Welcome</title><h1>Welcome</h1><p>This website is being prepared.</p></html>\n' > /var/www/html/index.html
        chmod 644 /var/www/html/index.html
    fi
fi
rm -f "$STATE/node-secret"
[[ -f /opt/remnanode/docker-compose.yml && -f /opt/remnanode/Caddyfile ]] || die 'Incomplete stack generation; inspect /opt/remnanode'

# Preserve SSH access before enabling firewall, including a nonstandard SSH port.
SSH_PORTS=$(sshd -T | awk '$1 == "port" {print $2}')
[[ -n $SSH_PORTS ]] || die 'Could not determine SSH ports'
if [[ -n ${SSH_CONNECTION:-} ]]; then
    SSH_ACTIVE_PORT=${SSH_CONNECTION##* }
    ufw allow "$SSH_ACTIVE_PORT/tcp"
fi
for ssh_port in $SSH_PORTS; do ufw allow "$ssh_port/tcp"; done
ufw allow 80/tcp
ufw allow 443/tcp
# Put the node restriction first so a pre-existing broad allow cannot bypass it.
ufw insert 1 deny 2222/tcp
ufw insert 1 allow from "$PANEL_IP" to any port 2222 proto tcp
ufw --force enable
ufw reload

cd /opt/remnanode
python3 "$HERE/ops.py" pin-images
docker compose config -q
docker compose up -d
docker exec caddy-remnawave caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
python3 "$HERE/ops.py" certificate-storage
for attempt in {1..30}; do
    [[ -S /dev/shm/nginx.sock ]] && break
    sleep 2
done
[[ -S /dev/shm/nginx.sock ]] || die 'Caddy socket not available'
docker cp "$STATE/xray.json" remnanode:/tmp/bootstrap-xray.json
docker exec remnanode /usr/local/bin/xray run -test -config /tmp/bootstrap-xray.json
docker exec remnanode rm -f /tmp/bootstrap-xray.json
python3 "$HERE/panel_setup.py" register
python3 "$HERE/panel_setup.py" verify
python3 "$HERE/panel_setup.py" squad

# The upstream core module uses a preverified archive; missing checksum is fatal.
python3 "$HERE/ops.py" fetch-core "$JOLY_TAG" "$(xc_asset_name)"
CORE_BACKUP=$(python3 "$HERE/ops.py" snapshot)
python3 "$HERE/panel_setup.py" snapshot "$CORE_BACKUP"
xc_download_core() { install -m 755 "$STATE/verified-core/xray" "$1/$XC_BINARY_NAME"; }
CORE_PENDING=1
(set +e +u; xc_install_core joly "$JOLY_TAG") || die 'Fork installation failed'
PROFILE_CORE=$(xc_profile_core)
[[ -z $PROFILE_CORE ]] || die "Panel profile uses another core: $PROFILE_CORE. Mounted fork is not active; inspect profile core selection"
HOST_HASH=$(sha256sum /opt/remnanode/xray-custom | awk '{print $1}')
CONTAINER_HASH=$(docker exec remnanode sha256sum /usr/local/bin/xray | awk '{print $1}')
[[ $HOST_HASH == "$CONTAINER_HASH" ]] || die 'Container is not using the downloaded fork binary'
docker exec remnanode /usr/local/bin/rw-core version 2>/dev/null || docker exec remnanode xray version
RUN_VERSION=$(xc_running_version)
WANT_VERSION=${JOLY_TAG#v}; WANT_VERSION=${WANT_VERSION%%-*}
ACTUAL_VERSION=$(printf '%s\n' "$RUN_VERSION" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)
[[ $ACTUAL_VERSION == "$WANT_VERSION" ]] || die 'Running core version does not match selected fork'
docker cp "$STATE/xray.json" remnanode:/tmp/bootstrap-xray.json
docker exec remnanode /usr/local/bin/xray run -test -config /tmp/bootstrap-xray.json
docker exec remnanode rm -f /tmp/bootstrap-xray.json
python3 "$HERE/panel_setup.py" verify
PROCESS_HASH=$(docker exec remnanode sh -c 'set --; for p in /proc/[0-9]*; do name=$(cat "$p/comm" 2>/dev/null) || continue; case "$name" in rw-core|xray) set -- "$@" "$p";; esac; done; [ "$#" -eq 1 ] || exit 1; sha256sum "$1/exe"' | awk '{print $1}')
[[ $PROCESS_HASH == "$HOST_HASH" ]] || die 'The running Xray process does not match the fork binary'
curl --fail --silent --show-error --retry 12 --retry-delay 5 --retry-all-errors --max-time 10 "https://$NODE_DOMAIN/" -o /dev/null
CORE_PENDING=0
[[ ${ENABLE_MONITOR,,} == n ]] || install_monitor
printf 'Initial backup: %s\nCore backup: %s\n' "$SETUP_BACKUP" "$CORE_BACKUP"
read -rp 'Создать временного пользователя и конфиг для внешнего теста Vision? [y/N]: ' CREATE_TEST
[[ ${CREATE_TEST,,} != y ]] || python3 "$HERE/panel_setup.py" test-user
unset PANEL_TOKEN
printf '\nГотово: %s. Конфиг и UUID: %s\nПроверьте VLESS/Vision с внешнего клиента. Мониторинг: journalctl -u remna-monitor.service\n' "$NODE_DOMAIN" "$STATE"
[[ ! -f /var/run/reboot-required ]] || printf 'Ubuntu требует перезагрузку; выполните её после проверки ноды.\n'
