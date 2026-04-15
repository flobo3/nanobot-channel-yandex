# nanobot-channel-yandex

> **⚠️ WIP — Untested.** Yandex Messenger Bot API requires a paid Yandex 360 for Business plan. Development is based on documentation only; no live testing yet.

Yandex Messenger (Яндекс Мессенджер) channel plugin for [nanobot](https://github.com/HKUDS/nanobot).

## Requirements

- Yandex 360 for Business organization with Bot API enabled
- Bot OAuth token (created in organization admin panel)
- nanobot >= 0.1.5

## Installation

```bash
pip install nanobot-channel-yandex
```

## Configuration

Add to your `config.json`:

```json
{
  "yandex": {
    "enabled": true,
    "token": "y0_AgAAAAB...",
    "mode": "polling",
    "allow_from": ["*"],
    "streaming": false
  }
}
```

### Options

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable the channel |
| `token` | `""` | Bot OAuth token from Yandex 360 admin |
| `mode` | `"polling"` | Update mode: `polling` or `webhook` |
| `webhook_url` | `""` | Public URL for webhook mode |
| `poll_interval` | `2` | Polling interval in seconds |
| `allow_from` | `[]` | Sender allow list (`["*"]` = all) |
| `streaming` | `false` | Enable streaming (sends complete messages) |

## API Coverage

- [x] Receive text messages (private + group)
- [x] Send text messages
- [x] Send files
- [x] Send images
- [x] Delete messages
- [x] Inline keyboard buttons
- [x] Thread support (thread_id)
- [x] Polling mode
- [x] Webhook mode
- [ ] Streaming (Yandex API has no edit message endpoint)
- [ ] Polls (not yet implemented)

## License

MIT
