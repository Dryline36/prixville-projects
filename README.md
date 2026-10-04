# Prixville projects

This repo is the Prixville phone swipe feed: one HTML page of upcoming and live NFT projects. Drag a card right to like it and left to dislike it. A short tap still opens the social links. Likes play a water splash, a squish sound, and a vibration when the phone allows it. Dislikes show a big red X and play a loud buzz. Audio stays silent until the first tap. Each card shows view, like, and dislike counts, the Prixville type (Rokkitt, Crimson Text, JetBrains Mono), and the wallet color already stored for that project (green, yellow, or red).

The page lists the seven projects that were already in the file. It does not add projects, prices, wallets, follower counts, or X posts.

## Run the server

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Then open http://127.0.0.1:8000

`server.py` uses the Python standard library plus `eth-account` (to check wallet signatures) and `sqlite3` (to store votes). The database file is `votes.sqlite3` and is gitignored.

If you open `index.html` as a plain file, swipes still work on that device, but the page says the vote was not saved. A downloaded file cannot see other wallets, so those swipes are not a real vote.

## Vote rule

A vote is a connected-wallet `personal_sign` of a clear message that includes the project id and either like or dislike:

```text
Prixville vote
Project: <project id>
Vote: like
```

or `Vote: dislike`. The server checks that signature and rejects a bad one.

One vote per lowercase address per project. A second vote from the same address is rejected.

Before accepting a vote, the server looks up public transaction history for that address on Ethereum (https://eth.blockscout.com/api/v2) and Base (https://base.blockscout.com/api/v2). It reads only the first page of results those APIs actually return: normal transactions (the newest page and the oldest page), internal transactions, and token transfers. It treats the address as the same person as any address it has sent to or received from on those pages, and as the address that first funded it when that incoming transfer appears on the oldest page. If any of those addresses already voted on that same project, the vote is rejected and the server says it is tied to a wallet that already voted. If the API fails, the server does not pretend it checked: it rejects the vote with "could not check related wallets" and does not count it. There is no private blacklist, and the server does not invent links between wallets.
