# telegram_bot_template



## Установка

`cd /opt/`

`git clone https://github.com/DrVidjet/telegram_bot_template.git`

`cd telegram_bot_template`

`nano API.conf`

Указываем API бота

## Создание виртуального окружения и компиляция бота

`sudo apt install python3.11-venv python3.11-dev`

`python3 -m venv .venv`

`source .venv/bin/activate`

`pyinstaller --onefile --add-data "API.conf:." --hidden-import=telebot --hidden-import=qrcode --name start_bot main.py`

Выходим из виртуальной среды

`deactivate`

## Пробный запуск

`./dist/start_bot`

Если был вывод 'Bot is successfully started', то всё в порядке.

## Делаем службу боту

`sudo adduser --system --group telegram_bot --home /opt/telegram_bot_template`

`sudo chown -R telegram_bot:telegram_bot /opt/telegram_bot_template`

`sudo nano /etc/systemd/system/telegram_bot_template.service`

<p>
[Unit]<br/>
Description=telegram_bot_template

[Service]<br/>
Type=exec<br/>
User=telegram_bot_template<br/>
WorkingDirectory=/opt/telegram_bot_template/<br/>
ExecStart=/opt/telegram_bot_template/dist/start_bot<br/>
Restart=always<br/>
RestartSec=3s<br/>

[Install]<br/>
WantedBy=multi-user.target
</p>

`systemctl enable telegram_bot_template`

`systemctl start telegram_bot_template`

`systemctl status telegram_bot_template`