# DGSM – Discord Gameserver Manager (MIT)

Manage and automate your game servers directly from Discord – no remote desktop required.  
Designed as a lightweight alternative to WindowsGSM, DGSM runs entirely through Discord commands and buttons.

![Main Bot UI](docs/images/bot_ui.png)

---

## ✨ Features
- Start / Stop / Restart / Status via buttons and slash commands
- Optional auto-update and scheduled restart
- Manage multiple servers (Palworld, Core Keeper, Satisfactory, Unturned…)
- Role & permission checks for admin actions
- SQLite logging, JSON configuration
- Runs on Windows servers without RDP

---

## 📦 Installation

1. **Install Python 3.12**  
   [https://www.python.org/downloads/](https://www.python.org/downloads/)  
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

4. **Configure .env**
   Copy .env.example to .env and fill in your details:
   DISCORD_TOKEN=YOUR_BOT_TOKEN
   ADMIN_CHANNEL_ID=123456789012345678
   
5. **Configure servers**
Edit server_config.json to include your servers.
Example:

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

6.Run the bot
   python -m src.Main

⚙️ Discord Bot Setup
Before running DGSM, you must create a bot account in the Discord Developer Portal.

Go to the Developer Portal
https://discord.com/developers/applications

Create a new application

Click New Application

Name it (e.g., DGSM Server Manager)

Add a Bot User

Go to Bot in the left menu

Click Add Bot → Yes, do it!

Enable these intents:

PRESENCE INTENT

SERVER MEMBERS INTENT

MESSAGE CONTENT INTENT

Reset token and copy it for .env

Set Admin Channel ID

In Discord, enable Developer Mode (Settings → Advanced)

Right-click your admin channel → Copy Channel ID

Add to .env

Invite the bot to your server

Go to OAuth2 → URL Generator

Under SCOPES, check:

bot

applications.commands

Under BOT PERMISSIONS, check:

Send Messages

Embed Links

Read Message History

Use Slash Commands

Copy the generated URL → open in browser → Authorize bot for your server.

📷 Screenshots

📥 SteamCMD Setup
Some servers require SteamCMD to install or update.

1. Download from Valve:
https://developer.valvesoftware.com/wiki/SteamCMD

Extract to a folder,

src/steam

💖 Support this project
If DGSM saves you time or helps you run your servers, please consider supporting development:
https://github.com/sponsors/meowztho
