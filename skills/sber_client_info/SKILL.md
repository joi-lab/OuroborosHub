---
name: sber_client_info
description: Информация о клиенте и счетах Сбербанка через прямой REST API Sber API — организация, реквизиты, список счетов.
version: 0.3.2
type: extension
runtime: python3
entry: plugin.py
permissions: [net, tool, route, widget, read_settings]
env_from_settings:
  - SBER_ACCESS_TOKEN
  - SBER_TLS_P12_PASSWORD
  - SBER_TLS_CERT_PATH
  - SBER_TLS_KEY_PATH
  - SBER_API_ENV
  - SBER_API_INSECURE
dependencies:
  - httpx
  - cryptography
when_to_use: Пользователь просит информацию о компании, реквизиты, ИНН, ОГРН, список счетов или данные клиента в Сбербанке.
timeout_sec: 60
ui_tab:
  tab_id: client_info
  title: Клиент Сбербанк
  icon: business
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: form
        route: info
        method: POST
        target: info_result
        fields:
          - name: api_env
            label: Контур API
            type: text
            placeholder: "prod или пусто для теста"
            required: false
        submit_label: Получить информацию о клиенте
      - type: json
        target: info_result
---

# Клиент Сбербанк (Sber API REST)

Расширение для получения информации о клиенте через прямой REST API [GET /v1/client-info](https://developers.sber.ru/docs/ru/sber-api/specifications/client-info/get-client-info).

## Возможности

| Инструмент агента | Назначение |
|---|---|
| `install_tls_certificate` | Установить .p12/.pfx из вложения чата в state скилла |
| `get_client_info` | Организация, реквизиты, счета |

## Настройка через чат (рекомендуется)

1. В **Settings** задайте и выдайте grant:
   - `SBER_ACCESS_TOKEN` — токен со scope `GET_CLIENT_ACCOUNTS`
   - `SBER_TLS_P12_PASSWORD` — пароль к P12 из личного кабинета Sber API
   - `SBER_API_INSECURE` — `1` на тестовом стенде при ошибках SSL (Windows)
2. **Прикрепите** `.p12` / `.pfx` в чат (скрепка).
3. Попросите агента: «Установи TLS-сертификат Сбера» — он вызовет `install_tls_certificate(source_path=...)` с путём из `data/uploads/`.
4. Запросите данные: «Покажи информацию о компании в Сбере».

Пароль и токен **не передаются через чат** — только через Settings с owner grant.

## Альтернатива: готовые PEM

Если PEM уже есть: `SBER_TLS_CERT_PATH` + `SBER_TLS_KEY_PATH` в Settings.

## API endpoints

| Контур | URL |
|---|---|
| Тест | `https://iftfintech.testsbi.sberbank.ru:9443/fintech/api/v1/client-info` |
| Пром | `https://fintech.sberbank.ru:9443/fintech/api/v1/client-info` |

## HTTP routes

- `POST /api/extensions/sber_client_info/info` — без тела; возвращает JSON с данными клиента

## Примеры запросов агенту

- «Вот сертификат, установи его» (с вложением .p12)
- «Покажи информацию о моей компании в Сбере»
- «Какие счета подключены к Sber API?»

## Зависимости

- `httpx` — HTTP-клиент
- `cryptography` — конвертация P12 в PEM
