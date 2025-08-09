# DGSM – Discord Gameserver Manager (MIT)

Manage and automate your game servers directly from Discord.
Designed as a lightweight alternative to WindowsGSM, DGSM runs entirely through Discord commands and buttons.

![Main Bot UI](docs/images/bot_ui.png)

---

## ✨ Features

* Start / Stop / Restart / Status via buttons and slash commands
* Optional auto-update and scheduled restart
* Manage multiple servers (Palworld, Core Keeper, Satisfactory, Unturned…)
* Role & permission checks for admin actions
* SQLite logging, JSON configuration
* **First run setup**: Automatically creates `.env`, `server_config.json` and default templates if missing
* After Setup runs on Windows servers without RDP

---

## 📦 Installation

1. **Install Python 3.12**
   [https://www.python.org/downloads/](https://www.python.org/downloads/)
   Make sure to check **"Add Python to PATH"** during installation.

2. **Download DGSM**

   * Click the green **Code** button → **Download ZIP**
   * Or clone:

     ```bash
     git clone https://github.com/meowztho/DGSM-Discord-Gameserver-Manager.git
     cd DGSM-Discord-Gameserver-Manager
     ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **First run setup**
   Simply start the bot:

   ```bash
   python -m src.Main
   ```

   On the first run, DGSM will:

   * Create `.env` (with placeholder values) if it does not exist

   You only need to edit these files afterwards to:

   * Change your Discord bot token and admin channel ID in `.env`
   * Adjust `server_config.json` and templates for special settings your servers

---

## ⚙️ Discord Bot Setup

Before running DGSM with a real token, you must create a bot account in the Discord Developer Portal.

1. **Go to the Developer Portal**
   [https://discord.com/developers/applications](https://discord.com/developers/applications)

2. **Create a new application**

   * Click **New Application**
   * Name it (e.g., `DGSM Server Manager`)

3. **Add a Bot User**

   * Go to **Bot** in the left menu
   * Click **Add Bot** → **Yes, do it!**
   * Enable these intents:

     * `PRESENCE INTENT`
     * `SERVER MEMBERS INTENT`
     * `MESSAGE CONTENT INTENT`
   * Reset token and copy it for `.env`

4. **Set Admin Channel ID**

   * In Discord, enable **Developer Mode** (Settings → Advanced)
   * Right-click your admin channel → **Copy Channel ID**
   * Add to `.env`

5. **Invite the bot to your server**

   * Go to **OAuth2 → URL Generator**
   * Under **SCOPES**, check:

     * `bot`
     * `applications.commands`
   * Under **BOT PERMISSIONS**, check:

     * `Send Messages`
     * `Embed Links`
     * `Read Message History`
     * `Use Slash Commands`
   * Copy the generated URL → open in browser → Authorize bot for your server.

---

## 📷 Screenshots

| Main Menu                              | Server Status                                |
| -------------------------------------- | -------------------------------------------- |
| ![Main Menu](docs/images/bot_main.png) | ![Server Status](docs/images/bot_status.png) |



---

## 📥 SteamCMD Setup

Some servers require SteamCMD to install or update.

1. Download from Valve:
   [https://developer.valvesoftware.com/wiki/SteamCMD](https://developer.valvesoftware.com/wiki/SteamCMD)

2. Extract to a folder, e.g.:

   ```
   src/steam
   ```

   or anywhere else (update path in your config).

---

## 💖 Support this project

If DGSM saves you time or helps you run your servers, please consider supporting development:

* **[GitHub Sponsors](https://github.com/sponsors/meowztho)**

---

## 📜 License

MIT – see [LICENSE](LICENSE) for details.
