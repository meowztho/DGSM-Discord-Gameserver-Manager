# DGSM – Discord Gameserver Manager (MIT)

Manage and automate your game servers directly from Discord – no remote desktop required.\
Designed as a lightweight alternative to WindowsGSM, DGSM runs entirely through Discord commands and buttons.

![Main Bot UI](docs/bot_ui.png)

---

## ✨ Features

- Start / Stop / Restart / Status via buttons and slash commands
- Optional auto-update and scheduled restart
- Manage multiple servers (Palworld, Core Keeper, Satisfactory, Unturned…)
- Role & permission checks for admin actions
- SQLite logging, JSON configuration
- **First run setup**: Automatically creates `.env`, `server_config.json` and default templates if missing
- Runs on Windows servers without RDP

---

## 📦 Installation

1. **Install Python 3.12**\
   [https://www.python.org/downloads/](https://www.python.org/downloads/)\
   Make sure to check **"Add Python to PATH"** during installation.

2. **Download DGSM**

   - Click the green **Code** button → **Download ZIP**
   - Or clone:
     ```bash
     git clone https://github.com/meowztho/DGSM-Discord-Gameserver-Manager.git
     cd DGSM-Discord-Gameserver-Manager
     ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **First run setup**\
   Simply start the bot:

   ```bash
   python -m src.Main
   ```

   On the first run, DGSM will:

   - Create `.env` (with placeholder values) if it does not exist
   - Create `server_config.json` with an example Palworld server
   - Create the `templates/` folder with default template files

   You only need to edit these files afterwards to:

   - Add your Discord bot token and admin channel ID in `.env`
   - Adjust `server_config.json` and templates for your servers

---

## ⚙️ Discord Bot Setup

Before running DGSM with a real token, you must create a bot account in the Discord Developer Portal.

1. **Go to the Developer Portal**\
   [https://discord.com/developers/applications](https://discord.com/developers/applications)

2. **Create a new application**

   - Click **New Application**
   - Name it (e.g., `DGSM Server Manager`)

3. **Add a Bot User**

   - Go to **Bot** in the left menu
   - Click **Add Bot** → **Yes, do it!**
   - Enable these intents:
     - `PRESENCE INTENT`
     - `SERVER MEMBERS INTENT`
     - `MESSAGE CONTENT INTENT`
   - Reset token and copy it for `.env`

4. **Set Admin Channel ID**

   - In Discord, enable **Developer Mode** (Settings → Advanced)
   - Right-click your admin channel → **Copy Channel ID**
   - Add to `.env`

5. **Invite the bot to your server**

   - Go to **OAuth2 → URL Generator**
   - Under **SCOPES**, check:
     - `bot`
     - `applications.commands`
   - Under **BOT PERMISSIONS**, check:
     - `Send Messages`
     - `Embed Links`
     - `Read Message History`
     - `Use Slash Commands`
   - Copy the generated URL → open in browser → Authorize bot for your server.

6. **Create required roles**

   - In your Discord server settings, go to **Roles** and create:
     - **Admin** – for full bot control and all admin commands
     - **Player** – for basic usage such as viewing status, starting servers (if allowed)
   - Assign these roles to users accordingly. The bot checks these roles to determine command permissions.

---

## 📷 Screenshots

| Main Menu | Server Status |
| --------- | ------------- |
|![Main Menu](docs/bot_ui.png)|![Server Status](docs/bot_status.png)|
|![Server Status](docs/slashcommand.png)|![Server Status](docs/full.png)|

---

## 📥 SteamCMD Setup

Some servers require SteamCMD to install or update.

1. Download from Valve:\
   [https://developer.valvesoftware.com/wiki/SteamCMD](https://developer.valvesoftware.com/wiki/SteamCMD)

2. Extract to a folder, e.g.:

   ```
   src/steam
   ```

   or anywhere else (update path in your config).

---

## 🛠 Configuration File Reference

DGSM stores its settings in JSON files. These files are created automatically on first run, but you can also edit them manually.

### 1. `server_config.json`

Defines all game servers managed by DGSM.

```json
{
  "servers": [
    {
      "name": "Palworld",
      "steam_app_id": 2394010,
      "executable": "PalServer.exe",
      "parameters": [
        "-useperfthreads", "-UseMultithreadForDS",
        "-RCONEnabled=True", "-RCONPort=25575",
        "-AdminPassword=CHANGE_ME"
      ]
    }
  ],
  "auto_update": true,
  "auto_restart": true,
  "stop_time": "02:09",
  "restart_after_stop": true
}
```

**Fields:**

- `name`: Server display name in Discord
- `steam_app_id`: Steam App ID for this game
- `executable`: The server `.exe` file name
- `parameters`: List of startup parameters
- `auto_update`: If `true`, updates before start
- `auto_restart`: If `true`, restarts at `stop_time`
- `stop_time`: Time for daily stop/restart (`HH:MM`)
- `restart_after_stop`: If `true`, restarts automatically after stop

---

### 2. `plugin_templates/`

Contains templates for supported games. Templates define the default configuration for a new server of that game. Example (`plugin_templates/Palworld/server_settings.json`):

```json
{
  "world_name": "MyPalworld",
  "max_players": 16,
  "password": "",
  "difficulty": "Normal"
}
```

These values are used when DGSM installs a new server instance.

---

### 3. `server_settings.json` (per server)

Each server folder (under `steam/`) can have its own `server_settings.json`. These override the defaults from the template.

**Notes:**

- Always keep a backup before manual editing
- JSON must be valid – check syntax with a JSON validator

---

## 💖 Support this project

If DGSM saves you time or helps you run your servers, please consider supporting development:

- [**GitHub Sponsors**](https://github.com/sponsors/meowztho)
- [**Patreon**](patreon.com/meowztho)

---

## 📜 License

MIT – see [LICENSE](LICENSE) for details.

