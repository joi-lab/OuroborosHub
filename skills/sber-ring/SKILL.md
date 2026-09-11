---
name: sber-ring
description: "Сбор данных с умного кольца Сбера (Life Balance): пульс, HRV, SpO2, сон, шаги, стресс, температура."
version: 1.0.0
type: extension
runtime: python3
entry: plugin.py
permissions: [net, tool, read_settings]
env_from_settings: [SBER_RING_TOKEN]
requested_keys:
  - key: SBER_RING_TOKEN
    description: "Bearer-токен для API Life Balance (Сбер Кольцо). Получить: app.life-balance.tech → профиль → API-доступ."
when_to_use: >
  Пользователь спрашивает о данных с умного кольца Сбера (Life Balance):
  пульс, вариабельность ритма (HRV), кислород в крови (SpO2), сон, шаги,
  стресс, температура тела. Примеры: «покажи мой пульс за неделю»,
  «как я спал?», «сводка здоровья с кольца».
timeout_sec: 60
---

# Sber Ring (Life Balance)

Extension-навык для получения данных с умного кольца Сбера через REST API
[app.life-balance.tech](https://app.life-balance.tech).

## Инструменты

### `sber_ring_fetch`
Универсальный запрос данных одного типа за указанный период.

**Параметры:**
- `data_type` — тип данных: `HEART_RATE`, `HRV`, `SPO2`, `SLEEP`, `STEP`, `STRESS`, `TEMPERATURE`
- `days_back` — количество дней назад от текущего момента (по умолчанию 7)
- `page` — номер страницы (по умолчанию 0)
- `page_size` — размер страницы (по умолчанию 100)

### `sber_ring_summary`
Сводка здоровья за последние 24 часа: забирает все 7 типов данных и
формирует компактный структурированный отчёт.

## Настройка

1. Получите Bearer-токен в приложении Life Balance или на сайте app.life-balance.tech
2. В Ouroboros: Settings → Skills → sber-ring → Grant → вставьте токен в поле `SBER_RING_TOKEN`
3. Включите навык (Enable)

## API

Единственный эндпоинт:
```
GET https://app.life-balance.tech/external/sync/{type}
Authorization: Bearer <token>
Query: from, to (Unix timestamp сек), page, pageSize
```
