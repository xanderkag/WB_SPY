# VPN Gate — публичные OpenVPN сервера

Топ-5 по score из https://www.vpngate.net/ (исследовательский проект University of Tsukuba).

## Серверы

### 🇷🇺 Россия — пробуй сначала

WB ориентирован на RU-аудиторию, у RU-IP выше шанс пройти WAF.

- **RU#1** `5.206.56.139`  score=1293482  ping=43ms  speed≈243.7Mb  ➔ `vpngate-RU-01-5_206_56_139.ovpn`
- **RU#2** `136.169.224.161`  score=523065  ping=43ms  speed≈47.3Mb  ➔ `vpngate-RU-02-136_169_224_161.ovpn`
- **RU#3** `31.148.3.252`  score=459355  ping=121ms  speed≈69.2Mb  ➔ `vpngate-RU-03-31_148_3_252.ovpn`

### 🇯🇵 Япония — fallback (стабильнее, но WB может фильтровать гео)

- **#1** JP  `219.100.37.218`  score=3074027  ping=9ms  speed≈229.7Mb  ➔ `vpngate-01-JP-219_100_37_218.ovpn`
- **#2** JP  `219.100.37.213`  score=2865688  ping=19ms  speed≈196.2Mb  ➔ `vpngate-02-JP-219_100_37_213.ovpn`
- **#3** JP  `219.100.37.59`  score=2658219  ping=14ms  speed≈146.3Mb  ➔ `vpngate-03-JP-219_100_37_59.ovpn`
- **#4** JP  `219.100.37.23`  score=2564541  ping=11ms  speed≈236.7Mb  ➔ `vpngate-04-JP-219_100_37_23.ovpn`
- **#5** JP  `219.100.37.187`  score=2462681  ping=13ms  speed≈260.8Mb  ➔ `vpngate-05-JP-219_100_37_187.ovpn`

## Как подключить на macOS (5 минут)

1. Скачай **Tunnelblick** (бесплатный GUI клиент OpenVPN): https://tunnelblick.net/downloads.html
2. Дважды кликни любой `.ovpn` файл из этой папки — Tunnelblick его импортирует.
3. В меню Tunnelblick (top bar) → **Connect** → выбери импортированный конфиг.
4. Дождись подключения. Проверь что VPN включён: `curl https://api.ipify.org` должен показать IP сервера.
5. Запусти парсер:
   ```bash
   cd wb-parser && source .venv/bin/activate && python -m wbp.cli once
   ```

## Альтернатива CLI

```bash
brew install openvpn
sudo openvpn --config vpn-configs/vpngate-01-XX-W_X_Y_Z.ovpn
```

## Важно

- VPN Gate сервера **публичные**, владельцы ведут логи (политика на vpngate.net). Для PoC OK, для прода — нет.
- Если первый сервер не открыл WB — пробуй следующий из списка. У WB могут быть забанены VPN Gate IP.
- Не все сервера одинаково стабильные. Score выше = сервер активнее.
