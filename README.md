# Telegram Scheduler Bot

A Telegram bot for scheduling posts to multiple channels with flexible scheduling options.

## Features

- **Flexible Scheduling**: Hourly (interval), daily, or weekly schedules
- **Multi-Channel Support**: Manage multiple channels independently
- **Bulk Upload**: Queue many posts at once with caption management
- **Caption Formatting**: Preserves Telegram formatting (links, code, etc.) in queued captions
- **Secure Verification**: Channel ownership verification flow
- **Queue Management**: View, pause, resume, and edit schedules
- **Auto-Retry**: Automatic retry with exponential backoff for failed posts
- **Dockerized**: Fully containerized for easy deployment
- **Admin Tools**: Debug, stats, and broadcast commands (restricted to bot admin)

## Quick Start

### Prerequisites

- Docker and Docker Compose
- Telegram Bot Token (from [@BotFather](https://t.me/botfather))
- Your Telegram User ID (from [@userinfobot](https://t.me/userinfobot))

### Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/nfrelink/telegram-scheduler-bot
   cd telegram-scheduler-bot
   ```

2. **Create environment file**
   ```bash
   cp .env.example .env
   # Edit .env and fill in real values
   ```

3. **Create a local docker compose file**
   ```bash
   cp docker-compose.yml.example docker-compose.yml
   cp docker-compose.dev.yml.example docker-compose.dev.yml

   # You can now edit the copied files locally.
   ```

4. **Create the data directory (for SQLite)**
   ```bash
   mkdir -p data
   # The container runs as UID 1000; make sure it can write the database file:
   sudo chown -R 1000:1000 data
   chmod 755 data
   ```

5. **Start the bot**
   ```bash
   docker compose up -d --build
   ```

6. **View logs**
   ```bash
   docker compose logs -f
   ```

## Usage

Everything is wizard-driven. Send a command and follow the inline buttons or prompts — no need to memorise IDs or syntax.

### Adding a Channel

1. Add the bot to your channel as an administrator (posting permission required)
2. Send `/channels` in your private chat with the bot, then tap **Add channel**
3. Either forward any message from the channel here, or type its numeric ID or `@handle`
4. Post the one-time verification code the bot gives you to the channel
5. The bot detects the code, completes verification, and deletes the code post

### Setting Your Timezone

Schedule times (daily/weekly) are stored in your configured timezone.

- `/timezone` — guided region picker
- `/settimezone Europe/Amsterdam` — set directly by IANA name
- `/gettimezone` — show your current timezone

### Selecting Defaults (recommended)

Pick a default channel + schedule once so that `/bulk` and `/queue` work without extra arguments.

- `/select` — interactive channel → schedule picker

### Managing Schedules

- `/schedules` — list schedules for the selected channel; tap **New**, **Edit**, **Pause/Resume**, or **Delete**
- Three schedule types: **interval** (e.g. every 2 hours), **daily** (times of day), **weekly** (days × times)

### Bulk Upload

- `/bulk` — queue many posts at once; choose a caption mode, send media, then `/done`
- Supports photos, videos, and documents; albums are detected automatically
- Messages forwarded from channels in your `/forward` allowlist are re-sent as native Telegram forwards

### Queue Management

- `/queue` — paginated queue browser with inline navigation
- `/deletepost <post_id>` — remove a single queued post

### Forwarding Allowlist

- `/forward` — manage the list of channels whose forwarded posts are passed through as native Telegram forwards during bulk upload

### Admin Commands

Restricted to the `ADMIN_USER_ID` configured in `.env`.

- `/debug` — uptime, DB path, live counts
- `/stats` — delivery stats (today and last 7 days)
- `/broadcast <message>` — send a message to all users active in the last 90 days

## Development

### Running Tests

```bash
docker compose -f docker-compose.dev.yml run --rm bot python -m pytest -q
```

### Database Smoke Test

```bash
docker compose -f docker-compose.dev.yml run --rm bot python scripts/verify_db.py
```

## Troubleshooting

### Bot won't start

```bash
# Check logs
docker compose logs

# Verify environment variables
docker compose config

# Shell into container
docker compose exec bot bash
```

### Permission issues

```bash
# Fix data directory permissions
sudo chown -R 1000:1000 data/
chmod 755 data/
```

### Database issues

```bash
# Check database integrity
docker compose exec bot sqlite3 /app/data/scheduler.db "PRAGMA integrity_check"
```

## License

This is a personal project. Use at your own discretion.

## Contributing

This is primarily a personal project, but suggestions and improvements are welcome!

## Support

For issues or questions, please open an issue on GitHub.

---

**Note**: This bot is designed for personal use with a small number of channels. For high-volume or commercial use, additional optimizations and infrastructure may be needed.

