# Costume Contest

Three small web pages plus a tiny Python backend you run yourself.

| Page | Who | What it does |
|------|-----|--------------|
| `add.html` | host | Add each costume (photo, name, title, description), link people into couples/groups, edit categories |
| `vote.html` | guests | Vote once per category (skip any), change or withdraw votes until you close voting |
| `results.html` | host | Open/close voting, see winners and rankings for every category |

## Run it

```
python3 server/server.py
```

Python 3 only, no packages to install. On start it prints the local and Wi-Fi URLs and the **host passcode**.
The passcode is saved in `data/admin_key.txt`; set `CONTEST_ADMIN_KEY=...` to choose your own.
Other options (environment variables): `PORT` (default 8787), `HOST`, `CONTEST_DATA` (data folder).

Open the printed URL on your computer, then:

1. **Add costumes** (`add.html`, passcode required). One entry per person. To make a couple or group, use "Add partner" on an existing entry, or pick "Goes with" in the form. Any size works. Give a linked set an optional group name.
2. **Categories** (bottom of the same page): rename, reorder, add, delete. Each category is either
   *Individual* (every entry competes, including people in couples) or *Linked sets* (only couples/groups compete, one choice per set).
   Changing a category's type clears its votes.
3. **Open voting** on `results.html`. Share the guest link (shown on the home page) as a link or QR code.
4. **Close voting** when you're ready and read out the winners. Ties are shown as ties. You can reopen voting afterwards.

## Putting the pages on S3 (optional)

Upload everything in `site/` to a bucket with static website hosting, then edit `config.js`:

```js
window.CONTEST_API = "http://your-backend-address:8787";
```

Guests' phones must be able to reach the backend:
- **Same Wi-Fi:** use the LAN URL the server prints. The S3 page must then be plain `http://` (S3 website endpoints are HTTP-only, so this works), because an `https` page can't call an `http` backend.
- **Anywhere:** put the backend behind an HTTPS tunnel (e.g. Cloudflare Tunnel) and use that URL. It works from either an HTTP or HTTPS page.

Simplest option: skip S3 entirely. The backend also serves `site/`, so guests can just open the URL it prints.

## Data

Everything lives in `data/` (`contest.db` plus `photos/`). Copy the folder to back it up; delete it to start over.

## Honor system

Votes are anonymous, one set per device (a random ID kept in the browser). Someone who clears their browser data or uses a second device could vote again, which is intentional given the honor-system approach. Results and all host actions require the passcode.
