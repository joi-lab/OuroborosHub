---
name: weather
description: Wide polished weather widget — current conditions, compact forecast, astronomy, and resilient wttr.in cache (no API key).
version: 0.3.2
type: extension
entry: plugin.py
permissions: [net, tool, route, widget]
env_from_settings: []
when_to_use: User asks about weather, temperature, forecast, current conditions, wind, humidity, sunrise/sunset, or a weather dashboard for a city.
ui_tab:
  tab_id: widget
  title: Weather
  icon: cloud
  render:
    kind: declarative
    schema_version: 1
    span: 2
    components:
      - type: form
        route: forecast
        method: GET
        target: result
        submit_label: Update
        fields:
          - name: city
            label: City
            type: text
            default: Moscow
            required: true
      - type: table
        target: result
        path: weather_summary
        columns:
          - label: Now
            path: now
          - label: Feels
            path: feels
          - label: Wind
            path: wind
          - label: Humidity
            path: humidity
      - type: markdown
        target: result
        path: advisory_markdown
      - type: key_value
        target: result
        path: metric_chips
      - type: tabs
        target: result
        tabs:
          - label: Forecast
            components:
              - type: table
                path: forecast_rows
                columns:
                  - label: Day
                    path: day
                  - label: Sky
                    path: outlook
                  - label: Temp
                    path: range
          - label: Hourly
            components:
              - type: chart
                path: hourly_chart
                chart_type: line
          - label: Details
            components:
              - type: kv
                fields:
                  - label: Location
                    path: resolved_to
                  - label: Temperature
                    path: temp_display
                  - label: Feels like
                    path: feels_like_display
                  - label: Condition
                    path: condition
                  - label: Cache
                    path: cache_state
              - type: markdown
                path: astro_markdown
---

# Weather

A polished, wide **weather cockpit** widget for Ouroboros.

- **No API key**: it uses public `wttr.in` JSON.
- **No binary assets**: weather identity comes from Unicode, host tables, key-value rows, and charts.
- **No custom browser JavaScript**: the widget stays inside the reviewed declarative renderer.
- **No broad filesystem permission**: cache files live only under `PluginAPI.get_state_dir()`.

## What changed in 0.3.2

The widget now uses the host's two-column card span and avoids oversized markdown typography:

1. A single city field and short **Update** button reduce form height.
2. The current weather appears as a compact cockpit table (`Now / Feels / Wind / Humidity`) instead of a giant hero paragraph.
3. Secondary metrics are dense key-value rows.
4. Forecast remains the primary tab with short `Day / Sky / Temp` columns.
5. Hourly and astronomy details stay behind tabs.

## Tool use

The agent-callable tool is exposed as `ext_9_r_weather_fetch` and returns the same rich JSON surface as the widget route:

```json
{
  "city": "Tokyo",
  "units": "metric"
}
```

The response includes current conditions, comfort index, wind, pressure, visibility, UV, astronomy text, hourly chart data, compact forecast rows, advisory text, and cache status.

## Network and cache policy

The extension contacts only `https://wttr.in/<city>?format=j1` and refuses cross-host redirects. If the live refresh fails, it may show a recent cached response from its private skill state directory so the widget remains useful during short network outages. Cached data is a convenience fallback, not a replacement for live weather.
