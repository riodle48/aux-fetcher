# Aux Fetcher

Runs on your home network and downloads songs for your Aux bot, so the bot's cloud server never hits
YouTube's "sign in to confirm you're not a bot" wall. Outbound-only: it polls the Aux server for jobs; nothing listens, no ports to open.

## As a Home Assistant add-on
1. Settings → Add-ons → Add-on Store → ⋮ → Repositories → add this repo's URL.
2. Install **Aux Fetcher**, open Configuration: `server_url` = your Aux address, `key` = the same long password you put in the `FETCHER_KEY` variable on Railway.
3. Start it (enable "Start on boot"). The Aux Settings page shows "Home fetcher: online".

## Anywhere else (Docker)
```
docker build -t aux-fetcher ./aux-fetcher
docker run -d --restart unless-stopped -e AUX_SERVER=https://getaux.xyz -e AUX_KEY=yourkey aux-fetcher
```
