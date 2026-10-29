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

Needs Python 3.6 or newer, no packages to install (no venv required). On start it prints the local and Wi-Fi URLs and the **host passcode**.
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

## Guests on cellular data (no venue Wi-Fi)

Guests load the pages from S3 over the internet, so the backend on your laptop must be reachable from the internet too. A tunnel does that: it gives your laptop a public HTTPS address. S3's website address is plain `http://`, and an `http` page can call an `https` backend, so that works.

Your laptop needs its own internet (phone hotspot or ethernet), must stay awake, and must keep both the server and the tunnel running. Your existing QR code keeps pointing at the S3 pages, and it never changes. Anyone who opens the bucket's root address (what the QR code points to) is sent straight to the voting page. To see the host home page from S3, open `index.html?host`; on your own computer (`localhost` or your local network) the home page shows normally.

**1. Start the server** (`python3 server/server.py`) and leave it running.

**2. Start a tunnel** in a second terminal. Easiest is a temporary Cloudflare tunnel (free, no account, install `cloudflared` first):

```
cloudflared tunnel --url http://localhost:8787
```

It prints an address like `https://random-words.trycloudflare.com`. That address changes every time you start the tunnel. Cloudflare provides these temporary tunnels for testing, with no uptime promise, so do a rehearsal and keep the tunnel running all night rather than restarting it.

**3. Tell the pages where the backend is.** Pick one:

- *Changing address (temporary tunnel):* create a file named `backend.json` and upload it to the root of your bucket, next to `vote.html`:

  ```
  echo '{"api": "https://random-words.trycloudflare.com"}' > backend.json
  aws s3 cp backend.json s3://YOUR-BUCKET/backend.json --content-type application/json --cache-control no-store
  ```

  (or upload it in the S3 console; it must be publicly readable like the other files). Upload a new one whenever the tunnel address changes. Guests with the page already open find the new address on their own within seconds, so you don't need to re-share anything.
- *Fixed address (named Cloudflare tunnel or ngrok static domain):* set `window.CONTEST_API` in `config.js` once and upload that. No `backend.json` needed.

If both exist, `backend.json` wins. See `site/backend.json.example`.

**4. Check it before the party.** Open the S3 `vote.html` on a phone with Wi-Fi turned off, and confirm the photos load.

Notes:
- With ngrok's free plan, a browser warning page can appear. The pages send the header that skips it for data requests, but photos are loaded as ordinary images, so test that photos show up before relying on ngrok.
- The host pages (`add.html`, `results.html`) work the same way, but it's simplest to use them on the laptop at `http://localhost:8787/`.

## Data

Everything lives in `data/` (`contest.db` plus `photos/`). Copy the folder to back it up; delete it to start over.

## Honor system

Votes are anonymous, one set per device (a random ID kept in the browser). Someone who clears their browser data or uses a second device could vote again, which is intentional given the honor-system approach. Results and all host actions require the passcode.
