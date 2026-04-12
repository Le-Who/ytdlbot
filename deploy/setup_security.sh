#!/bin/bash
# ==============================================================================
# Telegram Bot VPS Security Hardening Script (Ubuntu/Debian)
# Run as root or with sudo: sudo ./setup_security.sh
# ==============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err()  { echo -e "${RED}[ERROR]${NC} $1"; }

if [[ $EUID -ne 0 ]]; then
   log_err "Этот скрипт должен быть запущен с правами root (используйте sudo)." 
   exit 1
fi

log_info "Начинаем настройку базовой безопасности VPS..."

# 1. Update and Upgrade System
log_info "1. Обновление пакетов и системы..."
apt-get update -y && apt-get upgrade -y
apt-get install -y ufw fail2ban unattended-upgrades curl docker.io docker-compose

# 2. Setup Unattended Upgrades for Security Patches
log_info "2. Настройка автоматических обновлений безопасности..."
dpkg-reconfigure --priority=low unattended-upgrades < /dev/null

# 3. Setup UFW (Firewall)
# SSH, HTTP, HTTPS allowed. Everything else dropped.
log_info "3. Настройка брандмауэра (UFW)..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing

# ВНИМАНИЕ: Обязательно разрешаем SSH, иначе потеряем доступ
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
# Если используете кастомные порты Telegram Webhooks (8443, 88), раскомментируйте:
# ufw allow 8443/tcp
# ufw allow 88/tcp

ufw --force enable
log_info "UFW активирован. Открыты порты 22(SSH), 80, 443."

# 4. Fail2Ban Configuration
log_info "4. Настройка Fail2Ban для защиты от перебора паролей..."
cat > /etc/fail2ban/jail.local <<EOF
[sshd]
enabled = true
port    = ssh
logpath = %(sshd_log)s
backend = %(sshd_backend)s
maxretry = 4
bantime = 3600
findtime = 600
EOF
systemctl restart fail2ban
systemctl enable fail2ban

# 5. SSH Hardening
log_info "5. Настройка SSH: отключение входа по паролю и root-доступа..."
SSHD_CONFIG="/etc/ssh/sshd_config"

# Ищем пользователя, отличного от root, для которого будем сохранять доступ
NON_ROOT_USER=$(id -un 1000 2>/dev/null || echo "")

if [ -z "$NON_ROOT_USER" ]; then
    log_warn "В системе нет пользователя с ID 1000. Вход от root НЕ БУДЕТ полностью запрещен, чтобы не потерять доступ."
    log_warn "Рекомендуется создать пользователя (adduser username), добавить SSH ключи и перезапустить этот скрипт."
else
    log_info "Найден обычный пользователь: $NON_ROOT_USER"
    # Отключаем вход по паролю (разрешаем только по ключам)
    sed -i 's/^#*PasswordAuthentication .*/PasswordAuthentication no/' $SSHD_CONFIG
    
    # Отключаем вход для root (если есть хотя бы один обычный пользователь)
    sed -i 's/^#*PermitRootLogin .*/PermitRootLogin no/' $SSHD_CONFIG
fi

# Убедимся, что pubkey auth включена
sed -i 's/^#*PubkeyAuthentication .*/PubkeyAuthentication yes/' $SSHD_CONFIG

log_info "Перезапуск службы SSH..."
systemctl restart sshd || systemctl restart ssh

# 6. Docker Security Checks
log_info "6. Конфигурация Docker..."
systemctl enable docker
systemctl start docker
if [ -n "$NON_ROOT_USER" ]; then
    usermod -aG docker $NON_ROOT_USER
    log_info "Пользователь $NON_ROOT_USER добавлен в группу docker."
fi

log_info "================================================================"
log_info "Готово! Система базово защищена."
log_info "  - Обновления устанавливаются автоматически."
log_info "  - Порты закрыты (кроме 22, 80, 443)."
log_info "  - Вход по паролю для SSH отключен (требуются SSH ключи)."
log_info "  - Fail2Ban запущен."
log_info "  - Docker установлен."
log_info "================================================================"
log_warn "ВАЖНО: Если у вас настроены кастомные порты, убедитесь что они открыты в UFW."
log_warn "Для вступления в силу группы docker перезайдите на сервер."
