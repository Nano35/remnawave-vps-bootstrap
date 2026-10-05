# Разработка и проверки

Используйте Ubuntu 24.04 и Python 3.12. Для локальных проверок не нужны реальная панель, root, Docker daemon или VPS: тесты подменяют внешние вызовы и используют временные каталоги.

```bash
for script in bootstrap.sh host_options.sh rollback.sh external-test.sh; do
    bash -n "$script"
done
python3 -B test_panel_transport.py
python3 -B test_panel_setup.py
```

`test_panel_transport.py` проверяет вставку имени ноды в настоящем Bash, URL/cookie отдельно от токена, нормализацию токена, ошибки доступа, маскирование секретов и повторы GET. `test_panel_setup.py` моделирует создание объектов, повторный запуск, конфликты, добавление inbound без потери существующих, временного пользователя, откат, checksum, backup и мониторинг.

Эти проверки не запускают установку. Изменения upstream-адаптера, Compose, Caddy, ядра или firewall проверяйте на отдельном тестовом VPS, затем выполните внешний тест VLESS Reality Vision.

GitHub Actions выполняет те же офлайн-проверки на Ubuntu 24.04 при push в main и pull request. Workflow не использует доступ к панели или VPS; checkout закреплён по commit SHA официального actions/checkout.

Не добавляйте API-токены, секретные URL панели, реальные UUID пользователей, Reality private keys, пароли VPS или сгенерированные клиентские конфиги. В примерах используйте `node.example.com`, `panel.example.com` и адреса документационного диапазона `203.0.113.0/24`.

В описании изменения укажите проблему, итоговое поведение и выполненные проверки. Обновление `REVERSE_COMMIT` требует проверки структуры upstream-модулей; обновление `JOLY_TAG` — checksum релиза и фактического процесса в контейнере.
