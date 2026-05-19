# tg_vpn_bot



## Установка

`git clone https://github.com/DrVidjet/tg_vpn_bot.git`

`cd tg_vpn_bot`

`nano API.conf`

Указываем API бота, данные для оплаты и данные 3x-ui

`sudo apt install python3 python3.11-venv`

`python3 -m venv .venv`

`source .venv/bin/activate`

`pip install -r requirements.txt`

## Пробный запуск

`python3 main.py`

Если запуск прошел успешно и мы увидели

`Bot is successfully started`

то выходим из виртуальной среды

`deactivate`

## Делаем службу боту

`sudo adduser --system --group tg_vpn_bot --home /opt/tg_vpn_bot`

`sudo chown -R tg_vpn_bot:tg_vpn_bot /opt/tg_vpn_bot`

`sudo nano /etc/systemd/system/tg_vpn_bot.service`

<p>
[Unit]<br/>
Description=tg_vpn_bot

[Service]<br/>
Type=simple<br/>
User=tg_vpn_bot<br/>
WorkingDirectory=/opt/tg_vpn_bot/<br/>
ExecStart=/opt/tg_vpn_bot/.venv/bin/python3 /opt/tg_vpn_bot/main.py<br/>
Restart=always<br/>
RestartSec=3s<br/>

[Install]<br/>
WantedBy=multi-user.target
</p>

`sudo systemctl daemon-reload`

`sudo systemctl enable --now tg_vpn_bot`

`sudo systemctl status tg_vpn_bot`
