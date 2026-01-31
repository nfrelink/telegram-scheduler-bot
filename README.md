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

Note: all schedule times you enter are interpreted as UTC (not your local timezone).

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

### Adding a Channel (verification)

1. Add the bot to your channel as an administrator (with permission to post messages)
2. In the channel, post `/channelid` to show the numeric channel ID
3. In private chat with the bot: `/addchannel <channel_id>`
4. Post the verification code to the channel
5. The bot detects the code and completes verification

### Selecting defaults (recommended)

If you select a channel and schedule once, many commands work without explicit IDs.

- `/selectchannel <channel_id>`
- `/listschedules` then `/selectschedule <schedule_id>`
- `/selection` shows what is currently selected
- `/clearselection` clears selection

### Creating a Schedule

Run `/newschedule` (uses selected channel) or `/newschedule <channel_id>`.

### Bulk Upload

Run `/bulk` (uses selected schedule) or `/bulk <schedule_id>`, then follow the prompts.

### Common Commands

- `/listchannels`
- `/listschedules [channel_id]`
- `/viewqueue [schedule_id] [count]`
- `/pauseschedule [schedule_id]`
- `/resumeschedule [schedule_id]`
- `/deletepost <post_id>`
- `/testschedule [schedule_id] [run_count]`

## Schedule Examples

Note: schedule times are interpreted as UTC (not your local timezone).

### Post every hour
```json
{
  "type": "interval",
  "hours": 1
}
```

### Post daily at 9 AM and 4 PM
```json
{
  "type": "daily",
  "times": ["09:00", "16:00"]
}
```

### Post Monday-Friday at noon
```json
{
  "type": "weekly",
  "days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
  "times": ["12:00"]
}
```

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

