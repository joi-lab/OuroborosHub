---
id: whoop-health
name: WHOOP Health
version: 0.3.0
type: extension
description: >
  Сбор данных со смарт-браслета WHOOP через официальный API v1:
  сон, recovery, strain, тренировки, HRV, пульс покоя, SpO2.
  Полный OAuth2 Authorization Code flow для первичной авторизации
  и автоматическое обновление токенов.
trigger: >
  Пользователь спрашивает о данных с WHOOP: сон, восстановление,
  нагрузка (strain), тренировки, HRV, пульс, SpO2.
  Примеры: «покажи recovery с WHOOP», «как я спал по WHOOP?»,
  «сводка здоровья WHOOP», «авторизуй WHOOP».
entry: plugin.py
permissions:
  - tool
  - read_settings
  - net
env_from_settings:
  - WHOOP_CLIENT_ID
  - WHOOP_CLIENT_SECRET
scripts: []
tools:
  - whoop_auth_url
  - whoop_exchange_code
  - whoop_check
  - whoop_summary
  - whoop_fetch
---

# WHOOP Health v0.3.0

Extension skill для Ouroboros — собирает данные со смарт-браслета WHOOP
через [официальный API v1](https://developer.whoop.com/api).

## Аутентификация — OAuth2 Authorization Code Flow

### Первичная настройка

1. Зарегистрируйте приложение на [developer.whoop.com](https://developer.whoop.com)
2. Получите `client_id` и `client_secret`
3. Положите их в секреты Ouroboros: `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`
4. Вызовите `whoop_auth_url` — получите URL для авторизации в браузере
5. Откройте URL, авторизуйтесь, скопируйте `code` из redirect
6. Вызовите `whoop_exchange_code` с полученным кодом
7. Готово — токены сохранены, все data-инструменты работают автоматически

### Обновление токенов

Access token обновляется автоматически через refresh token при получении
401/403 от API. Refresh token сохраняется в state-директории навыка
и переживает рестарты.

## Инструменты

| Tool | Описание |
|------|----------|
| `whoop_auth_url` | Генерирует OAuth2 URL для авторизации в браузере |
| `whoop_exchange_code` | Обменивает authorization code на access + refresh tokens |
| `whoop_check` | Проверка подключения (GET /v1/user/profile/basic) |
| `whoop_summary` | Сводка за последние N дней: recovery, сон, strain |
| `whoop_fetch` | Детальные данные по типу: RECOVERY, SLEEP, WORKOUT, CYCLE |
